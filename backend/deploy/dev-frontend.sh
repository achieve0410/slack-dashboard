#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "$0")/env.sh"
cd "$PROJECT_ROOT/frontend"
exec npm run dev

