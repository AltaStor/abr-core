#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

set -euo pipefail

PATTERNS=(
  # How run_srv.sh and the systemd unit start it, plus the bare-uvicorn form
  # that older instructions used.
  "-m python.srv.backup_server"
  "uvicorn python.srv.backup_server:app"
  "python.srv.backup_server:app"
  "scripts/run_srv.sh"
)

TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-10}"

collect_pids() {
  local pattern
  for pattern in "${PATTERNS[@]}"; do
    ps -eo pid=,args= | awk -v pat="${pattern}" -v self_pid="$$" '
      index($0, pat) > 0 {
        pid = $1
        $1 = ""
        sub(/^[[:space:]]+/, "", $0)
        if (pid == self_pid) {
          next
        }
        if (index($0, "awk -v pat=") > 0) {
          next
        }
        if (index($0, "stop_srv.sh") > 0) {
          next
        }
        printf "%s\t%s\n", pid, $0
      }
    '
  done | sort -u
}

main() {
  mapfile -t matches < <(collect_pids)

  if [[ "${#matches[@]}" -eq 0 ]]; then
    echo "No running srv processes found."
    return 0
  fi

  echo "Stopping srv processes:"
  local entry pid cmd
  for entry in "${matches[@]}"; do
    pid="${entry%%$'\t'*}"
    cmd="${entry#*$'\t'}"
    echo "  PID=${pid} CMD=${cmd}"
  done

  for entry in "${matches[@]}"; do
    pid="${entry%%$'\t'*}"
    kill "${pid}" 2>/dev/null || true
  done

  local deadline=$((SECONDS + TIMEOUT_SECONDS))
  local remaining=()
  while (( SECONDS < deadline )); do
    remaining=()
    for entry in "${matches[@]}"; do
      pid="${entry%%$'\t'*}"
      if kill -0 "${pid}" 2>/dev/null; then
        remaining+=("${entry}")
      fi
    done
    if [[ "${#remaining[@]}" -eq 0 ]]; then
      echo "All srv processes stopped."
      return 0
    fi
    sleep 1
  done

  echo "Force stopping remaining srv processes:"
  for entry in "${remaining[@]}"; do
    pid="${entry%%$'\t'*}"
    cmd="${entry#*$'\t'}"
    echo "  PID=${pid} CMD=${cmd}"
    kill -9 "${pid}" 2>/dev/null || true
  done

  echo "Done."
}

main "$@"
