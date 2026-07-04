#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="${ROOT_DIR}/backend"
ENV_FILE="${TEXT2SQL_ENV_FILE:-${ROOT_DIR}/.env}"

require_cmd() {
  local cmd="$1"
  if ! command -v "${cmd}" >/dev/null 2>&1; then
    echo "Missing required command: ${cmd}" >&2
    exit 1
  fi
}

is_truthy() {
  case "$1" in
    1|true|TRUE|yes|YES|on|ON) return 0 ;;
    *) return 1 ;;
  esac
}

is_falsey() {
  case "$1" in
    0|false|FALSE|no|NO|off|OFF) return 0 ;;
    *) return 1 ;;
  esac
}

load_env_file() {
  if is_falsey "${TEXT2SQL_LOAD_ENV:-1}"; then
    return
  fi

  if [[ -f "${ENV_FILE}" ]]; then
    echo "Loading environment from ${ENV_FILE}"
    set -a
    # shellcheck disable=SC1090
    source "${ENV_FILE}"
    set +a
  fi
}

load_env_file

if [[ -n "${PYTHON:-}" ]]; then
  PYTHON_BIN="${PYTHON}"
  AUTO_CREATE_VENV=0
elif [[ -x "${BACKEND_DIR}/.venv/bin/python" ]]; then
  PYTHON_BIN="${BACKEND_DIR}/.venv/bin/python"
  AUTO_CREATE_VENV=0
elif [[ -x "${ROOT_DIR}/.venv/bin/python" ]]; then
  PYTHON_BIN="${ROOT_DIR}/.venv/bin/python"
  AUTO_CREATE_VENV=0
else
  PYTHON_BIN="python3"
  AUTO_CREATE_VENV=1
fi

HOST="${TEXT2SQL_API_HOST:-127.0.0.1}"
PORT="${TEXT2SQL_API_PORT:-8000}"
RELOAD="${TEXT2SQL_API_RELOAD:-1}"
INSTALL_DEPS="${TEXT2SQL_INSTALL_DEPS:-auto}"
CREATE_SAMPLE_DB="${TEXT2SQL_CREATE_SAMPLE_DB:-auto}"
SAMPLE_DB_PATH="${TEXT2SQL_SAMPLE_DB_PATH:-examples/demo.db}"

require_cmd "${PYTHON_BIN}"
export PYTHONPATH="${BACKEND_DIR}/src${PYTHONPATH:+:${PYTHONPATH}}"

can_import_backend() {
  (
    cd "${BACKEND_DIR}"
    "${PYTHON_BIN}" -c "import uvicorn; import text2sql.api" >/dev/null 2>&1
  )
}

ensure_install_python() {
  if [[ "${AUTO_CREATE_VENV}" == "1" ]] && ! is_truthy "${TEXT2SQL_USE_SYSTEM_PYTHON:-0}"; then
    echo "Creating virtual environment at ${BACKEND_DIR}/.venv"
    "${PYTHON_BIN}" -m venv "${BACKEND_DIR}/.venv"
    PYTHON_BIN="${BACKEND_DIR}/.venv/bin/python"
    AUTO_CREATE_VENV=0
  fi
}

install_backend() {
  ensure_install_python
  echo "Installing backend package in editable mode..."
  (
    cd "${BACKEND_DIR}"
    "${PYTHON_BIN}" -m pip install -e .
  )
}

sample_db_abs_path() {
  if [[ "${SAMPLE_DB_PATH}" = /* ]]; then
    printf '%s\n' "${SAMPLE_DB_PATH}"
  else
    printf '%s\n' "${BACKEND_DIR}/${SAMPLE_DB_PATH}"
  fi
}

create_sample_db() {
  echo "Creating sample database at ${SAMPLE_DB_PATH}"
  (
    cd "${BACKEND_DIR}"
    "${PYTHON_BIN}" -m text2sql.core.sample_data --output "${SAMPLE_DB_PATH}" >/dev/null
  )
}

case "${INSTALL_DEPS}" in
  auto)
    if ! can_import_backend; then
      install_backend
    fi
    ;;
  1|true|TRUE|yes|YES|on|ON)
    install_backend
    ;;
  0|false|FALSE|no|NO|off|OFF)
    if ! can_import_backend; then
      cat >&2 <<EOF
Backend dependencies are missing.
Run one of:
  ${PYTHON_BIN} -m pip install -e "${BACKEND_DIR}"
  TEXT2SQL_INSTALL_DEPS=1 ${ROOT_DIR}/scripts/start-backend.sh
EOF
      exit 1
    fi
    ;;
  *)
    echo "TEXT2SQL_INSTALL_DEPS must be auto, 1, or 0; got: ${INSTALL_DEPS}" >&2
    exit 1
    ;;
esac

case "${CREATE_SAMPLE_DB}" in
  auto)
    if [[ ! -f "$(sample_db_abs_path)" ]]; then
      create_sample_db
    fi
    ;;
  1|true|TRUE|yes|YES|on|ON|always)
    create_sample_db
    ;;
  0|false|FALSE|no|NO|off|OFF)
    ;;
  *)
    echo "TEXT2SQL_CREATE_SAMPLE_DB must be auto, 1, or 0; got: ${CREATE_SAMPLE_DB}" >&2
    exit 1
    ;;
esac

uvicorn_args=(text2sql.api:app --host "${HOST}" --port "${PORT}")
if is_truthy "${RELOAD}"; then
  uvicorn_args+=(--reload)
fi

echo "Starting Text2SQL backend at http://${HOST}:${PORT}"
echo "Press Ctrl+C to stop."

cd "${BACKEND_DIR}"
exec "${PYTHON_BIN}" -m uvicorn "${uvicorn_args[@]}"
