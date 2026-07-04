#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCAL_DIR="${ROOT_DIR}/.local"

MYSQL_CONTAINER="${TEXT2SQL_MYSQL_CONTAINER:-text2sql-mysql-local}"
LANGFUSE_DIR="${LANGFUSE_DIR:-${LOCAL_DIR}/langfuse}"

require_cmd() {
  local cmd="$1"
  if ! command -v "${cmd}" >/dev/null 2>&1; then
    echo "Missing required command: ${cmd}" >&2
    exit 1
  fi
}

require_cmd docker

if ! docker info >/dev/null 2>&1; then
  echo "Docker is not running or is not reachable." >&2
  exit 1
fi

if [[ -f "${LANGFUSE_DIR}/docker-compose.yml" ]]; then
  echo "Stopping Langfuse stack from ${LANGFUSE_DIR}..."
  (cd "${LANGFUSE_DIR}" && docker compose stop)
else
  echo "Skipping Langfuse: ${LANGFUSE_DIR}/docker-compose.yml not found."
fi

if docker container inspect "${MYSQL_CONTAINER}" >/dev/null 2>&1; then
  if [[ "$(docker inspect -f '{{.State.Running}}' "${MYSQL_CONTAINER}")" == "true" ]]; then
    echo "Stopping MySQL container ${MYSQL_CONTAINER}..."
    docker stop "${MYSQL_CONTAINER}" >/dev/null
  else
    echo "MySQL container ${MYSQL_CONTAINER} is already stopped."
  fi
else
  echo "Skipping MySQL: container ${MYSQL_CONTAINER} not found."
fi

echo
echo "Middleware containers after stop:"
docker ps -a \
  --filter "name=${MYSQL_CONTAINER}" \
  --filter "name=langfuse_selfhost" \
  --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
