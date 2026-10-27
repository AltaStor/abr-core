# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

"""The function ID each `abr` command executes.

The server authorizes every request against the signed-in user's role, so this
table grants nothing: it names, in each command's help and in the audit log,
which permission the command needs. A command that first looks a name up
(`job start --name`) also needs the area's `.view` function, which every role
allowed to run it has.
"""

from __future__ import annotations

COMMAND_FUNCTIONS: dict[str, str] = {
    "auth login": "auth.login",
    "auth logout": "auth.logout",
    "auth whoami": "auth.whoami",
    "auth change-password": "auth.change_password",
    "user add": "user.create",
    "user list": "user.view",
    "user info": "user.view",
    "user edit": "user.edit",
    "user delete": "user.delete",
    "role list": "role.view",
    "role info": "role.view",
    "role functions": "role.view",
    "role create": "role.create",
    "role clone": "role.create",
    "role edit": "role.edit",
    "role delete": "role.delete",
    "audit list": "audit.view",
    "repo add": "repository.create",
    "repo list": "repository.view",
    "repo info": "repository.view",
    "repo edit": "repository.edit",
    "repo remove": "repository.delete",
    "repo rescan": "repository.rescan",
    "repo verify": "repository.verify",
    "repo verify-immutability": "repository.verify",
    "repo apply-immutability": "repository.apply_immutability",
    "inventory add": "inventory.create",
    "inventory list": "inventory.view",
    "inventory list-vms": "inventory.view",
    "inventory info": "inventory.view",
    "inventory edit": "inventory.edit",
    "inventory remove": "inventory.delete",
    "inventory rescan": "inventory.rescan",
    "inventory test": "inventory.test",
    "job create": "job.create",
    "job create-copy": "job.create",
    "job list": "job.view",
    "job info": "job.view",
    "job edit": "job.edit",
    "job start": "job.start",
    "job retention": "job.retention",
    "job delete": "job.delete",
    "session list": "session.view",
    "session info": "session.view",
    "session log": "session.view",
    "session stop": "session.stop",
    "backup list": "backup.view",
    "backup info": "backup.view",
    "backup delete": "backup.delete",
    "backup verify": "backup.verify",
    "backup import": "backup.import",
    "restore list": "restore.view",
    "restore point list": "restore.view",
    "restore context": "restore.view",
    "restore start": "restore.start",
    "system version": "system.view",
    "system health": "system.view",
    "system clock show": "system.view",
    "system clock confirm": "system.clock_confirm",
    "system export": "system.export",
    "system import": "system.import",
    "system get-support-bundle": "system.support_bundle",
}
