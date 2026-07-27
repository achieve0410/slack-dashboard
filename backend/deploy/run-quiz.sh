#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "$0")/env.sh"
cd "$PROJECT_ROOT"
exec python backend/manage.py generate_quiz_questions "$@"
