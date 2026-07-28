import os

from django.core.management.base import BaseCommand, CommandError
from slack_sdk import WebClient

from dashboard.slack_digest import build_digest, send_digest


class Command(BaseCommand):
    help = "처리할 지식 항목을 Slack에 요약합니다. 채널을 설정해야만 전송합니다."

    def add_arguments(self, parser):
        parser.add_argument(
            "--channel",
            default=os.getenv("SLACK_DASHBOARD_DIGEST_CHANNEL", ""),
        )
        parser.add_argument(
            "--slack-token",
            default="",
            help="기본값은 SLACK_BOT_TOKEN입니다.",
        )
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument(
            "--only-if-actionable",
            action="store_true",
            help="처리할 항목이 없으면 메시지를 보내지 않습니다.",
        )

    def handle(self, *args, **options):
        digest = build_digest()
        if options["dry_run"]:
            self.stdout.write(digest.text)
            self.stdout.write(f"actionable_count={digest.actionable_count}")
            return
        channel = options["channel"].strip()
        if not channel:
            raise CommandError(
                "SLACK_DASHBOARD_DIGEST_CHANNEL 또는 --channel을 지정해주세요."
            )
        if options["only_if_actionable"] and digest.actionable_count == 0:
            self.stdout.write("slack_digest_skipped actionable_count=0")
            return
        token = options["slack_token"] or os.getenv("SLACK_BOT_TOKEN", "")
        if not token.strip():
            raise CommandError("SLACK_BOT_TOKEN 또는 --slack-token을 지정해주세요.")
        response = send_digest(WebClient(token=token), channel, digest)
        timestamp = response.get("ts", "") if hasattr(response, "get") else ""
        self.stdout.write(
            self.style.SUCCESS(
                "slack_digest_sent "
                f"channel={channel} actionable_count={digest.actionable_count} "
                f"ts={timestamp}"
            )
        )
