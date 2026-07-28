import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from integrations.dashboard_platform_mcp import DashboardApiClient, build_server


class DashboardApiClientConfigurationTests(unittest.TestCase):
    def test_accepts_https(self):
        client = DashboardApiClient(
            base_url="https://dashboard.example.com/api/v1/",
            token=" secret\n",
        )

        self.assertEqual(client.base_url, "https://dashboard.example.com/api/v1")
        self.assertEqual(client.token, "secret")
        self.assertIsNotNone(client.ssl_context)

    def test_accepts_loopback_http(self):
        for base_url in (
            "http://localhost:8000/api/v1",
            "http://127.0.0.1:8000/api/v1",
            "http://[::1]:8000/api/v1",
        ):
            with self.subTest(base_url=base_url):
                client = DashboardApiClient(base_url=base_url, token="secret")
                self.assertIsNone(client.ssl_context)

    def test_rejects_non_loopback_http(self):
        with self.assertRaisesRegex(ValueError, "must use HTTPS"):
            DashboardApiClient(
                base_url="http://dashboard.example.com/api/v1",
                token="secret",
            )

    def test_rejects_credentials_in_url(self):
        with self.assertRaisesRegex(ValueError, "without credentials"):
            DashboardApiClient(
                base_url="https://user:password@dashboard.example.com/api/v1",
                token="secret",
            )

    def test_rejects_query_string_and_fragment_in_base_url(self):
        for base_url in (
            "https://dashboard.example.com/api/v1?token=secret",
            "https://dashboard.example.com/api/v1#fragment",
        ):
            with (
                self.subTest(base_url=base_url),
                self.assertRaisesRegex(ValueError, "query string or fragment"),
            ):
                DashboardApiClient(base_url=base_url, token="secret")

    def test_rejects_ca_certificate_for_http(self):
        with self.assertRaisesRegex(ValueError, "only valid with HTTPS"):
            DashboardApiClient(
                base_url="http://localhost:8000/api/v1",
                token="secret",
                ca_cert="/tmp/ca.pem",
            )

    @patch("integrations.dashboard_platform_mcp.urlopen")
    def test_loopback_http_request_does_not_pass_tls_context(self, mocked_urlopen):
        response = mocked_urlopen.return_value.__enter__.return_value
        response.read.return_value = b'{"data": []}'
        client = DashboardApiClient(
            base_url="http://127.0.0.1:8000/api/v1",
            token="secret",
        )

        self.assertEqual(client.get("tasks/"), {"data": []})
        self.assertNotIn("context", mocked_urlopen.call_args.kwargs)

    def test_build_server_smoke(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            token_path = Path(temporary_directory) / "dashboard.token"
            token_path.write_text("secret-token\n", encoding="utf-8")
            environment = {
                "DASHBOARD_API_URL": "http://127.0.0.1:8000/api/v1",
                "DASHBOARD_API_TOKEN_FILE": str(token_path),
            }

            with patch.dict(os.environ, environment, clear=True):
                server = build_server()

        self.assertIsNotNone(server)


if __name__ == "__main__":
    unittest.main()
