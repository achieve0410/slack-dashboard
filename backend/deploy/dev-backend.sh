#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "$0")/env.sh"
cd "$PROJECT_ROOT"
exec python backend/manage.py runserver 127.0.0.1:8000
