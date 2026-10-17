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
.venv/bin/python -m python.cli auth login --name admin   # bootstrap admin/admin
.venv/bin/python -m python.cli system health
```

The CLI defaults to `https://127.0.0.1`, so no `BACKUP_SERVER_URL` is needed.
Ports 443 and 80 are privileged, so `run_srv.sh` asks `sudo` once to lower
`net.ipv4.ip_unprivileged_port_start`; failing that it falls back to 8443 and
8080 and prints the `export BACKUP_SERVER_URL=...` line to paste. Port 80 only
redirects to HTTPS, which is what makes the address work with no scheme typed -
see `doc/DeploymentBuildAndScenario.md` for the details and for what a real
install needs instead.

Every `/api/v1` call needs a session, so `auth login` comes first. The shipped
`admin`/`admin` is a bootstrap credential - change it with
`abr auth change-password`.

Stop the server with `./scripts/stop_srv.sh`, and remove the virtualenv and the
MongoDB bundle with `./scripts/clear.sh`.

The server generates its own self-signed certificate on first start, and the
CLI pins the certificate it sees on first connection - no flags needed for
normal use.

### Pointing the server at the data mover

For VMware backup and restore, build `abr-vmdk-tool` in the `abr-vmdk-tools`
repository and point this one at the launcher:

```bash
export ALTASTOR_DISK_TOOL_PATH=/path/to/abr-vmdk-tools/vmdk-tool/abr-vmdk-tool.sh
```

Without it the server falls back to `tools/abr-vmdk-tool/abr-vmdk-tool.sh` in
this checkout (`tools/` is git-ignored, so a symlink there works too) and, in a
packaged install, to `/opt/altastor-backup/tools/abr-vmdk-tool/`.

Hyper-V disks are moved by `abr-vhdx-tool.exe` from the `abr-vhdx-tools`
repository, on the Hyper-V host itself. Build it there (its README covers the
cross build on Linux) and point the server at the `.exe`; the server copies it
to each host the first time it backs up or restores a VM there:

```bash
export ALTASTOR_VHDX_TOOL_PATH=/path/to/abr-vhdx-tools/build/vhdx-tool/abr-vhdx-tool.exe
```

Without it the server looks in `tools/abr-vhdx-tool/` as above, and with none
there each host must have the tool installed already, at
`ALTASTOR_HYPERV_TOOL_PATH` (`C:\Program Files\AltaStor\abr-vhdx-tool.exe`).

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
