#!/usr/bin/env bash
set -euo pipefail

DB_ROOT="$(cd "$(dirname "$0")" && pwd)"
COMPOSE_FILE="$DB_ROOT/docker_compose/compose.yml"
ENV_FILE="$DB_ROOT/slack_dashboard_db/.env"
PROJECT_NAME="slack_dashboard_db"

create_env() {
  if [[ -f "$ENV_FILE" ]]; then
    return
  fi
  mkdir -p "$(dirname "$ENV_FILE")"
  umask 077
  local root_password app_password
  root_password="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
  app_password="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
  cat > "$ENV_FILE" <<EOF
MYSQL_ROOT_PASSWORD=$root_password
MYSQL_DATABASE=slack_dashboard
MYSQL_USER=slack_dashboard
MYSQL_PASSWORD=$app_password
EOF
  chmod 600 "$ENV_FILE"
  echo "Created private MySQL environment"
}

ensure_docker() {
  if docker info >/dev/null 2>&1; then
    return
  fi
  if command -v colima >/dev/null 2>&1; then
    colima start
  else
    echo "Docker daemon is not available." >&2
    exit 1
  fi
}

compose() {
  docker compose -f "$COMPOSE_FILE" -p "$PROJECT_NAME" "$@"
}

command="${1:-status}"
case "$command" in
  start|up)
    create_env
    ensure_docker
    mkdir -p "$DB_ROOT/slack_dashboard_db/data"
    rm -f "$DB_ROOT/slack_dashboard_db/data/.gitkeep"
    compose up -d --wait
    ;;
  stop)
    ensure_docker
    compose stop
    ;;
  down)
    ensure_docker
    compose down
    ;;
  restart)
    create_env
    ensure_docker
    compose restart
    compose up -d --wait
    ;;
  status)
    ensure_docker
    compose ps
    ;;
  logs)
    ensure_docker
    compose logs --tail=100 mysql
    ;;
  *)
    echo "Usage: $0 {start|stop|down|restart|status|logs}" >&2
    exit 2
    ;;
esac
