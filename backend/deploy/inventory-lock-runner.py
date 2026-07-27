#!/usr/bin/env python3
import argparse
import fcntl
import os
import sys
import time
from pathlib import Path


TIMEOUT_EXIT_CODE = 75


def parse_args():
    parser = argparse.ArgumentParser(
        description="Run a command while holding the shared knowledge inventory lock."
    )
    parser.add_argument("--lock-path", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=1800)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.command[:1] == ["--"]:
        args.command = args.command[1:]
    if not args.command:
        parser.error("a command is required after --")
    if args.timeout_seconds < 0:
        parser.error("--timeout-seconds must be zero or greater")
    return args


def main() -> int:
    args = parse_args()
    lock_path = Path(args.lock_path).expanduser()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    started_at = time.monotonic()
    deadline = started_at + args.timeout_seconds

    with lock_path.open("a+") as lock_file:
        waiting_logged = False
        while True:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if not waiting_logged:
                    print(
                        "inventory_lock_waiting "
                        f"timeout_seconds={args.timeout_seconds:g}",
                        flush=True,
                    )
                    waiting_logged = True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    print(
                        "inventory_lock_timeout "
                        f"timeout_seconds={args.timeout_seconds:g}",
                        file=sys.stderr,
                        flush=True,
                    )
                    return TIMEOUT_EXIT_CODE
                time.sleep(min(1, remaining))

        if waiting_logged:
            print(
                f"inventory_lock_acquired waited_seconds={time.monotonic() - started_at:.1f}",
                flush=True,
            )
        os.set_inheritable(lock_file.fileno(), True)
        try:
            os.execvp(args.command[0], args.command)
        except OSError as error:
            print(
                f"inventory_lock_exec_failed error={type(error).__name__}",
                file=sys.stderr,
                flush=True,
            )
            return 127


if __name__ == "__main__":
    raise SystemExit(main())
