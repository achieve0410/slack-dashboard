import os
from datetime import timedelta
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from dashboard.models import PlatformAgent, PlatformApiToken


KNOWN_SCOPES = {
    "platform:read",
    "inbox:write",
    "tasks:write",
    "artifacts:write",
    "approvals:request",
    "approvals:decide",
}


class Command(BaseCommand):
    help = "플랫폼 에이전트의 API 토큰을 파일로 안전하게 발급하거나 교체합니다."

    def add_arguments(self, parser):
        parser.add_argument("--agent-key", required=True)
        parser.add_argument("--agent-name", required=True)
        parser.add_argument("--token-name", required=True)
        parser.add_argument("--scopes", required=True, help="쉼표로 구분한 권한 범위")
        parser.add_argument("--capabilities", default="", help="쉼표로 구분한 에이전트 기능")
        parser.add_argument("--expires-days", type=int)
        parser.add_argument("--output", required=True, help="원문 토큰을 기록할 비공개 파일")

    def handle(self, *args, **options):
        scopes = sorted(set(filter(None, (item.strip() for item in options["scopes"].split(",")))))
        unknown_scopes = sorted(set(scopes) - KNOWN_SCOPES)
        if unknown_scopes:
            raise CommandError(f"지원하지 않는 scope: {', '.join(unknown_scopes)}")
        if not scopes:
            raise CommandError("최소 한 개의 scope가 필요합니다.")
        expires_at = None
        if options["expires_days"] is not None:
            if options["expires_days"] < 1:
                raise CommandError("--expires-days는 1 이상이어야 합니다.")
            expires_at = timezone.now() + timedelta(days=options["expires_days"])
        capabilities = list(
            dict.fromkeys(
                item.strip() for item in options["capabilities"].split(",") if item.strip()
            )
        )
        output_path = Path(options["output"]).expanduser().resolve()

        with transaction.atomic():
            agent, _ = PlatformAgent.objects.get_or_create(
                key=options["agent_key"],
                defaults={"name": options["agent_name"], "capabilities": capabilities},
            )
            changed_fields = []
            if agent.name != options["agent_name"]:
                agent.name = options["agent_name"]
                changed_fields.append("name")
            if capabilities and agent.capabilities != capabilities:
                agent.capabilities = capabilities
                changed_fields.append("capabilities")
            if not agent.is_active:
                agent.is_active = True
                changed_fields.append("is_active")
            if changed_fields:
                agent.save(update_fields=[*changed_fields, "updated_at"])

            now = timezone.now()
            PlatformApiToken.objects.filter(
                agent=agent,
                name=options["token_name"],
                is_active=True,
            ).update(is_active=False, revoked_at=now)
            token, raw_token = PlatformApiToken.issue(
                name=options["token_name"],
                agent=agent,
                scopes=scopes,
                expires_at=expires_at,
            )

        output_path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(output_path.parent, 0o700)
        file_descriptor = os.open(
            output_path,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as output:
            output.write(raw_token + "\n")
        os.chmod(output_path, 0o600)
        self.stdout.write(
            self.style.SUCCESS(
                f"Issued token {token.token_prefix} for {agent.key}; secret written to {output_path}"
            )
        )
