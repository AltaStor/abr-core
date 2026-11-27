# Changelog

All notable changes to this project are documented in this file.

## v26.11 (2026-11-27)

This release backs up and recovers Hyper-V VMs, copies restore points with backup copy jobs and keeps replica VMs up to
date with replication jobs.
VMware VMs are reached through the VDDK, with VMware's library or LibreVDDK, or over the vSphere SOAP API.
Users hold roles, what they do is audited, they may sign in with a second factor, and the server notifies them by
e-mail, Telegram, Slack, Discord or ntfy.

### Added

- VMware VMs are backed up and restored through the VDDK or over the vSphere SOAP API, chosen server-wide, per
  hypervisor and per backup job.
  `GET`/`PUT /api/v1/system/vsphere` (`api`, `vddk_library`, `apply_to_existing_hypervisors`) and
  `abr settings vsphere show|set`; `api` (vddk, soap or inherit; `transport` is its earlier name and still works) and
  `vddk_library` on `POST`/`PATCH /api/v1/inventories`; `--api`, `--vddk-lib`, `--reset-api` and `--reset-vddk-lib` on
  `abr inventory add|edit`; `--transport vddk|soap|inventory` on `abr job create|edit`.
  Restores and replications use the API of their target inventory, and an API the server cannot use is refused with the
  reason when it is chosen and when a session starts.
  Over SOAP a restore creates each disk new and thin from its whole chain in one run, a disk restore into an existing VM
  needs the VM powered off, and a backup needs vCenter: a standalone ESXi host is refused with the advice to use the
  VDDK.
  Each session records the API, the library, the file it resolves to and where each came from; session, job and
  inventory info show them.
  abr-vmdk-tool gets exactly one of `ABR_VMDK_TOOL_VDDK_LIB` and `ABR_VMDK_TOOL_VDDK_DIR`, or neither over SOAP,
  whatever the service's own environment has.
- The VDDK library file is chosen the same way, so VMware's VDDK or LibreVDDK, a free drop-in replacement for it, can be
  used.
  A library is `librevddk`, the LibreVDDK library of the `altastor-backup-librevddk` package
  (`/opt/altastor-backup/tools/librevddk/lib64/libvixDiskLib.so`), or the full path of a file the backup service can
  read that exports `VixDiskLib_InitEx`; anything else is refused with the reason (`invalid_vddk_library`).
  Unset, it is `lib64/libvixDiskLib.so` of the VDDK directory (`ABR_VMDK_TOOL_VDDK_DIR`, or the vddk folder next to
  abr-vmdk-tool), or LibreVDDK when that folder holds no VMware VDDK, as on a server upgraded from 26.09 after
  `apt autoremove`; a library chosen by its path or through `ABR_VMDK_TOOL_VDDK_DIR` is never replaced.
  `GET /api/v1/system/vsphere` lists the libraries found on the backup server (`detected_vddk_libraries`), and inventory
  info and `abr settings vsphere show` say why a library cannot be used (`vddk_library_problem`).
  A new server starts on the VDDK with LibreVDDK (`ABR_VSPHERE_API=vddk`, `ABR_VSPHERE_VDDK_LIB=librevddk` in
  defaults.env), and nothing asks for these at setup.
  A server that already has inventories, jobs or backups keeps the VDDK and the library of the VDDK directory, and a
  restart or an upgrade never changes the stored values.
  Needs abr-disk-tools a14e078 or later for a library file.
- Two-factor sign-in with an authenticator app such as Google Authenticator or Microsoft Authenticator (TOTP, RFC 6238),
  optional and per user, set up and managed in the Web UI.
  `POST /api/v1/auth/totp/setup|enable|disable` and `POST /api/v1/auth/totp/recovery-codes`; `totp_code` or
  `recovery_code` on `POST /api/v1/auth/login`, which answers `401 totp_required` without either.
  Setting it up again keeps the current secret working until the new one is confirmed, then replaces it.
  Ten one-time recovery codes, made again on request, which invalidates the earlier ones.
  A code is accepted `ABR_TOTP_VALID_WINDOW` 30-second steps either side of the server's time (1 by default) and only
  once; an expired or used one is refused with its own error.
  `abr auth login` asks for the code at a prompt that does not echo it and exits with code 5 when it is refused;
  recovery codes are for the Web UI only.
  An Administrator resets a user who lost their phone with `totp_reset` on `PATCH /api/v1/users/{id}`.
  Secrets are encrypted with the credential key and move with a catalog export; secrets, codes and recovery codes reach
  no log.
  New self-service functions `auth.totp_setup`, `auth.totp_enable`, `auth.totp_disable` and `auth.totp_recovery_codes`,
  new settings `ABR_TOTP_ISSUER` and `ABR_TOTP_VALID_WINDOW`, and new Python dependencies `pyotp` and `qrcode`.
  See `doc/TwoFactorSignIn.md`.
- Role-based access control.
  Every user holds one of five built-in roles (Administrator, Backup Administrator, Backup Operator, Restore Operator,
  Auditor), and every API operation and `abr` command runs one product function (`job.start`, `backup.delete`, ...) that
  the server checks against the user's role before it runs, for the Web UI, the CLI, `--output json` automation and the
  API alike.
  A denied request answers `403 permission_denied` with the function ID and role, and the CLI exits with code 7.
  The last enabled Administrator cannot be demoted, disabled or deleted.
  `abr user edit`, `abr user info`, `abr role list|info|functions`, `abr auth whoami`, `PATCH /api/v1/users/{id}`,
  `GET /api/v1/roles`, `GET /api/v1/functions` and `GET /api/v1/auth/me`.
  See `doc/RBAC.md`.
- Custom roles.
  An Administrator creates, clones (from a built-in or custom role), changes and deletes roles that allow an explicit
  list of functions, and gives them to users like the built-in ones; a change applies to the role's users on their next
  request.
  Nobody can grant, assign or take over more than their own role allows, nor change their own role, a built-in role
  can't be changed and a role users hold can't be deleted.
  `abr role create|clone|edit|delete`, `POST /api/v1/roles`, `PATCH|DELETE /api/v1/roles/{id}`, functions `role.create`,
  `role.edit`, `role.delete`.
- An audit log of every command and every user or role change, from every interface: time (UTC), user, the role they
  held, function, target, result (allowed, denied or failed) and the session the command started, with credentials
  masked.
  `abr audit list` and `GET /api/v1/audit`.
  The runs the server starts on its own (schedules, copies after a backup, replications after their source, scheduled
  verifies) are recorded as user `system` with interface `scheduler`, naming the trigger and the sessions.
- Hyper-V backup and recovery: full and incremental (resilient change tracking) backups of Hyper-V VMs, standalone or
  clustered, from application-consistent recovery checkpoints with a crash-consistent fallback, kept as reference points
  between backups; synthetic fulls and retention as for VMware; in-place and out-of-place recovery with target host,
  storage and virtual switch mapping.
  Disks are moved by abr-vhdx-tool (built in the `abr-disk-tools` repository) on the Hyper-V host, which the appliance
  deploys there (`ALTASTOR_VHDX_TOOL_PATH`, `ALTASTOR_HYPERV_TOOL_PATH`).
  The appliance reaches the hosts over WinRM, by HTTPS on port 5986 unless `--protocol http` is given to
  `abr inventory add|edit`.
  Hyper-V jobs need a repository the hosts can reach (S3, SMB or a shared local repository), and a job on another one is
  refused when it is created or edited (`hyperv_repository_unreachable`).
  A backup or restore rides out a short loss of the WinRM connection, the repository share or the S3 storage
  (`ALTASTOR_HYPERV_RECONNECT_SECONDS`, `ALTASTOR_HYPERV_STORAGE_ATTEMPTS`), and a disk that changed size makes the run
  a full backup.
  A VM restored in place is set back to the configuration of its restore point, network adapters included.
  See `doc/HyperVBackup.md`.
- SMB repositories (`abr repo add --type smb`): a share Hyper-V hosts reach, with the account the hosts open it with.
  Added without `--path`, it is a share the server mounts itself at `/run/altastor-backup-mounts/<id>`, through a root
  mounts broker that systemd starts per request (SMB 3.1.1 with encryption, the password never on a command line);
  `--username` and `--password` are required, and a share that does not mount is refused with the reason
  (`share_mount_failed`).
  With `--path` it is a share mounted by hand, which must be a mount of that share whose folders have mode 0750 or
  stricter (`repository_folder_mode`).
  A share that is not mounted makes the repository unavailable and nothing runs on it, and
  `POST /api/v1/repositories/test-smb` checks either kind without adding it.
  A new share, account or password is opened from a Hyper-V host before it is saved, and every Hyper-V backup writes a
  test file through the share before its checkpoint.
  They take over the SMB share local repositories used to carry; such local repositories become SMB repositories when
  the server starts or a catalog holding them is imported, and a local repository no longer takes `--server` or
  `--share`.
- Local repositories shared over SMB, so Hyper-V hosts can back up into them: `abr repo share show|add|del|check`,
  `GET /api/v1/repositories/shares`, `POST`/`DELETE /api/v1/repositories/{id}/share` and `POST .../share/check`.
  Each share is hidden and open only to an account of its own, whose password is stored encrypted.
  Samba is changed by a root shares broker that systemd starts per request, with no sudo rule; the shares live in
  `/etc/altastor-backup/samba-shares.conf` behind one include line in `smb.conf`, which the server puts back when it is
  lost.
  Removing the package closes the shares, and the next start of the server brings them back.
  A share is shown and checked by the address Hyper-V hosts are given for it, or by the name in
  `ABR_SHARES_SERVER_NAME`.
  A failed check names its cause (`409 repository_share_check_failed`), and a folder whose name holds `%`, which Samba
  reads as a macro, is not shared.
- Backup copy jobs copy the restore points of backup jobs or repositories into another repository:
  `abr job create-backup-copy` and `POST /api/v1/jobs/backup-copy`, changed with `abr job edit`.
  A periodic copy runs on a schedule, by hand or after another job (`--run-after-job`), an immediate one after each new
  restore point (`--mode immediate`); each VM has its own chain in the destination, with its own retention, Object Lock
  and key.
  VMware and Hyper-V restore points are copied between local, SMB and S3 repositories in any direction.
  A copy job can copy the restore points of another one, but never its own, also not through other jobs
  (`backup_copy_cycle`).
  A copy window (`--copy-window 'mon,tue 22:00-06:00'`) holds copies while it is closed and stops a running copy when it
  closes or is changed to leave the time outside it; the copy is queued again for the next opening.
  A synthetic full continues the copied chain, a VM that several source jobs back up is copied from one of them by a
  stated rule, and a job or repository a copy job reads cannot be deleted (`backup_copy_source_in_use`).
  Their sessions have the type `backup-copy` (`GET /api/v1/sessions?type=backup-copy`), and `abr restore list --job`
  lists the restore points of one job.
- Replication jobs keep a powered-off replica VM up to date from the restore points of a backup job:
  `abr job create-replication` and `POST /api/v1/jobs/replication`, with the target inventory, host, datastore, network
  mapping, replica name and an optional first restore point.
  A replication runs on a schedule, by hand or after its source backup, and its job is deleted with or without its
  replica.
  The first run restores the replica from backup data only; later runs write the new restore points under a snapshot or
  checkpoint of the job's own, or rebuild the disks after a new full, a disk change or a new encryption key.
  A replica that was powered on, holds a snapshot the job did not take or other disks is refused on that run and every
  later one; temporary errors are retried in the same session, and a stopped run puts the replica back.
  VMware with vCenter or a standalone ESXi host and Hyper-V, from local, SMB and S3 repositories.
- Backups verified on a job schedule: `--verify-schedule` and the `--verify-*` options of backup and backup copy jobs.
  Each run reads the newest restore point of every chain with every point it builds on back to its full, and every
  `--verify-whole-chain-every`-th run (4 by default) whole chains.
  A point whose data a verify finds damaged is marked corrupt with every point that builds on it, by
  `abr backup verify`, `abr repo verify`, the verify schedule and a synthetic full that reads damaged data; an intact
  check clears the marks.
  A corrupt point is refused for restore, the job's next run takes an active full (`DamagedChain`), and a chain whose
  newest point is healthy shows as available with its corrupt points counted.
- Backup jobs retry a VM whose backup failed: `--retry-count` (0-20) and `--retry-interval` (0-1440 minutes) on
  `abr job create|edit`.
  A new job retries 3 times, 10 minutes apart; a job from before does not until it is set.
  Each retry is a new session of that VM in the same run, and the run, its notification and job info report the last
  attempt of each VM.
- `abr backup delete` asks first in a terminal, naming the chain, the VM and its restore points; `--yes` skips the
  question and is required without a terminal.
- A recycle bin for deleted backups: `abr settings recycle-bin show|set --keep-days N` (0-365, 0 by default) and
  `GET`/`PUT /api/v1/system/recycle-bin` (function `system.recycle_bin`) keep a deleted chain for N days.
  Its points are listed as deleted with who deleted them, when and their purge date, and nothing restores, copies,
  verifies, replicates or retains them.
  `abr backup undelete` and `POST /api/v1/backups/{id}/undelete` (function `backup.undelete`) bring a chain back;
  deleting it again, or the purge date passing, deletes it for good.
- `--retention-min-fulls N` (0-100) on `abr job create|create-copy|edit`: retention never deletes the N newest full
  backups of a chain, with their incrementals, whatever their age.
  New jobs keep 2, so the first full after a pause longer than `--retention-days` keeps the chain from before the pause;
  jobs from before keep their value, and 0 deletes strictly by age.
- Retention is held back after the server clock jumps forward by more than `BACKUPSERVER_CLOCK_JUMP_HOURS` (24), also
  across a restart, while backups go on with a warning.
  `abr system clock show|confirm`, `GET /api/v1/system/clock` and `POST /api/v1/system/clock/confirm` (function
  `system.clock_confirm`) release it, and `abr system health` reports it.
- Each session and restore point records whether the backup is application-consistent or crash-consistent, and why
  (`consistency`, `consistency_reason`); a crash-consistent backup of a running VM ends with a warning.
  Job option `--on-quiesce-failure fail|crash-consistent`: `fail`, the VMware default, fails a backup whose guest cannot
  be quiesced, and `crash-consistent`, the Hyper-V default, takes the backup without quiescing.
- A VMware restore puts each disk back on the controller type, bus and slot it was backed up from, and gives the VM the
  boot order of its source; restore points made before keep the earlier layout and the session says so.
- A VMware VM restored in place keeps its VM object, moRef and snapshots: the restored disks are written next to the old
  ones and the VM is switched to them, under a snapshot that puts it back as it was if the restore fails.
  Its folder, MAC addresses, notes, tags, custom attributes, permissions and alarms stay, and its configuration is set
  back to the restore point.
- The inventory shows the IP address a running VM's guest reports, on vCenter and on Hyper-V.
- `abr user edit --new-name` and `--reset-password`, and `name` and `password` in `PATCH /api/v1/users/{id}`: an
  Administrator renames a user, who keeps their password, or gives them a new password, which ends their sessions.
- Hyper-V job option `--guest-consistency`.
- E-mail notifications through the organisation's SMTP server: a central notification service every component hands its
  notifications to, which sends them in the background, retries a delivery that can still succeed (three attempts) and
  records every outcome in a delivery history and the server log.
  STARTTLS, SSL/TLS or plain connections with certificate verification; sign-in with a user name and password, or none.
  Events: job failed, finished with warnings or succeeded (one e-mail per run, listing every VM), restore failed or
  finished, repository running out of space, server clock jump, and an installed HTTPS certificate about to expire.
  Settings are checked as a whole and refused with every problem by field; secrets are stored encrypted and never
  returned.
  A test e-mail reports why it was not sent (connection, TLS, sign-in, sender or recipients refused).
  `abr notification show|set|test|history|events list|events set`, `GET|PUT /api/v1/notifications/email`,
  `POST /api/v1/notifications/email/test`, `GET /api/v1/notifications/email/deliveries`,
  `GET /api/v1/notifications/events`, functions `notification.view`, `notification.edit`, `notification.test`.
  See `doc/Notifications.md`.
- Messaging platforms: Telegram, Slack, Discord and ntfy notifications, sent through Apprise (`/usr/bin/apprise` from
  the Debian package `apprise`, run as a program of its own; the server package needs `apprise (>= 1.9)`).
  Each is added when its settings are first saved and can be removed again (its history stays).
  Telegram: a bot token and up to ten chats, each sent to on its own, so a retry repeats none.
  Slack: an incoming webhook URL, posting as AltaStor.
  Discord: a channel webhook URL, long messages split.
  ntfy: a topic URL on ntfy.sh or a server of one's own, and an access token for a server with access control; failures
  go at high priority.
  Each has its own switch, events, test message and delivery history, with the e-mail's retries and restart handling.
  Secrets are stored encrypted and never returned; they reach Apprise in a private file, never on its command line, and
  Apprise loads no plugins, configuration or cache of its own.
  Apprise is asked whether it can read the settings before a platform is switched on.
  Microsoft Teams is not offered: Debian 13's Apprise 1.9.3 cannot read the workflow URLs Teams gives now.
  `abr notification platforms` and `abr notification telegram|slack|discord|ntfy show|set|test|history|remove`;
  `GET /api/v1/notifications/messaging`, `GET|PUT|DELETE /api/v1/notifications/{platform}`, `POST .../test`,
  `GET .../deliveries`, under `notification.view`, `notification.edit` and `notification.test`.
  `ABR_APPRISE` names another Apprise program.
- Notification events for a failed verify (`backup.verify_failed`) and for privileged actions, off by default:
  `backup.deleted`, `repository.removed`, `user.created`, `user.role_changed` and `repository.immutability_changed`.
  A privileged action is notified from its audit record, naming the user, their role, the interface and address, the
  object and the new role or setting; a refused action sends nothing.
- A web session signs out after a time without activity instead of at a fixed time: every request the user makes moves
  its expiry, and `ABR_WEB_SESSION_LIFETIME_SECONDS` (7200, 0 for none) ends it that long after sign-in whatever the
  activity.
  `POST /api/v1/auth/session/touch` renews it; CLI sessions keep their rules.
- `GET /api/v1/auth/me` reports the session's `expires_at` and `absolute_expires_at`; sent with `X-ABR-Refresh` it
  renews nothing, so the Web UI reads the expiry without moving it.
- `GET /api/v1/system/clock` and `/system/info` report the server's time zone (`timezone`, `utc_offset`).
- `abr job retention [--dry-run]` and `POST /api/v1/jobs/{id}/retention` apply (or preview) a job's retention across all
  of its chains and report, per chain, what was removed, what Object Lock still holds and what could not be removed.
- Per-disk and per-adapter restore placement: `restore start --disk-datastore` and `--nic-network` place a single disk
  or network adapter, which `--datastore-map` and `--network-map` cannot express when everything shares one source.
  Precedence is disk, then source datastore, then whole VM, and free space is checked against where each disk actually
  lands.
- `restore context` also lists disks and network adapters, so the exact names the mapping flags take are discoverable.
- CLI secrets can come from an environment variable (`ABR_PASSWORD`, `ABR_INVENTORY_PASSWORD`, `ABR_REPO_SECRET_KEY`,
  `ABR_ENCRYPTION_KEY`, ...) or an interactive prompt instead of only a command-line flag.
  Without a terminal the CLI reads a secret or a two-factor code once from standard input, with no prompt, and a missing
  password is refused with exit code 2 and how to give it.
- A job reports how its last run went across all of its VMs, and its soonest next run across all of its schedules; the
  incremental schedule keeps its own next-run field.

### Enhanced

- A restore hands a disk's whole chain, the full and its incrementals, to the disk tool in one run (`--source-names`),
  so each block is read and written once; a tool without the option restores one backup at a time.
- Object Lock holds every backup of a chain to the chain's own date (`--retention-till`), so an incremental written
  after midnight UTC is no longer released a day before its full.
  A lock is never shortened, a lock already longer than asked is not reported as failed, and a backup warns when the
  full of its chain stays held shorter than its newer backups.
- A delete that Object Lock holds back is remembered: the points wait for deletion, shown so in `abr backup list` and
  `abr backup info`, and the server removes them once their lock ends (`BACKUPSERVER_DELETE_RETRY_SECONDS`).
- The disks of a VMware VM are consolidated after the backup snapshot is removed when vCenter asks for it
  (`BACKUPSERVER_CONSOLIDATION_SETTLE_SECONDS`), and a consolidation that fails warns (`consolidation_needed`).
- `abr repo rescan` marks the restore points that build on a missing one `chain_broken`, and a restore of one is refused
  at once.
- A restore is refused before it starts when the given `--encryption-key` is not the key of its restore point
  (`409 encryption_key_mismatch`).
  `abr restore start --backup` says why no point of a chain can be restored, and warns when it passes over newer points
  (`newer_points_unavailable`).
- Refusals name what they did not find: the job, host, datastore or network, and the source network, disk or adapter a
  restore point does not carry, with the ones it does carry.
- `--reset-encryption` keeps a job's keys, so its earlier backups still restore without a key typed by hand;
  `abr job edit --set-encryption` takes a new key from `ABR_ENCRYPTION_KEY` or a prompt and rotates to it; a restore
  tries the key its restore point was written with first.
- `abr job edit --schedule none` (`"schedule": null`) takes a job back to manual runs, a schedule detail given without
  `--schedule` changes the regular schedule and keeps its type, `abr job create --disabled` works without a schedule,
  and `abr job create` waits for the inventory's first scan instead of failing after 30 s.
- A certificate installed without its intermediate CA certificates gets a `chain_incomplete` warning; when the server's
  key changes, the CLI accepts a certificate that a CA the machine trusts issued for the host; `tls_bootstrap --renew`
  no longer replaces an administrator's certificate unless `--force` is given.
- The support bundle carries the VDDK and LibreVDDK logs of `/var/log/altastor-backup/vddk` under `logs/vddk/`, with
  secrets redacted.
- The restore points of a chain in `GET /api/v1/backups` carry `copy_source`, as `GET /api/v1/restore/points` does, so a
  client that reads the chains can tell a copy a backup copy job made, and where it was copied from.
- A replication session in `GET /api/v1/sessions` carries `replication`: whether the run created, updated, rebuilt or
  found the replica up to date, the restore point it brought the replica to, the incrementals applied, the target host
  and the replica VM.
- A schedule's `timezone` in the job DTO is the zone it actually runs on: the server's zone for one saved without a zone
  or with an abbreviation of the server's zone, and UTC for a name the server does not know, instead of the stored name.
  The API reference describes it.
- Retention no longer tries to delete a restore point still under S3 Object Lock: it is reported as held, with the older
  points it depends on, instead of as a failure.
  Deleting a chain goes newest point first and stops at the first point the storage keeps, so an older point it depends
  on is not removed from under it, and restore points report when their Object Lock ends.
- A synthetic full records the hypervisor kind of the chain it merged instead of always reporting vCenter.
- A restore point is only offered, and only accepted, for restore to an inventory of the platform it came from.
- Logging: storage operations (with their bucket and path, never keys), Object Lock validation and application, Hyper-V
  connection, authentication and discovery outcomes, per-chain retention results, and a summary of every queued backup
  and restore session.
- A Hyper-V backup checks from the host that it can reach the repository before it takes a checkpoint, so an unreachable
  S3 endpoint, refused keys or a share the host cannot open fail the run without touching the VM.
  A failed abr-vhdx-tool run on the host reports the tool's own reason, not only its last line.
- One detail of a full backup schedule (e.g. `--active-full-time`) can be changed on its own, keeping the stored type
  and the rest of its fields.
- A restore runs against the inventory named by `--target-inventory`, not the one the restore point came from.
- `--network-map` also applies to the adapters `--nic-network` leaves out.
- A VM's independent disks are skipped with a warning instead of failing the whole backup.
- `repo verify` reads every restore point of each chain, not only the newest.
- `repo edit` refuses S3 credentials that cannot set Object Lock on an immutable repository.
- Clearer failures: a repository running out of space during a backup or synthetic full, a VM powered off, deleted,
  unregistered or moved in vCenter while it was backed up, and an ESXi data path that went silent (the backup is stopped
  and says so).
  A disk tool that ends without an error of its own is named with its signal or exit code, with a hint at the
  out-of-memory killer for SIGKILL, and a repository folder it cannot read with the read error instead of a missing
  folder.
- Restore points dated ahead of the server clock are reported together with the points that chain onto them; a job's
  last run ahead of the clock is logged once, marked in `job list` and shown as a warning in `job info`.
- An inventory rescan skips a VM deleted from vCenter mid-read instead of failing the whole rescan.
- The dashboard summary reports real times and recent backup size, and labels sessions by type.

### Changed

- A new local repository is shared over SMB right away, so Hyper-V hosts can back up into it
  (`POST /api/v1/repositories`, `abr repo add`); `"shared": false` (`--no-share`) adds it unshared, and when the share
  cannot be made it is added unshared with the reason in `share_error` and its warnings.
- The repository the server creates at its first start is now the **Initial Repo** (marked `initial`); a catalog that
  still has the Default Repo under that name gets the new one at startup.
  Its Hyper-V switch is gone with `GET`/`POST /api/v1/repositories/default` and `abr repo default`: it is shared like
  any other local repository (`abr repo share`).
- `default` on a repository now marks the default repository, where a new backup job stores unless another is named:
  `PATCH /api/v1/repositories/{id}` with `{"default": true}` or `false`, `abr repo edit --default|--no-default`, and a
  DEFAULT column in `abr repo list`.
  A backup job created without `repo_id` (`abr job create` without `--repository`) stores there, and is refused when no
  repository is the default.
- `abr user add` and `POST /api/v1/users` need a role (`--role`).
  Users from before roles existed become Administrators when the server starts, which is what they could do before.
- Inventory passwords, repository passwords, S3 secret keys and job encryption keys are stored encrypted with the
  credential key, and session tokens as digests.
  Values an earlier version stored in clear are encrypted at the first start, and a key the server cannot decrypt is
  never used.
- The server hands the disk tools their secrets (encryption keys, S3 secret keys, the vCenter password) on standard
  input with `--stdin-keys`, and the Hyper-V hosts as PowerShell secure strings, so none shows on a command line.
  Needs abr-vmdk-tool and abr-vhdx-tool from abr-disk-tools 3ee1dab or later.
- Secret settings are masked in the catalog-restore response.
- User, password and job input is validated strictly, and a boolean field of a request takes only true, false or null.
- A new job encryption key needs at least 12 characters of 3 kinds (lowercase and uppercase letters, digits, symbols)
  and no space at either end, as the Web UI asked; a key a job already has is taken as it is.
- A job description takes any Unicode text on one line; the job name stays Latin.
- A periodic synthetic full runs at most once a day, and a shorter `--synthetic-full-every` is refused
  (`invalid_schedule`).
- An S3 repository whose bucket and prefix equal, contain or lie inside another's on the same endpoint is refused
  (`409 repository_path_overlaps`).
- The CLI refuses an abbreviated option instead of completing it, so `--vm` no longer runs as `--vm-id`.
- `abr inventory edit --secure`, or a new address of an inventory that verifies certificates, is refused before it is
  saved when the endpoint's certificate is not trusted.
- A restored VMware VM is refused a datastore folder that already exists, which the refusal names.
- `abr job start --synthetic` is refused when the job's encryption changed since the VM's last restore point.
- The server and CLI log files start each line with the local time, milliseconds and the zone offset
  (`YYYY-MM-DDTHH:MM:SS.mmm±HH:MM`).
- Object Lock retention up to 36500 days is accepted, as the console promises.
- Network mappings are refused on in-place restore, and an unknown source adapter or network is reported as an invalid
  request.
- `system version` reports the version of the installed server package instead of a constant baked into the source.

### Fixed

- A job with a pending session can no longer be deleted: the refusal now counts the same queued, running and stopping
  sessions as the job's `active_session_count`.
- A server log line keeps its ID from one `GET /api/v1/system/logs` to the next once the log is larger than the 512 KiB
  read window, so a client can tell the lines it has from new ones.

- Cleanup after interrupted runs:
  - a backup killed with the server leaves no snapshot on the VM; the stranded one is removed when the server comes
    back;
  - a restore stopped at the very start destroys its half-made VM;
  - a failed restore destroys only the VM it created, never one merely matching by name;
  - the data mover's relocation block on a VM is lifted when a backup ends abnormally, with retries;
  - a vCenter or Hyper-V backup, backup copy or synthetic full that fails or is stopped before a VM's first restore
    point leaves no empty folder for the VM (or its job) in the repository;
  - a backup stopped right after its snapshot is created removes it, and a snapshot a backup cannot remove is named
    (`snapshot_left`) and removed by the next backup of the VM;
  - a restore point is marked as being deleted before its data is removed, so a crash never leaves it offered for
    restore;
  - a failed or stopped synthetic full removes everything it wrote, its manifest included.
- Schedules:
  - changing a full backup schedule replaces its weekday list instead of merging into the old one;
  - `--days` on a schedule that takes no weekdays is refused instead of silently doing nothing;
  - full backup schedule state flags are honoured on job create, and a job's switch and its schedule stay in step on
    edit;
  - a next run pushed into the future by a wrong clock, or into the past by the repeated DST hour, is corrected;
  - a job in the repeated DST hour runs once on the fall-back night, and the change of the UTC offset is no longer taken
    for a clock that was ahead;
  - `--active-full-days` and `--synthetic-full-days` on a full schedule that takes no weekdays are refused instead of
    ignored;
  - a manual start on a synthetic-full day runs the incremental before the synthetic full, as the scheduled run does;
  - a scheduled run of a VM gone from the inventory gets a failed session that says why, instead of being tried again on
    every poll.
- GFS: a later full of the same week, month or year no longer takes the mark from the full that has it, and a forced
  full (`abr job start --full` or `--synthetic`) takes the mark of a period in which the job made no other full.
- A VMware backup whose change tracking restarted on one disk backs up every disk in full, so no restore point marked
  full holds only changes.
- While the catalog database is down, a backup that cannot record its restore point fails (`catalog_unavailable`) and
  removes its data instead of completing without one, catalog calls fail fast so a write is refused within the CLI's
  timeout, and job writes answer `503 catalog_unavailable` instead of 500.
- An inventory rescan keeps a job on its VM by UUID, so a renamed VM keeps its job; a job moved to a same-named VM
  because its own is gone says so (`vm_reassigned`).
- Jobs, backup chains and restore points show the current names of their repository, inventory and job after a rename,
  and `abr backup list --repo` finds a chain by the new name.
- A new-VM restore into another inventory with no host chosen lands on a host of that inventory instead of failing with
  `host_not_found`.
- A VMware VM restored under a name with spaces gets a folder of its own instead of failing to open its disk.
- A running session is no longer marked failed while the server still runs it, such as between retries or in a long step
  without progress.
- A synthetic full merges each disk under the backup's timeout, scaled by the disk's capacity, instead of the flat tool
  timeout of 15 minutes.
- An imported chain is read from the folder it was imported from, keeps the type, time, VM and platform of its manifest,
  and an incremental goes on its imported parent.
- The CLI writes its token and trust files 0600 from the start, and makes a new configuration directory 0700.
- `abr auth login --print-token` no longer replaces the CLI's cached session.
- `abr system import` writes the recovered server configuration beside an existing file, 0600, with the secret settings
  left as comments to fill in, and names `/etc/altastor-backup/server.conf` as the file to merge it into.
- Dates such as `updated_at` carry their UTC zone in the API, so a browser outside UTC shows them right.
- `POST /api/v1/inventories` without a type or with an unknown one answers 422 instead of 500.
- A job saved from a browser that reports its zone by an old IANA name, as Chrome, Firefox and Node do for some zones,
  is accepted and kept by the zone's current name instead of being refused with "Unknown time zone": Debian 13 no longer
  has the old names unless `tzdata-legacy` is installed.
  A schedule or copy window already stored by an old name runs on that zone instead of on UTC.
- A session stopped while still queued gets an end time and a reason, shown for earlier ones too.
- A deletion counts as done when the data is already gone from storage, instead of failing on an artifact that no longer
  exists.
- A catalog import onto a fresh install succeeds, all or nothing.
- `--network-map` sources are compared exactly, the way the map is applied.
- No warning that a VM stays blocked from relocating when it is vCenter that holds the block.
- An unknown `--log-level` and `auth login` without a terminal give a usage error instead of a traceback.
- The API no longer logs a schema warning on every response.

## v26.09.2 (2026-10-15)

### Changed

- Backups are stored in folders named by job and VM ID, so a renamed VM keeps one chain and a synthetic full can still
  merge it.
  Existing jobs move on their next full backup, and a VM's previous backup is found by identity, not by name.
- A queued backup decides its type when it starts, not when it was queued.
  The next run of a damaged chain is an active full, and a manual synthetic full of a damaged chain is refused.

### Fixed

- A full backup of a VM that already had a snapshot read the VM's base disk instead of the disk of its own snapshot.
- An incremental could chain onto another job's backup of the same VM; it now continues from its own job's previous
  restore point, and refuses rather than take another job's backup when its own parent is gone.
- A backup or synthetic full is flushed to stable storage before it completes, so a power loss right after cannot leave
  a restore point with empty files.
  One that cannot be flushed fails and keeps nothing.
- A failed Object Lock extension is recorded as a session warning instead of crashing, and the retention pass still runs
  after it.
- Retention keeps a restore point its Object Lock still holds without errors or warnings, and removes it on the first
  pass after the lock expires.

## v26.09.1 (2026-10-02)

### Fixed

- A synthetic full did not carry the change ID of the newest backup it merged, forcing the next incremental to run as a
  full.
- A restored disk keeps its source provisioning: thin stays thin, and an eager-zeroed thick disk is recreated
  eager-zeroed instead of collapsing to lazy-zeroed thick.
- A synthetic full that fails or is cancelled also discards the disk it was merging at that moment, not just the ones
  already finished.
- Tasks left running when the server stopped are marked failed at startup, and the output they stranded is removed
  unless a restore point references it.

## v26.09 (2026-09-28)

### Added

- Support for backup and restore of VMware vCenter and ESXi 8–9+ VMs.
- Support for local file system and S3 backends for backup and restore workflows.
- CRC32C integrity validation for headers, index blocks and data blocks.
- zstd compression of backup data blocks, with configurable levels from 1 to 9.
- Data block encryption, improving protection for stored backup content.
- S3 Object Lock immutability for backup repositories, managed from the CLI.
  `--immutability-retention <job|minimum>` chooses whether the lock period runs to the end of the job's full-backup
  cycle or is only `--immutability-days` counted from each backup.
- Synthetic full backups: `job start --synthetic` builds a full by merging the chain already in the repository, and
  `--target-repo` puts the merged full in another repository.
- Jobs run three independent schedules: base incrementals, active full and synthetic full.
  When several fall due at once, active full wins over synthetic full and synthetic full over the incremental.
  Every job keeps at least one full schedule, so retention can always free the chain.
- HTTPS is served on port 443, with a plain-HTTP listener on port 80 that redirects browsers to it.
- Administrators can install their own HTTPS certificate and private key (CA-issued or self-signed, optionally
  passphrase-protected) through `PUT /api/v1/system/tls-certificate` or the web console.
  It is validated first and served without a restart; `GET` describes the current certificate and `DELETE` reverts to a
  generated self-signed one.
- `restore start --datastore-map <src=dst>` places disks from a given source datastore somewhere other than
  `--datastore`, for VMs spread over several datastores, and `--network-map` does the same for networks.
  Free space is checked per target against only the disks that land on it.
- `restore context` lists the source datastores and networks a restore point carries, with the target each falls back
  to.
- Restore points carry their encryption key, so a chain can be verified or restored after its job is gone, and every key
  a job has used is kept, so key rotation no longer orphans older restore points.
  `backup verify` and `restore start` take `--encryption-key` to override it.
  Turning encryption on requires an operator key.

### Enhanced

- Improved backup and restore reliability with corrected backup and restore logic and better handling of storage
  backends.
- Expanded CLI and web console capabilities, including support for S3-based operations and more polished backup
  management workflows.
- Better job and VM management with VM tree selection, dynamic progress reporting, repository search and delete actions.
- Operational improvements such as automatic rescan, cluster validation, serialized same-VM backups and CBT enablement
  for full backups.
- Usability and stability refinements, including cleaner JSON output, improved deletion handling and stricter input
  validation in the web console.
- Updated VDDK integration with support for the newer 9.1 toolchain.
- Repositories and inventories are validated against real storage and endpoints before they are registered; nothing is
  created when a check fails.
  Changing only the Object Lock period no longer round-trips to the bucket.
- An in-place restore keeps the VM's identity (SMBIOS UUID, vSphere instanceUuid), so licensing, tags, monitoring and
  existing backup chains keep matching it.
- A restore resolves its source from the catalog, so a deleted VM can still be restored.
- A synthetic full reports what it merged and which restore point it created, and is refused up front when the chain
  cannot supply it.
- A restore says how much space a datastore is short, instead of just refusing.
- The session log says which network each adapter was restored on, and when one could not be attached.
- vCenter objects are resolved in one call instead of one per object.
- A stopped backup discards what it wrote, and a stopped restore removes the VM or disk it created.

### Changed

- Split the product into separate repositories.
  This one now holds the Python core only: the BackupServer API (`python/srv`) and the `abr` CLI (`python/cli`).
- `scripts/check_deps.sh` now prepares only what the Python core needs: the virtualenv, the Python requirements and the
  local MongoDB bundle.
- `scripts/clear.sh` now removes what `check_deps.sh` actually creates (the virtualenv and the local MongoDB bundle)
  instead of MongoDB binaries in `/usr/bin` that were never installed there.
- `scripts/run_srv.sh` starts the server through `python -m python.srv.backup_server`, the same entry point the systemd
  unit uses, so it applies `defaults.env` and the pinned TLS cipher list rather than invoking `uvicorn` directly.
  It also defaults `MONGODB_URI` to the local instance, so a development run persists instead of silently running in
  memory.
- Point the server at a data mover built elsewhere with `ALTASTOR_DISK_TOOL_PATH`, and at a web console bundle built
  elsewhere with `ALTASTOR_WEB_ROOT`.
- The TLS renewal check only renews or regenerates the self-signed certificate the server created.
  It used to replace any certificate whose addresses didn't match the host's own (which is every CA-issued one) at
  startup and on the daily check.
- A certificate reload into the running server now fully switches over even when the key type changes (RSA to EC or
  back); before, the previous certificate stayed on offer to clients that preferred its key type.
- TLS certificates a browser would refuse are rejected at install time.
- Object Lock: backups in one chain share one expiry date per block generation, derived from the job's backup cycle and
  retention.
  `--immutability-days` is the minimum, not an addend, and a new generation starts once the remaining lock drops below
  the promised retention.
- A world-writable directory is refused as a local backup repository.
- API responses carry `Cache-Control: no-store`, and every endpoint has an explicit request and response schema in the
  published OpenAPI document.

### Fixed

- Network adapters on the same source network, or on different port groups of one distributed switch, were all restored
  to one target network.
  Backups and inventory scans now record each port group's own name, and every adapter keeps its own target.
- A day-list full schedule reports when it actually fires and no longer invents a time zone.
- A source with nothing allocated is counted as zero instead of guessed at.
- The support bundle reads `mongod.log` from the path `mongod.conf` declares, and reports a write failure as a single
  error line instead of a traceback.
