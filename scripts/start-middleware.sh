#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCAL_DIR="${ROOT_DIR}/.local"
ENV_FILE="${ROOT_DIR}/.env"

MYSQL_CONTAINER="${TEXT2SQL_MYSQL_CONTAINER:-text2sql-mysql-local}"
MYSQL_IMAGE="${TEXT2SQL_MYSQL_IMAGE:-mysql:8.4}"
MYSQL_VOLUME="${TEXT2SQL_MYSQL_VOLUME:-text2sql_mysql_data_v2}"
MYSQL_INIT_DIR="${TEXT2SQL_MYSQL_INIT_DIR:-${LOCAL_DIR}/mysql/init}"
LANGFUSE_DIR="${LANGFUSE_DIR:-${LOCAL_DIR}/langfuse}"

read_env() {
  local key="$1"
  local file="$2"

  [[ -f "${file}" ]] || return 1
  awk -v key="${key}" '
    index($0, key "=") == 1 {
      sub("^[^=]*=", "")
      print
      exit
    }
  ' "${file}"
}

require_cmd() {
  local cmd="$1"
  if ! command -v "${cmd}" >/dev/null 2>&1; then
    echo "Missing required command: ${cmd}" >&2
    exit 1
  fi
}

metadata_url="${TEXT2SQL_METADATA_DATABASE_URL:-$(read_env TEXT2SQL_METADATA_DATABASE_URL "${ENV_FILE}" || true)}"
if [[ -z "${metadata_url}" ]]; then
  cat >&2 <<'EOF'
TEXT2SQL_METADATA_DATABASE_URL is not configured.
Add it to .env, for example:
TEXT2SQL_METADATA_DATABASE_URL=mysql+pymysql://text2sql_app:password@127.0.0.1:3307/text2sql_meta?charset=utf8mb4
EOF
  exit 1
fi

if [[ ! "${metadata_url}" =~ ^mysql[^:]*://([^:]+):([^@]+)@([^:/?]+):([0-9]+)/([^?]+) ]]; then
  echo "TEXT2SQL_METADATA_DATABASE_URL must be a mysql URL: ${metadata_url}" >&2
  exit 1
fi

mysql_user="${BASH_REMATCH[1]}"
mysql_password="${BASH_REMATCH[2]}"
mysql_host="${BASH_REMATCH[3]}"
mysql_port="${BASH_REMATCH[4]}"
mysql_database="${BASH_REMATCH[5]}"
mysql_root_password="${TEXT2SQL_MYSQL_ROOT_PASSWORD:-$(read_env TEXT2SQL_MYSQL_ROOT_PASSWORD "${ENV_FILE}" || true)}"
mysql_root_password="${mysql_root_password:-text2sql_root_pw_local}"

require_cmd docker

if ! docker info >/dev/null 2>&1; then
  echo "Docker is not running or is not reachable." >&2
  exit 1
fi

if [[ "${mysql_host}" != "127.0.0.1" && "${mysql_host}" != "localhost" ]]; then
  echo "Skipping local MySQL container because metadata DB host is ${mysql_host}."
else
  if docker container inspect "${MYSQL_CONTAINER}" >/dev/null 2>&1; then
    echo "Starting MySQL container ${MYSQL_CONTAINER}..."
    docker start "${MYSQL_CONTAINER}" >/dev/null
  else
    echo "Creating MySQL container ${MYSQL_CONTAINER} on 127.0.0.1:${mysql_port}..."
    docker volume create "${MYSQL_VOLUME}" >/dev/null

    mysql_args=(
      run
      -d
      --name "${MYSQL_CONTAINER}"
      -p "127.0.0.1:${mysql_port}:3306"
      -e "MYSQL_ROOT_PASSWORD=${mysql_root_password}"
      -e "MYSQL_DATABASE=${mysql_database}"
      -e "MYSQL_USER=${mysql_user}"
      -e "MYSQL_PASSWORD=${mysql_password}"
      -v "${MYSQL_VOLUME}:/var/lib/mysql"
    )

    if [[ -d "${MYSQL_INIT_DIR}" ]]; then
      mysql_args+=(-v "${MYSQL_INIT_DIR}:/docker-entrypoint-initdb.d:ro")
    fi

    mysql_args+=("${MYSQL_IMAGE}")
    docker "${mysql_args[@]}" >/dev/null
  fi
fi

if [[ -f "${LANGFUSE_DIR}/docker-compose.yml" ]]; then
  echo "Starting Langfuse stack from ${LANGFUSE_DIR}..."
  (cd "${LANGFUSE_DIR}" && docker compose up -d)
else
  echo "Skipping Langfuse: ${LANGFUSE_DIR}/docker-compose.yml not found."
fi

echo
echo "Middleware containers:"
docker ps \
  --filter "name=${MYSQL_CONTAINER}" \
  --filter "name=langfuse_selfhost" \
  --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
