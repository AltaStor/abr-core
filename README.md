# AltaStor Backup & Replication - Core

The Python core of AltaStor Backup & Replication: the BackupServer API and the
`abr` command-line client.

## Directory structure
- `doc` - documentation
- `python/srv` - BackupServer API service
- `python/cli` - `abr` command-line client
- `python/tests` - test suite
- `scripts` - shell scripts for the local development environment

## Quick start

```bash
./scripts/check_deps.sh                 # .venv, Python requirements, local MongoDB bundle
tools/mongodb/bin/mongod --dbpath /tmp/br-mongo --bind_ip 127.0.0.1 --port 27018 &
./scripts/run_srv.sh                    # HTTPS on 127.0.0.1:8443
```

Then, in another shell:

```bash
export BACKUP_SERVER_URL=https://127.0.0.1:8443
.venv/bin/python -m python.cli auth login --name admin   # bootstrap admin/admin
.venv/bin/python -m python.cli system health
```

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

## Tests

```bash
.venv/bin/python -m unittest discover -s python/tests -t . -p 'test_*.py'
```

The suite is self-contained: it mocks the data mover and runs without MongoDB,
VDDK, or a vCenter endpoint.

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
