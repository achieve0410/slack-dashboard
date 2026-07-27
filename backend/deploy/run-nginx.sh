#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "$0")/env.sh"
mkdir -p "$PROJECT_ROOT/backend/run"
NGINX_BIN="${NGINX_BIN:-nginx}"
exec "$NGINX_BIN" -c "$PROJECT_ROOT/backend/deploy/nginx.conf" -g 'daemon off;'

