#!/usr/bin/env bash

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${REPO_ROOT}/.venv"
REQUIREMENTS_FILE="${REPO_ROOT}/python/requirements.txt"
MONGODB_CACHE_DIR="${REPO_ROOT}/tools/mongodb"

# Official MongoDB Community archive naming for Debian 12 x64.
# Override MONGODB_VERSION if you want a different release.
MONGODB_VERSION="${MONGODB_VERSION:-8.2.6}"
MONGODB_BASENAME="mongodb-linux-x86_64-debian12-${MONGODB_VERSION}"
MONGODB_ARCHIVE="${MONGODB_BASENAME}.tgz"
MONGODB_URL="https://fastdl.mongodb.org/linux/${MONGODB_ARCHIVE}"

# This repository holds the Python server and CLI only. The C++ data mover
# (abr-vmdk-tool, its VDDK bundle and its vcpkg/crc32c build dependencies)
# lives in the separate abr-vmdk-tools repository and is built there, so none
# of the C++ toolchain is installed here.
APT_PACKAGES=(
  ca-certificates
  curl
  git
  python3
  python3-pip
  python3-venv
  tar
)

run_as_root() {
  if [[ "${EUID}" -eq 0 ]]; then
    "$@"
  else
    require_command sudo
    sudo "$@"
  fi
}

require_command() {
  local cmd="$1"
  if ! command -v "${cmd}" >/dev/null 2>&1; then
    echo "Missing required command: ${cmd}" >&2
    exit 1
  fi
}

ensure_writable_directory() {
  local path="$1"
  local label="$2"

  if [[ -e "${path}" && ! -w "${path}" ]]; then
    cat >&2 <<EOF
${label} is not writable by $(id -un):$(id -gn): ${path}
This usually means it was created by another user or with preserved archive ownership.

Fix it with one of these options:
  sudo chown -R "$(id -un):$(id -gn)" "${path}"
  mv "${path}" "${path}.bak"  # preserve the old contents, then rerun this script
EOF
    exit 1
  fi
}

download_file() {
  local url="$1"
  local output="$2"

  if command -v curl >/dev/null 2>&1; then
    curl -fL "${url}" -o "${output}"
    return
  fi

  if command -v wget >/dev/null 2>&1; then
    wget -O "${output}" "${url}"
    return
  fi

  echo "Neither curl nor wget is available for downloading files." >&2
  exit 1
}

install_apt_dependencies() {
  export DEBIAN_FRONTEND=noninteractive
  run_as_root apt-get update
  run_as_root apt-get install -y "${APT_PACKAGES[@]}"
}

install_python_dependencies() {
  if [[ ! -f "${REQUIREMENTS_FILE}" ]]; then
    echo "Missing Python dependency file: ${REQUIREMENTS_FILE}" >&2
    exit 1
  fi

  ensure_writable_directory "${VENV_DIR}" "Python virtualenv directory"
  python3 -m venv "${VENV_DIR}"
  "${VENV_DIR}/bin/python" -m pip install --upgrade pip setuptools wheel
  "${VENV_DIR}/bin/pip" install -r "${REQUIREMENTS_FILE}"
}

download_mongodb() {
  local archive_path

  archive_path="${MONGODB_CACHE_DIR}/${MONGODB_ARCHIVE}"
  mkdir -p "${MONGODB_CACHE_DIR}"

  if [[ -f "${archive_path}" ]]; then
    echo "MongoDB archive already present: ${archive_path}"
    return
  fi

  download_file "${MONGODB_URL}" "${archive_path}"
}

extract_mongodb_bundle() {
  local archive_path
  local extract_root

  archive_path="${MONGODB_CACHE_DIR}/${MONGODB_ARCHIVE}"
  extract_root="${MONGODB_CACHE_DIR}/${MONGODB_BASENAME}"

  ensure_writable_directory "${MONGODB_CACHE_DIR}" "MongoDB cache directory"

  if [[ ! -f "${archive_path}" ]]; then
    echo "Missing MongoDB archive: ${archive_path}" >&2
    exit 1
  fi

  if [[ ! -x "${extract_root}/bin/mongod" ]]; then
    tar -xzf "${archive_path}" -C "${MONGODB_CACHE_DIR}"
  fi

  if [[ ! -x "${extract_root}/bin/mongod" ]]; then
    echo "MongoDB extraction did not produce ${extract_root}/bin/mongod" >&2
    exit 1
  fi

  ln -sfn "${MONGODB_BASENAME}/bin" "${MONGODB_CACHE_DIR}/bin"
}

report_disk_tool() {
  # The server shells out to the abr-vmdk-tool launcher for every VMware disk
  # operation. Nothing here can build it, so this is a report, not a check that
  # can fail: the server and CLI run (and the whole test suite passes) without
  # it, only real vCenter backup/restore needs it.
  local configured="${ALTASTOR_DISK_TOOL_PATH:-}"

  if [[ -n "${configured}" && -x "${configured}" ]]; then
    echo "Disk tool: ${configured} (from ALTASTOR_DISK_TOOL_PATH)"
    return
  fi

  if [[ -x "${REPO_ROOT}/tools/abr-vmdk-tool/abr-vmdk-tool.sh" ]]; then
    echo "Disk tool: ${REPO_ROOT}/tools/abr-vmdk-tool/abr-vmdk-tool.sh"
    return
  fi

  cat <<EOF
Disk tool: not found.
  abr-vmdk-tool is built from the separate abr-vmdk-tools repository. Point the
  server at the built launcher with:
    export ALTASTOR_DISK_TOOL_PATH=/path/to/abr-vmdk-tools/vmdk-tool/abr-vmdk-tool.sh
  or symlink it into ${REPO_ROOT}/tools/abr-vmdk-tool/.
EOF
}

main() {
  local mongodb_bin

  require_command apt-get

  mongodb_bin="${MONGODB_CACHE_DIR}/bin/mongod"

  install_apt_dependencies
  install_python_dependencies
  download_mongodb
  extract_mongodb_bundle

  cat <<EOF
Environment prepared.
- Python virtualenv: ${VENV_DIR}
- Python requirements: ${REQUIREMENTS_FILE}
- MongoDB archive cached at: ${MONGODB_CACHE_DIR}/${MONGODB_ARCHIVE}
- MongoDB binary: ${mongodb_bin}

Activate the virtualenv with:
  source "${VENV_DIR}/bin/activate"

EOF

  report_disk_tool
}

main "$@"
