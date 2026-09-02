#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
VENV_DIR="${REPO_ROOT}/.venv"

# Dev default: loopback only. defaults.env ships 0.0.0.0 for a real install.
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8443}"
MONGODB_URI="${MONGODB_URI:-mongodb://127.0.0.1:27018}"

if [[ ! -d "${VENV_DIR}" ]]; then
  echo "Virtualenv not found: ${VENV_DIR}" >&2
  echo "Run ${SCRIPT_DIR}/check_deps.sh first." >&2
  exit 1
fi

cd "${REPO_ROOT}"

export ALTASTOR_BACKUP_HOST="${HOST}"
export ALTASTOR_BACKUP_PORT="${PORT}"
export MONGODB_URI

# Launch through the module, the way the altastor-backup-server systemd unit
# does -- not through `uvicorn python.srv.backup_server:app`. The module is what
# generates/renews the self-signed certificate and pins the TLS 1.2 cipher list;
# a bare uvicorn invocation skips that and serves whatever suites the installed
# OpenSSL defaults to.
exec "${VENV_DIR}/bin/python" -m python.srv.backup_server
