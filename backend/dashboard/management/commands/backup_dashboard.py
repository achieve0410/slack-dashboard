from django.core.management.base import BaseCommand, CommandError

from dashboard.data_lifecycle import (
    DataLifecycleError,
    create_dashboard_backup,
)


class Command(BaseCommand):
    help = "대시보드 데이터베이스를 복원 가능한 JSON fixture로 백업합니다."

    def add_arguments(self, parser):
        parser.add_argument(
            "--filename",
            default="",
            help="DASHBOARD_BACKUP_DIR 아래에 생성할 .json 파일명",
        )

    def handle(self, *args, **options):
        try:
            path = create_dashboard_backup(filename=options["filename"])
        except DataLifecycleError as error:
            raise CommandError(str(error)) from error
        self.stdout.write(self.style.SUCCESS(f"dashboard_backup_created path={path}"))
