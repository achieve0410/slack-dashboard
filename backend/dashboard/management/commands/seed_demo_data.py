from django.core.management.base import BaseCommand

from dashboard.onboarding import purge_demo_data, seed_demo_data


class Command(BaseCommand):
    help = "첫 실행 체험용 샘플 지식, 퀴즈, 일정 데이터를 생성하거나 삭제합니다."

    def add_arguments(self, parser):
        parser.add_argument(
            "--purge",
            action="store_true",
            help="이 명령으로 생성된 데모 데이터만 삭제합니다.",
        )

    def handle(self, *args, **options):
        if options["purge"]:
            summary = purge_demo_data()
            action = "purged"
        else:
            summary = seed_demo_data()
            action = "seeded"
        self.stdout.write(
            self.style.SUCCESS(
                f"demo_data_{action} "
                + " ".join(f"{key}={value}" for key, value in summary.items())
            )
        )
