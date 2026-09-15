# Changelog

## v26.08: (Upcoming Release)

### Added
- Support of backup and restore of VMs for Vmware vCenter and ESXi 8-9+.
- Support of local file system and S3 backends for backup and restore workflows.
- Use of CRC32C integrity validation for headers, index blocks, and data blocks.
- Support of zstd compression of backup data blocks, with configurable levels from 1 to 9.
- Support of data block encryption, improving protection for stored backup content.
- Administrators can install their own HTTPS certificate and private key (CA-issued or
  self-signed, optionally passphrase-protected) through `PUT /api/v1/system/tls-certificate`
  or the web console. It is validated first and served without a restart; `GET` describes
  the current certificate and `DELETE` reverts to a generated self-signed one.

### Enhanced
- Improved backup and restore reliability with corrected backup/restore logic and better handling of storage backends.
- Expanded CLI and Web UI capabilities, including support for S3-based operations and more polished backup management workflows.
- Better job and VM management experience with VM tree selection, dynamic progress reporting, repository search, and delete actions.
- Added operational improvements such as automatic rescan, cluster validation, serialized same-VM backups, and CBT enablement for full backups.
- Usability and stability refinements, including cleaner JSON output, improved deletion handling, and stricter UI input validation.
- Updated VDDK integration with support for the newer 9.1 toolchain.

### Changed
- Split the product into separate repositories. This one now holds the Python
  core only - the BackupServer API (`python/srv`) and the `abr` CLI
  (`python/cli`).
- `scripts/check_deps.sh` now prepares only what the Python core needs:
  the virtualenv, the Python requirements, and the local MongoDB bundle.
- `scripts/clear.sh` now removes what `check_deps.sh` actually creates - the
  virtualenv and the local MongoDB bundle - instead of MongoDB binaries in
  `/usr/bin` that were never installed there.
- `scripts/run_srv.sh` starts the server through `python -m
  python.srv.backup_server`, the same entry point the systemd unit uses, so it
  applies `defaults.env` and the pinned TLS cipher list rather than invoking
  `uvicorn` directly. It also defaults `MONGODB_URI` to the local instance, so
  a development run persists instead of silently running in memory.
- Point the server at a data mover built elsewhere with
  `ALTASTOR_DISK_TOOL_PATH`, and at a web console bundle built elsewhere with
  `ALTASTOR_WEB_ROOT`.
- The TLS renewal check only renews or regenerates the self-signed certificate
  the server created. It used to replace any certificate whose addresses didn't
  match the host's own -- which is every CA-issued one -- at startup and on the
  daily check.
- A certificate reload into the running server now fully switches over even
  when the key type changes (RSA to EC or back); before, the previous
  certificate stayed on offer to clients that preferred its key type.
