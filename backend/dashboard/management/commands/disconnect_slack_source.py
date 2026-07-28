from django.core.management.base import BaseCommand, CommandError

from dashboard.data_lifecycle import (
    DataLifecycleError,
    disconnect_slack_source,
)


class Command(BaseCommand):
    help = "Slack 지식 소스를 비활성화하거나 관련 데이터를 영구 삭제합니다."

    def add_arguments(self, parser):
        parser.add_argument("--channel-id", required=True)
        parser.add_argument(
            "--purge",
            action="store_true",
            help="소스와 가져온 지식 및 관련 퀴즈를 영구 삭제합니다.",
        )
        parser.add_argument(
            "--confirm",
            action="store_true",
            help="--purge 영구 삭제 확인",
        )

    def handle(self, *args, **options):
        if options["purge"] and not options["confirm"]:
            raise CommandError("--purge에는 --confirm이 필요합니다.")
        try:
            result = disconnect_slack_source(
                options["channel_id"],
                purge=options["purge"],
            )
        except DataLifecycleError as error:
            raise CommandError(str(error)) from error
        action = "purged" if options["purge"] else "disconnected"
        self.stdout.write(
            self.style.SUCCESS(
                f"slack_source_{action} "
                + " ".join(
                    f"{key}={value}"
                    for key, value in result.payload().items()
                )
            )
        )
