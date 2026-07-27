#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "$0")/env.sh"
cd "$PROJECT_ROOT"
exec python backend/deploy/inventory-lock-runner.py \
  --lock-path "${KNOWLEDGE_INVENTORY_LOCK_PATH:-$PROJECT_ROOT/backend/run/knowledge_inventory.lock}" \
  --timeout-seconds "${KNOWLEDGE_INVENTORY_LOCK_TIMEOUT_SECONDS:-1800}" \
  -- \
  python backend/manage.py sync_slack "$@"
