import os
import secrets
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction



class Command(BaseCommand):
    help = "Create the initial dashboard owner account without exposing its password."

    def add_arguments(self, parser):
        parser.add_argument(
            "--credentials-file",
            required=True,
            help="Private file that receives the initial username and password.",
        )

    def handle(self, *args, **options):
        user_model = get_user_model()
        if user_model.objects.filter(
            is_active=True,
            is_superuser=True,
        ).exists():
            return

        username = settings.DASHBOARD_OWNER_USERNAME
        if user_model.objects.filter(username=username).exists():
            raise CommandError(
                f"User '{username}' already exists but is not an active superuser."
            )

        credentials_file = Path(options["credentials_file"]).expanduser().resolve()
        if credentials_file.exists():
            raise CommandError(
                "Owner credentials file exists but no active superuser was found."
            )
        credentials_file.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(credentials_file.parent, 0o700)

        password = secrets.token_urlsafe(36)
        temporary = credentials_file.with_suffix(credentials_file.suffix + ".tmp")
        credentials_published = False
        try:
            file_descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(file_descriptor, "w", encoding="utf-8") as output:
                output.write(f"username={username}\npassword={password}\n")
            os.chmod(temporary, 0o600)
            with transaction.atomic():
                user_model.objects.create_superuser(
                    username=username,
                    email="",
                    password=password,
                )
                os.replace(temporary, credentials_file)
                credentials_published = True
        except Exception:
            temporary.unlink(missing_ok=True)
            if credentials_published:
                credentials_file.unlink(missing_ok=True)
            raise

        self.stdout.write(
            self.style.SUCCESS(
                f"Created dashboard owner credentials at {credentials_file}"
            )
        )
