ABR CLI Command Reference v4.1 

AltaStor Backup & Replication CLI Specification (Job-Centric Schedule Shift) 

OVERVIEW 

abr is the command-line interface for configuring, executing, inspecting, and maintaining backup and recovery workflows. This edition implements a structural design: 

job: defines WHAT to protect (Virtual Machines), WHEN to execute (base scheduling heartbeats), HOW LONG to keep data (rolling operational retention and GFS archival milestones, and explicit rules for Full backup generations (Active or Synthetic Full windows). 

Semantic Model 

auth = session login/logout against the REST API 

user = accounts allowed to authenticate 

repo = backup storage targets 

inventory = virtualization discovery sources 

job = binding between workloads (VMs), schedule, operational retention, GFS, and repo 

session = runtime execution tracking of job runs 

backup = physical storage view (chains, sizes, compression, repository placement) 

restore = logical recovery workflows and restore points 

system = platform health, licensing, version 

Global Options 

abr [--output table|json] [--log-level debug|info|warn|error] 

Default output is table. Use --output json for automation and GUI integration. 

Selector Matrix & Canonical Rules 

Configuration Layer: [inventory, repo, job] prefer --name as primary selector; --id is supported for automation. 
Execution & Storage Layer: [session, backup] are primarily ID-based. Restore supports restore-point IDs plus source/target selectors such as --backup, --vm, --inventory, --target-host, and --datastore for full VM recovery workflows. 

CORE RULES (ENGINE MODEL) 

First run of any job always produces a Full backup (Active Full method).  

Subsequent runs default to incremental processing. 

Full backups (Active or Synthetic) occur exclusively on days of the week specified directly within the job configuration parameters. 

Synthetic Full is used when: the current day matches the job configuration for synthetic windows. 

Active Full is used when: the current day matches the job configuration for active windows. 

Rule of Priority: If a specific execution day is accidentally targeted for both Active and Synthetic execution within the job parameters, Active Full always takes priority. 

Admins can override engine selection manually via runtime flags such as --full (`--force-full` is accepted as a compatibility alias). 

Engine Execution & Priority Matrix 

Scenario / Condition 

Resolved Backup Type 

First execution of a job 

Full Backup (Active Full) 

Explicit manual override flag 

Active Full (via --full / --force-full alias) 

Scheduled Day: Active & Synthetic inside Job rules 

Active Full (Active overrides Synthetic) 

Scheduled Day: Synthetic Only 

Synthetic Full 

Standard Scheduled Day (No Full windows active in job) 

Incremental 

 

Retention & GFS Coexistence Model 

retention-days (Operational) manages a short rolling window of daily recovery points (e.g., 14 days). 

GFS (Long-term) flags specific scheduled Full backups to be frozen as Weekly/Monthly/Yearly milestones. 

Rule of Exclusion: When a Full backup point reaches the end of the operational window, it is NOT deleted if it carries an active GFS archival mark. 

It transitions exclusively to GFS lifecycle tracking. 

Safety Model 

Manual physical deletion of specific chains is executed via the backup layer (backup delete) with mandatory verification checks. GFS protection can be deliberately overridden with backup delete --force, which also releases the repository holding the chain once every protected chain in it is removed this way. 

COMMAND TREE 

abr 
├─ auth 
├─ user 
├─ repo 
├─ inventory 
├─ job 
├─ session 
├─ backup 
├─ restore 
└─ system 

AUTHENTICATION COMMANDS 

Every REST API call requires a session token. The token is cached locally per --base-url and used automatically; ABR_TOKEN, if set, always wins over the cache. 

abr auth login 

Description: Log in and cache a session token. Prompts for the password (or reads ABR_PASSWORD); --name falls back to ABR_USERNAME. 

abr auth login [--name <name>] [--print-token] 

Examples: 
abr auth login --name abr 
ABR_USERNAME=abr ABR_PASSWORD='secret' abr auth login 

--print-token: print only the token to stdout, for `export ABR_TOKEN=$(abr auth login --print-token)`. 

abr auth logout 

Description: Invalidate the current session and clear the cached token. 

abr auth logout 

abr auth change-password 

Description: Change your own password. Prompts for the current and new password. Invalidates your other sessions; this one stays valid. 

abr auth change-password 

Note: 
Always acts on the current session's account, never another user's. abr auth login warns if the account is still on its default password. 

USER COMMANDS 

Manage accounts allowed to authenticate. Requires an existing session. The first user is created at startup from ABR_BOOTSTRAP_ADMIN_USER / ABR_BOOTSTRAP_ADMIN_PASSWORD, not via this command. 

abr user add 

Description: Add a new user. Prompts for the password; there is no --password flag. 

abr user add --name <name> [--full-name <text>] 

Example: 
abr user add --name alice --full-name "Alice Doe" 

abr user list 

Description: Show all users. Never includes password hashes. 

abr user list [--output json] 

abr user delete 

Description: Delete a user and invalidate their sessions. 

abr user delete --name <name> [--id <id>] 

REPOSITORY COMMANDS 

Configure and manage backup storage targets for chains, metadata, and repository state. 

abr repo add 

Description: Create a backup repository. 

abr repo add --name <name> --type <local|nfs|smb|s3> [--path <path>] [--server <host/ip>] [--share <name>] [--username <user>] [--password <pass>] [--description <text>] 

For --type s3: [--bucket <name>] [--prefix <key-prefix>] [--region <region>] [--endpoint-url <url>] [--access-key <key>] [--secret-key <key>] [--profile <name>] [--path-style | --virtual-host-style] [--verify-tls | --no-verify-tls] [--immutable <none|governance|compliance>] [--immutability-days <days>] 

Example: 
abr repo add --name MyLocal --type local --path /mnt/backup 
abr repo add --name MyWorm --type s3 --bucket altastor-objectlock --region us-east-1 --endpoint-url http://minio.local:9000 --path-style --access-key AKIA... --secret-key ... --immutable governance --immutability-days 30 

Note: a local path that is world-writable is refused - its directory mode is the 
only thing keeping other accounts on the host away from the backup data. Restrict 
it to 750, the mode the product uses for its own storage, and add it again. 

S3 OBJECT STORAGE IMMUTABILITY 

--immutable and --immutability-days hold every new backup under S3 Object Lock; both are required together. governance can be lifted with the BypassGovernanceRetention permission, compliance by nobody at all until it expires. 

Object Lock can only be enabled when a bucket is created - never added later, and never on a bucket AltaStor provisions itself. Enabling immutability on a bucket that cannot deliver it is refused, naming the failing check. 

The lock written is longer than --immutability-days: a full must outlive the last incremental chained onto it, so AltaStor adds one full-backup cycle - the widest gap between the job's full days, or ALTASTOR_S3_IMMUTABILITY_GENERATION_DAYS when the job schedules none. --immutability-days 30 on a weekly-full job writes a 37-day lock. 

The period runs from each backup's own creation, and stored restore points keep the lock they were written with - repo apply-immutability puts them under the current setting. Retention can be lengthened, never shortened or removed. Keep the job's --retention-days at or above --immutability-days, or operational retention keeps failing until locks expire; repo add/edit warn when it is shorter. 

abr repo list 

Description: Show all configured repositories. 

abr repo list [--output json] 

abr repo info 

Description: Display repository details. 

abr repo info --name <name> [--id <id>] 

abr repo edit 

Description: Update repository settings. 

abr repo edit --name <name> [--path <path>] [--server <host/ip>] [--share <name>] [--username <user>] [--password <pass>] [--description <text>] 

For an s3 repository: [--bucket <name>] [--prefix <key-prefix>] [--region <region>] [--endpoint-url <url>] [--access-key <key>] [--secret-key <key>] [--profile <name>] [--path-style | --virtual-host-style] [--verify-tls | --no-verify-tls] [--immutable <none|governance|compliance>] [--immutability-days <days>] 

Example: 
abr repo edit --name MyWorm --immutability-days 60 
abr repo edit --name MyWorm --immutable none 

--immutability-days alone changes the period; --immutable alone switches mode and keeps the period; --immutable none turns it off. A repository holding non-immutable backups can be switched at any time - existing restore points stay unprotected until repo apply-immutability. 

abr repo remove 

Description: Remove a repository reference from management. Rule: repo remove NEVER deletes physical data, it only unlinks it from ABR. 

abr repo remove --name <name> [--id <id>] 

abr repo rescan 

Description: Refresh repository metadata and catalog state. 

abr repo rescan --name <name> [--id <id>] 

abr repo verify 

Description: Validate filesystem availability, permissions, and block consistency. Runs as a single synchronous server call (no percentage progress channel), so when the console is an interactive terminal the CLI shows an elapsed-time indicator ("Verifying repository... Ns elapsed") automatically while it waits -- no flag needed, and it's silent when output isn't a terminal (redirected/CI). Exits non-zero when the verdict is not a clean pass, so `abr repo verify --name r1 || alert ...` reacts to it. 

abr repo verify --name <name> [--id <id>] 

For an immutable s3 repository, verify also reports Object Lock state without writing anything. 

abr repo verify-immutability 

Description: Validate that an s3 repository can hold immutable backups - Object Lock, versioning and the required permissions - proven by writing a small locked test backup and confirming the storage refuses to delete it. Each check is reported by name, with the storage's own message on failure; exits non-zero unless the verdict is clean. The test backup sits under .abr-immutability-probe, locked for a day; the next run clears expired ones. 

abr repo verify-immutability --name <name> [--id <id>] 

abr repo apply-immutability 

Description: Put stored restore points under the repository's current immutability setting, counted from now. Object Lock cannot be shortened or lifted, so this cannot be undone; a storage that refuses a change is reported per restore point (MinIO refuses any mode change on a held object). Exits non-zero if any point was refused. 

abr repo apply-immutability --name <name> [--id <id>] 

INVENTORY COMMANDS 

Manage virtualization inventory sources so AltaStor can discover and manage protected objects. 

abr inventory add 

Description: Register a new virtualization inventory source. 

abr inventory add --name <name> --type <vsphere|hyper-v|proxmox-ve|xcp-ng> --address <host/ip> [--port <int>] [--username <user>] [--password <pass>] [--token <token>] [--insecure] 

Example: 
abr inventory add --name Prod-vCenter --type vsphere --address 192.168.1.10 --username administrator@vsphere.local --password 'SecretPassword' 

Note:
VM discovery is not required up front. It runs automatically the first time you
reference VM names in job create, so no manual inventory rescan is needed after
inventory add.

abr inventory list 

Description: Show all registered inventories. 

abr inventory list [--output json] 

abr inventory list-vms 

Description: List the VMs discovered in an inventory. Use the NAME or VM ID shown here as the value for --vm/--vms in job create. 

abr inventory list-vms --name <name> [--id <id>] [--filter <substring>] [--output json] 

Examples: 
abr inventory list-vms --name Prod-vCenter 
abr inventory list-vms --name Prod-vCenter --filter web --output json 

abr inventory info 

Description: Display connection properties and discovery metadata. 

abr inventory info --name <name> [--id <id>] 

abr inventory edit 

Description: Update server connection attributes or credentials. 

abr inventory edit --name <name> [--address <host/ip>] [--port <int>] [--username <user>] [--password <pass>] [--token <token>] [--insecure] 

abr inventory remove 

Description: Remove an inventory source from management. 

abr inventory remove --name <name> [--id <id>] 

abr inventory rescan 

Description: Refresh the discovered object catalog. 

abr inventory rescan --name <name> [--id <id>] 

Note:
Job create discovers VMs automatically when needed; run this to re-discover the
object catalog on demand.

abr inventory test 

Description: Validate connectivity and authentication. 

abr inventory test --name <name> [--id <id>] 

JOB COMMANDS 

Jobs bind protected workloads (VMs) to a repository. 

abr job create 

Description: Create a backup job for one or more virtual machines and optionally attach an enabled or disabled schedule. Supported schedule types are daily, weekly, monthly, and periodic. 

abr job create --name <name> (--repository <name> | --repository-id <id>) [--inventory <name> | --inventory-id <id>] (--vm <vm> ... | --vms "<vm1,vm2,...>") [--schedule <daily|weekly|monthly|periodic>] [--time <HH:MM>] [--days <mon,tue,...>] [--day-of-month <1..31>] [--every <15m|1h|6h|1d>] [--enabled | --disabled] [--description <text>] [--retention-days <int>] 

Examples: 
abr job create --name "Production VMs" --inventory "vcenter-main" --vm "vm-101" --vm "vm-102" --repository "repo-local-01" --schedule daily --time "23:00" --enabled 

abr job create --name "Weekly Backup" --inventory "vcenter-main" --vm "db-01" --repository "repo-01" --schedule weekly --days mon,wed,fri --time "23:00" 

abr job create --name "Monthly Backup" --inventory "vcenter-main" --vm "vm-01" --repository "repo-01" --schedule monthly --day-of-month 1 --time "01:00" 

abr job create --name "Periodic Backup" --inventory "vcenter-main" --vm "critical-01" --repository "repo-01" --schedule periodic --every 6h --disabled 
 

Notes & Behavior: 
--retention-days = operational rolling retention affecting non-GFS points 
--gfs-* = extended lifecycle marks enforced during automated block evaluation. 

abr job create-copy 

Description: Clone an existing job configuration. The copied job receives a new ID and name while preserving inventory, selected VMs, repository, schedule configuration, schedule enabled/disabled state, and existing backup options. Optional repository, schedule, and retention overrides are still accepted for compatibility. 

abr job create-copy --name <name> (--source <name> | --source-id <id>) [--repository <name> | --repository-id <id>] [--schedule <daily|weekly|monthly|periodic>] [--time <HH:MM>] [--days <mon,tue,...>] [--day-of-month <1..31>] [--every <15m|1h|6h|1d>] [--enabled | --disabled] 

Example: 
abr job create-copy --name "Copied Job" --source "Existing Job" 

abr job list 

Description: Show all configured jobs, including inventory, repository, VM count, schedule summary, schedule state, and run timestamps when available. 

abr job list [--output json] 

abr job info 

Description: Display detailed job bindings, tracked virtual machines, schedule configuration, schedule state, execution policy references, and timestamps. 

abr job info --name <name> [--id <id>] 

abr job edit 

Description: Update job configuration binding, scheduling, schedule state, or protected targets. 

abr job edit --name <name> [--vm <vm> ... | --vms "<vm1,vm2,...>"] [--repository <repo_name>] [--inventory <inventory_name>] [--schedule <daily|weekly|monthly|periodic>] [--time <HH:MM>] [--days <mon,tue,...>] [--day-of-month <1..31>] [--every <15m|1h|6h|1d>] [--enabled | --disabled] [--description <text>] [--retention-days <int>] 

abr job delete 

Description: Delete a job record. 

abr job delete --name <name> [--id <id>] [--yes] 

abr job start 

Description: Start a job immediately. The engine resolves execution type (Incremental, Synthetic Full, Active Full) automatically based on today's day-of-week match against the parameters configured inside the job. 

abr job start --name <name> [--id <id>] [--full | --synthetic] [--target-repo <name>] [--wait] 

Notes: 
• First-run rule: The very first execution of a job always produces a Full backup, regardless of scheduled execution windows. 
• --full forces an immediate Active Full execution regardless of previous restore points or scheduling rules. `--force-full` is accepted as a backward-compatible alias. 
• --synthetic forces a Synthetic Full: the full backup is built by merging the chain already in the repository, so the VM is never read and no snapshot is taken. It needs a full backup followed by at least one incremental, and a repository that stores the .abr format (local or s3). AltaStor takes the chain order from its own catalog, so the ordering hazard the storage tool warns about does not apply here. The chain is only read - the restore points it merged are left in place for retention to age out. --full and --synthetic are mutually exclusive. 
• --target-repo writes the merged full into another repository instead of the job's own, so a chain in one repository can be collapsed into a standalone full in another (local and s3 in any combination). The chain itself is only read, and the copy lands as the first restore point of its own chain there - the job keeps writing to its own repository. Only valid with --synthetic. 
• A run refused because the chain cannot be merged (no incrementals, a full in the middle, disks that do not match) exits 6, separately from 4 used when a run fails while it is already under way. 
• Priority notice: If the current day matches both synthetic and active targets in the job parameters, Active Full always takes priority. 
• Repository capability notice: If today is a job-designated Synthetic Full day, but the target repository does not natively support synthesis, the engine transparently escalates the run execution to an Active Full. nfs and smb repositories are in that group; local and s3 both merge natively. 
• --wait follows the started session's real progress (abr-vmdk-tool's own percent per disk) on stderr as a live bar until it finishes, then prints the final session state; stdout stays clean for scripting either way. If the job starts more than one session at once, --wait refuses to guess which one to follow and instead prints all of their session IDs so you can watch each with `abr session info --id <id> --watch`. Without --wait, the command returns immediately with the queued session(s), same as before this flag existed. If the followed session does not end completed (failed/stopped/canceled), the command exits with the same non-zero code used elsewhere for an unhealthy state, so `abr job start --wait || alert ...` reacts to the run's real outcome instead of always seeing exit 0. 

SESSION COMMANDS 

Monitor runtime execution state, logs, and telemetry. 

abr session list 

Description: Show active and historical sessions. 

abr session list [--job <name>] [--output json] 

abr session info 

Description: Display detailed session telemetry. 

abr session info --id <id> [--watch] 

Notes: 
• --watch follows the session's real progress on stderr as a live bar (redrawn in place on an interactive terminal; printed as one line every 5s otherwise) until it reaches a terminal state (completed/failed/stopped/canceled), then prints the final session details, same as a plain `session info` would. Useful for reattaching to a session already started elsewhere (e.g. without `job start --wait`, or after reconnecting to the terminal). If the session it followed did not end completed, the command exits non-zero for the same reason --wait does above -- a plain `session info` (no --watch) always exits 0 regardless of the session's state, since it is just reporting whatever is in the catalog rather than waiting on an outcome. 

abr session log 

Description: Show logs for a session. 

abr session log --id <id> 

abr session stop 

Description: Stop a running session. A stopped backup discards the data it had written; a stopped restore removes the VM or disk it had created. 

abr session stop --id <id> [--force] 

BACKUP COMMANDS 

Physical storage-oriented layer focused on chains, blocks, and stored restore points. 

abr backup list 

Description: Show stored backup chains and sizes. 

abr backup list [--repo <name>] [--output json] 

abr backup info 

Description: Display chain metadata for a single backup identifier. 

abr backup info --id <id> 

abr backup delete 

Description: Delete a backup chain from the repository.  

abr backup delete --id <id> [--force] 

Behavior: 
• Safety enforcement: If deletion would orphan dependent increments, the command fails with a dependency protection error regardless of --force. If deletion would violate GFS protection, the command fails with a dependency protection error unless --force is given. 
• --force: deliberately deletes a chain that carries an active GFS archival mark, discarding data the GFS policy marked for retention. This is the supported way to retire a repository whose only remaining chains are GFS-protected -- without it, such a chain (and the repository holding it) can never be removed. 
• Executing this command physically and permanently deletes data blocks from the storage. 
 

abr backup verify 

Description: Verify backup chain readability, logical dependency, and integrity. Exits non-zero unless the result is a clean "valid", so `abr backup verify --id <id> || alert ...` reacts to a "warning" or "invalid" verdict instead of always seeing exit 0. 

abr backup verify --id <id> 

abr backup import 

Description: Import a backup chain from an external path. 

abr backup import --path <path> --repo <name> 

RESTORE COMMANDS 

Logical recovery orchestration to provision data back into working environments. 

abr restore list 

Description: Show restore points that can be used for VM recovery. 

abr restore list [--vm <name-or-id>] [--backup <backup-id>] [--inventory <name-or-id>] [--output json] 

Note: 
This command returns stable restore-point records including point ID, backup ID, VM ID/name, inventory, repository, time, type, and status. Empty results return success with `items: []`. 

abr restore point list 

Description: Show restore points for a specific VM using its unique identifier. 

abr restore point list --vm-id <id> [--output json] 

Example: 
abr restore point list --vm-id vm-28491-prod 

abr restore start 

Description: Start a full VM restore workflow from a selected restore point or backup chain. 

abr restore start --restore-point <id> --target-inventory <name-or-id> --target-host <host-or-id> --datastore <name-or-id> --new-name <name> [--network-map <src=dst>] [--power-on] [--wait] 

Alternate source resolution:
abr restore start --backup <backup-id> [--vm <name-or-id>] --target-inventory <name-or-id> --target-host <host-or-id> --datastore <name-or-id> --new-name <name> [--network-map <src=dst>] [--power-on] [--wait] 

Legacy compatibility:
abr restore start --point-id <id> [--name <new-name>] [--target-inventory <name>] [--target-host <host>] [--target-datastore <name>] [--network-map <src=dst>] [--repo <name>] [--power-on] [--in-place] [--force] [--wait] 

Targeted Restore Scenarios: 
 
1. In-Place Recovery (Replace the original VM) 
Description: The original VM is damaged or unavailable, and the backup must be restored over the existing production object, at its own original host, datastore, and name. 
Syntax: abr restore start --point-id <id> --in-place --force [--power-on] 
Example: abr restore start --point-id rp-2026-05-16-0200-f1 --in-place --force --power-on 

--in-place selects this mode explicitly. It cannot be combined with --target-inventory/--target-host/--datastore/--new-name -- in-place restore always uses the source VM's own placement, never a caller-supplied one. It can be combined with --repo for a backup-copy source (scenario 3 below). 

--force authorizes the overwrite once --in-place has been selected; it has no effect and no meaning without --in-place. Omitting --in-place leaves the flag rejected outright, since there is nothing for it to authorize. 

Requires that the backup captured the VM's original host and datastore -- if the source VM's placement is unknown, in-place restore is refused with restore_mode_unsupported. 
 
2. Out-of-Place / Advanced DR (Restore as a new VM on another host) 
Description: A duplicate VM is deployed on a secondary host or recovery site for testing or disaster recovery. 
Syntax: abr restore start --restore-point <id> --new-name <new-name> --target-inventory <name> --target-host <host> --datastore <name> [--network-map <src=dst>] [--power-on] 
Example: abr restore start --restore-point rp-2026-05-16-0200-f1 --new-name VM-SQL-01-DR --target-inventory DR-Inventory --target-host esxi-dr-01.company.local --datastore DR-SSD --network-map Production=DR-Network --power-on 

JSON output:
- `abr --output json restore list` returns `{ "items": [...] }`
- `abr --output json restore start ...` returns the queued restore session ID plus source and target placement details

Tracking progress:
- Pass --wait to follow the restore's real progress (abr-vmdk-tool's own percent per disk) on stderr as a live bar until it finishes, then print the final session state -- no separate command needed. Exits non-zero if the restore session does not end completed, same as `job start --wait`.
- Without --wait: use `abr session list`, then `abr session info --id <restore-session-id> --watch` to attach to it afterward.
- Use `abr session log --id <restore-session-id>`
 
3. Restore from Backup Copy (From remote / disaster storage) 
Description: The local backup repository is lost, so recovery is performed directly from an external or cloud backup copy repository. 
Syntax: abr restore start --point-id <id> --repo <name> --target-inventory <name> --target-datastore <name> [--power-on] 
Example: abr restore start --point-id rp-2026-05-16-0200-f1 --repo My-NFS --target-inventory DR-Inventory --target-datastore DR-SSD --power-on 

SYSTEM COMMANDS 

Inspect health and version state. 

abr system version 

Description: Show the installed product version. 

abr system version 

abr system health 

Description: Show component health status. 

abr system health [--output json] 

abr system export 

Description: Create an encrypted portable snapshot of server`s state (backups, repositories, inventories, jobs, settings).

abr system export --file <path> 

Example: 
abr system export --file /tmp/test.adrx

The passphrase comes from ABR_EXPORT_PASSWORD, or is asked for interactively, and must be at least 8 characters - the same minimum the product enforces for user passwords. It is the only thing protecting the credentials and server settings inside the archive, and it cannot be recovered. A shorter one is refused before anything is written. 

abr system import 

Description: Restore DB catalog + configuration from a previously exported system archive.

abr system import --file <path> 

Example: 
abr system import --file /tmp/test.adrx

abr system get-support-bundle 

Description: Collect logs, sanitized configuration, and system info into a local ZIP for troubleshooting. Passwords/secrets are redacted.

abr system get-support-bundle [--file <path>] 

Example: 
abr system get-support-bundle --file /tmp/support-bundle.zip

Bundle contents:
logs: logs/server/server.log, logs/cli/cli.log, logs/mongodb/mongod.log
config: server.conf, mongod.conf
dumps: logs/server/server.log.fault (if server crashed)
catalog (from DB, as JSON): sessions.json (200 most recent), jobs.json, repositories.json, inventories.json, restore-points.json, health.json
system-info.json (OS/host/CPU/RAM/disk)
manifest.json (file list + warnings)

Anything missing is skipped and the reason is recorded in manifest.json warnings.

--- END OF SPECIFICATION v4.1 --- 
