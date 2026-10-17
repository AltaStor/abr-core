# Changelog

All notable changes to this project are documented in this file.

## v26.09

### Added

- Support for backup and restore of VMware vCenter and ESXi 8–9+ VMs.
- Support for local file system and S3 backends for backup and restore
  workflows.
- CRC32C integrity validation for headers, index blocks and data blocks.
- zstd compression of backup data blocks, with configurable levels from 1 to 9.
- Data block encryption, improving protection for stored backup content.
- Administrators can install their own HTTPS certificate and private key
  (CA-issued or self-signed, optionally passphrase-protected) through
  `PUT /api/v1/system/tls-certificate` or the web console. It is validated
  first and served without a restart; `GET` describes the current certificate
  and `DELETE` reverts to a generated self-signed one.
- Jobs run three independent schedules: base incrementals, active full and
  synthetic full. When several fall due at once, active full wins over
  synthetic full and synthetic full over the incremental. Every job keeps at
  least one full schedule, so retention can always free the chain. One detail
  of a full schedule (e.g. `--active-full-time`) can be changed without
  restating the rest.
- Restore placement beyond a single target: `restore start --datastore-map
  <src=dst>` and `--network-map` remap by source datastore and network, and
  `--disk-datastore` and `--nic-network` place a single disk or network
  adapter. Precedence is disk, then source datastore, then whole VM, and free
  space is checked per target against only the disks that land on it.
- `restore context` lists the source datastores, networks, disks and adapters
  a restore point carries, with the target each falls back to.
- Restore points carry their encryption key, so a chain can be verified or
  restored after its job is gone, and every key a job has used is kept, so key
  rotation no longer orphans older restore points. `backup verify` and
  `restore start` take `--encryption-key` to override it. Turning encryption
  on requires an operator key.
- CLI secrets can come from an environment variable (`ABR_PASSWORD`,
  `ABR_INVENTORY_PASSWORD`, `ABR_REPO_SECRET_KEY`, `ABR_ENCRYPTION_KEY`, ...)
  or an interactive prompt instead of only a command-line flag.
- `--immutability-retention <job|minimum>` chooses whether the Object Lock
  period runs to the end of the job's full-backup cycle or is only
  `--immutability-days` counted from each backup.
- A job reports how its last run went across all of its VMs, and its soonest
  next run across all of its schedules.
- `abr job retention [--dry-run]` and `POST /api/v1/jobs/{id}/retention`
  apply (or preview) a job's retention across all of its chains and report,
  per chain, what was removed, what Object Lock still holds and what could not
  be removed.
- Hyper-V backup and recovery: full and incremental (resilient change
  tracking) backups of Hyper-V VMs, standalone or clustered, from
  application-consistent recovery checkpoints with a crash-consistent
  fallback, kept as reference points between backups; synthetic fulls and
  retention as for VMware; in-place and out-of-place recovery with target
  host, storage and virtual switch mapping. Disks are moved by abr-vhdx-tool
  (the `abr-vhdx-tools` repository) on the Hyper-V host, which the appliance
  deploys there (`ALTASTOR_VHDX_TOOL_PATH`, `ALTASTOR_HYPERV_TOOL_PATH`);
  Hyper-V jobs need an S3 repository, or a local one shared to the hosts over
  SMB (`abr repo edit --server --share`). See `doc/HyperVBackup.md`.
- Hyper-V job option `--guest-consistency`.

### Enhanced

- Retention no longer tries to delete a restore point still under S3 Object
  Lock: it is reported as held, with the older points it depends on, instead of
  as a failure.
- A synthetic full records the hypervisor kind of the chain it merged instead
  of always reporting vCenter.
- A restore point is only offered, and only accepted, for restore to an
  inventory of the platform it came from.
- Logging: storage operations (with their bucket and path, never keys), Object
  Lock validation and application, Hyper-V connection, authentication and
  discovery outcomes, per-chain retention results, and a summary of every
  queued backup and restore session.
- A Hyper-V backup checks from the host that it can reach the repository
  before it takes a checkpoint, so an unreachable S3 endpoint, refused keys or
  a share the host cannot open fail the run without touching the VM. A failed
  abr-vhdx-tool run on the host reports the tool's own reason, not only its
  last line.
- Improved backup and restore reliability with corrected backup and restore
  logic and better handling of storage backends.
- Expanded CLI and web console capabilities, including support for S3-based
  operations and more polished backup management workflows.
- Better job and VM management with VM tree selection, dynamic progress
  reporting, repository search and delete actions.
- Operational improvements such as automatic rescan, cluster validation,
  serialized same-VM backups and CBT enablement for full backups.
- Usability and stability refinements, including cleaner JSON output,
  improved deletion handling and stricter input validation in the web console.
- Updated VDDK integration with support for the newer 9.1 toolchain.
- Repositories and inventories are validated against real storage and
  endpoints before they are registered; nothing is created when a check fails.
  `repo edit` refuses S3 credentials that cannot set Object Lock on an
  immutable repository, and changing only the Object Lock period no longer
  round-trips to the bucket.
- An in-place restore keeps the VM's identity (SMBIOS UUID, vSphere
  instanceUuid), so licensing, tags, monitoring and existing backup chains
  keep matching it.
- A restored disk keeps its source provisioning: thin stays thin and
  eager-zeroed thick stays eager-zeroed thick.
- A restore resolves its source from the catalog, so a deleted VM can still be
  restored, and runs against the inventory named by `--target-inventory`.
- Backups are stored in folders named by job and VM ID, so a renamed VM keeps
  one chain. Existing jobs move on their next full backup.
- A VM's independent disks are skipped with a warning instead of failing the
  whole backup.
- A synthetic full reports what it merged and which restore point it created,
  and is refused up front when the chain cannot supply it.
- `repo verify` reads every restore point of each chain, not only the newest.
- Clearer failures: how much space a datastore is short, a repository running
  out of space during a backup or synthetic full, a VM powered off, deleted,
  unregistered or moved while it was backed up, and an ESXi data path that
  went silent.
- The session log says which network each adapter was restored on, and when
  one could not be attached.
- Restore points dated ahead of the server clock are reported together with
  the points that chain onto them; a job's last run ahead of the clock is
  flagged in `job list` and `job info`.
- An inventory rescan skips a VM deleted from vCenter mid-read instead of
  failing, and vCenter objects are resolved in one call instead of one per
  object.
- The dashboard summary reports real times and recent backup size, and labels
  sessions by type.

### Changed

- Split the product into separate repositories. This one now holds the Python
  core only: the BackupServer API (`python/srv`) and the `abr` CLI
  (`python/cli`).
- `scripts/check_deps.sh` now prepares only what the Python core needs: the
  virtualenv, the Python requirements and the local MongoDB bundle.
- `scripts/clear.sh` now removes what `check_deps.sh` actually creates (the
  virtualenv and the local MongoDB bundle) instead of MongoDB binaries in
  `/usr/bin` that were never installed there.
- `scripts/run_srv.sh` starts the server through
  `python -m python.srv.backup_server`, the same entry point the systemd unit
  uses, so it applies `defaults.env` and the pinned TLS cipher list rather
  than invoking `uvicorn` directly. It also defaults `MONGODB_URI` to the
  local instance, so a development run persists instead of silently running
  in memory.
- Point the server at a data mover built elsewhere with
  `ALTASTOR_DISK_TOOL_PATH`, and at a web console bundle built elsewhere with
  `ALTASTOR_WEB_ROOT`.
- The TLS renewal check only renews or regenerates the self-signed
  certificate the server created. It used to replace any certificate whose
  addresses didn't match the host's own (which is every CA-issued one) at
  startup and on the daily check.
- A certificate reload into the running server now fully switches over even
  when the key type changes (RSA to EC or back); before, the previous
  certificate stayed on offer to clients that preferred its key type.
- TLS certificates a browser would refuse are rejected at install time.
- Object Lock: backups in one chain share one expiry date per block
  generation, derived from the job's backup cycle and retention.
  `--immutability-days` is the minimum, not an addend, and a new generation
  starts once the remaining lock drops below the promised retention.
  Retention up to 36500 days is accepted.
- Retention keeps a restore point its Object Lock still holds without errors
  or warnings, and removes it on the first pass after the lock expires.
- Inventory credentials are stored encrypted and session tokens as digests.
  Secret settings are masked in the catalog-restore response.
- User, password and job input is validated strictly.
- A queued backup decides its type (full or incremental) when it starts, not
  when it was queued.
- API responses carry `Cache-Control: no-store`, and the published OpenAPI
  document matches the real request and response shapes.
- `system version` reports the version of the installed server package.

### Fixed

- A backup captured the VM's base disk instead of the disk of its own
  snapshot.
- An incremental could chain onto another job's backup of the same VM; it now
  continues from its own job's previous restore point.
- A synthetic full did not carry the change ID of the newest backup it
  merged, forcing the next incremental to run as a full.
- A finished backup is flushed to stable storage before it completes, so a
  power loss right after cannot leave a restore point with empty files.
- Network adapters on the same source network, or on different port groups of
  one distributed switch, were all restored to one target network. Backups and
  inventory scans now record each port group's own name, and every adapter
  keeps its own target.
- Cleanup after interrupted runs:
  - tasks left running when the server stopped are marked failed at startup,
    and their stranded output and VM snapshots are removed;
  - a restore stopped at the start destroys its half-made VM;
  - a failed restore destroys only the VM it created, never one matching by
    name;
  - a failed or cancelled synthetic full discards the backup it was writing;
  - the data mover's relocation block on a VM is lifted when a backup ends
    abnormally.
- Schedules:
  - a day-list full schedule reports when it actually fires and no longer
    invents a time zone;
  - changing a weekday list replaces it instead of merging;
  - `--days` on a schedule that takes no weekdays is refused;
  - full schedule state flags are honoured on job create;
  - a next run pushed into the future by a wrong clock, or into the past by
    the repeated DST hour, is corrected.
- A session stopped while still queued gets an end time and a reason.
- A deletion counts as done when the data is already gone from storage.
- A catalog import onto a fresh install succeeds, all or nothing.
- A failed Object Lock extension is recorded as a warning instead of crashing.
- A source with nothing allocated is counted as zero instead of guessed at.
- An unknown `--log-level` and `auth login` without a terminal give a usage
  error instead of a traceback.
- The API no longer logs a schema warning on every response.
