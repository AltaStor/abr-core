#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "Run it as root: sudo bash $0" >&2; exit 1; }

missing=""
for program in smbd testparm smbpasswd pdbedit smbcontrol smbclient; do
    command -v "${program}" >/dev/null 2>&1 || missing="${missing} ${program}"
done

if [ -n "${missing}" ]; then
    if ! command -v smbd >/dev/null 2>&1 && ss -ltn 2>/dev/null | grep -q ':445 '; then
        echo "Port 445 is already taken by another SMB server (for example ksmbd); Samba is not installed next to it." >&2
        exit 1
    fi
    echo "Installing Samba (missing:${missing})..."
    apt-get update -q
    DEBIAN_FRONTEND=noninteractive apt-get install -y -q samba smbclient
    systemctl enable --now smbd
elif ! systemctl is-active --quiet smbd; then
    echo "Samba is installed but smbd is not running; it is left as it is. Start it with: sudo systemctl enable --now smbd" >&2
    exit 1
fi

echo "Samba $(smbd --version | awk '{print $2}') is installed and smbd is $(systemctl is-active smbd)."
echo "Share a local repository with: sudo python3 abr_smb_share.py add <path> (sudo python3 abr_smb_share.py show lists them)"
