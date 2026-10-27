# Deployment, Build, And ESXi Backup/Restore Scenario

This runbook explains how to deploy the local BackupServer solution, obtain its
disk utility, start the services, and run the full CLI workflow against the ESXi
or vCenter endpoint using placeholder connection values.

The concrete scenario registers the server, discovers inventory, backs up a VM
named `example-vm`, restores it as a new VM, and verifies jobs, tasks, and VM
inventory afterward.

## 1. System overview

The solution has four main parts:

- FastAPI service: `python.srv.backup_server:app`
- CLI client: `python -m python.cli.main`
- MongoDB persistence for registered hypervisors, resources, jobs, and tasks
- VDDK-backed disk tool `abr-vmdk-tool`, built from the separate
  `abr-vmdk-tools` repository

The CLI talks to the local API service. The API service stores state in MongoDB
when `MONGODB_URI` is configured. Backup and restore tasks are processed by the
API background worker pool, which calls the VDDK disk tool for VMware disk
operations.

This repository holds the Python core only. The disk tool is not built here; see
section 4 for how to point the server at a build of it.

## 2. Prerequisites

Required host tools:

- Linux shell environment
- `python3`, `python3-venv`, and `pip`
- Git
- network reachability from this machine to your ESXi or vCenter endpoint
- a built `abr-vmdk-tool` from the `abr-vmdk-tools` repository, for VMware
  backup and restore

The API service and the CLI start and run without the disk tool; only the
backup and restore tasks in sections 9 onward need it. The VDDK runtime is part
of the disk tool's own build, and is not installed by anything in this
repository. If you install via the Debian package, that extracted VDDK runtime
is bundled into the `.deb` and installed under
`/opt/altastor-backup-server/tools/vddk`.

Repository layout assumed by this runbook:

```text
/path/to/backup-server
```

The commands below are written for that path. If the repository is elsewhere,
replace `/path/to/backup-server` with the actual path.

## 3. Prepare dependencies

From a fresh machine, run the dependency helper:

```bash
cd /path/to/backup-server
./scripts/check_deps.sh
```

That script creates `.venv`, installs Python requirements from
`python/requirements.txt`, and prepares a local MongoDB bundle under
`tools/mongodb/`. It finishes by reporting where it found the disk tool, or
that it found none.

If the virtual environment already exists, this quick verification is enough:

```bash
cd /path/to/backup-server
.venv/bin/python -m pip show fastapi pymongo pyvmomi requests uvicorn
test -x tools/mongodb/bin/mongod
```

## 4. Obtain the VDDK disk tool

`abr-vmdk-tool` is not built in this repository. Clone and build it from the
`abr-vmdk-tools` repository, following the build guide in its `README.md`; that
guide also covers the VDDK download, which Broadcom gates behind a support
account.

The build produces two files, and the launcher script is the one callers run --
it locates the VDDK bundle, puts it on the library search path and picks a
writable `TMPDIR`:

```text
/path/to/abr-vmdk-tools/build/vmdk-tool/abr-vmdk-tool
/path/to/abr-vmdk-tools/vmdk-tool/abr-vmdk-tool.sh
```

Point this repository at the launcher:

```bash
export ALTASTOR_DISK_TOOL_PATH=/path/to/abr-vmdk-tools/vmdk-tool/abr-vmdk-tool.sh
```

The server reads `ALTASTOR_DISK_TOOL_PATH` first, then falls back to
`/opt/altastor-backup/tools/abr-vmdk-tool/abr-vmdk-tool.sh` (a packaged
install) and to `tools/abr-vmdk-tool/abr-vmdk-tool.sh` under this checkout.
`tools/` is git-ignored, so symlinking the built launcher there is an
alternative to exporting the variable:

```bash
mkdir -p /path/to/backup-server/tools/abr-vmdk-tool
ln -sfn /path/to/abr-vmdk-tools/vmdk-tool/abr-vmdk-tool.sh \
  /path/to/backup-server/tools/abr-vmdk-tool/abr-vmdk-tool.sh
```

Individual backup and restore commands can also override it per call with
`--tool-path`, which is what the scenario below does.

The server hands the tools their secrets (encryption keys, S3 secret keys and
the vCenter password) on standard input with `--stdin-keys`, so they never show
in `ps` or `/proc/<pid>/cmdline`. That needs abr-vmdk-tool and abr-vhdx-tool
from abr-disk-tools 3ee1dab or later; an older tool refuses the option as
unknown, so ship the tools together with the server.

## 5. Start services

Use three terminals. Keep Terminal 1 and Terminal 2 open while running the CLI
commands in Terminal 3.

### Terminal 1: MongoDB

```bash
cd /path/to/backup-server
mkdir -p /tmp/br-mongo
tools/mongodb/bin/mongod --dbpath /tmp/br-mongo --bind_ip 127.0.0.1 --port 27018
```

MongoDB is ready when it prints a message similar to:

```text
Waiting for connections
```

### Terminal 2: BackupServer API

The server always serves HTTPS. It generates its own self-signed certificate
on first start (see `python/srv/core/tls_bootstrap.py`) if
`ABR_TLS_CERT_FILE`/`ABR_TLS_KEY_FILE` aren't set to point at a real one:

```bash
cd /path/to/backup-server
MONGODB_URI=mongodb://127.0.0.1:27018 \
  .venv/bin/python -m python.srv.backup_server
```

Launch it through the module, the way the `altastor-backup-server` systemd unit
does, rather than by invoking `uvicorn` directly. The module is what generates
the certificate, applies `defaults.env` and pins the TLS 1.2 cipher list
(`ABR_TLS_CIPHERS`); a bare `uvicorn python.srv.backup_server:app` never runs
that code, so it comes up with whatever cipher suites the installed OpenSSL
defaults to. Host and port come from `ALTASTOR_BACKUP_HOST` and
`ALTASTOR_BACKUP_PORT`, and the plain-HTTP redirect listener from
`ABR_HTTP_REDIRECT_PORT`.

The API is ready when it prints:

```text
Plain-HTTP redirect listener on 0.0.0.0:80 -> https://<host>:443
Application startup complete.
Uvicorn running on https://0.0.0.0:443
```

### Ports and privileges

`defaults.env` ships 443 for HTTPS and 80 for the redirect listener. Both are
below 1024, so the process needs `CAP_NET_BIND_SERVICE`; the
`altastor-backup-server` systemd unit grants exactly that and nothing else:

```ini
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
```

The bounding set line is a restriction, not a grant: it drops every other
capability the service would otherwise be allowed to hold. Without the ambient
line the server cannot bind 443 and will not start (`[Errno 13] permission
denied`). The redirect listener is softer -- a port it cannot bind is a warning
in the log and HTTPS comes up regardless.

The redirect listener exists because the browser, not the user, chooses the
scheme when someone types a bare host name: that guess is `http`, it would land
on a TLS port, and the handshake failure shows as an unexplained browser error.
Port 80 answers it with a 308 to `https://<the host the client asked for>`. A
single TCP port cannot serve both TLS and plaintext, so this cannot live on 443.

For a development run, `scripts/run_srv.sh` handles the privilege question on
its own (see the README). To use a different port instead:

```bash
cd /path/to/backup-server
MONGODB_URI=mongodb://127.0.0.1:27018 ALTASTOR_BACKUP_PORT=8001 \
  ABR_HTTP_REDIRECT_PORT= \
  .venv/bin/python -m python.srv.backup_server
```

Then add `--base-url https://127.0.0.1:8001` to every CLI command, or export:

```bash
export BACKUP_SERVER_URL=https://127.0.0.1:8001
```

### Terminal 3: CLI shell

```bash
cd /path/to/backup-server
export BACKUP_SERVER_URL=https://127.0.0.1
```

`https://127.0.0.1` is the CLI default, so the export is only needed when the
server runs on a non-default port. Note that `https://host` and
`https://host:443` are the same endpoint and share one stored session token.

The certificate is self-signed, so the CLI doesn't verify it against a CA;
instead it pins the certificate it sees on first connection and refuses to
proceed if a later connection to the same host:port presents a different one
(see `python/cli/tls_trust.py`) -- no extra flags needed for normal use.

To serve a certificate of your own -- CA-issued or self-signed -- install it
from the web console (Settings > Security > HTTPS certificate) or with
`PUT /api/v1/system/tls-certificate` (`certificate`: PEM, server certificate
first then intermediates; `private_key`: PEM; optional `passphrase` for an
encrypted key). The pair is validated before anything is replaced and loaded
into the running server without a restart. It is never renewed or regenerated
by the appliance -- that only happens to the self-signed certificate it created
itself -- so replace it before it expires; `DELETE` on the same path goes back
to a freshly generated self-signed one. If the server certificate is
installed without its intermediate CA certificates, the console and the API
warn (`chain_incomplete`): clients that do not already have the intermediate
refuse the connection.

Because the key changes, a CLI that pinned the previous certificate checks the
new one against the CA certificates the machine trusts (the system store, or
`SSL_CERT_FILE`): one a trusted CA issued for the host the CLI connects to is
accepted and becomes the new pin. Any other new key is refused until its entry
is removed from `~/.altastor-backup/trusted_certs.json`.

The renewal check, at start and on its interval, never touches an installed
certificate; it logs that the certificate is not renewed automatically, and an
error once it has expired. `python -m python.srv.core.tls_bootstrap --renew`
refuses to replace one as well; `--renew --force` replaces it with a
self-signed certificate and a new key. Every refused or failed install is
logged with its reason.

Log in. Every `/api/v1` call needs a session, so this comes before anything
else. The shipped `admin`/`admin` is a bootstrap credential -- change it with
`abr auth change-password` once the environment is up:

```bash
.venv/bin/python -m python.cli.main auth login --name admin
```

Check the API:

```bash
.venv/bin/python -m python.cli.main system health
```

Expected result: a table whose `STATUS` column reads `ok`, with `api`,
`database`, `scheduler` and `task_pool` all reporting `ok` under `COMPONENTS`.
Add `--output json` for the machine-readable form.

## 6. Scenario variables

Set the values used by the scenario:

```bash
cd /path/to/backup-server

ESXI_HOST=<host_or_ip>
ESXI_USER=<username>
ESXI_PASSWORD='<password>'
HV_NAME=lab-hypervisor
VM_NAME=example-vm
TARGET_HOST_NAME=<target_host_name>
TARGET_DATASTORE_NAME=<target_datastore_name>
TOOL_PATH=/path/to/abr-vmdk-tools/vmdk-tool/abr-vmdk-tool.sh
```

If your target restore host or datastore differs, change
`TARGET_HOST_NAME` and `TARGET_DATASTORE_NAME` before running restore.

## 7. Register the ESXi/vCenter endpoint

Register the server:

```bash
HV_ID=$(.venv/bin/python -m python.cli.main register "$ESXI_HOST" "$ESXI_USER" "$ESXI_PASSWORD" \
  --name "$HV_NAME" \
  | .venv/bin/python -c 'import sys,json; print(json.load(sys.stdin)["id"])')

echo "HV_ID=$HV_ID"
```

List registrations:

```bash
.venv/bin/python -m python.cli.main --output table list
```

If this server was already registered and you want to reuse it instead of
creating another record, set the known ID manually:

```bash
HV_ID=<hypervisor_id>
echo "HV_ID=$HV_ID"
```

## 8. Discover inventory

Poll the endpoint:

```bash
.venv/bin/python -m python.cli.main inventory "$HV_ID"
```

List virtual machines:

```bash
.venv/bin/python -m python.cli.main --output table vms "$HV_ID"
```

List disks and refresh inventory first:

```bash
.venv/bin/python -m python.cli.main --output table disks "$HV_ID" --refresh
```

Extract the VM external ID for `example-vm`:

```bash
VM_ID=$(.venv/bin/python -m python.cli.main vms "$HV_ID" \
  | VM_NAME="$VM_NAME" .venv/bin/python -c 'import os,sys,json; data=json.load(sys.stdin); print(next(vm["external_id"] for vm in data["virtual_machines"] if vm["name"] == os.environ["VM_NAME"]))')

echo "VM_ID=$VM_ID"
```

The command prints the VM external ID:

```text
<vm_external_id>
```

If the extraction command fails, inspect the table output from `vms` and either
change `VM_NAME` or set `VM_ID` manually:

```bash
VM_ID=<vm_external_id_from_vms_output>
```

## 9. Back up the VM

Confirm the disk tool exists:

```bash
test -x "$TOOL_PATH"
```

Start a VM backup and wait for completion:

```bash
.venv/bin/python -m python.cli.main backup-vm "$HV_ID" "$VM_ID" \
  --tool-path "$TOOL_PATH" \
  --wait
```

Expected behavior:

- a backup job is created automatically
- a backup task is queued, processed, and reaches `completed`
- backup files are written below `backups/<vm-name>/<task-id>/`
- a `manifest.json` is written for the VM backup

List jobs and tasks for the hypervisor:

```bash
.venv/bin/python -m python.cli.main --output table jobs --hypervisor-id "$HV_ID"
.venv/bin/python -m python.cli.main --output table tasks --hypervisor-id "$HV_ID"
```

Find the newest backup manifest:

```bash
MANIFEST=$(find /path/to/backup-server/backups -name manifest.json -printf '%T@ %p\n' \
  | sort -nr \
  | head -n 1 \
  | cut -d' ' -f2-)

echo "MANIFEST=$MANIFEST"
```

Verify the manifest file exists:

```bash
test -f "$MANIFEST"
```

## 10. Restore the VM as a new VM

Generate a restore VM name that should not collide with an existing VM:

```bash
RESTORE_NAME="example-vm-restored-$(date +%Y%m%d-%H%M%S)"
echo "RESTORE_NAME=$RESTORE_NAME"
```

Restore the VM backup manifest as a new VM:

```bash
.venv/bin/python -m python.cli.main restore-vm "$HV_ID" "$VM_ID" "$MANIFEST" \
  "$RESTORE_NAME" \
  "$TARGET_HOST_NAME" \
  "$TARGET_DATASTORE_NAME" \
  --tool-path "$TOOL_PATH" \
  --wait
```

Optional disk type controls:

```bash
.venv/bin/python -m python.cli.main restore-vm "$HV_ID" "$VM_ID" "$MANIFEST" \
  "$RESTORE_NAME" \
  "$TARGET_HOST_NAME" \
  "$TARGET_DATASTORE_NAME" \
  --tool-path "$TOOL_PATH" \
  --disk-type thin-provisioned \
  --wait
```

Per-disk overrides use 1-based disk indexes:

```bash
.venv/bin/python -m python.cli.main restore-vm "$HV_ID" "$VM_ID" "$MANIFEST" \
  "$RESTORE_NAME" \
  "$TARGET_HOST_NAME" \
  "$TARGET_DATASTORE_NAME" \
  --tool-path "$TOOL_PATH" \
  --disk-types-json '{"1":"original","2":"thin-provisioned"}' \
  --wait
```

## 11. Verify restore results

Refresh and list VMs:

```bash
.venv/bin/python -m python.cli.main inventory "$HV_ID"
.venv/bin/python -m python.cli.main --output table vms "$HV_ID"
```

Check jobs and tasks:

```bash
.venv/bin/python -m python.cli.main --output table jobs --hypervisor-id "$HV_ID"
.venv/bin/python -m python.cli.main --output table tasks --hypervisor-id "$HV_ID"
```

The restore task should reach `completed`, and the VM table should include the
new `RESTORE_NAME`.

## 12. Optional: restore a single disk

If you want to restore a single VMDK rather than the whole VM, first choose a
disk external ID:

```bash
.venv/bin/python -m python.cli.main --output table disks "$HV_ID" --refresh
DISK_ID=<disk_external_id_from_disks_output>
BACKUP_VMDK=<path_to_local_backup_vmdk>
```

Restore and attach it to the original VM:

```bash
.venv/bin/python -m python.cli.main restore-disk "$HV_ID" "$DISK_ID" "$BACKUP_VMDK" \
  --tool-path "$TOOL_PATH" \
  --wait
```

Restore without attaching:

```bash
.venv/bin/python -m python.cli.main restore-disk "$HV_ID" "$DISK_ID" "$BACKUP_VMDK" \
  --tool-path "$TOOL_PATH" \
  --no-attach \
  --wait
```

## 13. Service shutdown

Stop the API server with `Ctrl+C` in Terminal 2.

Stop MongoDB with `Ctrl+C` in Terminal 1.

If you need to stop leftover processes by command:

```bash
pkill -f 'python -m python.srv.backup_server'
pkill -f 'mongod --dbpath /tmp/br-mongo'
```

Use those only when you are sure no other work depends on those local
processes.

## 14. Troubleshooting

API is not reachable:

```bash
.venv/bin/python -m python.cli.main system health
```

If it fails with connection refused, start Terminal 2 or verify
`BACKUP_SERVER_URL`. If it reports that there is no session, run
`.venv/bin/python -m python.cli.main auth login --name admin` first.

Inventory is empty:

```bash
.venv/bin/python -m python.cli.main inventory "$HV_ID"
.venv/bin/python -m python.cli.main --output table vms "$HV_ID"
```

VM name lookup fails:

```bash
.venv/bin/python -m python.cli.main --output table vms "$HV_ID"
echo "$VM_NAME"
```

Change `VM_NAME` to match the table exactly, or set `VM_ID` manually.

Disk tool is missing:

```bash
test -x "$TOOL_PATH"
```

If it is not there, build it in the `abr-vmdk-tools` repository (section 4) and
set `TOOL_PATH`/`ALTASTOR_DISK_TOOL_PATH` to its launcher.

Restore VM name already exists:

```bash
RESTORE_NAME="example-vm-restored-$(date +%Y%m%d-%H%M%S)"
```

Wrong API port:

```bash
export BACKUP_SERVER_URL=https://127.0.0.1:8001
```

Server will not start, `[Errno 13] permission denied` on port 443: the unit is
missing `AmbientCapabilities=CAP_NET_BIND_SERVICE`, or the run is a development
one outside `scripts/run_srv.sh`.

Browser reaches HTTPS without ever being redirected: expected. Once 443 answers,
a browser may try `https://` first and never touch port 80. To exercise the
redirect, name the scheme: `curl -sS -o /dev/null -w '%{redirect_url}\n'
http://<host>`.

CLI asks to log in again after an upgrade: the stored session token is keyed by
server URL, so moving from `https://host:8443` to `https://host` is a different
key. Run `abr auth login` once.

Upgraded installation still on 8443: a port written into
`/etc/altastor-backup/server.conf` overrides `defaults.env`, and an upgrade does
not rewrite it. Change it there deliberately, and update `BACKUP_SERVER_URL` for
every CLI client at the same time.


## 15. Minimal command checklist

This is the short version once the environment is ready:

```bash
cd /path/to/backup-server
export BACKUP_SERVER_URL=https://127.0.0.1

ESXI_HOST=<host_or_ip>
ESXI_USER=<username>
ESXI_PASSWORD='<password>'
HV_NAME=lab-hypervisor
VM_NAME=example-vm
TARGET_HOST_NAME=<target_host_name>
TARGET_DATASTORE_NAME=<target_datastore_name>
TOOL_PATH=/path/to/abr-vmdk-tools/vmdk-tool/abr-vmdk-tool.sh

HV_ID=$(.venv/bin/python -m python.cli.main register "$ESXI_HOST" "$ESXI_USER" "$ESXI_PASSWORD" --name "$HV_NAME" \
  | .venv/bin/python -c 'import sys,json; print(json.load(sys.stdin)["id"])')

.venv/bin/python -m python.cli.main inventory "$HV_ID"
.venv/bin/python -m python.cli.main --output table vms "$HV_ID"
.venv/bin/python -m python.cli.main --output table disks "$HV_ID" --refresh

VM_ID=$(.venv/bin/python -m python.cli.main vms "$HV_ID" \
  | VM_NAME="$VM_NAME" .venv/bin/python -c 'import os,sys,json; data=json.load(sys.stdin); print(next(vm["external_id"] for vm in data["virtual_machines"] if vm["name"] == os.environ["VM_NAME"]))')

.venv/bin/python -m python.cli.main backup-vm "$HV_ID" "$VM_ID" --tool-path "$TOOL_PATH" --wait

MANIFEST=$(find /path/to/backup-server/backups -name manifest.json -printf '%T@ %p\n' | sort -nr | head -n 1 | cut -d' ' -f2-)
RESTORE_NAME="restored-$(date +%Y%m%d-%H%M%S)"

.venv/bin/python -m python.cli.main restore-vm "$HV_ID" "$VM_ID" "$MANIFEST" \
  "$RESTORE_NAME" "$TARGET_HOST_NAME" "$TARGET_DATASTORE_NAME" \
  --tool-path "$TOOL_PATH" \
  --wait

.venv/bin/python -m python.cli.main --output table jobs --hypervisor-id "$HV_ID"
.venv/bin/python -m python.cli.main --output table tasks --hypervisor-id "$HV_ID"
.venv/bin/python -m python.cli.main --output table vms "$HV_ID"
```
