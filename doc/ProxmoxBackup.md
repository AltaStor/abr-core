# Proxmox VE backup and recovery

How AltaStor backs up and restores Proxmox VE virtual machines, what it needs from the PVE side, and what to expect when
something goes wrong. Proxmox VE 9 is supported; containers are not.

## The data mover runs on the nodes

PVE backs up and restores VMs itself, through the backup provider API of `vzdump` and `qmrestore`. AltaStor plugs into
it with the **abr-pve-backup** package (the `abr-pve-backup` repository), which every node of the cluster carries:

- the AltaStor backup provider, a Perl module PVE loads for a storage of type `abr`;
- `abr-nbd-tool`, which reads a disk from QEMU's NBD export and writes it to the repository as an `.abr` backup, full
  or incremental from the disk's dirty bitmap;
- `nbdkit-abr-plugin`, which serves a disk's chain of backups to PVE during a restore, through a kernel NBD device.

The backup server never touches the disks. It starts each backup and restore as a PVE task through the PVE API, with
an API token of its own, and hands the provider what the task needs in a run file it writes to `/run/abr` on the node
through AltaStor's node helper over SSH: the repository and the backups to write or read. The secrets never go into a
file: the helper keeps them in memory and hands them once to the task, see [The node helper](#the-node-helper). The
provider runs only tasks abr-core started:
a backup or restore on the `abr` storage started from the PVE web interface, a PVE backup job or the shell is refused.
The backup server keeps the catalog, the manifests and the keys, merges chains, applies retention and verifies, as for
the other platforms.

The appliance carries its own copy of the package and installs it with `apt` on every node that lacks it or has an
older one, when the cluster is set up and before each backup or restore on a node, so its dependencies come from the
node's Debian repositories. `ALTASTOR_PVE_PACKAGE_PATH` names the package; unset, the server takes the newest
`abr-pve-backup_<version>_<arch>.deb` in `tools/abr-pve-backup/`. With no package on the appliance, each node must have
it installed already.

## What it needs

| From | What | Why |
| --- | --- | --- |
| Every node | PVE 9, the PVE API on TCP 8006 and SSH on TCP 22 from the backup server | Inventory and tasks through the API; run files, secrets, package installs and the one-time setup over SSH, through the node helper |
| The cluster | Root's password, once, when the inventory is added or set up again | The setup creates AltaStor's own credentials; the password is not kept |
| Every node | Its Debian repositories, or a mirror | `apt` installs the package's dependencies: nbdkit, nbd-client, cifs-utils, libnbd0 |
| The repository | **S3** with an access key of its own, or an **SMB** repository, reachable from every node and from the backup server; a local repository the appliance shares over SMB works too | The nodes write and read the backups; the backup server keeps manifests, merges chains and applies retention. Both reach the same storage |
| The VM | A storage for VM disks on its node | vzdump keeps its fleecing images there while a backup runs |

An S3 repository needs an access key and secret key of its own: the nodes cannot use the appliance's AWS profile. An SMB
repository is mounted by the provider for each process, in a private mount namespace the host never sees, read-only
for restores, with the repository's account and the password passed through a pipe.

## Adding a cluster

```bash
ABR_INVENTORY_PASSWORD=... abr inventory add --name "PVE-Cluster" --type proxmox-ve --address pve1.example.com --username root@pam
```

An inventory is the cluster of the node at `--address`, or that node alone when it is not in a cluster. A cluster is
added once: adding it again under another name, or adding one of its nodes, is refused. The setup runs over SSH with
root's password, on the node given, and:

1. writes AltaStor's node helper to `/etc/pve/priv/abr-node-helper`, from the package, and installs AltaStor's own SSH
   key for root in `/etc/pve/priv/authorized_keys`, which PVE shares with every node, as
   `from="<backup server address>",restrict,command="/usr/bin/perl -T /etc/pve/priv/abr-node-helper"` and marked
   `altastor-inventory:<inventory id>`: the key runs the helper and nothing else;
2. pins every node's SSH host keys and the API certificates the cluster trusts, read from `/etc/pve`: its root CA and any
   node certificate of its own;
3. creates the role `AltaStorBackup`, the PVE user `abr@pve` with that role on `/`, and an API token
   `abr@pve!altastor-<id>` for this inventory;
4. installs the package on every node that is up, and adds the storage `abr` of type `abr` for backups.

The role holds what backups, restores and the inventory need: `Datastore.AllocateSpace`, `Datastore.Audit`, `SDN.Use`,
`Sys.Audit`, `VM.Allocate`, `VM.Audit`, `VM.Backup`, `VM.Config.Network`, `VM.Config.Options` and `VM.PowerMgmt`.
The setup can run again: `abr inventory edit --password` sets the cluster up anew with the same key, and says what it
changed. A first setup that fails takes back what it made.

`abr inventory rescan` reads the cluster's nodes and certificates over SSH from a node the inventory has pinned before
it lists the VMs: a node that joined the cluster gets its address and host keys, a node that left is dropped, and a
renewed API certificate replaces the old one. No password is needed for that, since `/etc/pve` is the cluster's own
file system. A node that joined is given the package before its first backup or restore. An inventory set up before
the node helper is moved onto it by the rescan: its key, which can still run any command, writes the helper and gives
itself the helper as its forced command.

## The node helper

AltaStor's SSH key runs one program on the nodes, `abr-node-helper` (from the abr-pve-backup package), as the key's
forced command: whatever the backup server asks for, sshd runs the helper, which does only what it knows and refuses
the rest. There is no shell, no SFTP and no forwarding with the key. The helper lives in `/etc/pve/priv`, next to the
shared `authorized_keys`, so every node has it, also before the package is installed and on nodes that join later.

| Command | What it does |
| --- | --- |
| `version`, `package-status` | The helper's version, the installed package's, and whether the package is current |
| `install-package` | Installs the abr-pve-backup package that comes on its input, with `apt`; any other package is refused |
| `put-run`, `get-result`, `remove-run` | Writes a run file to `/run/abr`, reads a result file, removes a run's files; only names of runs |
| `hand-secrets` | Keeps a run's secrets in memory and hands them once to the PVE task the run file names |
| `nbd-devices` | How many kernel NBD devices the node has and how many are in use |
| `cluster-info` | The cluster's members, SSH host keys and API certificates from `/etc/pve`, and the node's public host keys |
| `remove-token`, `remove-key` | Removes the inventory's API token and its own `authorized_keys` line |
| `update-helper` | Replaces the helper with a newer one, which must start and answer before it takes the old one's place |

**Secrets.** The S3 secret key, the share's password and the encryption key never go into the run file. The backup
server streams them over its SSH connection to `hand-secrets`, which keeps them in locked memory and offers them on a
socket in `/run/abr`, root only. It gives them to a process only when it is the run's PVE task: root, on this node,
named after a task of the run's kind, VM and API token, and asking for the run's ID. It hands them over once, and
withdraws the offer when the backup server closes its connection or the run file expires.

**Updates.** One copy serves the whole cluster. Before each run the backup server compares its package's helper with
the one in `/etc/pve` and replaces an older one; a command already running keeps its old code. Replacing it needs the
cluster's quorum, like every write to `/etc/pve`. The previous helper stays as `abr-node-helper.previous`. If the
helper cannot be used, setting the cluster up again with `abr inventory edit --password` writes it anew.

**Logging.** Every request the helper grants or refuses is logged on the node: `journalctl -t abr-node-helper`.

**Limits.** Root on a node can read anything there, the secrets of a running backup included, since the tools need
them. Installing a package runs its scripts as root, and the helper checks only that a package is abr-pve-backup for
the node's architecture, not who built it: whoever holds the backup server's key can still have code run as root
through a crafted package. The key stays limited to the backup server's address.

`abr inventory remove` takes the API token and the SSH key back from the first node that answers. The user, the role,
the package and the storage stay, for other inventories and appliances. When no node answers, the result names what to
remove by hand.

## How a backup runs

1. abr-core finds the VM's node and configuration through the API, checks the node has the package, and decides for
   each disk whether PVE should keep its dirty bitmap (an incremental) or start a new one (a full).
2. It writes the run file `/run/abr/backup-<vmid>.json` through the node helper, readable by root only and valid for
   `ALTASTOR_PVE_RUN_FILE_MINUTES`, has the helper offer the run's secrets to its task, and starts `vzdump` on the `abr`
   storage in snapshot mode with fleecing, on a storage for VM disks of the VM's own.
3. The provider backs up `ALTASTOR_PVE_PARALLEL_DISKS` disks at a time, each with `abr-nbd-tool` into the session's
   folder of the repository, and writes a result file abr-core reads at the end.
4. abr-core writes the manifest and registers the restore point; a failed or stopped run is discarded.

Before vzdump starts, abr-core checks the room on the fleecing storage. PVE reserves the full size of the VM's disks
for the fleecing images on thick LVM, and on ZFS unless the storage is sparse, so a backup needs that much free there.
On other storages the images grow only with what the guest overwrites during the backup: with less room than the VM's
disks a backup starts with a warning, and below `ALTASTOR_PVE_FLEECING_MIN_FREE_MIB` it does not start.

One vzdump runs at a time on a node: PVE holds a lock per node, so the backups of a job that land on one node wait for
each other. The task runs in PVE and does not depend on the backup server. Its UPID is kept with the session, so a
session the server lost by a restart follows the same task again when the server is back, and registers its backup.

Every disk of the VM is backed up except CD-ROM drives, empty drives and disks marked `backup=0`. A backup is of the
whole VM; disk-level backups are refused.

**Full and incremental.** The first backup is a full, the following ones incrementals, with active and synthetic full
backups on their own schedules, as for the other platforms. PVE keeps a disk's dirty bitmap only while the VM runs, so
one disk can be backed up in full within an incremental session:

- the TPM state is backed up in full every time, since PVE attaches it only for the backup;
- a disk whose bitmap PVE lost, after the VM was restarted, stopped or migrated, is backed up in full, with a warning;
  when every disk was, the session is a full with the reason `IncrementalFallbackToFull`;
- a disk added or resized since the previous backup starts a new bitmap.

Each disk's chain starts at its own newest full backup, so a synthetic full merges each disk from there, and a restore
reads each disk from there.

**Stopped VMs.** vzdump reads a stopped VM's disks in full every time. A stopped VM that has not run since its previous
backup gets a restore point without changes instead: an empty incremental of each disk, written by the backup server,
with no vzdump run. abr-core judges that the VM has not run when it was stopped at the previous backup and still is,
is on the same node, its configuration and firewall settings are unchanged, and the node's task history shows only
backups of it since. When any of these cannot be shown, the backup runs vzdump. `abr job edit --unchanged-stopped-vm full` turns
this off for a job.

**Bandwidth.** `abr job edit --bandwidth-limit <MiB/s>` caps how fast a backup reads, split evenly among the disks it
backs up at a time; 0 removes the limit.

A stop from AltaStor stops the PVE task, and the provider lets the running tools discard what they wrote before it ends.

## What a restore point holds

Per VM (`vm_config` in the manifest): the guest configuration PVE handed to the backup, with its `#qmdump#map` lines,
and the firewall configuration; the parsed configuration, the node, firmware, processors, memory and network adapters
(bridge, MAC address, VLAN, link state); the vzdump task's UPID, the configuration and firewall digests and the power
state the stopped-VM check compares.

Per disk: its configuration key (`scsi0`, `efidisk0`, `tpmstate0`), volume and storage, size, the bitmap asked for and
the one PVE granted, full or incremental, and where its backup is in the repository.

## Recovery

A Proxmox VE restore point is restored to a Proxmox VE inventory only. PVE restores the VM from the guest configuration
the backup recorded, through the provider: for each disk, `nbdkit-abr-plugin` serves the chain of backups that makes up
the disk at the restore point on `/dev/nbdN`, and PVE copies it to a new volume with `qemu-img`, on any storage type.
abr-core changes the guest configuration in the run file for the restore: the name of a new VM, the storage of each disk,
and the bridge and link state of each network adapter.

**As a new VM** (`abr restore start --new-name ... --target-host <node> --datastore <storage>`):

- the VM gets the cluster's next free VMID, new MAC addresses and a new SMBIOS uuid;
- `--datastore` names the storage for its disks, `--datastore-map` the storage for the disks of one source storage, and
  `--disk-datastore "scsi1=<storage>"` one disk by its configuration key, which wins over the mapping;
- `--network-map` moves the adapters of one source bridge to another, and `--nic-network "net1=<bridge>"` one adapter;
- the name must suit PVE: letters, digits and hyphens, in parts separated by dots, up to 63 characters.

Before anything is written the restore checks that the node is in the cluster and online, that it has a free kernel
NBD device (the package gives each node 64, and a restore holds one while PVE copies a disk), that every target storage
holds VM disks on that node and is active, and that every mapped bridge exists there. A restore that fails or is
stopped removes the VM it had created.

**In place** (`abr restore start --in-place --force`) restores over the VM itself, keeping its VMID, MAC addresses and
SMBIOS uuid, with each disk on the storage the VM keeps it on now. The VM is turned off first. It is refused while the
VM is protected or locked, and when the VM with the backup's VMID is no longer named as it was at the backup. PVE
removes the VM's disks before it writes the restored ones, so an in-place restore that fails leaves the VM without
them; the session says whether it did, and the VM is restored again from the same point.

`--power-on` starts the VM after the restore; a VM that does not start is restored all the same, with a warning. A
restore's task is followed by its UPID too, so a restore survives a restart of the backup server. Two restores of one
restore point on one node run one after the other. A restore is not limited in bandwidth: PVE applies its datacenter
restore limit to its own backups only.

## Settings

| Setting | Default | What it does |
| --- | --- | --- |
| `ALTASTOR_PVE_PACKAGE_PATH` | empty | The abr-pve-backup package the appliance installs on the nodes; empty takes the newest in `tools/abr-pve-backup/` |
| `ALTASTOR_PVE_PARALLEL_DISKS` | 4 | How many disks of one VM the provider backs up at a time |
| `ALTASTOR_PVE_RUN_FILE_MINUTES` | 60 | How long a run file is valid after abr-core writes it |
| `ALTASTOR_PVE_FLEECING_MIN_FREE_MIB` | 1024 | The least free space a thin fleecing storage needs for a backup to start |

## Monitoring and logging

Every backup and restore is a session in *Activity* (and `abr session list` / `abr session log`), with its progress and
log: the node and the package version, each disk with its bitmap and whether it was backed up in full or incrementally
and why, the PVE task's UPID and the lines of its log that matter, warnings and the cause of a failure. The PVE task log
on the node holds the provider's own lines, starting with one that names the run's ID. Credentials and encryption keys are never
written to either; on the node they never reach a file, and live only in the node helper's memory until the task takes
them, and then in the processes that use them.

## Troubleshooting

| Session says | Look at |
| --- | --- |
| `PVE VMs need an S3 or SMB repository` | Point the job at an S3 or SMB repository, or share the local repository over SMB (`abr repo share add`) |
| `S3 repository ... has no access key of its own` | Give the repository an access key and secret key; the nodes cannot use the appliance's AWS profile |
| `S3 bucket ... is not accessible at endpoint ...: curlCode: 28` | The network between that node and the S3 endpoint: the node gave up after several attempts to connect |
| `mounting //server/share failed` | The share's account and password, and that every node reaches the share on TCP 445 |
| `does not have the abr-pve-backup package, and this appliance has none` | Put the package on the appliance (`ALTASTOR_PVE_PACKAGE_PATH`), or install it on the node |
| `apt could not install abr-pve-backup` | The node's Debian repositories, which provide nbdkit, nbd-client and cifs-utils |
| `Node ... is not one AltaStor knows in this cluster` | Run `abr inventory rescan`, which reads the nodes that joined from the cluster |
| `The TLS certificate of the PVE API ... is not the one AltaStor trusts` | Run `abr inventory rescan`, which reads renewed certificates over SSH |
| `The PVE API refused AltaStor's API token` | The token was removed or replaced in PVE: set the cluster up again with `abr inventory edit --password` |
| `... has no AltaStor node helper in /etc/pve/priv/abr-node-helper` | The cluster was set up before the backup server used the helper: run `abr inventory rescan`, or set the cluster up again with `abr inventory edit --password` |
| `The node helper on ... could not keep the secrets of ...` | See `journalctl -t abr-node-helper` on that node; `/run/abr` must be writable, and the run file must be there |
| `presented SSH host key ..., which is not one of its own host keys` | The node's SSH host key changed outside the cluster's records; check the node, then set the cluster up again |
| `PVE no longer had the dirty bitmap of ...` | Expected after the VM was restarted, stopped or migrated: those disks were backed up in full |
| `Fleecing storage ... has ... free` | Free room on that storage, or give the job another one |
| `All ... kernel NBD devices of node ... are in use` | Too many restores at once on that node, or other NBD users; run the restore when one has ended |
| `cannot name a Proxmox VE VM` | Choose a name of letters, digits and hyphens, in parts separated by dots |
| `Storage ... does not hold VM disks on node ...` | Restore to a storage that holds VM disks on that node and is enabled there |
| `would land on bridge ..., which node ... does not have` | Map the adapter to a bridge of the target node with `--network-map` or `--nic-network` |
| `In-place recovery replaces VM ..., which is now named ...` | The VMID belongs to another VM now; restore as a new VM, or rename the VM back if it is the same one |
| `PVE had removed the disks of ... before the restore failed` | Restore the VM again from the same restore point once the cause is fixed |
| `start backups of this storage from AltaStor`, `AltaStor restore points are restored from AltaStor` (PVE task log) | The backup or restore was started in PVE; start it from AltaStor |
