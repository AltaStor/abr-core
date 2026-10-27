# Changelog

All notable changes to this project are documented in this file.

## v26.11-pre (Unreleased)

Includes every fix shipped in v26.09.1 and v26.09.2.

### Added

- Role-based access control. Every user holds one of five built-in roles
  (Administrator, Backup Administrator, Backup Operator, Restore Operator,
  Auditor), and every API operation and `abr` command runs one product function
  (`job.start`, `backup.delete`, ...) that the server checks against the
  user's role before it runs, for the Web UI, the CLI, `--output json`
  automation and the API alike. A denied request answers `403
  permission_denied` with the function ID and role, and the CLI exits with code
  7. The last enabled Administrator cannot be demoted, disabled or deleted.
  `abr user edit`, `abr user info`, `abr role list|info|functions`,
  `abr auth whoami`, `PATCH /api/v1/users/{id}`, `GET /api/v1/roles`,
  `GET /api/v1/functions` and `GET /api/v1/auth/me`. See `doc/RBAC.md`.
- Custom roles. An Administrator creates, clones (from a built-in or custom
  role), changes and deletes roles that allow an explicit list of functions,
  and gives them to users like the built-in ones; a change applies to the
  role's users on their next request. Nobody can grant, assign or take over
  more than their own role allows, nor change their own role, a built-in role
  can't be changed and a role users hold can't be deleted. `abr role
  create|clone|edit|delete`, `POST /api/v1/roles`, `PATCH|DELETE
  /api/v1/roles/{id}`, functions `role.create`, `role.edit`, `role.delete`.
- An audit log of every command and every user or role change, from every
  interface: time (UTC), user, the role they held, function, target, result
  (allowed, denied or failed) and the session the command started, with
  credentials masked. `abr audit list` and `GET /api/v1/audit`.
- Hyper-V backup and recovery: full and incremental (resilient change
  tracking) backups of Hyper-V VMs, standalone or clustered, from
  application-consistent recovery checkpoints with a crash-consistent
  fallback, kept as reference points between backups; synthetic fulls and
  retention as for VMware; in-place and out-of-place recovery with target
  host, storage and virtual switch mapping. Disks are moved by abr-vhdx-tool
  (the `abr-vhdx-tools` repository) on the Hyper-V host, which the appliance
  deploys there (`ALTASTOR_VHDX_TOOL_PATH`, `ALTASTOR_HYPERV_TOOL_PATH`);
  Hyper-V jobs need an S3 or SMB repository. See `doc/HyperVBackup.md`.
- SMB repositories (`abr repo add --type smb --path --address`): a share
  Hyper-V hosts reach, which the appliance mounts at `--path`, with the account
  the hosts open it with. They take over the SMB share local repositories used
  to carry; such local repositories become SMB repositories when the server
  starts or a catalog holding them is imported, and a local repository no
  longer takes `--server` or `--share`.
- `abr user edit --new-name` and `--reset-password`, and `name` and `password`
  in `PATCH /api/v1/users/{id}`: an Administrator renames a user, who keeps
  their password, or gives them a new password, which ends their sessions.
- Hyper-V job option `--guest-consistency`.
- `abr job retention [--dry-run]` and `POST /api/v1/jobs/{id}/retention`
  apply (or preview) a job's retention across all of its chains and report,
  per chain, what was removed, what Object Lock still holds and what could not
  be removed.
- Per-disk and per-adapter restore placement: `restore start
  --disk-datastore` and `--nic-network` place a single disk or network
  adapter, which `--datastore-map` and `--network-map` cannot express when
  everything shares one source. Precedence is disk, then source datastore,
  then whole VM, and free space is checked against where each disk actually
  lands.
- `restore context` also lists disks and network adapters, so the exact names
  the mapping flags take are discoverable.
- CLI secrets can come from an environment variable (`ABR_PASSWORD`,
  `ABR_INVENTORY_PASSWORD`, `ABR_REPO_SECRET_KEY`, `ABR_ENCRYPTION_KEY`, ...)
  or an interactive prompt instead of only a command-line flag.
- A job reports how its last run went across all of its VMs, and its soonest
  next run across all of its schedules; the incremental schedule keeps its own
  next-run field.

### Enhanced

- Retention no longer tries to delete a restore point still under S3 Object
  Lock: it is reported as held, with the older points it depends on, instead
  of as a failure. Deleting a chain goes newest point first and stops at the
  first point the storage keeps, so an older point it depends on is not
  removed from under it, and restore points report when their Object Lock
  ends.
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
- One detail of a full backup schedule (e.g. `--active-full-time`) can be
  changed on its own, keeping the stored type and the rest of its fields.
- A restore runs against the inventory named by `--target-inventory`, not the
  one the restore point came from.
- `--network-map` also applies to the adapters `--nic-network` leaves out.
- A VM's independent disks are skipped with a warning instead of failing the
  whole backup.
- `repo verify` reads every restore point of each chain, not only the newest.
- `repo edit` refuses S3 credentials that cannot set Object Lock on an
  immutable repository.
- Clearer failures: a repository running out of space during a backup or
  synthetic full, a VM powered off, deleted, unregistered or moved in vCenter
  while it was backed up, and an ESXi data path that went silent (the backup
  is stopped and says so).
- Restore points dated ahead of the server clock are reported together with
  the points that chain onto them; a job's last run ahead of the clock is
  logged once, marked in `job list` and shown as a warning in `job info`.
- An inventory rescan skips a VM deleted from vCenter mid-read instead of
  failing the whole rescan.
- The dashboard summary reports real times and recent backup size, and labels
  sessions by type.

### Changed

- `abr user add` and `POST /api/v1/users` need a role (`--role`). Users from
  before roles existed become Administrators when the server starts, which is
  what they could do before.
- Inventory credentials are stored encrypted and session tokens as digests.
- Secret settings are masked in the catalog-restore response.
- User, password and job input is validated strictly.
- Object Lock retention up to 36500 days is accepted, as the console promises.
- Network mappings are refused on in-place restore, and an unknown source
  adapter or network is reported as an invalid request.
- `system version` reports the version of the installed server package
  instead of a constant baked into the source.

### Fixed

- A job with a pending session can no longer be deleted: the refusal now
  counts the same queued, running and stopping sessions as the job's
  `active_session_count`.
- A server log line keeps its ID from one `GET /api/v1/system/logs` to the
  next once the log is larger than the 512 KiB read window, so a client can
  tell the lines it has from new ones.

- Cleanup after interrupted runs:
  - a backup killed with the server leaves no snapshot on the VM; the
    stranded one is removed when the server comes back;
  - a restore stopped at the very start destroys its half-made VM;
  - a failed restore destroys only the VM it created, never one merely
    matching by name;
  - the data mover's relocation block on a VM is lifted when a backup ends
    abnormally, with retries.
- Schedules:
  - changing a full backup schedule replaces its weekday list instead of
    merging into the old one;
  - `--days` on a schedule that takes no weekdays is refused instead of
    silently doing nothing;
  - full backup schedule state flags are honoured on job create, and a job's
    switch and its schedule stay in step on edit;
  - a next run pushed into the future by a wrong clock, or into the past by
    the repeated DST hour, is corrected.
- A session stopped while still queued gets an end time and a reason, shown
  for earlier ones too.
- A deletion counts as done when the data is already gone from storage,
  instead of failing on an artifact that no longer exists.
- A catalog import onto a fresh install succeeds, all or nothing.
- `--network-map` sources are compared exactly, the way the map is applied.
- No warning that a VM stays blocked from relocating when it is vCenter that
  holds the block.
- An unknown `--log-level` and `auth login` without a terminal give a usage
  error instead of a traceback.
- The API no longer logs a schema warning on every response.

## v26.09.2 (2026-10-15)

### Changed

- Backups are stored in folders named by job and VM ID, so a renamed VM keeps
  one chain and a synthetic full can still merge it. Existing jobs move on
  their next full backup, and a VM's previous backup is found by identity,
  not by name.
- A queued backup decides its type when it starts, not when it was queued.
  The next run of a damaged chain is an active full, and a manual synthetic
  full of a damaged chain is refused.

### Fixed

- A full backup of a VM that already had a snapshot read the VM's base disk
  instead of the disk of its own snapshot.
- An incremental could chain onto another job's backup of the same VM; it now
  continues from its own job's previous restore point, and refuses rather
  than take another job's backup when its own parent is gone.
- A backup or synthetic full is flushed to stable storage before it
  completes, so a power loss right after cannot leave a restore point with
  empty files. One that cannot be flushed fails and keeps nothing.
- A failed Object Lock extension is recorded as a session warning instead of
  crashing, and the retention pass still runs after it.
- Retention keeps a restore point its Object Lock still holds without errors
  or warnings, and removes it on the first pass after the lock expires.

## v26.09.1 (2026-10-02)

### Fixed

- A synthetic full did not carry the change ID of the newest backup it
  merged, forcing the next incremental to run as a full.
- A restored disk keeps its source provisioning: thin stays thin, and an
  eager-zeroed thick disk is recreated eager-zeroed instead of collapsing to
  lazy-zeroed thick.
- A synthetic full that fails or is cancelled also discards the disk it was
  merging at that moment, not just the ones already finished.
- Tasks left running when the server stopped are marked failed at startup,
  and the output they stranded is removed unless a restore point references
  it.

## v26.09 (2026-09-28)

### Added

- Support for backup and restore of VMware vCenter and ESXi 8–9+ VMs.
- Support for local file system and S3 backends for backup and restore
  workflows.
- CRC32C integrity validation for headers, index blocks and data blocks.
- zstd compression of backup data blocks, with configurable levels from 1 to 9.
- Data block encryption, improving protection for stored backup content.
- S3 Object Lock immutability for backup repositories, managed from the CLI.
  `--immutability-retention <job|minimum>` chooses whether the lock period
  runs to the end of the job's full-backup cycle or is only
  `--immutability-days` counted from each backup.
- Synthetic full backups: `job start --synthetic` builds a full by merging
  the chain already in the repository, and `--target-repo` puts the merged
  full in another repository.
- Jobs run three independent schedules: base incrementals, active full and
  synthetic full. When several fall due at once, active full wins over
  synthetic full and synthetic full over the incremental. Every job keeps at
  least one full schedule, so retention can always free the chain.
- HTTPS is served on port 443, with a plain-HTTP listener on port 80 that
  redirects browsers to it.
- Administrators can install their own HTTPS certificate and private key
  (CA-issued or self-signed, optionally passphrase-protected) through
  `PUT /api/v1/system/tls-certificate` or the web console. It is validated
  first and served without a restart; `GET` describes the current certificate
  and `DELETE` reverts to a generated self-signed one.
- `restore start --datastore-map <src=dst>` places disks from a given source
  datastore somewhere other than `--datastore`, for VMs spread over several
  datastores, and `--network-map` does the same for networks. Free space is
  checked per target against only the disks that land on it.
- `restore context` lists the source datastores and networks a restore point
  carries, with the target each falls back to.
- Restore points carry their encryption key, so a chain can be verified or
  restored after its job is gone, and every key a job has used is kept, so key
  rotation no longer orphans older restore points. `backup verify` and
  `restore start` take `--encryption-key` to override it. Turning encryption
  on requires an operator key.

### Enhanced

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
  Changing only the Object Lock period no longer round-trips to the bucket.
- An in-place restore keeps the VM's identity (SMBIOS UUID, vSphere
  instanceUuid), so licensing, tags, monitoring and existing backup chains
  keep matching it.
- A restore resolves its source from the catalog, so a deleted VM can still be
  restored.
- A synthetic full reports what it merged and which restore point it created,
  and is refused up front when the chain cannot supply it.
- A restore says how much space a datastore is short, instead of just
  refusing.
- The session log says which network each adapter was restored on, and when
  one could not be attached.
- vCenter objects are resolved in one call instead of one per object.
- A stopped backup discards what it wrote, and a stopped restore removes the
  VM or disk it created.

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
- A world-writable directory is refused as a local backup repository.
- API responses carry `Cache-Control: no-store`, and every endpoint has an
  explicit request and response schema in the published OpenAPI document.

### Fixed

- Network adapters on the same source network, or on different port groups of
  one distributed switch, were all restored to one target network. Backups and
  inventory scans now record each port group's own name, and every adapter
  keeps its own target.
- A day-list full schedule reports when it actually fires and no longer
  invents a time zone.
- A source with nothing allocated is counted as zero instead of guessed at.
- The support bundle reads `mongod.log` from the path `mongod.conf` declares,
  and reports a write failure as a single error line instead of a traceback.
