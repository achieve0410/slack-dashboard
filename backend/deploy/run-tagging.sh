#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "$0")/env.sh"

ARTIFACT_ROOT="${TAGGING_ARTIFACT_ROOT:-$PROJECT_ROOT/backend/run/tagging}"
RUN_STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
ARTIFACT_DIR="$ARTIFACT_ROOT/$RUN_STAMP"

has_mode=0
has_artifact_dir=0
for arg in "$@"; do
  case "$arg" in
    --publish|--dry-run)
      has_mode=1
      ;;
    --artifact-dir|--artifact-dir=*)
      has_artifact_dir=1
      ;;
  esac
done

mkdir -p "$ARTIFACT_DIR"
chmod 700 "$ARTIFACT_ROOT" "$ARTIFACT_DIR"

args=()
if [[ "$has_mode" -eq 0 ]]; then
  args+=(--publish)
fi
if [[ "$has_artifact_dir" -eq 0 ]]; then
  args+=(--artifact-dir "$ARTIFACT_DIR")
fi
args+=("$@")

cd "$PROJECT_ROOT"
exec python backend/deploy/inventory-lock-runner.py \
  --lock-path "${KNOWLEDGE_INVENTORY_LOCK_PATH:-$PROJECT_ROOT/backend/run/knowledge_inventory.lock}" \
  --timeout-seconds "${KNOWLEDGE_INVENTORY_LOCK_TIMEOUT_SECONDS:-1800}" \
  -- \
  python backend/manage.py tag_knowledge "${args[@]}"
