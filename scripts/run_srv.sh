#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
VENV_DIR="${REPO_ROOT}/.venv"

# Dev default: loopback only. defaults.env ships 0.0.0.0 for a real install.
HOST="${HOST:-127.0.0.1}"
MONGODB_URI="${MONGODB_URI:-mongodb://127.0.0.1:27018}"

PORT_WAS_SET=1
if [[ -z "${PORT:-}" ]]; then
  PORT=443
  PORT_WAS_SET=0
fi

REDIRECT_WAS_SET=1
if [[ -z "${ABR_HTTP_REDIRECT_PORT+x}" ]]; then
  REDIRECT_PORT=80
  REDIRECT_WAS_SET=0
else
  REDIRECT_PORT="${ABR_HTTP_REDIRECT_PORT}"
fi

FALLBACK_PORT="${FALLBACK_PORT:-8443}"
FALLBACK_REDIRECT_PORT="${FALLBACK_REDIRECT_PORT:-8080}"

if [[ ! -d "${VENV_DIR}" ]]; then
  echo "Virtualenv not found: ${VENV_DIR}" >&2
  echo "Run ${SCRIPT_DIR}/check_deps.sh first." >&2
  exit 1
fi

cd "${REPO_ROOT}"

can_bind() {
  "${VENV_DIR}/bin/python" - "$1" "$2" <<'PY' 2>/dev/null
import socket, sys
try:
    socket.create_server((sys.argv[1], int(sys.argv[2]))).close()
except OSError:
    sys.exit(1)
PY
}

unprivileged_port_start() {
  cat /proc/sys/net/ipv4/ip_unprivileged_port_start 2>/dev/null || echo 1024
}

allow_low_ports() {
  local wanted="$1"
  local current
  current="$(unprivileged_port_start)"
  if [[ "${current}" -le "${wanted}" ]]; then
    return 1
  fi
  if ! command -v sudo >/dev/null 2>&1; then
    return 1
  fi
  echo "Ports below ${current} are privileged. Lowering net.ipv4.ip_unprivileged_port_start to ${wanted} so this dev run can use ${PORT} and ${REDIRECT_PORT}." >&2
  echo "It lasts until reboot. To keep it: echo 'net.ipv4.ip_unprivileged_port_start=${wanted}' | sudo tee /etc/sysctl.d/10-altastor-dev.conf" >&2
  if sudo -n true 2>/dev/null; then
    sudo -n sysctl -q -w "net.ipv4.ip_unprivileged_port_start=${wanted}"
  elif [[ -t 0 ]]; then
    sudo sysctl -q -w "net.ipv4.ip_unprivileged_port_start=${wanted}"
  else
    echo "No usable sudo in a non-interactive shell; leaving the threshold alone." >&2
    return 1
  fi
}

lowest_wanted_port() {
  local lowest="${PORT}"
  if [[ -n "${REDIRECT_PORT}" && "${REDIRECT_PORT}" -lt "${lowest}" ]]; then
    lowest="${REDIRECT_PORT}"
  fi
  echo "${lowest}"
}

if ! can_bind "${HOST}" "${PORT}" || { [[ -n "${REDIRECT_PORT}" ]] && ! can_bind "${HOST}" "${REDIRECT_PORT}"; }; then
  allow_low_ports "$(lowest_wanted_port)" || true
fi

if ! can_bind "${HOST}" "${PORT}"; then
  if [[ "${PORT_WAS_SET}" -eq 1 ]]; then
    echo "Cannot bind ${HOST}:${PORT}." >&2
    exit 1
  fi
  echo "Cannot bind ${HOST}:${PORT}; falling back to ${FALLBACK_PORT}. For the CLI in this shell:" >&2
  echo "  export BACKUP_SERVER_URL=https://${HOST}:${FALLBACK_PORT}" >&2
  PORT="${FALLBACK_PORT}"
fi

if [[ -n "${REDIRECT_PORT}" ]] && ! can_bind "${HOST}" "${REDIRECT_PORT}"; then
  if [[ "${REDIRECT_WAS_SET}" -eq 1 ]]; then
    echo "Cannot bind the redirect listener on ${HOST}:${REDIRECT_PORT}." >&2
    exit 1
  fi
  echo "Cannot bind the redirect listener on ${HOST}:${REDIRECT_PORT}; falling back to ${FALLBACK_REDIRECT_PORT}." >&2
  REDIRECT_PORT="${FALLBACK_REDIRECT_PORT}"
fi

export ALTASTOR_BACKUP_HOST="${HOST}"
export ALTASTOR_BACKUP_PORT="${PORT}"
export ABR_HTTP_REDIRECT_PORT="${REDIRECT_PORT}"
export MONGODB_URI

# Launch through the module, the way the altastor-backup-server systemd unit
# does -- not through `uvicorn python.srv.backup_server:app`. The module is what
# generates/renews the self-signed certificate and pins the TLS 1.2 cipher list;
# a bare uvicorn invocation skips that and serves whatever suites the installed
# OpenSSL defaults to.
exec "${VENV_DIR}/bin/python" -m python.srv.backup_server
