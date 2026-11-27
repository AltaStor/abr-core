# AltaStor Backup & Replication - Core

The Python core of AltaStor Backup & Replication: the BackupServer API and the
`abr` command-line client.

## Directory structure
- `doc` - documentation
- `python/srv` - BackupServer API service
- `python/cli` - `abr` command-line client
- `python/tests` - test suite: `srv/` (server, by area: `api`, `backup`, `hyperv`, `s3`, `vcenter`, ...) and `cli/`; `manual/` holds scripts run by hand against a live server
- `scripts` - shell scripts for the local development environment

## Quick start

```bash
./scripts/check_deps.sh                 # .venv, Python requirements, local MongoDB bundle
tools/mongodb/bin/mongod --dbpath /tmp/br-mongo --bind_ip 127.0.0.1 --port 27018 &
./scripts/run_srv.sh                    # HTTPS on 127.0.0.1:443, redirect on :80
```

Then, in another shell:

```bash
.venv/bin/python -m python.cli auth login --name admin   # the bootstrap account, see below
.venv/bin/python -m python.cli system health
```

The CLI defaults to `https://127.0.0.1`, so no `BACKUP_SERVER_URL` is needed.
Ports 443 and 80 are privileged, so `run_srv.sh` asks `sudo` once to lower
`net.ipv4.ip_unprivileged_port_start`; failing that it falls back to 8443 and
8080 and prints the `export BACKUP_SERVER_URL=...` line to paste. Port 80 only
redirects to HTTPS, which is what makes the address work with no scheme typed -
see `doc/DeploymentBuildAndScenario.md` for the details and for what a real
install needs instead.

Every `/api/v1` call needs a session, so `auth login` comes first. The
bootstrap account comes from `ABR_BOOTSTRAP_ADMIN_USER` and
`ABR_BOOTSTRAP_ADMIN_PASSWORD` in `python/srv/defaults.env` - change its password
with `abr auth change-password`. It holds the Administrator role; give everyone else
the role they need (`abr user add --name alice --role backup_operator`, see
`doc/RBAC.md`).

Stop the server with `./scripts/stop_srv.sh`, and remove the virtualenv and the
MongoDB bundle with `./scripts/clear.sh`.

The server generates its own self-signed certificate on first start, and the
CLI pins the certificate it sees on first connection - no flags needed for
normal use.

### Pointing the server at the data mover

VMware and Hyper-V disks are moved by the tools of the `abr-disk-tools` repository.
Its README covers the builds, the Windows one cross-built on Linux included.

For VMware backup and restore, build `abr-vmdk-tool` with the `linux` preset and point this repository at the launcher:

```bash
export ALTASTOR_DISK_TOOL_PATH=/path/to/abr-disk-tools/build-linux/bin/abr-vmdk-tool.sh
```

Without it the server falls back to `tools/abr-vmdk-tool/abr-vmdk-tool.sh` in
this checkout (`tools/` is git-ignored, so a symlink there works too) and, in a
packaged install, to `/opt/altastor-backup/tools/abr-vmdk-tool/`.

The server tells the tool which VDDK library to load.
A new server takes LibreVDDK, `librevddk/lib64/libvixDiskLib.so` next to the launcher's folder (`tools/librevddk/`),
and without it the VMware VDDK of `ABR_VMDK_TOOL_VDDK_DIR` or of a `vddk` folder there.
`abr settings vsphere` changes the choice, server-wide or per hypervisor; see `doc/AltaStorCLI.md`.

Hyper-V disks are moved by `abr-vhdx-tool.exe`, on the Hyper-V host itself.
Build it with the `windows-cross` preset and point the server at the `.exe`; the server copies it to each host the
first time it backs up or restores a VM there:

```bash
export ALTASTOR_VHDX_TOOL_PATH=/path/to/abr-disk-tools/build-windows-cross/bin/abr-vhdx-tool.exe
```

Without it the server looks in `tools/abr-vhdx-tool/x64/`, where the `altastor-backup-vhdx-tools` package installs it,
and then in `tools/abr-vhdx-tool/`, as above.
With none there each host must have the tool installed already, at `ALTASTOR_HYPERV_TOOL_PATH`
(`C:\Program Files\AltaStor\abr-vhdx-tool.exe`).

## Tests

```bash
.venv/bin/python -m unittest discover -s python/tests -t . -p 'test_*.py'
```

One area only, for example the S3 tests:

```bash
.venv/bin/python -m unittest discover -s python/tests/srv/s3 -t . -p 'test_*.py'
```

The suite is self-contained: it mocks the data mover and runs without MongoDB,
VDDK, or a vCenter or Hyper-V endpoint.

## Documentation
- [Deployment, build, and ESXi scenario](doc/DeploymentBuildAndScenario.md) -
  runbook for preparing the environment, starting the services, and running a
  generic ESXi backup/restore scenario.
- [CLI reference](doc/CLI.md) - the supported `abr` command tree.
- [AltaStor CLI reference](doc/AltaStorCLI.md) - full command structure and
  descriptions.
- [ESXi backup and restore sample](doc/BackupRestoreEsxiSample.md) - concrete
  end-to-end lab flow.
- [Server state recovery](doc/ServerStateRecovery.md) - recovering the catalog
  after a server loss.
- [Hyper-V backup and recovery](doc/HyperVBackup.md) - what Hyper-V backup and
  recovery need from the host, how they run, and what to check when they fail.
- [Roles, permissions and the audit log](doc/RBAC.md) - the built-in roles,
  the permission matrix, how requests are authorized, and the audit log.
- [Two-factor sign-in](doc/TwoFactorSignIn.md) - authenticator app codes and
  recovery codes, and how an Administrator resets a user who lost their phone.
- [Task processing](doc/TaskProcessing.md) - how a task moves from its creation
  through the background workers to its result.
- [Notifications](doc/Notifications.md) - the notification service, its
  events, e-mail (SMTP with TLS, password sign-in, failures and retries),
  messaging platforms (Telegram, Slack, Discord, ntfy) through Apprise, API
  and CLI.

Note: the environment and service setup in the deployment runbook and the ESXi
sample is current, but their scenario commands still use the pre-v4.1 CLI
(`register`, `backup-vm`, ...). `doc/CLI.md` and `doc/AltaStorCLI.md` are the
authority on the command tree the CLI actually implements today.

## Branches
- `main` - is used for the common base to which all tested updates are merged
- personal work branches should be named as `<initials>.<custon_suffix>`, like `ab.test`
- developers may use personal branches as they like and delete them if they are not needed

## Naming
- Use `snake_case` names with clear meaning and try to keep them short.
- Modifications in the folder tree should be discussed with the team.

## License

Apache License 2.0 — see [LICENSE](LICENSE).

Copyright © 2026 Norvitex OÜ (AltaStor.com)
