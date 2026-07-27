#!/usr/bin/env python3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


RUNNER = Path(__file__).with_name("inventory-lock-runner.py")


class InventoryLockRunnerTests(unittest.TestCase):
    def test_waiter_runs_after_holder_releases_lock(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            lock_path = root / "inventory.lock"
            holder_started = root / "holder-started"
            waiter_finished = root / "waiter-finished"
            holder = subprocess.Popen(
                [
                    sys.executable,
                    str(RUNNER),
                    "--lock-path",
                    str(lock_path),
                    "--timeout-seconds",
                    "2",
                    "--",
                    sys.executable,
                    "-c",
                    (
                        "from pathlib import Path; import time; "
                        f"Path({str(holder_started)!r}).write_text('started'); "
                        "time.sleep(0.5)"
                    ),
                ]
            )
            self.addCleanup(holder.kill)
            self._wait_for_path(holder_started)

            started_at = time.monotonic()
            result = subprocess.run(
                [
                    sys.executable,
                    str(RUNNER),
                    "--lock-path",
                    str(lock_path),
                    "--timeout-seconds",
                    "2",
                    "--",
                    sys.executable,
                    "-c",
                    (
                        "from pathlib import Path; "
                        f"Path({str(waiter_finished)!r}).write_text('finished')"
                    ),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            elapsed = time.monotonic() - started_at

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertGreaterEqual(elapsed, 0.3)
            self.assertTrue(waiter_finished.exists())
            self.assertEqual(holder.wait(timeout=2), 0)

    def test_timeout_does_not_run_command(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            lock_path = root / "inventory.lock"
            holder_started = root / "holder-started"
            waiter_finished = root / "waiter-finished"
            holder = subprocess.Popen(
                [
                    sys.executable,
                    str(RUNNER),
                    "--lock-path",
                    str(lock_path),
                    "--timeout-seconds",
                    "2",
                    "--",
                    sys.executable,
                    "-c",
                    (
                        "from pathlib import Path; import time; "
                        f"Path({str(holder_started)!r}).write_text('started'); "
                        "time.sleep(1)"
                    ),
                ]
            )
            self.addCleanup(holder.kill)
            self._wait_for_path(holder_started)

            result = subprocess.run(
                [
                    sys.executable,
                    str(RUNNER),
                    "--lock-path",
                    str(lock_path),
                    "--timeout-seconds",
                    "0.1",
                    "--",
                    sys.executable,
                    "-c",
                    (
                        "from pathlib import Path; "
                        f"Path({str(waiter_finished)!r}).write_text('finished')"
                    ),
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 75)
            self.assertFalse(waiter_finished.exists())
            self.assertIn("inventory_lock_timeout", result.stderr)
            self.assertEqual(holder.wait(timeout=2), 0)

    def _wait_for_path(self, path: Path):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if path.exists():
                return
            time.sleep(0.01)
        self.fail(f"timed out waiting for {path}")


if __name__ == "__main__":
    unittest.main()
