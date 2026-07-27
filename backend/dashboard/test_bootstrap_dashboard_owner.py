import os
import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings


class BootstrapDashboardOwnerTests(TestCase):
    def test_creates_owner_once_and_writes_private_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            credentials = Path(directory) / "owner-credentials.txt"
            with override_settings(DASHBOARD_OWNER_USERNAME="owner"):
                call_command(
                    "bootstrap_dashboard_owner",
                    credentials_file=credentials,
                    verbosity=0,
                )
                first_contents = credentials.read_text()
                call_command(
                    "bootstrap_dashboard_owner",
                    credentials_file=credentials,
                    verbosity=0,
                )

            owner = get_user_model().objects.get(username="owner")
            self.assertTrue(owner.is_active)
            self.assertTrue(owner.is_staff)
            self.assertTrue(owner.is_superuser)
            self.assertTrue(owner.check_password(first_contents.split("password=", 1)[1].strip()))
            self.assertEqual(credentials.read_text(), first_contents)
            self.assertEqual(os.stat(credentials).st_mode & 0o777, 0o600)
