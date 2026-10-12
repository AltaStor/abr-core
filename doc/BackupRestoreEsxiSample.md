# ESXi Backup And Restore Sample

This is a concrete sample flow for backing up and restoring a VM named
`example-vm` from an ESXi or vCenter endpoint with the current utility and local
API server.

## Prerequisites

- repository checked out at `/path/to/backup-server`
- Python virtual environment available at `.venv`
- local MongoDB bundle prepared under `tools/mongodb/` (by `scripts/check_deps.sh`)
- `abr-vmdk-tool` built from the separate `abr-vmdk-tools` repository, together
  with the VDDK runtime its build pulls in

## 1. Start MongoDB

```bash
cd /path/to/backup-server
mkdir -p /tmp/br-mongo
tools/mongodb/bin/mongod --dbpath /tmp/br-mongo --bind_ip 127.0.0.1 --port 27018
```

## 2. Start the API server

If port `8000` is free:

```bash
cd /path/to/backup-server
source .venv/bin/activate
export MONGODB_URI='mongodb://127.0.0.1:27018'
ALTASTOR_BACKUP_HOST=0.0.0.0 ALTASTOR_BACKUP_PORT=8000 python -m python.srv.backup_server
```

Run it through the module, not `uvicorn` directly: the module is what generates
the certificate, applies `defaults.env` and pins the TLS 1.2 cipher list. A bare
`uvicorn python.srv.backup_server:app` skips all of it and comes up with the
installed OpenSSL's default cipher suites.

If port `8000` is already in use, either stop the old process:

```bash
pkill -f 'uvicorn python.srv.backup_server:app'
pkill -f 'python -m python.srv.backup_server'
```

or start on `8001` and add `--base-url http://127.0.0.1:8001` to every CLI command below.

## 3. Locate the utility

`abr-vmdk-tool` is not built in this repository. Build it in the
`abr-vmdk-tools` repository, following its `README.md`, and note the launcher
path -- it is what the commands below pass to `--tool-path`:

```bash
TOOL_PATH=/path/to/abr-vmdk-tools/vmdk-tool/abr-vmdk-tool.sh
test -x "$TOOL_PATH"
```

Exporting it as `ALTASTOR_DISK_TOOL_PATH` instead makes it the server-wide
default, and `--tool-path` can then be left off.

## 4. Register the ESXi host

The server currently refuses a direct ESXi connection with "A direct ESXi
connection is not supported": backups through it do not work yet, because the
data mover asks VDDK for a vCenter-only operation. Register the vCenter Server
that manages the host instead. The steps below apply once direct connections
are supported.

```bash
cd /path/to/backup-server
source .venv/bin/activate
python -m python.cli.main register <host_or_ip> <username> '<password>' --name lab-hypervisor
```

Save the returned hypervisor ID for later commands:

```text
<hypervisor_id>
```

## 5. Poll inventory

Use the hypervisor ID, not the display name:

```bash
python -m python.cli.main inventory <hypervisor_id>
```

## 6. Find the VM external ID

```bash
python -m python.cli.main vms <hypervisor_id>
```

Find the row for `example-vm` and save its external ID:

```text
<vm_external_id>
```

## 7. Back up the VM

```bash
python -m python.cli.main backup-vm \
  <hypervisor_id> \
  <vm_external_id> \
  --tool-path "$TOOL_PATH" \
  --wait
```

Expected result:

- task reaches `completed`
- a local backup directory appears under `backups/example-vm/`
- a `manifest.json` is written there

## 8. Find the manifest

```bash
find /path/to/backup-server/backups -name manifest.json | sort
```

Example manifest from a successful run:

```text
/path/to/backup-server/backups/example-vm/<backup_id>/manifest.json
```

## 9. Restore the VM as a new VM

Use a new VM name on the same host and datastore:

```bash
python -m python.cli.main restore-vm \
  <hypervisor_id> \
  <vm_external_id> \
  /path/to/backup-server/backups/example-vm/<backup_id>/manifest.json \
  example-vm-restored \
  <target_host_name> \
  <target_datastore_name> \
  --tool-path "$TOOL_PATH" \
  --wait
```

If that target VM name already exists, use a different name such as:

```text
example-vm-restored-2
```

## 10. Verify the restored VM

```bash
python -m python.cli.main vms <hypervisor_id>
```

You can also power it on directly with Python:

```bash
source /path/to/backup-server/.venv/bin/activate
python -c "
import ssl, time
from pyVim.connect import SmartConnect, Disconnect
from pyVmomi import vim

si = SmartConnect(
    host='<host_or_ip>',
    user='<username>',
    pwd='<password>',
    port=443,
    sslContext=ssl._create_unverified_context(),
)
content = si.RetrieveContent()
view = content.viewManager.CreateContainerView(content.rootFolder, [vim.VirtualMachine], True)
vm = next(x for x in view.view if x.name == 'example-vm-restored')
view.Destroy()
task = vm.PowerOnVM_Task()
while task.info.state in (vim.TaskInfo.State.queued, vim.TaskInfo.State.running):
    time.sleep(1)
print(task.info.state)
Disconnect(si)
"
```

## Notes

- The CLI uses the hypervisor UUID, not the display name, for `inventory`, `vms`, and `disks`.
- The `backup-vm` and `restore-vm` commands use the VM external ID, not the VM name.
- The `HostAgent` restore path in this repo now creates a real managed disk through VMware first,
  then attaches it, then clones the backup into it. That is the restore shape that worked in our live run.
- If the API is running on a different port, add `--base-url http://127.0.0.1:<port>` to each CLI command.
