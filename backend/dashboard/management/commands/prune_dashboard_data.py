import os

from django.core.management.base import BaseCommand, CommandError

from dashboard.data_lifecycle import DataLifecycleError, prune_dashboard_data


class Command(BaseCommand):
    help = "보존 기간이 지난 Slack 지식을 숨기거나 명시적으로 영구 삭제합니다."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=None,
            help="보존 기간. 기본값은 DASHBOARD_RETENTION_DAYS입니다.",
        )
        parser.add_argument("--hard-delete", action="store_true")
        parser.add_argument("--confirm", action="store_true")

    def handle(self, *args, **options):
        days = options["days"]
        if days is None:
            try:
                days = int(os.getenv("DASHBOARD_RETENTION_DAYS", "0"))
            except ValueError as error:
                raise CommandError("DASHBOARD_RETENTION_DAYS는 정수여야 합니다.") from error
        if options["hard_delete"] and not options["confirm"]:
            raise CommandError("--hard-delete에는 --confirm이 필요합니다.")
        try:
            result = prune_dashboard_data(
                days=days,
                hard_delete=options["hard_delete"],
            )
        except DataLifecycleError as error:
            raise CommandError(str(error)) from error
        action = "deleted" if options["hard_delete"] else "hidden"
        self.stdout.write(
            self.style.SUCCESS(
                f"retention_{action} "
                + " ".join(
                    f"{key}={value}"
                    for key, value in result.payload().items()
                )
            )
        )
