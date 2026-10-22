# Hyper-V backup and recovery

How AltaStor backs up and restores Microsoft Hyper-V virtual machines, what it
needs from the Hyper-V side, and what to expect when something goes wrong.

## The data mover runs on the host

VMware disks are moved by abr-vmdk-tool on the backup server, which reaches
them through VDDK. Hyper-V disks are moved by **abr-vhdx-tool**
(the `abr-vhdx-tools` repository), its Windows counterpart: the same commands,
options and `--json` results, and the same `.abr` repository format. It runs on
the Hyper-V host itself, where it opens a checkpoint's disk through the Windows
virtual disk service and reads exactly the sectors the guest sees -- VHD, VHDX
and differencing chains alike. The backup server starts it there over WinRM,
one disk at a time, and follows its progress.

At the start of each backup or restore the appliance makes sure the host holds
its copy of the tool and of its PowerShell scripts, in a folder named by their
SHA-256 under `ALTASTOR_HYPERV_WORK_DIR` (by default
`C:\ProgramData\AltaStor\abr-hyperv`). When that folder is there with every
file intact, nothing is uploaded; otherwise the files go to a staging folder,
are checked, and are moved into place. Another build of the appliance gets a
folder of its own, so several appliances can share a host without replacing
each other's files. For the rest of the run the scripts are read from that
folder instead of being sent with every command. `ALTASTOR_VHDX_TOOL_PATH` names
the appliance's copy of `abr-vhdx-tool.exe` (by default
`tools/abr-vhdx-tool/abr-vhdx-tool.exe` under the install root). With no copy on
the appliance, each host must have the tool installed at
`ALTASTOR_HYPERV_TOOL_PATH` (by default `C:\Program Files\AltaStor\abr-vhdx-tool.exe`).

## What it needs

| From | What | Why |
| --- | --- | --- |
| Every Hyper-V host (every node of a cluster) | WinRM over HTTPS, TCP 5986 (or HTTP, TCP 5985, when the inventory is set to HTTP) | Inventory, checkpoints, VM configuration, running abr-vhdx-tool, building and changing VMs on restore |
| The registered account | Local administrator on every node | Hyper-V WMI, and the elevated token abr-vhdx-tool needs to attach virtual disks |
| The repository | **S3** (or S3-compatible), reachable from every host and from the backup server; or an **SMB** repository, a share every host reaches and the appliance mounts (see below) | The host writes and reads the disks; the server keeps the manifests, merges chains, applies retention and verifies. Both have to reach the same storage |
| The VM | Configuration version 8.0 or later for incremental backups | Resilient change tracking (RCT) starts with version 8.0; older VMs get a full backup every time |

### WinRM: HTTPS by default

A Hyper-V inventory connects over WinRM HTTPS (port 5986) unless it is set to
HTTP (`--protocol http`, port 5985; *Protocol* in the web UI). `--port` moves
the port and keeps the protocol. The server never falls back from HTTPS to
HTTP: when a host has no HTTPS listener, or offers a certificate the server
does not trust, adding the inventory is refused and a backup or restore fails
with that reason. A self-signed certificate is accepted with `--insecure`
(*Skip TLS verification*). Every node of a cluster is reached the same way.

An inventory registered before the setting existed is checked once: the first
connection tries HTTPS, then HTTP, and the protocol that answered is saved.
From then on it is used strictly; an inventory that settled on HTTP logs a
warning to set up HTTPS and switch it with `abr inventory edit --protocol https`.

A WinRM HTTPS listener with a self-signed certificate, on each node:

```powershell
$cert = New-SelfSignedCertificate -DnsName $env:COMPUTERNAME -CertStoreLocation Cert:\LocalMachine\My
New-Item -Path WSMan:\localhost\Listener -Transport HTTPS -Address * -CertificateThumbPrint $cert.Thumbprint -Force
New-NetFirewallRule -DisplayName 'WinRM HTTPS' -Direction Inbound -Protocol TCP -LocalPort 5986 -Action Allow
```

### An SMB repository

A local repository is a folder of the appliance, which Windows cannot see. For
Hyper-V, use an SMB repository: a share every Hyper-V host reaches, which the
appliance mounts as a folder of its own (Samba on the appliance sharing that
folder, or a NAS share the appliance mounts):

```bash
abr repo add --name "HV-SMB" --type smb --path /mnt/hv --address '\\backup01\hv' --username 'CORP\backupsvc' --password
```

`--address` (or `--server` and `--share`) names `\\backup01\hv`, which must be
the same storage as `--path` (the share may go on into a folder of it,
`\\backup01\hv\altastor`). abr-vhdx-tool then writes each backup to that folder
through the share, and the appliance reads it at `--path`. The appliance does
not mount the share itself; `--path` must already be mounted.
Local repositories that named a share before SMB repositories existed become
SMB repositories when the server starts. A WinRM session has no network credentials
of its own, so the host opens the share for each run with the repository's
`--username` and `--password`, or the inventory's account when it has none.
`--password` without a value reads `ABR_REPO_PASSWORD` or prompts for it, so the
password never shows on the command line.

The appliance creates every backup folder itself, owned by the user its server
runs as and writable by nobody else (mode 0750), so the share must write as
that user (Samba's `force user`), and every node needs TCP 445 to it.

The encryption keys and the S3 secret key reach the tool on its standard
input (`--stdin-keys`), never on its command line, where process-creation
auditing (event 4688) would keep them. They travel to the host as PSRP secure
strings, parameters of the remote command rather than script text, so they
also stay out of PowerShell's script block logging. The S3 access key, an
identifier rather than a secret, stays on the command line.

Supported disks are VHDX, VHD and differencing disks (a VM with checkpoints of
its own). Pass-through physical disks and shared VHD Sets (`.vhds`) are skipped
with a warning in the session. A VM with no supported disk fails.

## How a backup runs

It mirrors the VMware backup step for step:

| VMware | Hyper-V |
| --- | --- |
| ESXi host connected? | VM state backup-ready? (running, off, saved or paused; not mid-transition, not critical, not merging disks or migrating) |
| Quiesced snapshot | Recovery checkpoint (snapshot type 32768), application-consistent through the guest's VSS, crash-consistent as a fallback |
| abr-vmdk-tool reads the snapshot disk through VDDK, CBT says what changed | abr-vhdx-tool on the host reads the checkpoint's disk, RCT says what changed |
| Remove the snapshot | Turn the checkpoint into a reference point |

1. Any recovery checkpoint an interrupted backup of the same VM left behind is
   removed first. The same cleanup runs when the server starts.
2. abr-vhdx-tool on the node that owns the VM checks that it can reach the
   repository (`info` on the run's folder, with the same storage options and
   credentials the backup uses). A node that cannot reach it fails the run
   with the tool's reason before the VM is touched: no checkpoint is taken and
   the previous backup's reference point stays.
3. The checkpoint freezes the VM's disks: from then on the VM writes to new
   differencing disks, and the files the backup reads no longer change. Taking
   it also turns RCT on for the VM's disks.
4. Each disk is backed up by abr-vhdx-tool on the node that owns the VM:
   `backup --vhdx <checkpoint disk>` for a full backup, with
   `--type incremental --change-id <RCT id>` for an incremental, against the
   RCT id the previous backup recorded for that disk. The tool reports the
   disk's new RCT id, which the manifest keeps for the next incremental.
5. The checkpoint is turned into a **reference point**: Hyper-V merges it back
   into the VM's disks and keeps the change tracking position it marked. The
   reference point of the job's previous backup is then removed.

A job's runs follow the usual schedule rules: the first backup is full, the
following ones incremental, with active and synthetic full backups on their
own schedules or on demand. A synthetic full merges the chain on the backup
server (abr-vmdk-tool `copy`) and keeps the newest RCT ids, so the next
incremental carries on from it.

An incremental becomes a full backup, with a warning in the session and the
reason `IncrementalFallbackToFull`, when:

- a disk has no RCT id from the previous backup (a disk added since, a VHD disk,
  a VM of configuration version below 8.0);
- RCT no longer knows the previous id (a disk that was moved, compacted, merged
  by hand or replaced). The tool refuses such an incremental before writing
  anything; whatever this run had already written is discarded and the run
  starts over as a full.

Whatever happens, the checkpoint does not outlive the run, and a failed or
stopped run leaves no disk image in the repository. A failed run keeps the
previous backup's reference point, so the next incremental still has its
position. If the checkpoint cannot be turned into a reference point, it is
removed instead, and the next backup is a full one.

## What a restore point holds

Per VM (`vm_config` in the manifest): generation, configuration version,
processor count, startup/dynamic memory, firmware (BIOS/UEFI, Secure Boot and
its template, boot order), TPM, network adapters (switch, MAC, VLAN), the
controller slot of every hard disk and DVD drive, automatic start/stop actions
and notes; the reference point the backup left.

Per disk: controller slot, the base disk path and identifier, virtual size,
logical and physical sector size, block size, VHDX type, the RCT id, and where
its backup is in the repository.

## Recovery

A Hyper-V restore point is restored to a Hyper-V inventory only. Its disks are
written by abr-vhdx-tool on the target node, straight from the repository:
`restore --vhdx` creates a dynamic VHDX from the full backup, then applies each
incremental up to the restore point onto it (`--force`), in order. A disk
asked for as fixed is converted on the host afterwards.

**Out of place** (`abr restore start --new-name ... --target-host ... --datastore ...`,
or *New location* in the console's restore wizard) builds a new VM on the chosen
host: the disks go under `<volume>\Hyper-V\<new name>\Virtual Hard Disks`
(at the root of a Cluster Shared Volume or share), the VM gets the backup's
generation, processors, memory, Secure Boot settings and adapters, each adapter
on the virtual switch the network mapping names, and boots from the disk it
booted from. `--datastore-map` places the disks of each source storage location,
and `--disk-datastore "SCSI0:1=E:"` one disk by its slot, which wins over the
mapping of its location; `--network-map` connects the adapters of each source
switch, and `--nic-network "nic2=Isolated"` one adapter by its name, so two
adapters on one switch can land on different ones. The source VM is not touched. Before anything is written the
restore checks, on the host itself, that every target storage location exists
there and has room, that every mapped virtual switch exists, and that the name
is free. A VM that had a virtual TPM gets a new one, so secrets sealed to the
old TPM (BitLocker keys, for one) need their recovery keys; the session warns
about it.

**In place** (`abr restore start --in-place --force`, or *Original location*
with the typed confirmation in the console) keeps the VM itself -- its id,
network adapters and cluster role -- and replaces its disks. The restored disks
are written next to the current ones, then the VM is turned off, set back to the
processor and memory the backup recorded, pointed at the restored files, and only
then are the replaced files deleted. Its network adapters go back to the virtual
switches and access VLANs the backup recorded, and the session says which ones
moved; a switch the backup recorded that the host no longer has refuses the
restore before anything changes. A VM with checkpoints is refused: replacing
disks under a checkpoint chain would break it. Its next backup is a full one,
since the new disks start their change tracking over.

A failure removes what the restore created and, in place, points the VM back at
its original disks.

## Monitoring and logging

Every backup and restore is a session in *Activity* (and `abr session list` /
`abr session log`), with its progress, statistics and log: what was backed up
or restored, from and to where (host, VM, repository, storage, mappings), full
or incremental and why, the checkpoint and reference point, every disk with
its size and RCT id, the checks a restore made and their outcome, warnings and
the cause of a failure. The server log (`python.srv.*` loggers) carries the same
events plus connection and authentication outcomes, discovery results, every
abr-vhdx-tool run on a host (its command line with keys masked, its exit code
and duration), storage operations (bucket and path, never keys) and Object
Lock validation. Credentials and encryption keys are never written to either.

## Troubleshooting

| Session says | Look at |
| --- | --- |
| `reaches S3 and SMB repositories only` | Point the job at an S3 or SMB repository (`abr repo add --type smb`) |
| `cannot reach repository` | The reason that follows it: the S3 endpoint, keys and bucket the repository names, or its share, as seen from that node |
| `cannot open the repository share` | The share's account (the repository's `--username`, else the inventory's), and that every node reaches the share on TCP 445 |
| `did not answer WinRM over HTTPS on port ...` | No HTTPS listener on that port of the host: set one up, or set the inventory to HTTP (`abr inventory edit --protocol http`) |
| `the port answers without TLS` | The port is an HTTP listener: give the HTTPS listener's port, or set the inventory to HTTP |
| `The TLS certificate of Hyper-V host ... was not trusted` | Give the host a certificate the server trusts, or accept it with `--insecure` (*Skip TLS verification*) |
| `abr-vhdx-tool is not installed on ...` | Set `ALTASTOR_VHDX_TOOL_PATH` on the appliance, or install the tool on the host at `ALTASTOR_HYPERV_TOOL_PATH` |
| `needs an elevated (administrator) process` | The registered account is not a local administrator of that node |
| `is in the OffCritical state` | The host lost the VM's storage or configuration |
| `is busy (MergingDisks)` | A checkpoint merge from an earlier operation is still running; retry later |
| `Application-consistent checkpoint ... failed` | The guest's *Backup (volume shadow copy)* integration service or VSS writers |
| `no longer knows the previous backup's position` | Expected after a disk was moved or replaced; the run took a full backup |
| `Virtual switch(es) ... do not exist on` | Map the adapters to switches the target host has; in place, recreate the switch the VM was connected to, or restore out of place |
| `did not list its VMs within 90s` (server log) | That node's Hyper-V management service or cluster storage; the node is shown as unavailable and the rest of the cluster goes on |
| `has N checkpoint(s); replacing its disks would break` | Delete the VM's checkpoints, or restore out of place |
