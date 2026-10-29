#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HELPER="${REPO_ROOT}/python/srv/plugins/hyperv/smb_share/abr-samba-helper.sh"
RULE=/etc/sudoers.d/abr-samba-helper-dev
DEV_USER="${SUDO_USER:-$(id -un)}"
STAGED=""
trap '[[ -z "${STAGED}" ]] || rm -f "${STAGED}"' EXIT

usage() {
  echo "usage: $(basename "$0") [--remove]" >&2
  echo "  Installs Samba and lets ${DEV_USER} run ${HELPER} as root, so the Default Repo's" >&2
  echo "  Hyper-V switch works on a development machine. --remove takes the sudo rule away." >&2
  exit 2
}

as_root() {
  if [[ "$(id -u)" -eq 0 ]]; then
    "$@"
  else
    sudo "$@"
  fi
}

remove_rule() {
  if as_root test -e "${RULE}"; then
    as_root rm -f "${RULE}"
    echo "Removed ${RULE}; Samba stays installed."
  else
    echo "${RULE} is not there; nothing to remove."
  fi
}

install_rule() {
  [[ "${DEV_USER}" != "root" ]] || { echo "Run this as your own user, not as root: the rule is for the user who runs the server." >&2; exit 1; }
  [[ -x "${HELPER}" ]] || { echo "${HELPER} is missing or not executable." >&2; exit 1; }

  if ! command -v smbd >/dev/null 2>&1 || ! command -v testparm >/dev/null 2>&1; then
    echo "Installing Samba..."
    as_root apt-get install -y samba
  fi
  if ! systemctl is-active --quiet smbd; then
    echo "Warning: smbd is installed but not running; start it with 'sudo systemctl start smbd' before turning the switch on." >&2
  fi

  STAGED="$(mktemp)"
  printf '%s ALL=(root) NOPASSWD: %s\n' "${DEV_USER}" "${HELPER}" >"${STAGED}"
  as_root visudo -cf "${STAGED}" >/dev/null
  as_root install -m 0440 -o root -g root "${STAGED}" "${RULE}"

  echo "Samba: $(command -v smbd), smbd $(systemctl is-active smbd || true)"
  echo "${RULE}: $(as_root cat "${RULE}")"
  echo "Turn the share on with: abr repo default --support-hyperv"
  echo "The rule gives user ${DEV_USER} root through a script in this checkout; keep it to a development machine."
}

case "${1:-}" in
  "") install_rule ;;
  --remove) remove_rule ;;
  *) usage ;;
esac
