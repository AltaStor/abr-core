#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

set -euo pipefail

# Removes exactly what scripts/check_deps.sh creates inside the checkout: the
# Python virtualenv and the local MongoDB bundle. Anything installed system-wide
# (apt packages) is left alone, and so is anything else under tools/ -- a
# symlink to the abr-vmdk-tool built in the abr-vmdk-tools repository lives
# there and is not ours to delete.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${REPO_ROOT}/.venv"
MONGODB_CACHE_DIR="${REPO_ROOT}/tools/mongodb"

remove_path_if_exists() {
  local path="$1"
  if [[ -e "${path}" || -L "${path}" ]]; then
    rm -rf "${path}"
  fi
}

main() {
  remove_path_if_exists "${VENV_DIR}"
  remove_path_if_exists "${MONGODB_CACHE_DIR}"

  cat <<EOF
Dependencies removed.
- Python virtualenv removed: ${VENV_DIR}
- Local MongoDB bundle removed: ${MONGODB_CACHE_DIR}

Note: shared Debian packages installed with apt were left in place, and so was
the MongoDB data directory (default /tmp/br-mongo).
EOF
}

main "$@"
