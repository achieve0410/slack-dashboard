import logging
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from django.core.management.base import BaseCommand, CommandError
from slack_sdk import WebClient

from dashboard.knowledge_actions import prune_bulk_snapshots
from dashboard.models import (
    Citation,
    ContentRun,
    CronJob,
    FreeQuestionMessage,
    KnowledgeItem,
)
from dashboard.operation_runs import (
    finish_operation,
    prune_operation_runs,
    start_operation,
)
from dashboard.schedule_sync import reconcile_schedule_channel
from dashboard.services import (
    _provisional_title,
    extract_citations,
    reconcile_cron_runs,
    reconcile_slack_thread,
    slack_ts_to_datetime,
)


logger = logging.getLogger(__name__)

BOT_MESSAGE_SUBTYPES = {None, "bot_message", "thread_broadcast"}


@dataclass(frozen=True)
class ChannelSyncResult:
    imported: int
    deleted: int
    cursor_ts: str


def _env_channel_list(name: str) -> list[str]:
    raw = os.getenv(name, "")
    return [item.strip() for item in raw.split(",") if item.strip()]


def _latest_ts(values) -> str:
    timestamps = []
    for value in values:
        if value is None:
            continue
        normalized = str(value).strip()
        if normalized:
            timestamps.append(normalized)
    for value in timestamps:
        if not re.fullmatch(r"\d+\.\d+", value):
            raise CommandError(f"Invalid Slack timestamp: {value}")
    return max(timestamps, key=float) if timestamps else ""


def _newer_ts(*values: str) -> str:
    return _latest_ts(values)


class Command(BaseCommand):
    help = (
        "Imports content from configured Slack channels into the knowledge "
        "base, and optionally reconciles the free-question and schedule "
        "channels."
    )

    def add_arguments(self, parser):
        parser.add_argument("--slack-token", default="")
        parser.add_argument(
            "--channels",
            nargs="*",
            default=None,
            help=(
                "Slack channel IDs to import as knowledge sources. Defaults "
                "to SLACK_KNOWLEDGE_CHANNELS (comma-separated)."
            ),
        )
        parser.add_argument(
            "--free-question-channel",
            default=os.getenv("SLACK_DASHBOARD_FREE_QUESTION_CHANNEL", ""),
        )
        parser.add_argument(
            "--free-question-start-ts",
            default=os.getenv("SLACK_DASHBOARD_FREE_QUESTION_START_TS", "0"),
        )
        parser.add_argument(
            "--free-question-user",
            default=os.getenv("SLACK_DASHBOARD_FREE_QUESTION_USER", ""),
        )
        parser.add_argument(
            "--slack-workspace-url",
            default=os.getenv("SLACK_DASHBOARD_WORKSPACE_URL", ""),
        )
        parser.add_argument(
            "--schedule-channel",
            default=os.getenv("SLACK_DASHBOARD_SCHEDULE_CHANNEL", ""),
        )
        parser.add_argument(
            "--oldest",
            default="",
            help="Only import knowledge-channel messages newer than this Slack timestamp.",
        )
        parser.add_argument(
            "--full-rescan",
            action="store_true",
            help=(
                "Ignore saved channel checkpoints and reconcile messages deleted "
                "from Slack. Use periodically, not on every run."
            ),
        )

    def handle(self, *args, **options):
        attempt = start_operation("sync", logger=logger)
        try:
            summary = self._sync(*args, **options)
        except CommandError:
            finish_operation(
                attempt,
                "failed",
                error_code="configuration_error",
                logger=logger,
            )
            raise
        except Exception:
            finish_operation(
                attempt,
                "failed",
                error_code="unexpected_error",
                logger=logger,
            )
            raise
        else:
            finish_operation(
                attempt,
                "success",
                summary=summary,
                logger=logger,
            )
        finally:
            prune_operation_runs(logger=logger)
            try:
                prune_bulk_snapshots()
            except Exception as error:
                logger.warning(
                    "bulk_snapshot_prune_failed exception=%s",
                    type(error).__name__,
                )

    def _sync(self, *args, **options):
        token = options["slack_token"] or os.getenv("SLACK_BOT_TOKEN", "")
        if not token:
            raise CommandError(
                "SLACK_BOT_TOKEN is not set. Create a Slack bot token and "
                "export it, or pass --slack-token."
            )

        client = WebClient(token=token)
        bot_user_id = client.auth_test().get("user_id")

        summary = {
            "channels_synced": 0,
            "runs_imported": 0,
            "runs_deleted": 0,
            "questions_imported": 0,
            "schedule_created": 0,
            "schedule_updated": 0,
            "schedule_deleted": 0,
            "schedule_skipped": 0,
        }

        channels = options["channels"]
        if channels is None:
            channels = _env_channel_list("SLACK_KNOWLEDGE_CHANNELS")
        for channel_id in channels:
            result = self.sync_knowledge_channel(
                client,
                channel_id,
                bot_user_id,
                oldest=options["oldest"],
                full_rescan=options["full_rescan"],
            )
            summary["runs_imported"] += result.imported
            summary["runs_deleted"] += result.deleted
            summary["channels_synced"] += 1
            self.stdout.write(
                self.style.SUCCESS(
                    f"Channel {channel_id}: imported {result.imported}, "
                    f"deleted {result.deleted}, cursor {result.cursor_ts or '-'}"
                )
            )

        free_question_channel = options["free_question_channel"]
        if free_question_channel:
            free_imported = self.sync_free_question_channel(
                client,
                bot_user_id,
                channel_id=free_question_channel,
                user_id=options["free_question_user"],
                start_ts=options["free_question_start_ts"] or "0",
                workspace_url=options["slack_workspace_url"],
            )
            summary["questions_imported"] = free_imported
            self.stdout.write(
                self.style.SUCCESS(f"Free-question channel: imported {free_imported} message(s)")
            )

        schedule_channel = options["schedule_channel"]
        if schedule_channel:
            schedule_messages = self.fetch_channel(client, schedule_channel)
            schedule_stats = reconcile_schedule_channel(
                schedule_channel,
                schedule_messages,
                bot_user_id,
            )
            self.stdout.write(
                "schedule_reconcile "
                + " ".join(
                    f"{key}={value}" for key, value in sorted(schedule_stats.items())
                )
            )
            for source_key, summary_key in (
                ("created", "schedule_created"),
                ("updated", "schedule_updated"),
                ("deleted", "schedule_deleted"),
                ("skipped", "schedule_skipped"),
            ):
                summary[summary_key] = schedule_stats[source_key]

        return summary

    # -- knowledge channels ------------------------------------------------

    def sync_knowledge_channel(
        self,
        client: WebClient,
        channel_id: str,
        bot_user_id: str,
        *,
        oldest: str = "",
        full_rescan: bool = False,
    ) -> ChannelSyncResult:
        channel_name = self.channel_display_name(client, channel_id)
        job, _ = CronJob.objects.update_or_create(
            external_id=f"channel:{channel_id}",
            defaults={
                "name": channel_name,
                "category": CronJob.Category.OTHER,
                "prompt": "",
                "schedule": "",
                "channel_id": channel_id,
                "thread_ts": "",
                "enabled": True,
                "state": "active",
                "disconnected_at": None,
            },
        )

        checkpoint = "" if full_rescan else _newer_ts(job.sync_cursor_ts, oldest)
        try:
            messages = self.fetch_channel(client, channel_id, oldest=checkpoint)
        except Exception as error:
            job.last_run_at = datetime.now(tz=UTC)
            job.last_status = "error"
            job.last_error = type(error).__name__
            job.save(
                update_fields=[
                    "last_run_at",
                    "last_status",
                    "last_error",
                    "updated_at",
                ]
            )
            raise
        run_ids: list[int] = []
        fetched_timestamps: set[str] = set()
        for message in messages:
            if message.get("subtype") not in BOT_MESSAGE_SUBTYPES:
                continue
            text = str(message.get("text") or "").strip()
            if not text:
                continue
            ts = str(message.get("ts") or "")
            if not ts:
                continue
            fetched_timestamps.add(ts)
            existing_run = ContentRun.objects.filter(external_ts=ts).only(
                "hidden_at",
                "structured_data",
            ).first()
            restore_source_deleted = bool(
                existing_run
                and (existing_run.structured_data or {}).get("source_deleted")
            )
            run, _ = ContentRun.objects.update_or_create(
                external_ts=ts,
                defaults={
                    "job": job,
                    "status": ContentRun.Status.SUCCESS,
                    "title": _provisional_title(text),
                    "body": text,
                    "raw_text": text,
                    "error": "",
                    "structured_data": {"source": "slack_channel", "channel": channel_id},
                    "generated_at": slack_ts_to_datetime(ts),
                },
            )
            if restore_source_deleted:
                if run.hidden_at:
                    run.hidden_at = None
                    run.save(update_fields=["hidden_at", "updated_at"])
                KnowledgeItem.objects.filter(content_run=run).update(hidden_at=None)
            Citation.objects.filter(run=run).delete()
            Citation.objects.bulk_create(
                [Citation(run=run, **citation) for citation in extract_citations(text)]
            )
            run_ids.append(run.pk)

        deleted = 0
        if full_rescan:
            missing_runs = ContentRun.objects.filter(
                job=job,
                external_ts__isnull=False,
                hidden_at__isnull=True,
            )
            if fetched_timestamps:
                missing_runs = missing_runs.exclude(
                    external_ts__in=fetched_timestamps
                )
            hidden_at = datetime.now(tz=UTC)
            missing = list(missing_runs)
            missing_ids = [run.pk for run in missing]
            if missing:
                for run in missing:
                    run.hidden_at = hidden_at
                    run.updated_at = hidden_at
                    run.structured_data = {
                        **(run.structured_data or {}),
                        "source_deleted": True,
                    }
                ContentRun.objects.bulk_update(
                    missing,
                    ["hidden_at", "structured_data", "updated_at"],
                )
                deleted = len(missing)
                KnowledgeItem.objects.filter(
                    content_run_id__in=missing_ids,
                ).update(hidden_at=hidden_at)

        cursor_ts = _latest_ts(
            [job.sync_cursor_ts, *fetched_timestamps],
        )
        reconcile_stats = reconcile_cron_runs(run_ids) if run_ids else None

        job.last_run_at = datetime.now(tz=UTC)
        job.last_status = "success"
        job.last_error = ""
        job.sync_cursor_ts = cursor_ts
        job.last_import_count = len(run_ids)
        job.save(
            update_fields=[
                "last_run_at",
                "last_status",
                "last_error",
                "sync_cursor_ts",
                "last_import_count",
                "updated_at",
            ]
        )

        if reconcile_stats is not None:
            self.stdout.write(
                f"cron_reconcile channel={channel_id} "
                + " ".join(
                    f"{key}={value}"
                    for key, value in sorted(reconcile_stats.items())
                )
            )
        return ChannelSyncResult(len(run_ids), deleted, cursor_ts)

    @staticmethod
    def channel_display_name(client: WebClient, channel_id: str) -> str:
        try:
            info = client.conversations_info(channel=channel_id)
        except Exception:
            return channel_id
        name = (info.get("channel") or {}).get("name")
        return f"#{name}" if name else channel_id

    # -- free-question channel ----------------------------------------------

    @staticmethod
    def fetch_channel(
        client: WebClient,
        channel_id: str,
        *,
        oldest: str = "",
    ) -> list[dict]:
        messages: dict[str, dict] = {}
        cursor = ""
        while True:
            kwargs = {"channel": channel_id, "limit": 200}
            if oldest:
                kwargs.update({"oldest": oldest, "inclusive": False})
            if cursor:
                kwargs["cursor"] = cursor
            response = client.conversations_history(**kwargs)
            for message in response.get("messages", []):
                if message.get("ts"):
                    messages[message["ts"]] = message
            cursor = (response.get("response_metadata") or {}).get("next_cursor", "")
            if not cursor:
                break
        return sorted(messages.values(), key=lambda item: float(item["ts"]))

    @staticmethod
    def fetch_thread(client: WebClient, channel_id: str, thread_ts: str) -> list[dict]:
        messages: dict[str, dict] = {}
        cursor = ""
        while True:
            kwargs = {
                "channel": channel_id,
                "ts": thread_ts,
                "limit": 200,
                "inclusive": True,
            }
            if cursor:
                kwargs["cursor"] = cursor
            response = client.conversations_replies(**kwargs)
            for message in response.get("messages", []):
                if message.get("ts"):
                    messages[message["ts"]] = message
            cursor = (response.get("response_metadata") or {}).get("next_cursor", "")
            if not cursor:
                break
        return sorted(messages.values(), key=lambda item: float(item["ts"]))

    @staticmethod
    def _is_bot_message(message: dict, bot_user_id: str) -> bool:
        return bool(
            message.get("user") == bot_user_id
            or message.get("bot_id")
            or message.get("subtype") == "bot_message"
        )

    @staticmethod
    def select_free_question_roots(
        messages: list[dict],
        *,
        bot_user_id: str,
        user_id: str,
        start_ts: str,
    ) -> list[dict]:
        mention = f"<@{bot_user_id}>"
        return [
            message
            for message in messages
            if (
                not message.get("thread_ts")
                or str(message.get("thread_ts")) == str(message.get("ts"))
            )
            and (not user_id or str(message.get("user") or "") == user_id)
            and float(str(message.get("ts") or "0")) > float(start_ts or "0")
            and mention in str(message.get("text") or "")
        ]

    def sync_free_question_channel(
        self,
        client: WebClient,
        bot_user_id: str,
        *,
        channel_id: str,
        user_id: str,
        start_ts: str,
        workspace_url: str,
    ) -> int:
        roots = self.select_free_question_roots(
            self.fetch_channel(client, channel_id),
            bot_user_id=bot_user_id,
            user_id=user_id,
            start_ts=start_ts,
        )
        imported = 0
        for root in roots:
            root_ts = str(root["ts"])
            messages = self.fetch_thread(client, channel_id, root_ts)
            imported += self.import_free_question_messages(
                messages,
                bot_user_id,
                root_ts,
                channel_id=channel_id,
                workspace_url=workspace_url,
            )
        return imported

    @staticmethod
    def import_free_question_messages(
        messages: list[dict],
        bot_user_id: str,
        thread_ts: str,
        *,
        channel_id: str = "",
        workspace_url: str = "",
    ) -> int:
        imported = 0
        normalized_messages = []
        for message in messages:
            external_ts = str(message.get("ts", ""))
            content = str(message.get("text", "")).strip()
            if not external_ts or not content:
                continue
            is_bot = Command._is_bot_message(message, bot_user_id)
            if not is_bot:
                content = re.sub(
                    rf"<@{re.escape(bot_user_id)}>",
                    "",
                    content,
                ).strip()
                if not content:
                    continue
                kind = (
                    FreeQuestionMessage.Kind.REQUEST
                    if external_ts == thread_ts
                    else FreeQuestionMessage.Kind.CLARIFICATION
                )
            else:
                kind = FreeQuestionMessage.Kind.WORKFLOW_STATUS
            normalized_messages.append((message, external_ts, content, is_bot, kind))

        message_timestamps = [entry[1] for entry in normalized_messages]
        affected_threads = set(
            FreeQuestionMessage.objects.filter(
                external_ts__in=message_timestamps
            ).values_list("thread_ts", flat=True)
        )
        affected_threads.add(thread_ts)
        for message, external_ts, content, is_bot, kind in normalized_messages:
            FreeQuestionMessage.objects.update_or_create(
                external_ts=external_ts,
                defaults={
                    "channel_id": channel_id,
                    "thread_ts": thread_ts,
                    "role": (
                        FreeQuestionMessage.Role.ASSISTANT
                        if is_bot
                        else FreeQuestionMessage.Role.USER
                    ),
                    "message_kind": kind,
                    "content": content,
                    "generated_at": slack_ts_to_datetime(external_ts),
                },
            )
            imported += 1

        for affected_thread in sorted(affected_threads):
            reconcile_slack_thread(affected_thread, workspace_url=workspace_url)
        return imported
