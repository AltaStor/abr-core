#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

set -euo pipefail
umask 077
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
export LC_ALL=C

SHARE_CONF=/etc/samba/altastor-backup.conf
SMB_CONF=/etc/samba/smb.conf
INCLUDE_LINE="include = ${SHARE_CONF}"
ACCOUNT_COMMENT="AltaStor Backup Hyper-V share"
SHARE_PATTERN='^abr-[a-z0-9]{8}[$]$'
USER_PATTERN='^abr-smb-[a-z0-9]{8}$'
PASSWORD_PATTERN='^[A-Za-z0-9]{32,128}$'
SAMBA_TOOLS="smbd testparm smbpasswd pdbedit smbcontrol"
BACKUP_COPY=""
CREATED_ACCOUNT=""
SHARE=""
SHARE_USER=""
PASSWORD=""
FOLDER=""

fail() {
    echo "abr-samba-helper: $*" >&2
    exit 1
}

usage() {
    fail "usage: abr-samba-helper.sh enable-share|disable-share|remove-share|status (enable-share and disable-share read key=value lines on stdin)"
}

require_root() {
    [ "$(id -u)" -eq 0 ] || fail "must run as root"
}

read_request() {
    local line key value
    while IFS= read -r line || [ -n "${line}" ]; do
        [ -n "${line}" ] || continue
        key=${line%%=*}
        value=${line#*=}
        case "${key}" in
            share) SHARE=${value} ;;
            user) SHARE_USER=${value} ;;
            password) PASSWORD=${value} ;;
            path) FOLDER=${value} ;;
            *) fail "unknown request field '${key}'" ;;
        esac
    done
    [[ "${SHARE}" =~ ${SHARE_PATTERN} ]] || fail "the share name must look like abr-xxxxxxxx\$"
    [[ "${SHARE_USER}" =~ ${USER_PATTERN} ]] || fail "the user name must look like abr-smb-xxxxxxxx"
}

samba_parameter() {
    testparm -s --parameter-name="$1" "${SMB_CONF}" 2>/dev/null | tr '[:upper:]' '[:lower:]'
}

included() {
    [ -f "${SMB_CONF}" ] && grep -qxF "${INCLUDE_LINE}" "${SMB_CONF}"
}

write_atomically() {
    local target=$1 mode=$2 temporary
    temporary=$(mktemp "${target}.XXXXXX")
    cat >"${temporary}"
    chown root:root "${temporary}"
    chmod "${mode}" "${temporary}"
    mv -f "${temporary}" "${target}"
}

rewrite_smb_conf() {
    local temporary
    temporary=$(mktemp "${SMB_CONF}.XXXXXX")
    cat >"${temporary}"
    chown --reference="${SMB_CONF}" "${temporary}"
    chmod --reference="${SMB_CONF}" "${temporary}"
    mv -f "${temporary}" "${SMB_CONF}"
}

configured_value() {
    [ -f "${SHARE_CONF}" ] || return 0
    sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" "${SHARE_CONF}" | head -n 1
}

configured_share() {
    [ -f "${SHARE_CONF}" ] || return 0
    sed -n 's/^\[\(.*\)\]$/\1/p' "${SHARE_CONF}" | head -n 1
}

preflight() {
    local missing="" tool
    for tool in ${SAMBA_TOOLS}; do
        command -v "${tool}" >/dev/null 2>&1 || missing="${missing} ${tool}"
    done
    [ -f "${SMB_CONF}" ] || missing="${missing} ${SMB_CONF}"
    [ -z "${missing}" ] || fail "Samba is not installed (missing:${missing})"
    systemctl is-active --quiet smbd || fail "smbd is installed but not running; it is not started from here, since another SMB server may own port 445 on purpose. Start it with systemctl start smbd and turn the switch on again"
    testparm -s "${SMB_CONF}" >/dev/null 2>&1 || fail "the existing Samba configuration does not validate (testparm -s ${SMB_CONF}); it is left as it is"
    case "$(samba_parameter 'server role')" in
        *"active directory domain controller"*) fail "this Samba is an Active Directory domain controller; its configuration is left as it is" ;;
    esac
    case "$(samba_parameter 'config backend')" in
        *registry*) fail "this Samba keeps its configuration in the registry (config backend = registry); it is left as it is" ;;
    esac
    case "$(samba_parameter 'passdb backend')" in
        tdbsam*) ;;
        *) fail "this Samba keeps its accounts in '$(samba_parameter 'passdb backend')', not tdbsam; no account is added to it" ;;
    esac
    if [ "$(configured_share)" != "${SHARE}" ] && testparm -s "${SMB_CONF}" 2>/dev/null | grep -qixF "[${SHARE}]"; then
        fail "a share named ${SHARE} already exists in the Samba configuration; it is left as it is"
    fi
}

check_folder() {
    local owner
    [[ "${FOLDER}" == /* ]] || fail "the folder must be an absolute path"
    [ -d "${FOLDER}" ] || fail "${FOLDER} is not a folder"
    [ "$(realpath -e -- "${FOLDER}")" = "${FOLDER}" ] || fail "${FOLDER} must be given without symbolic links or relative parts"
    owner=$(stat -c %u -- "${FOLDER}")
    [ "${owner}" -ne 0 ] || fail "${FOLDER} belongs to root; only a folder of the service user is shared"
    if [ -n "${SUDO_UID:-}" ] && [ "${owner}" != "${SUDO_UID}" ]; then
        fail "${FOLDER} does not belong to the user that asked for the share"
    fi
    FOLDER_OWNER=$(stat -c %U -- "${FOLDER}")
    FOLDER_GROUP=$(stat -c %G -- "${FOLDER}")
}

share_config() {
    cat <<EOF
[${SHARE}]
    path = ${FOLDER}
    browseable = no
    read only = no
    guest ok = no
    valid users = ${SHARE_USER}
    force user = ${FOLDER_OWNER}
    force group = ${FOLDER_GROUP}
    create mask = 0640
    directory mask = 0750
    server smb encrypt = required
EOF
}

account_exists() {
    pdbedit -L -u "$1" >/dev/null 2>&1
}

ours() {
    [ "$(getent passwd "$1" | cut -d: -f5)" = "${ACCOUNT_COMMENT}" ]
}

remove_account() {
    local name=$1
    [[ "${name}" =~ ${USER_PATTERN} ]] || return 0
    if account_exists "${name}"; then
        smbpasswd -x "${name}" >/dev/null || fail "the Samba account ${name} could not be removed"
    fi
    if ours "${name}"; then
        userdel "${name}" >/dev/null 2>&1 || fail "the system account ${name} could not be removed"
    fi
}

create_account() {
    if getent passwd "${SHARE_USER}" >/dev/null; then
        ours "${SHARE_USER}" || fail "a system account named ${SHARE_USER} already exists and is not one this helper made"
    else
        useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin \
            --no-user-group --gid nogroup --comment "${ACCOUNT_COMMENT}" "${SHARE_USER}" \
            || fail "the system account ${SHARE_USER} could not be created"
        CREATED_ACCOUNT="${SHARE_USER}"
    fi
    printf '%s\n%s\n' "${PASSWORD}" "${PASSWORD}" | smbpasswd -s -a "${SHARE_USER}" >/dev/null || fail "smbpasswd refused the account ${SHARE_USER}"
    smbpasswd -e "${SHARE_USER}" >/dev/null || fail "the Samba account ${SHARE_USER} could not be enabled"
}

restore_configuration() {
    if [ -n "${BACKUP_COPY}" ] && [ -f "${BACKUP_COPY}" ]; then
        cp -p "${BACKUP_COPY}" "${SMB_CONF}"
        rm -f "${BACKUP_COPY}"
        if [ -n "${PREVIOUS_SHARE_CONF:-}" ] && [ -f "${PREVIOUS_SHARE_CONF}" ]; then
            cp -p "${PREVIOUS_SHARE_CONF}" "${SHARE_CONF}"
            rm -f "${PREVIOUS_SHARE_CONF}"
        else
            rm -f "${SHARE_CONF}"
        fi
    fi
    if [ -n "${CREATED_ACCOUNT}" ]; then
        (remove_account "${CREATED_ACCOUNT}") || true
    fi
}

reload_samba() {
    smbcontrol smbd reload-config >/dev/null 2>&1 || systemctl reload smbd >/dev/null 2>&1 \
        || fail "smbd did not reload its configuration; the previous configuration is restored"
    systemctl is-active --quiet smbd || fail "smbd stopped after reloading its configuration; the previous configuration is restored"
}

enable_share() {
    local previous_user
    [[ "${PASSWORD}" =~ ${PASSWORD_PATTERN} ]] || fail "the password must be 32 to 128 letters and digits"
    check_folder
    preflight
    previous_user=$(configured_value "valid users")
    BACKUP_COPY=$(mktemp /etc/samba/.smb.conf.abr-backup.XXXXXX)
    cp -p "${SMB_CONF}" "${BACKUP_COPY}"
    if [ -f "${SHARE_CONF}" ]; then
        PREVIOUS_SHARE_CONF=$(mktemp /etc/samba/.altastor-backup.conf.abr-backup.XXXXXX)
        cp -p "${SHARE_CONF}" "${PREVIOUS_SHARE_CONF}"
    fi
    trap restore_configuration EXIT
    share_config | write_atomically "${SHARE_CONF}" 0644
    if ! included; then
        { cat "${SMB_CONF}"; [ -z "$(tail -c 1 "${SMB_CONF}")" ] || echo; printf '%s\n' "${INCLUDE_LINE}"; } | rewrite_smb_conf
    fi
    testparm -s "${SMB_CONF}" >/dev/null 2>&1 || fail "the Samba configuration does not validate with the share added; the previous configuration is restored"
    testparm -s "${SMB_CONF}" 2>/dev/null | grep -qixF "[${SHARE}]" || fail "Samba does not see the share after it was added; the previous configuration is restored"
    create_account
    reload_samba
    trap - EXIT
    rm -f "${BACKUP_COPY}" "${PREVIOUS_SHARE_CONF:-}"
    if [ -n "${previous_user}" ] && [ "${previous_user}" != "${SHARE_USER}" ]; then
        remove_account "${previous_user}"
    fi
}

disable_share() {
    local configured
    configured=$(configured_share)
    if [ -n "${configured}" ] && [ "${configured}" != "${SHARE}" ]; then
        fail "the configured share is ${configured}, not ${SHARE}; it is left as it is"
    fi
    if included; then
        { grep -vxF "${INCLUDE_LINE}" "${SMB_CONF}" || true; } | rewrite_smb_conf
    fi
    rm -f "${SHARE_CONF}"
    remove_account "${SHARE_USER}"
    if systemctl is-active --quiet smbd; then
        smbcontrol smbd reload-config >/dev/null 2>&1 || systemctl reload smbd >/dev/null 2>&1 || true
    fi
}

status() {
    local share user include=false account=false running=false
    share=$(configured_share)
    user=$(configured_value "valid users")
    included && include=true
    [ -n "${user}" ] && account_exists "${user}" && account=true
    systemctl is-active --quiet smbd && running=true
    printf '{"share": "%s", "user": "%s", "path": "%s", "included": %s, "account": %s, "smbd_running": %s}\n' \
        "${share}" "${user}" "$(configured_value path)" "${include}" "${account}" "${running}"
}

[ "$#" -eq 1 ] || usage
require_root
case "$1" in
    enable-share)
        read_request
        enable_share
        status
        ;;
    disable-share)
        read_request
        disable_share
        status
        ;;
    remove-share)
        SHARE=$(configured_share)
        SHARE_USER=$(configured_value "valid users")
        if [ -n "${SHARE}" ]; then
            [[ "${SHARE}" =~ ${SHARE_PATTERN} ]] || fail "the configured share ${SHARE} is not one this helper made; it is left as it is"
            disable_share
        fi
        status
        ;;
    status)
        status
        ;;
    *)
        usage
        ;;
esac
