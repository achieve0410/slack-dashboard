#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "$0")/env.sh"
cd "$PROJECT_ROOT"
exec python -m gunicorn --config backend/deploy/gunicorn.conf.py config.wsgi:application
