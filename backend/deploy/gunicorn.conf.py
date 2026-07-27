import multiprocessing
import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUN_DIR = PROJECT_ROOT / "backend" / "run"
RUN_DIR.mkdir(parents=True, exist_ok=True)

bind = os.getenv("GUNICORN_BIND", "127.0.0.1:8000")
chdir = str(PROJECT_ROOT / "backend")
workers = min(4, max(2, multiprocessing.cpu_count() // 2))
worker_class = "sync"
timeout = 180
graceful_timeout = 30
keepalive = 5
accesslog = str(RUN_DIR / "gunicorn-access.log")
errorlog = str(RUN_DIR / "gunicorn-error.log")
capture_output = True
