ABR CLI Command Reference v4.1 

AltaStor Backup & Replication CLI Specification (Job-Centric Schedule Shift) 

OVERVIEW 

abr is the command-line interface for configuring, executing, inspecting, and maintaining backup and recovery workflows. This edition implements a structural design: 

job: defines WHAT to protect (Virtual Machines), WHEN to execute (base scheduling heartbeats), HOW LONG to keep data (rolling operational retention and GFS archival milestones, and explicit rules for Full backup generations (Active or Synthetic Full windows). 

Semantic Model 

auth = session login/logout against the REST API 

user = accounts allowed to authenticate, each holding one role 

role = built-in roles and the functions each allows 

audit = the record of every command and user or role change 

notification = e-mail notifications (SMTP server, sign-in, recipients, events, deliveries) and the notification script 

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
├─ role 
├─ audit 
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

Description: Log in and cache a session token. Prompts for the password (or reads ABR_PASSWORD); --name falls back to ABR_USERNAME. An account with two-factor sign-in is then asked for the 6-digit code from its authenticator app, at a prompt that does not echo it; the code is never taken from a flag or an environment variable, which a shell keeps in its history. An invalid, expired or already used code exits with code 5. Recovery codes, and setting up or turning off two-factor sign-in, are in the Web UI. 

abr auth login [--name <name>] [--print-token] 

Examples: 
abr auth login --name abr 
ABR_USERNAME=abr ABR_PASSWORD='secret' abr auth login 

--print-token: print only the token to stdout, for `export ABR_TOKEN=$(abr auth login --print-token)`. The session the CLI has cached is left as it was, so fetching a token for another account does not sign the CLI in as that account. 

After signing in, a user who may change inventories (inventory.edit) gets a NOTE on stderr while nobody has set the server-wide vSphere settings yet (confirmed: false in abr settings vsphere show): it names the API and VDDK library the server started with and points to abr settings vsphere set, which confirms them. It is not printed while the account is on its default password, nor with --print-token. 

abr auth logout 

Description: Invalidate the current session and clear the cached token. 

abr auth logout 

abr auth change-password 

Description: Change your own password. Prompts for the current and new password. The new password follows the same rules as for a new user and must differ from the current one. Invalidates your other sessions; this one stays valid. 

abr auth change-password 

Note: 
Always acts on the current session's account, never another user's. abr auth login warns if the account is still on its default password. 

abr auth whoami 

Description: Show the signed-in user, their role, whether two-factor sign-in is on, and the function IDs that role allows. 

abr auth whoami [--output json] 

PERMISSIONS 

Every command runs one product function, named by a function ID such as job.start or backup.delete, and each command's --help ends with the one it needs ("Permission: job.start"). The server checks the signed-in user's role before it runs anything, for the CLI, --output json automation, the Web UI and direct API calls alike; see doc/RBAC.md for the roles and what each allows. 

A command the role does not allow is not run, and the CLI exits with code 7: 

Error: Permission denied: The Auditor role does not allow job.start (start a job). 

With --output json, stderr carries the machine-readable error: 

{"error": {"code": "permission_denied", "message": "...", "details": {"function_id": "job.start", "role": "auditor", "reason": "not_permitted"}}} 

A disabled account, or one without a valid role, cannot sign in (exit 7, code account_disabled or role_invalid). 

USER COMMANDS 

Manage accounts allowed to authenticate. Requires an existing session. The first user is created at startup from ABR_BOOTSTRAP_ADMIN_USER / ABR_BOOTSTRAP_ADMIN_PASSWORD, not via this command, and holds the administrator role. Every user holds exactly one role: administrator, backup_administrator, backup_operator, restore_operator or auditor. 

At least one enabled Administrator always remains: the last one cannot be demoted, disabled or deleted (exit 4, code last_administrator), even when two Administrators change each other at the same moment: the later change is refused. 

abr user add 

Description: Add a new user with a role. Prompts for the password; there is no --password flag. The name is 1 to 64 Latin letters, numbers, spaces, dots, underscores or hyphens and starts with a letter or number. The password is 8 to 128 characters, not only spaces, and not the user name. 

abr user add --name <name> --role <role> [--full-name <text>] 

Example: 
abr user add --name alice --full-name "Alice Doe" --role backup_operator 

abr user edit 

Description: Change a user's login name, full name, role, enabled state, or password. A role change applies to the user's next request. A new login name keeps the user's password and sessions. --reset-password prompts for a new password and ends the user's sessions; your own password is changed with abr auth change-password, which asks for the current one. Disabling a user ends their sessions and stops them signing in. 

abr user edit --name <name> [--id <id>] [--new-name <name>] [--reset-password] [--role <role>] [--full-name <text>] [--enable | --disable] 

Examples: 
abr user edit --name alice --role restore_operator 
abr user edit --name alice --new-name alice.doe 
abr user edit --name alice --reset-password 
abr user edit --name alice --disable 

abr user info 

Description: Show one user, with their role and whether they are enabled. 

abr user info --name <name> [--id <id>] 

abr user list 

Description: Show all users with their roles. Never includes password hashes. 

abr user list [--output json] 

abr user delete 

Description: Delete a user and invalidate their sessions. 

abr user delete --name <name> [--id <id>] 

ROLE COMMANDS 

The roles are built in and cannot be created, changed or deleted. 

abr role list 

Description: Show the roles, with how many functions each allows. 

abr role list [--output json] 

abr role info 

Description: Show a role's permission, allowed or disabled, on every function. A function a role does not list is disabled. 

abr role info --id <role-id> | --name <role-name> 

Examples: 
abr role info --id backup_operator 
abr role info --name "Restore Operator" 

abr role functions 

Description: Show every product function by function ID, with its category (read, operate, configure, destructive, administer, self-service). 

abr role functions [--output json] 

AUDIT COMMANDS 

abr audit list 

Description: Show the audit log, newest first: the time (UTC), the user and the role they held then, the function ID, the target, the result (allowed, denied or failed), the session the command started, and the interface it came from (cli, web or api). Every command from any interface is recorded, and every user or role change; reads the Web UI repeats in the background are not. A run the server starts on its own is recorded too, as user system with interface scheduler: a scheduled backup, copy or replication run (job.start), a backup copy or replication that follows another job, and each session of a scheduled verify (backup.verify, with the job in details). Its details say what started it (trigger: schedule, immediate, after-job, after-source, copy-backlog or copy-window) and, for a schedule, which one fired (schedule: Incremental, ActiveFull or SyntheticFull). A scheduled backup copy that has no source left is recorded as failed, with the reason. Passwords, keys and tokens are masked before a record is written. 

abr audit list [--user <name>] [--function <function-id | area.>] [--result allowed|denied|failed] [--since <iso>] [--until <iso>] [--limit <n>] [--offset <n>] 

Examples: 
abr audit list --result denied 
abr audit list --user alice --since 2026-10-01 
abr audit list --user system --function job.start 
abr --output json audit list --function job. --limit 500 

NOTIFICATION COMMANDS 

E-mail notifications: the SMTP server, how the appliance signs in to it (user name and password, or OAuth 2.0), the sender and recipients, and the events that send e-mail; and the script the appliance can run on events. The Web UI's Settings > Notifications changes the same settings. See doc/Notifications.md. 

abr notification show 

Description: Show the settings (secrets only as set or not set), every event with whether it sends e-mail, and the last delivery. 

abr notification show [--output json] 

abr notification set 

Description: Change the settings; options left out keep their value. The server refuses the whole change, naming every problem by field, when a value is invalid or when notifications are on and a setting delivery needs is missing (exit 2). A secret flag without a value reads ABR_SMTP_PASSWORD, ABR_SMTP_OAUTH_CLIENT_SECRET or ABR_SMTP_OAUTH_REFRESH_TOKEN, or prompts; an empty value removes the stored secret. The sender name (--from-name) is one line of at most 100 characters; --smtp-host is a host name of at most 253 characters or an IP address. 

abr notification set [--enable | --disable] [--smtp-host <host>] [--smtp-port <port>] [--security starttls|ssl|none] [--verify-tls | --no-verify-tls] [--timeout <seconds>] [--auth password|oauth2|none] [--username <user-or-mailbox>] [--password [<pass>]] [--oauth-provider microsoft|google|custom] [--oauth-tenant <tenant>] [--oauth-client-id <id>] [--oauth-client-secret [<secret>]] [--oauth-grant client_credentials|refresh_token] [--oauth-refresh-token [<token>]] [--oauth-token-url <url>] [--oauth-scope <scope>] [--from <address>] [--from-name <name>] [--recipients <a,b> | --add-recipient <a> --remove-recipient <b>] [--space-threshold <percent>] 

Examples: 
abr notification set --smtp-host smtp.example.com --smtp-port 587 --security starttls --auth password --username backup@example.com --password --from backup@example.com --recipients ops@example.com --enable 
abr notification set --auth oauth2 --oauth-provider microsoft --oauth-tenant contoso.onmicrosoft.com --oauth-client-id 0f1e... --oauth-client-secret --username backup@contoso.com 
abr notification set --disable 

abr notification test 

Description: Send a test e-mail now and report whether the SMTP server took it, or why not: smtp_connection_failed, smtp_tls_failed, smtp_auth_failed, oauth_token_failed, smtp_sender_refused, smtp_recipients_refused or smtp_error, with the stage it reached (exit 1). The set options test a change without saving it. --to sends it to those addresses instead of the recipients, also while no recipients are saved; without --to at least one recipient must be saved. 

abr notification test [--to <addresses>] [any abr notification set option except --enable, --disable and the recipient options] 

abr notification history 

Description: The latest 50 deliveries, newest first: event, status (queued, retrying, sent, failed, cancelled), attempts, recipients and the error. Times are UTC. A delivery still queued or retrying when the server restarts is taken up again when it starts; one left waiting by an earlier version ends failed (interrupted). 

abr notification events list 

Description: The events that can send e-mail (job.failed, job.warning, job.succeeded, restore.failed, restore.succeeded, repository.low_space, system.clock_jump, tls.certificate_expiring), whether each does, and its default. 

abr notification events set --enable <ids> --disable <ids> 

Examples: 
abr notification events set --enable job.succeeded,restore.succeeded --disable repository.low_space 

abr notification script show 

Description: Show the uploaded script (name, size, SHA-256, who uploaded it), whether it runs, on which events, and its latest runs. 

abr notification script show [--output json] 

abr notification script upload 

Description: Upload the script the appliance runs on events, in place of any earlier one: text of at most 256 KiB starting with a #! line naming an interpreter the appliance has. It runs on the appliance as the service user, so it can do whatever that user can. Uploading does not switch it on. 

abr notification script upload --file <path> 

abr notification script content 

Description: Print the stored script. Reading it is recorded in the audit log. 

abr notification script set 

Description: Change whether the script runs, on which events (chosen apart from those that send e-mail) and its time limit. 

abr notification script set [--enable | --disable] [--timeout <seconds>] [--event-on <ids>] [--event-off <ids>] 

abr notification script remove 

Description: Delete the script, which also stops it running. 

abr notification script test 

Description: Run the script now on a sample event, whether or not it is switched on, and print how it went. Exits 1 when the script fails. 

REPOSITORY COMMANDS 

Configure and manage backup storage targets for chains, metadata, and repository state. 

abr repo add 

Description: Create a backup repository. 

abr repo add --name <name> --type <local|nfs|smb|s3> [--path <path>] [--server <host/ip>] [--share <name>] [--username <user>] [--password [<pass>]] [--description <text>] [--no-share] 

For --type s3: [--bucket <name>] [--prefix <key-prefix>] [--region <region>] [--endpoint-url <url>] [--access-key <key>] [--secret-key [<key>]] [--profile <name>] [--path-style | --virtual-host-style] [--verify-tls | --no-verify-tls] [--immutable <none|governance|compliance>] [--immutability-days <days>] [--immutability-retention <job|minimum>] [--create-bucket] 

The SMB password comes from ABR_REPO_PASSWORD and the S3 secret key from ABR_REPO_SECRET_KEY, or is asked for interactively. A value typed after --password or --secret-key is still accepted, but every local user can read it (ps, /proc) while the command runs, and it stays in the shell history. The server stores both encrypted with its credential key (abr-enc:v1:, as inventory passwords) and never returns them: the API and repo info show only has_secret_key for S3. Without a usable credential key, repo add and repo edit refuse a new or changed password or secret key (503 credential_key_unavailable) and store nothing. 

An smb repository without --path is a share this server mounts itself (managed mode): --address `\\server\share` or `\\server\share\folder` (or --server and --share) names it, --username (user, `DOMAIN\user` or `user@domain`) and --password are the account it is mounted with, and the same account is the one Hyper-V hosts open it with. The server asks the mounts broker (altastor-backup-mounts.socket, run as root for each request) to mount it at /run/altastor-backup-mounts/<repository id>, with SMB 3.1.1 and encryption (seal), as the service user, and never writes the password to disk: it is kept encrypted in the catalog and handed to mount.cifs through a file descriptor. --username and --password are required, since an encrypted session needs an account; a domain may only hold Latin letters, digits, dots, - and _. A share that cannot be mounted is refused with the reason (for example a wrong user name or password, a folder the share does not have, a server that does not answer) and nothing is added. The share stays mounted while the server restarts. When the server starts, a share still mounted from the same address is kept, one mounted from another address is mounted again, and a missing one is mounted in the background, again after 30 s, 1, 2, 5, 10 and then every 15 minutes; until then repo list and repo info show the repository unavailable with the reason, and a backup or restore that needs it tries to mount it first. While it is not mounted its folder belongs to root and cannot be written to, so nothing lands on the appliance's own disk. Removing the server package unmounts every such share (shares in use are detached once nothing uses them); installing it again mounts them again from the catalog. An smb repository with --path is an SMB share for Hyper-V hosts: --address `\\server\share` (or --server and --share) names it as the hosts reach it, --path is the folder this appliance mounts the same share at, and --username and --password are the account the hosts open it with (the inventory's when there is none). --username without --password, and --password without --username, are refused. repo add, repo edit and repo verify check that --path is that share: an SMB (cifs) mount of `//server/share`, or a folder this appliance itself shares under that name in Samba. A folder on the appliance's own disk, or a mount of another share or server, is refused with what is mounted there instead; an NFS export of the same server is accepted with a warning, since it cannot be matched to the share. POST /api/v1/repositories/test-smb runs the same check without adding the repository. The account is checked where it is used: when the share, --username or --password is set or changed (repo add, repo edit, POST /api/v1/repositories/test-smb), a Hyper-V host (one whose jobs write to the repository first) opens the share with it, and an account or password the host refuses is refused with the host's reason. When no Hyper-V inventory is registered, or no host answers within ALTASTOR_SMB_ACCOUNT_CHECK_SECONDS (20 s by default), the change is saved with a warning that the account was not checked. Adding or editing a repository does not read its stored backups; repo verify does. The same check runs while the repository is in use: when the share is no longer mounted at --path (for example the mount did not come back after a reboot, so the folder is an empty one on the appliance's own disk), repo list and repo info show the repository unavailable with that reason and no capacity, and nothing runs on it. Backups, backup copies, synthetic fulls and restores fail at once with a message that names the share and the folder; backup delete and repo rescan are refused (409 repository_share_not_mounted), so restore points stay in the catalog and are not marked missing; job retention leaves those chains alone and exits non-zero naming the cause; restore points waiting for deletion wait. Once the share is mounted again, everything goes on as before. A local repository is a folder of the appliance and takes no --server or --share; the server shares it over SMB itself right away, as abr repo share add does, so Hyper-V hosts can back up into it, and prints the share and its account. --no-share adds it unshared. When the share cannot be made (no Samba, the shares broker is not running, a folder that does not belong to the service user), the repository is added unshared with a warning that gives the reason; share it later with abr repo share add. 

Example: 
abr repo add --name MyLocal --type local --path /mnt/backup 
ABR_REPO_PASSWORD='...' abr repo add --name MyNas --type smb --address '\\\\nas\\backups\\vm' --username 'backup@corp.local' --password 
abr repo add --name MySmb --type smb --path /mnt/hv --address '\\\\backup01\\hv' --username 'CORP\\backupsvc' --password 
ABR_REPO_SECRET_KEY='...' abr repo add --name MyWorm --type s3 --bucket altastor-objectlock --region us-east-1 --endpoint-url http://minio.local:9000 --path-style --access-key AKIA... --immutable governance --immutability-days 30 

Note: a local path that is world-writable is refused - its directory mode is the 
only thing keeping other accounts on the host away from the backup data. Restrict 
it to 750, the mode the product uses for its own storage, and add it again. 

A repository is checked against its storage before it is registered, and refused if it 
cannot be used: the endpoint has to answer, the credentials have to authenticate, and the 
target has to survive a write, a read-back and a delete. A local path has to exist and be 
readable and writable by the service account. Nothing is created when a check fails, and 
the outcome is kept on the repository - repo list and repo info report a repository whose 
last check failed as unavailable rather than as healthy, until repo verify passes again. 

--create-bucket creates the S3 bucket when it does not exist; without it a missing bucket 
is refused. Object Lock can only be turned on while a bucket is being created and --create-bucket 
does not turn it on, so a bucket created this way can never hold immutable backups, and 
--create-bucket is refused together with --immutable. Outside of --create-bucket a bucket is 
never created: a backup written to a repository whose bucket has disappeared fails instead of 
silently starting a new empty one. 

S3 OBJECT STORAGE IMMUTABILITY 

--immutable and --immutability-days hold every new backup under S3 Object Lock; both are required together. governance can be lifted with the BypassGovernanceRetention permission, compliance by nobody at all until it expires. 

Object Lock can only be enabled when a bucket is created - never added later, and never on a bucket AltaStor provisions itself. Enabling immutability on a bucket that cannot deliver it is refused, naming the failing check. 

The lock a backup is written under is derived from the job, not from --immutability-days 
alone. A restore point stays valid only while the rest of its chain does, so the lock has to 
reach the moment the whole chain stops being needed: the next full backup, plus the job's 
--retention-days. Every point of one chain therefore lands on the same date, and the chain is 
freed in a single pass. --immutability-days is the floor under that figure, for an environment 
with a fixed minimum of its own; it is not added to it. --immutability-days must be between 1 
and 36500. 

A job with weekly full backups keeping 30 days writes a 37-day lock on the day of the full and 
a 31-day lock on the day before the next one - both expiring on the same date. With monthly 
full backups the same job writes 61 days, because the chain genuinely lives that long. 

The lock is only ever recalculated when the answer grows: a full backup that did not run when 
it was due, a longer --retention-days, a changed full-backup schedule, or a raised 
--immutability-days. A job running to schedule with settings left alone never has its stored 
locks rewritten. ALTASTOR_S3_BLOCK_GENERATION_DAYS is the fallback used in place of the cycle 
when the date of the next full backup cannot be determined. 

--immutability-retention decides what the period counts from. job, the default, uses the 
figure above; minimum ignores both the job retention and its cycle and uses --immutability-days 
alone. A job keeping 13 days with weekly full backups on a repository set to 7 writes a 20-day 
lock under job, and a 7-day lock under minimum. 

The period runs from each backup's own creation, and stored restore points keep the lock they were written with - repo apply-immutability puts them under the current setting. Retention can be lengthened, never shortened or removed. An --immutability-days longer than the job's --retention-days locks data past the point the job still needs it and is paid for either way; repo add/edit warn when that happens. 

abr repo share 

Description: Share a local repository's folder over SMB from this server, for example so Hyper-V hosts can back up into it, and list, check or remove those shares. add makes a hidden share (abr-xxxxxxxx$) open only to a Samba account the server generates (abr-smb-xxxxxxxx); its password is kept encrypted and is never printed, and the repository is an SMB repository from then on, whose share, account and password cannot be edited by hand. del removes the share and the account and keeps the backups; it is refused while Hyper-V jobs back up into the repository. add and del are refused while a session uses the repository. show lists every local repository with its share (\\<server>\<share>) or none, by the server Hyper-V hosts are given for the share: the name set in ABR_SHARES_SERVER_NAME (in the server's environment; every Hyper-V host and this server must resolve it), else this server's address toward the first Hyper-V inventory it has a route to, or the address of its default route, so that a host that cannot resolve the server's name still opens the path. abr repo add and abr repo info show the same server. A value of ABR_SHARES_SERVER_NAME that is not a host name or an address is refused with 409 invalid_share_server_name, and Hyper-V backups and restores into the share fail with the same reason. check writes, reads back and deletes a test file through that path with the share's account. abr repo remove of a shared repository removes its share too. A local repository is shared when it is added (abr repo add without --no-share), the Initial Repo included. The server never runs as root: the Samba side is done by the shares broker, which systemd starts as root for each request (altastor-backup-shares.socket); it refuses a folder that does not belong to the service user, an Active Directory domain controller, a registry configuration, a passdb other than tdbsam, and an smbd that is not running, which it never starts. When the server starts it puts back any share the catalog records but Samba no longer has (after the package was installed again, or the section was removed by hand) with the same name, account and password; a share is closed for good only with abr repo share del. When add fails after the account was made, the Samba configuration is put back and the account removed from Samba (pdbedit, else smbpasswd) and from the system; if Samba does not let it go, the error says the account is left, and its system account is kept so that it can still be removed. del removes the share even then and logs the account it could not remove. Removing the package closes every share and keeps the accounts. Without the broker the command exits non-zero with 503 repository_share_unavailable; a refusal is 409 (exit 4). Permissions: show repository.view, add and del repository.edit, check repository.verify. 

abr repo share show [--output json] 
abr repo share add --name <name> | --id <id> 
abr repo share del --name <name> | --id <id> 
abr repo share check --name <name> | --id <id> 

Examples: 
abr repo share show 
abr repo share add --name "Local-Repo-1" 
abr repo share check --name "Local-Repo-1" 
abr repo share del --name "Local-Repo-1" 

abr repo list 

Description: Show all configured repositories. 

abr repo list [--output json] 

abr repo info 

Description: Display repository details. 

abr repo info --name <name> [--id <id>] 

abr repo edit 

Description: Update repository settings. 

abr repo edit --name <name> [--path <path>] [--server <host/ip>] [--share <name>] [--username <user>] [--password [<pass>]] [--description <text>] [--default | --no-default] 

--default makes the repository the default one, where abr job create stores when neither --repository nor --repository-id is given; the previous default stops being it. --no-default leaves no default. At most one repository is the default; abr repo list shows which. The server makes the Initial Repo the default when it creates it and no other repository is. 

For an smb repository the server mounts itself, a new --address, --username or --password is first mounted at a separate point (/run/altastor-backup-mounts/<repository id>.trial); only when that works is the share unmounted and mounted again with the new settings, so a typo leaves the repository as it was. A new password that cannot be stored (503 credential_key_unavailable) is refused before the share is touched, and so is a new managed repository. When the edit is refused after the swap (for example an account a Hyper-V host refuses), the share is mounted again with the settings the catalog keeps; if those no longer work, the repository is unavailable with the reason and the server keeps trying. The change is refused while sessions use the repository (409), while the share is in use (409 repository_share_busy), and when the repository holds backups and none of them is on the new share (409 repository_share_mismatch), since the address would then point at other data. Such a repository takes no --path. abr repo remove unmounts its share, and is refused while the share is in use (409 repository_share_busy). For an smb repository, --address or --server and --share change the share Hyper-V hosts reach it by, which it cannot be without, and --username and --password the account that opens it; --username "" leaves that to the inventory's account. A new --username needs its own --password (the stored password belongs to the old account and is not carried over), and --password needs an account; --password alone changes the password of the current account. --path may be changed after the repository is added, but only while it holds no backups (otherwise 409 repository_has_backups), not to a folder inside or above another repository's (409 repository_path_overlaps), and only to a folder that passes the same share check as repo add. A local repository takes no share. 

For an s3 repository: [--bucket <name>] [--prefix <key-prefix>] [--region <region>] [--endpoint-url <url>] [--access-key <key>] [--secret-key [<key>]] [--profile <name>] [--path-style | --virtual-host-style] [--verify-tls | --no-verify-tls] [--immutable <none|governance|compliance>] [--immutability-days <days>] [--immutability-retention <job|minimum>] 

Example: 
abr repo edit --name MyWorm --immutability-days 60 
abr repo edit --name MyWorm --immutable none 

--immutability-days alone changes the period; --immutable alone switches mode and keeps the period; --immutability-retention alone switches what the period counts from; --immutable none turns it off. A repository holding non-immutable backups can be switched at any time - existing restore points stay unprotected until repo apply-immutability. 

abr repo remove 

Description: Remove a repository reference from management. Rule: repo remove NEVER deletes physical data, it only unlinks it from ABR. It is refused while a job writes into the repository (409 repository_in_use), while backup chains are in it (409 repository_has_backups), and while a backup copy job reads from it (--source-repo; 409 backup_copy_source_in_use, naming those copy jobs): take it out of their sources with abr job edit --source-repo, or delete them first. 

abr repo remove --name <name> [--id <id>] 

abr repo rescan 

Description: Refresh repository metadata and catalog state. A restore point whose stored data is gone is marked `missing`, and every restore point that builds on it is marked `chain_broken`; restore start refuses both. Once the data is back, the next rescan marks them `available` again. 

abr repo rescan --name <name> [--id <id>] 

abr repo verify 

Description: Validate filesystem availability, permissions, and block consistency. Runs as a single synchronous server call (no percentage progress channel), so when the console is an interactive terminal the CLI shows an elapsed-time indicator ("Verifying repository... Ns elapsed") automatically while it waits -- no flag needed, and it's silent when output isn't a terminal (redirected/CI). Exits non-zero when the verdict is not a clean pass, so `abr repo verify --name r1 || alert ...` reacts to it. 

abr repo verify --name <name> [--id <id>] 

For an smb repository the server mounts itself, verify first mounts its share when it is not mounted; when that fails, the verdict is an error with the reason (check mounted). For an immutable s3 repository, verify also reports Object Lock state without writing anything. 

The block check marks the restore points whose data it finds damaged, and the points that build on them, corrupt, as abr backup verify does, and sends the backup.verify_failed event when it fails. 

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

abr inventory add --name <name> --type <vsphere|hyper-v|proxmox-ve|xcp-ng> --address <host/ip> [--port <int>] [--protocol <https|http>] [--username <user>] [--password [<pass>]] [--token [<token>]] [--insecure] [--api <vddk|soap>] [--vddk-lib <path>] 

The password comes from ABR_INVENTORY_PASSWORD and the token from ABR_INVENTORY_TOKEN, or the password is asked for interactively. A value typed after --password or --token is still accepted, but every local user can read it (ps, /proc) while the command runs, and it stays in the shell history. 

Example: 
abr inventory add --name Prod-vCenter --type vsphere --address 192.168.1.10 --username administrator@vsphere.local 

Note:
VM discovery is not required up front. It runs automatically the first time you
reference VM names in job create, so no manual inventory rescan is needed after
inventory add. job create, and job edit with new VMs, waits for that discovery
as long as inventory rescan does (up to 15 minutes), so it reports the outcome
the server reaches instead of failing while the job is still being created.

A hyper-v inventory speaks WinRM over HTTPS on port 5986 unless --protocol
http is given (port 5985). --port moves the port and keeps the protocol, so
HTTPS works on any port the host's listener uses. HTTPS is never given up for
HTTP: a host without an HTTPS listener, or with a certificate the server does
not trust, is refused with the reason. --insecure accepts a self-signed
certificate. --protocol applies to hyper-v only.

vSphere API. --api chooses how a vsphere inventory's (hypervisor's) disks are
read and written: vddk (the VMware VDDK) or soap (the vSphere SOAP API, which
needs no VMware library on the server). Without it the inventory uses the
server-wide API (abr settings vsphere show|set); inventory edit --reset-api
goes back to it. --transport is the earlier name of --api and still
works. The server refuses an API it cannot use, with the reason, when it is
chosen and when a backup, restore or replication session starts: vddk when
there is no VDDK library to load, soap when abr-vmdk-tool has no --api soap. A
backup job can override it (job create/edit --transport); a restore and a
replication always use the API of their target inventory.

VDDK library. --vddk-lib names the VDDK library file the inventory's VDDK
sessions load, under whatever name it has: VMware's VDDK
(lib64/libvixDiskLib.so.9.1.0.0), openVDDK or our oVDDK build. It must be a full
path to a file the backup service can read on the backup server, and export
VixDiskLib_InitEx; anything else is refused with the reason
(invalid_vddk_library). A VMware VDDK's file has to stay in its bundle's lib64,
where its transport plugins are. Without it, or after inventory edit
--reset-vddk-lib, the inventory uses the server-wide library, and when none is
set the library of the VDDK directory:
<directory>/lib64/libvixDiskLib.so, where the directory is
ABR_VMDK_TOOL_VDDK_DIR in the server's environment or the vddk folder next to
abr-vmdk-tool. It needs abr-disk-tools a14e078 or later, and is refused on an
older abr-vmdk-tool. A library set on a soap inventory is refused.

inventory info shows the inventory's own values (api: vddk, soap or inherit;
vddk_library: a path or null), the values in effect (effective_api,
effective_vddk_library, and vddk_library_file, the file the library resolves to,
e.g. libvixDiskLib.so.9.1.0.0) and where each comes from (api_source and
vddk_library_source: hypervisor, global, or vddk_directory for the fallback).

Each session records, when it is created, the API, the library, the file the
library resolves to and where each came from (job, hypervisor, global or
vddk_directory), so changing them never touches a session already created; job
info and session info show them, and the session log says which ones read or
wrote the disks. abr-vmdk-tool then gets exactly one of
ABR_VMDK_TOOL_VDDK_LIB (a library set for the inventory or server-wide) and
ABR_VMDK_TOOL_VDDK_DIR (the VDDK directory), whatever the server's own
environment has, and neither over soap; the server log names the one passed.

Over soap:
- a running VM is backed up through vSphere's export of the backup snapshot,
  which streams the whole disk: an incremental takes as long to read as a full,
  and keeps only what changed;
- a backup needs vCenter: an ESXi host registered on its own cannot export the
  backup snapshot, so a SOAP backup of its VMs is refused at the start of the
  session with the advice to use vddk (restores to such a host over soap work);
- a restore creates each disk new and thin, through a temporary VM, and the
  server then attaches it to the controller and slot it had and gives it its
  disk UUID back; a disk asked to be thick stays thin, and the session warns
  (soap_thin_disks); a disk is written in one run of its whole chain, and a
  chain that cannot be handed over in one run is refused with the advice to use
  vddk;
- a disk restore into an existing VM needs the VM powered off;
- a replication rebuilds the replica from each new restore point (SOAP cannot
  write into a disk that has a snapshot), and its session says so.

A vsphere inventory can be a vCenter Server or a standalone ESXi host. When an
ESXi host is also managed by a vCenter, register the vCenter instead: through a
direct host connection the server cannot stop that vCenter's Storage vMotion and
DRS from moving the VM while its disks are being read.

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

abr inventory edit --name <name> [--address <host/ip>] [--port <int>] [--protocol <https|http>] [--username <user>] [--password [<pass>]] [--token [<token>]] [--insecure] [--api <vddk|soap> | --reset-api] [--vddk-lib <path> | --reset-vddk-lib] 

Changing --protocol of a hyper-v inventory moves a standard port along with it
(5986 for https, 5985 for http); a port of your own stays as it is.

abr inventory remove 

Description: Remove an inventory source from management. 

abr inventory remove --name <name> [--id <id>] 

abr inventory rescan 

Description: Refresh the discovered object catalog. A job keeps its VM by ID, whatever the VM is called now. Only when a job's VM is gone from the inventory and another VM carries its name does the rescan move the job to that VM; abr job info then shows the move as a warning, and so does the next backup session of that VM. 

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

Description: Create a backup job for one or more virtual machines and optionally attach an enabled or disabled schedule. Supported schedule types are daily, weekly, monthly, and periodic. --disabled (or --enabled) also works without --schedule, for a job whose only schedule is its full schedule: the job is created switched off, as job edit --disabled would leave it; the same holds for job create-backup-copy and job create-copy. Each VM may be listed once. --retention-days is a whole number from 1 to 3650 (14 when omitted); the GFS counts are whole numbers up to 520 weekly, 120 monthly and 99 yearly; a schedule's time zone, when given through the API, must be an IANA name such as Europe/Kyiv or UTC.  Without --repository and --repository-id the job stores to the default repository (abr repo edit --default); with no default repository the command is refused, naming the options.

abr job create --name <name> [--repository <name> | --repository-id <id>] [--inventory <name> | --inventory-id <id>] (--vm <vm> ... | --vms "<vm1,vm2,...>") [--schedule <daily|weekly|monthly|periodic>] [--time <HH:MM>] [--days <mon,tue,...>] [--day-of-month <1..31>] [--every <15m|1h|6h|1d>] [--enabled | --disabled] [--description <text>] [--retention-days <int>] [--retention-min-fulls <0..100>] [--active-full-schedule <daily|weekly|monthly|periodic|none>] [--active-full-time <HH:MM>] [--active-full-days <mon,tue,...>] [--active-full-day-of-month <1..31>] [--active-full-every <15m|1h|6h|1d>] [--active-full-enabled | --active-full-disabled] [--synthetic-full-schedule <daily|weekly|monthly|periodic|none>] [--synthetic-full-time <HH:MM>] [--synthetic-full-days <mon,tue,...>] [--synthetic-full-day-of-month <1..31>] [--synthetic-full-every <15m|1h|6h|1d>] [--synthetic-full-enabled | --synthetic-full-disabled] [--verify-schedule <daily|weekly|monthly|periodic|none>] [--verify-time <HH:MM>] [--verify-days <mon,tue,...>] [--verify-day-of-month <1..31>] [--verify-every <15m|1h|6h|1d>] [--verify-enabled | --verify-disabled] [--verify-whole-chain-every <1..100>] [--on-quiesce-failure <fail|crash-consistent>] [--transport <vddk|soap|inventory>] 

Examples: 
abr job create --name "Production VMs" --inventory "vcenter-main" --vm "vm-101" --vm "vm-102" --repository "repo-local-01" --schedule daily --time "23:00" --enabled 

abr job create --name "Weekly Backup" --inventory "vcenter-main" --vm "db-01" --repository "repo-01" --schedule weekly --days mon,wed,fri --time "23:00" 

abr job create --name "Monthly Backup" --inventory "vcenter-main" --vm "vm-01" --repository "repo-01" --schedule monthly --day-of-month 1 --time "01:00" 

abr job create --name "Periodic Backup" --inventory "vcenter-main" --vm "critical-01" --repository "repo-01" --schedule periodic --every 6h --disabled 

abr job create --name "Nightly with Fulls" --inventory "vcenter-main" --vm "vm-01" --repository "repo-01" --schedule daily --time "22:00" --retention-days 30 --active-full-schedule monthly --active-full-day-of-month 1 --active-full-time "02:00" --synthetic-full-schedule weekly --synthetic-full-days sun --synthetic-full-time "03:00" 

abr job edit --name "Nightly with Fulls" --synthetic-full-disabled 

abr job edit --name "Nightly with Fulls" --synthetic-full-enabled 

abr job edit --name "Nightly with Fulls" --synthetic-full-schedule none 

abr job edit --name "Nightly with Fulls" --verify-schedule weekly --verify-days sat --verify-time "06:00" --verify-whole-chain-every 4 
 

Notes & Behavior: 
--retention-days = operational rolling retention affecting non-GFS points 
--retention-min-fulls = how many of the newest full backups of each VM, with the incrementals on them, retention keeps whatever their age. A new backup or backup copy job keeps 2 unless the option is given: the newest full and the one before it, each with its incrementals, always stay. So after a pause in backups longer than --retention-days (the job disabled, the VM off, the repository or the server down) the first full that follows keeps the chain from before the pause instead of removing it, and a problem that started during the pause can still be undone. 1 keeps only the newest full and its incrementals, so a VM never loses its last restore point to retention, but right after such a pause the newest full is the one just made; up to 100. 0 keeps nothing beyond --retention-days, as jobs did before the setting: a chain whose points are all older than --retention-days goes entirely, and the first full after such a pause leaves the VM with that one restore point. Jobs created before 2 became the default keep the value they have, 0 included, until abr job edit changes it. It only adds to what --retention-days, GFS and Object Lock already keep. A copied job takes the source job's value unless --retention-min-fulls is given; abr job edit --retention-min-fulls 0 switches it off. 
--gfs-* = extended lifecycle marks enforced during automated block evaluation. 
--verify-schedule = when the job's backups are verified, set like a full schedule (--verify-time, --verify-days, --verify-day-of-month, --verify-every, --verify-enabled / --verify-disabled; none removes it). Each run checks the newest restore point of every chain the job wrote (each VM in each repository) together with the points it builds on - its full and the incrementals between them, the data a restore of it reads - so damage in that full is found on the next run; every --verify-whole-chain-every-th run (4 by default, 1 for every run) checks every restore point of each chain, older chains included. A run waits while the job backs up, and a backup the job is due to take waits while it verifies. Each chain gets a verify session of its own in the job's history (abr session list --job <name>), and job info shows the next run (verify_next_run_at) and the last one (last_verify: its number, scope, result and sessions). Like the full schedules it rides along with the job: a disabled job verifies nothing. A backup copy job takes the same options for its own chains; job create-copy copies the source job's verify schedule. A new job has none until one is set. 
A job carries three independent schedules: the base --schedule produces incrementals, --active-full-schedule reads the full VM again, and --synthetic-full-schedule takes an incremental and merges it into a new full on the repository. When more than one is due at the same moment the job runs a single session, with active full taking priority over synthetic full and synthetic full over the incremental. Each full schedule takes either form of switch-off: --active-full-schedule none / --synthetic-full-schedule none removes it and forgets its settings, while --active-full-disabled / --synthetic-full-disabled suspends it and keeps them for a later --active-full-enabled / --synthetic-full-enabled. 
A backup job cannot be created without at least one enabled full backup schedule - an active full, a synthetic full, or the legacy day lists - because operational retention can only free a chain once a newer full starts the next one, and a job that only ever takes incrementals keeps a chain that retention can never prune. The same check runs on edit whenever retention or a full schedule is touched, so the last remaining full schedule cannot be removed or suspended. 
The full schedules ride along with the job: a job whose base schedule is disabled, or which has no base schedule at all, runs nothing until it is enabled again. 
Jobs that already use --active-full-days or --synthetic-full-days keep working: those day lists are read as weekly full schedules at the job's own schedule time. 
--active-full-days and --synthetic-full-days belong to weekly full schedules only: job create and job edit refuse them, none included, with a daily, monthly or periodic one and leave the job as it was. --active-full-schedule none or --synthetic-full-schedule none turns such a schedule off. 
A backup of a VMware VM takes its snapshot with quiescing, so VMware Tools flushes the guest and runs its pre-freeze scripts or VSS writers first. --on-quiesce-failure sets what happens when that fails: fail (the default) ends that VM's session as failed, naming the cause the guest reported and where to look (/etc/vmware-tools/backupScripts.d/ on Linux, 'vssadmin list writers' on Windows); crash-consistent takes the snapshot again without quiescing and completes with a warning. Either way the session, abr backup info and abr restore point list show whether each backup is quiesced or crash-consistent, and a crash-consistent backup carries a warning that says why. A powered-off VM has no guest to quiesce and needs none: its backup is consistent, is recorded as crash_consistent with the reason vm_not_running, and carries no warning; the session log says the VM was powered off. 
--transport sets how a backup of a VMware VM reads its disks: vddk or soap overrides the transport of the job's vSphere inventory for this job only, and inventory (the default) hands the choice back to the inventory, so a job created without it keeps working as before. It applies to backup jobs; a replication job uses the transport of its target inventory. job info shows it as transport (vddk, soap or inventory) and effective_transport (what the next session uses), and session info shows the transport each session ran with; the session keeps it even when the setting changes while it runs. A transport the server cannot use is refused with the reason (see inventory add). 

Hyper-V jobs follow the same schedule rules: a full backup, then incrementals from Hyper-V's resilient change tracking, with active and synthetic fulls on their own schedules (see doc/HyperVBackup.md). Their disks are written by abr-vhdx-tool on the Hyper-V host, so their repository must be S3 or SMB (abr repo add --type smb, see doc/HyperVBackup.md), or a local repository this server shares (shared when it is added, or later with abr repo share add). job create, and job edit when it changes the repository or the inventory, refuse any other repository with the reason and exit non-zero; a job made before that check fails with the same reason when it runs. One option is Hyper-V only: 
• --guest-consistency <application|crash> sets the recovery checkpoint's consistency. application (the default) quiesces a running guest through VSS and, when the guest cannot be quiesced, takes a crash-consistent checkpoint and says so in the session. crash never involves the guest. A VM that is off, saved or paused always gets a crash-consistent checkpoint, which is consistent since nothing runs inside it. 

Example: 
abr job create --name "HV nightly" --inventory "hv-cluster" --vms "sql-01,web-01" --repository "repo-01" --schedule daily --time "22:00" --retention-days 14 --guest-consistency application 

abr job create-copy 

Description: Clone an existing job configuration. The copied job receives a new ID and name while preserving inventory, selected VMs, repository, schedule configuration, schedule enabled/disabled state, and existing backup options. Optional repository, schedule, and retention overrides are still accepted for compatibility. 

abr job create-copy --name <name> (--source <name> | --source-id <id>) [--repository <name> | --repository-id <id>] [--schedule <daily|weekly|monthly|periodic>] [--time <HH:MM>] [--days <mon,tue,...>] [--day-of-month <1..31>] [--every <15m|1h|6h|1d>] [--enabled | --disabled] [--retention-days <int>] [--retention-min-fulls <0..100>] 

Example: 
abr job create-copy --name "Copied Job" --source "Existing Job" 

abr job create-backup-copy 

Description: Create a backup copy job. It copies the restore points of its sources - backup jobs (--source-job) or whole repositories, the points of the jobs in them (--source-repo) - into another repository (--repository), where it keeps a chain of its own for each VM with its own retention, GFS, Object Lock dates and encryption key, independently of the sources. It reads stored backups only and never touches the VMs, vCenter or the Hyper-V hosts, so it runs whether a VM is on, off or gone. Copies are restored like any other restore point (abr restore list --job <name>). 

abr job create-backup-copy --name <name> (--source-job <name> ... | --source-job-id <id> ... | --source-repo <name> ... | --source-repo-id <id> ...) (--repository <name> | --repository-id <id>) [--mode <periodic|immediate>] [--vm <vm> ... | --vms "<vm1,vm2,...>"] [--schedule <daily|weekly|monthly|periodic>] [--time <HH:MM>] [--days <mon,tue,...>] [--day-of-month <1..31>] [--every <15m|1h|6h|1d>] [--run-after-job <name> | --run-after-job-id <id>] [--copy-window "<[days] HH:MM-HH:MM>" ...] [--retry-count <0..20>] [--retry-interval <minutes>] [--enabled | --disabled] [--description <text>] [--retention-days <int>] [--retention-min-fulls <0..100>] [--gfs-weekly <n>] [--gfs-monthly <n>] [--gfs-yearly <n>] [--active-full-schedule ...] [--synthetic-full-schedule ...] [--verify-schedule ...] [--compression-level <0..9>] [--set-encryption [--encryption-key]] 

Notes: 
• --mode periodic (the default) copies on the job's own schedule (daily, weekly, monthly, periodic or cron, as for backup jobs), by hand with abr job start, or each time a run of another job ends (--run-after-job). Each run copies the newest state of every VM and merges whatever is new since the last copy into one restore point. A --run-after-job copy starts after every run of that job, whatever its result (a stopped or failed run too). A run on the job's schedule, by hand or after another job gives a VM with nothing new a session that completes with "Nothing new to copy" and makes no restore point, and moves last_run_at, so the job's history shows each start. 
• --mode immediate copies every new restore point of a source as soon as the backup that wrote it ends, one at a time; points that piled up (for example while the destination was down) are copied one after another. It takes no --schedule and no --run-after-job. 
• A VM that several source jobs back up (two jobs into one source repository, or two --source-job jobs) is copied from one of them, since the copy keeps one chain per VM: the first --source-job job (in the order given) that backs it up, else the job created first. Once the copy holds a point of that job it stays on it. Each run of such a VM names the other jobs in its log, and "Nothing new to copy" names the job whose newest point is already copied. 
• The first copy of a VM is a full backup of the source's current chain - its newest full and the incrementals on it; older chains are not copied. Later copies are incrementals on it. When the source starts a new chain (an active or a synthetic full), the next copy is a new full too. 
• Without --vm/--vms every VM the sources back up is copied, including VMs added to them later; with them, only those VMs, which must be backed up by the sources. 
• A --source-job can be a backup job or another backup copy job, so a copy can be copied again (for example, a second offsite copy for 3-2-1). A copy job cannot be its own source, neither directly nor through other copy jobs, nor through a --source-repo that another job of such a cycle writes to: job create-backup-copy and job edit refuse it (backup_copy_cycle) and name each step of the cycle. The check is made when the sources or the repository change, so a cycle made before it can still be switched off or broken. 
• The destination must not be a source: neither a --source-repo nor the repository of a --source-job. When the sources hold Hyper-V VMs, it must be a repository the Hyper-V hosts reach (S3, or SMB with its share), since a Hyper-V restore reads it from the host. 
• Encrypted sources need an encryption key of the copy job's own (--set-encryption, read from ABR_ENCRYPTION_KEY or prompted for), so the copies stay encrypted; unencrypted sources may still be copied encrypted. The key is entered, never generated. 
• A full schedule (--active-full-schedule or --synthetic-full-schedule) is required, as for backup jobs, so retention can drop old chains. An active full copies the newest state from the source again; a scheduled synthetic full first copies whatever is new in the source, then merges the copied chain inside the destination without reading the source again. Later copies go on from that synthetic full as incrementals with only the source points not copied yet; it records the source point whose state it holds (copy_source), like any copy. When there is nothing to merge - the newest copy is already a full and the source has nothing new - a synthetic full of the copy job, scheduled or started by hand (abr job start --synthetic), completes with "Nothing to merge" and makes no restore point, instead of failing; on a backup job such a manual synthetic full is refused up front (422 chain_cannot_be_merged). 
• --copy-window limits when copies move data. A rule is "[days] HH:MM-HH:MM": days separated by commas in week order (mon,wed,fri), each day once; without days, every day. The end may be past midnight (22:00-06:00 runs from the evening of each day given into the next morning). Repeat the option for more rules; none means any time. A copy started outside the window waits in the queue (its log says when the window opens). A copy still running when the window closes is stopped, what it wrote is discarded, and it is queued again for the next opening. A change of the window takes effect at once, for queued copies and for a copy that is running: when the time is then outside the window, the running copy is stopped the same way, with the same message; a change that keeps the time inside the window, or removes the window, lets it go on. 
• A VM whose copy fails is retried within the same session --retry-count times, --retry-interval minutes apart: 5 times right away for periodic, 3 times 10 minutes apart for immediate, unless set. Configuration errors and a stop are not retried. 
• A source job or repository cannot be deleted or removed while the copy job uses it (see abr job delete and abr repo remove). A copy job whose source or run-after job was deleted before that check existed shows it as "(missing)" in job info (missing: true in --output json). Each of its copy sessions then warns (copy_source_missing), naming what is gone, so the run ends as Warning; when no source is left at all, abr job start is refused (409 backup_copy_no_source, exit 4) and a scheduled run is recorded as Failed with that reason, without sessions. 
• A failed or stopped copy leaves the source and the copies made so far as they were. Each copy session logs the source job and repository, the VM, the destination, the restore points copied, the bytes and the time. 

Examples: 
abr job create-backup-copy --name "SQL-offsite" --source-job "SQL-nightly" --repository "S3-offsite" --schedule daily --time 03:00 --retention-days 90 --active-full-schedule monthly --active-full-day-of-month 1 --active-full-time 04:00 
abr job create-backup-copy --name "All-to-cloud" --mode immediate --source-repo "Local-Repo-1" --repository "S3-offsite" --copy-window "mon,tue,wed,thu,fri 22:00-06:00" --copy-window "sat,sun 00:00-24:00" --active-full-schedule weekly --active-full-days sun --active-full-time 04:00 
abr job create-backup-copy --name "SQL-after-nightly" --source-job "SQL-nightly" --vms "sql-01" --repository "Archive-SMB" --run-after-job "SQL-nightly" --synthetic-full-schedule weekly --synthetic-full-days sat --synthetic-full-time 05:00 

abr job create-replication 

Description: Create a replication job. It keeps a powered-off replica of one VM on a target environment, built from the restore points of an existing backup job: the first run creates the replica VM, every later run updates that same VM to the newest restore point. It reads stored backups only, never the production VM, and never changes the source backups. Creating or updating the replica never powers it on; to use it, power it on by hand as part of the recovery procedure. 

abr job create-replication --name <name> (--source-job <name> | --source-job-id <id>) [--source-vm <vm>] (--target-inventory <name> | --target-inventory-id <id>) --target-host <host> --datastore <datastore> --replica-name <name> [--network-map "<source>=<target>" ...] [--restore-point <id>] [--run-after-source | --no-run-after-source] [--schedule <daily|weekly|monthly|periodic>] [--time <HH:MM>] [--days <mon,tue,...>] [--day-of-month <1..31>] [--every <15m|1h|6h|1d>] [--enabled | --disabled] [--description <text>] 

Notes: 
• The source must be a backup job (not a backup copy or replication job) that backs up exactly one VM and has a restore point of it that can be read. --source-vm names that VM when it should be given explicitly. The target must be the same platform as the source: VMware to VMware (vCenter or a standalone ESXi), Hyper-V to Hyper-V. On Hyper-V --datastore is the storage path of the replica's files (for example D:\Replicas), and the repository of the source must be one the Hyper-V host reaches (S3, or SMB with its share). 
• --replica-name is required, and no VM of that name may already be on the target. --network-map sends the source VM's adapters on a source network to a target network (VMware: port group; Hyper-V: virtual switch); a source network with no mapping goes to the target network of the same name. --restore-point picks the point the first run creates the replica from; without it the newest one. 
• Replication jobs have no repository, retention, GFS, full schedules, compression or encryption of their own: those options are refused. An encrypted source is read with the source job's own keys. 
• When it runs: on its --schedule; with --run-after-source, after each run of the source backup job that ends with success or warning and left a new restore point (a failed or stopped backup does not start it); and by hand with abr job start. Both may be set. A trigger that comes while the job is running starts no second run: it is kept as one pending run (shown as run_pending in abr job info) that starts when the current run ends, and that run serves every trigger that came meanwhile. A disabled job runs nothing and drops a pending run. 
• Every run brings the replica to the newest restore point of the source, whichever trigger started it; several new restore points are applied in one run. When the source job is disabled, the replica stays at its newest restore point and the run says so. 
• How a run updates the replica: when the replica's restore point and the newest one are in the same chain with incrementals between them, it takes a snapshot (Hyper-V: a checkpoint) of the replica named abr-replica-<job>-<session>, writes the changed blocks of those incrementals into the replica's disks and removes the snapshot. A synthetic full in the source is followed the same way when the chain it merged is still there. Otherwise (a new active full, a disk added, removed or resized, or restore points between the two that are no longer kept) the run rebuilds the disks: it restores the newest restore point as a temporary VM <replica>-staging-<session> on the same host and datastore, then switches the replica to those disks in one step and deletes the old disks and the temporary VM. The new disks stay in the temporary VM's folder (named ...-staging-...) on the datastore. The datastore needs as much free space again as the replica's disks take while a rebuild runs. 
• At the end of every run the replica's vCPU count and memory (on Hyper-V with its dynamic memory settings) are set to the restore point's, and its network adapters follow the source VM's: a new adapter is added, a removed one is removed, and an adapter that moved to another network moves too (Hyper-V: also its VLAN). An adapter whose source network has no mapping and no network of that name on the replica's host is added (or left) disconnected, with a warning. Firmware, Secure Boot, the virtual TPM, DVD drives, the configuration version and the boot order are set when the replica is created and not changed afterwards; when the source's differ, the run warns once. On VMware a new adapter is a VMXNET 3 adapter, as in a restore. 
• The replica must be left alone between runs. A run refuses to update a replica that was powered on since the last run, has snapshots or checkpoints the job did not take, has other disks, or whose configuration or disks changed, and fails with "Replica was changed: ... Create a new Replication Job". The same happens when the replica VM is gone, or when the source job's VM was replaced by another one. 
• A failed or stopped run leaves the source backups untouched and the replica at the restore point it was at: the snapshot is reverted, or the rebuilt disks are removed before the switch. A run interrupted by a server or host failure is finished or undone by the next run before anything else. A run that fails on a temporary error (the target or the repository cannot be reached, the connection dropped, the storage tool ended unexpectedly) is tried again in the same session, 3 times 10 minutes apart (ALTASTOR_REPLICA_RETRY_COUNT, ALTASTOR_REPLICA_RETRY_INTERVAL_MINUTES); a stop and a check that fails are not retried. A run waits up to ALTASTOR_REPLICA_TASK_WAIT_SECONDS (1800) for a task already running on the replica to end. 
• While a run reads a backup chain of the source, abr backup delete of that chain is refused (409 backup_chain_in_use) and retention skips it, saying so in its log. The source backup job cannot be deleted, nor its VM removed from it, while a replication job uses it (409 replication_source_in_use): delete the replication job first. 
• Each session logs the source backup job and VM, the restore point used, the target environment, host and replica VM, the incrementals applied, the bytes read and written and the duration; abr job info shows the replica, its restore point and the time of the last successful update. 

Examples: 
abr job create-replication --name "SQL-DR" --source-job "SQL-nightly" --target-inventory "vCenter-DR" --target-host "esx-dr-01" --datastore "ds-dr" --network-map "VM Network=DR Network" --replica-name "sql-01-replica" --run-after-source 
abr job create-replication --name "App-DR" --source-job "App-HV" --target-inventory "HV-DR" --target-host "HV-DR-01" --datastore "D:\Replicas" --replica-name "app-01-replica" --schedule daily --time 03:00 

abr job list 

Description: Show all configured jobs, including inventory, repository, VM count, schedule summary, schedule state, and run timestamps when available. LAST RESULT is how the job's last run went over all of its VMs: Success, Warning, Stopped or Failed, with the number of VMs affected, or Running while any VM is still being backed up. It is empty for a job that has not run since the server was upgraded to report it. TYPE is backup, backup-copy or replication; a backup copy job has no inventory of its own, and a replication job shows its target environment. 

abr job list [--output json] 

abr job info 

Description: Display detailed job bindings, tracked virtual machines, schedule configuration, schedule state, execution policy references, and timestamps. last_result summarises the last run, last_run_issues names each VM that failed, stopped or finished with warnings together with the reason and its session, and last_run_id lists that run's sessions with abr session list --run. With --output json the run is in last_run, with the result of every VM. For a backup copy job it also shows copy_mode, source_jobs, source_repositories, run_after_job, copy_window and retry; with --output json they are in backup_copy. For a replication job it shows source_job, source_vm, target_inventory, target_host, target_datastore, network_map, replica_name, run_after_source, replica_vm (not created yet before the first run), replica_point with its date and last_success_at; start_point until the replica is created, pending_rename while a new replica name waits for the next run, and run_pending while a run waits for the current one to end. With --output json they are in replication. 

abr job info --name <name> [--id <id>] 

abr job edit 

Description: Update job configuration binding, scheduling, schedule state, or protected targets. 

abr job edit --name <name> [--vm <vm> ... | --vms "<vm1,vm2,...>"] [--repository <repo_name>] [--inventory <inventory_name>] [--schedule <daily|weekly|monthly|periodic|none>] [--time <HH:MM>] [--days <mon,tue,...>] [--day-of-month <1..31>] [--every <15m|1h|6h|1d>] [--enabled | --disabled] [--description <text>] [--retention-days <int>] [--retention-min-fulls <0..100>] [--active-full-schedule <daily|weekly|monthly|periodic|none>] [--active-full-time <HH:MM>] [--active-full-days <mon,tue,...>] [--active-full-day-of-month <1..31>] [--active-full-every <15m|1h|6h|1d>] [--active-full-enabled | --active-full-disabled] [--synthetic-full-schedule <daily|weekly|monthly|periodic|none>] [--synthetic-full-time <HH:MM>] [--synthetic-full-days <mon,tue,...>] [--synthetic-full-day-of-month <1..31>] [--synthetic-full-every <15m|1h|6h|1d>] [--synthetic-full-enabled | --synthetic-full-disabled] [--verify-schedule <daily|weekly|monthly|periodic|none>] [--verify-time <HH:MM>] [--verify-days <mon,tue,...>] [--verify-day-of-month <1..31>] [--verify-every <15m|1h|6h|1d>] [--verify-enabled | --verify-disabled] [--verify-whole-chain-every <1..100>] [--on-quiesce-failure <fail|crash-consistent>] [--transport <vddk|soap|inventory>] 

--schedule none (or manual) removes the regular schedule of a backup, backup copy or replication job: it shows "Manual only" again, as a job created without one, runs when started by hand (or after its source), and keeps its active and synthetic full schedules. It takes no --time, --days, --day-of-month or --every. In the API, PATCH /api/v1/jobs/{id} with "schedule": null does the same.

A backup copy job takes the options of job create-backup-copy as well: --mode, --source-job, --source-job-id, --source-repo, --source-repo-id (each replaces that list; none empties it), --run-after-job (none clears it), --copy-window (replaces the rules; none means any time), --retry-count, --retry-interval and --all-vms (copy every VM of the sources again). They are refused on a backup job, and --inventory, --on-quiesce-failure and --guest-consistency on a backup copy job, with the reason. 

A replication job takes the options of job create-replication. Until its replica is created all of them may change. Once it exists, the job's name, description, schedule, --enabled/--disabled, --run-after-source and --replica-name may still change; the source job and VM, the target environment, host, datastore, network mapping and start restore point are refused with 422 replication_setting_locked ("Cannot be changed after the replica is created. Create a new Replication Job instead."), while giving the value they already have passes. A new --replica-name renames the replica VM on the next run (abr job info shows it as pending_rename until then; on VMware the VM's folder keeps its old name); it is refused when a VM of that name is already on the target, and when such a VM appears before the run, the run keeps the old name, warns, and tries again next time. 

--set-encryption takes the key as job create does: from ABR_ENCRYPTION_KEY, or prompted for (twice) when it is not set, or with --encryption-key. On a job without encryption it turns encryption on. On an encrypted job a different key becomes the job's new key for the backups that follow (key rotation): the earlier backups keep the key they were written with and restore with it, and giving the current key again changes nothing. With no key in the environment and no terminal to prompt on, the command fails instead of leaving the key as it was. --reset-encryption turns encryption off for the backups that follow. The job keeps the keys it used, so its earlier encrypted backups still restore without a key typed by hand; a key given when encryption is turned on again gets a key ID of its own (a key the job used before gets its old ID back), so no two keys ever share an ID. 

abr job retention 

Description: Apply the job's retention policy now to every backup chain the job wrote, or preview it with --dry-run. For a backup copy job that is its own chains in its destination; the restore points of its sources are never touched. Retention also runs after each backup, for that backup's own chain; this covers all of them at once, including VMs that have since left the job. 

abr job retention --name <name> [--id <id>] [--dry-run] [--output json] 

Notes: 
• A full backup and the incrementals on it go together, only once every one of them is older than --retention-days and none is kept by GFS, newest first - so whatever stays always restores. Under a GFS-kept full, its expired incrementals may go on their own. 
• A restore point still under S3 Object Lock is not even tried: it is reported as held (with the date its lock ends), and so are the older points it depends on. They go on a later pass. 
• A chain with a backup of the same VM running is skipped and reported, so the point that backup chains onto cannot go from under it. 
• Age is measured against the server clock. After the clock jumps forward by more than BACKUPSERVER_CLOCK_JUMP_HOURS (24 by default), while the server runs or between two runs, retention deletes nothing - here or after a backup - until an Administrator confirms the time with abr system clock confirm. Backups keep running, and each session carries a warning. The command then exits 4 and names the jump; --dry-run still shows what would go. 
• With --retention-min-fulls N the newest N full backups of each chain and their incrementals are kept whatever their age; the table's NOTE says how many expired points stayed for that reason. 
• Every removed restore point is written to the server log with its creation date, the cutoff and the server time it was measured against. 
• The table lists each chain (VM and repository) with its points, what was (or would be) removed, what is held, and why. --output json returns the API's full report. The command exits 1 when an expired point could not be removed; it stays in the catalog for the next pass. 

abr job delete 

Description: Delete a job record. Its restore points stay in the catalog and in the repository. Deleting a replication job keeps its replica VM on the target as an ordinary powered-off VM that AltaStor no longer manages; --delete-replica deletes the replica VM and its disks too, with any temporary VM an interrupted run of the job left. --delete-replica is refused, and the job kept, while the replica is powered on (it may be in use after a failover), when the target environment cannot be reached or is no longer registered, or when the VM cannot be deleted; the answer says why. It applies to replication jobs only. A job with a session queued or running cannot be deleted. 

abr job delete --name <name> [--id <id>] [--yes] [--delete-replica] 

Notes: 
• A job that a backup copy job uses as a source (--source-job) or as its --run-after-job cannot be deleted (409 backup_copy_source_in_use); the refusal names those copy jobs and what each uses the job for. Take it out of them with abr job edit (--source-job, --run-after-job none), or delete them first. A backup job a replication job reads from is refused the same way (409 replication_source_in_use). 

abr job start 

Description: Start a job immediately. The engine resolves execution type (Incremental, Synthetic Full, Active Full) automatically based on today's day-of-week match against the parameters configured inside the job. 

abr job start --name <name> [--id <id>] [--full | --synthetic] [--target-repo <name>] [--wait] 

Notes: 
• A replication job takes no --full, --synthetic or --target-repo: it creates its replica on its first run and brings it to the newest restore point of its source on every later one. A start while the job runs is refused; a start that comes from a schedule or the source backup at that time waits as the job's one pending run. 
• First-run rule: The very first execution of a job always produces a Full backup, regardless of scheduled execution windows. 
• A backup copy job that already has a session queued or running refuses another start (409 job_already_running, exit 4) until that session ends. A copy session queued as a first run that starts after another one has already copied the VM (a manual start next to a scheduled, run-after or immediate one) re-decides when it starts: it copies only what is new, or completes with "Nothing new to copy", so the destination never holds two copies of the same restore point. 
• --full forces an immediate Active Full execution regardless of previous restore points or scheduling rules. `--force-full` is accepted as a backward-compatible alias. 
• --synthetic forces a Synthetic Full: the full backup is built by merging the chain already in the repository, so the VM is never read and no snapshot is taken. It needs a full backup followed by at least one incremental, and a repository that stores the .abr format (local or s3). AltaStor takes the chain order from its own catalog, so the ordering hazard the storage tool warns about does not apply here. The chain is only read - the restore points it merged are left in place for retention to age out. --full and --synthetic are mutually exclusive. 
• --target-repo writes the merged full into another repository instead of the job's own, so a chain in one repository can be collapsed into a standalone full in another (local and s3 in any combination). The chain itself is only read, and the copy lands as the first restore point of its own chain there - the job keeps writing to its own repository. Only valid with --synthetic. A Hyper-V job's target must be one its hosts reach (S3, or SMB with its share), the same as its own repository. 
• A run refused because the chain cannot be merged (no incrementals, a full in the middle, disks that do not match) exits 6, separately from 4 used when a run fails while it is already under way. 
• Synthetic Full day: a plain `job start` on a job-designated Synthetic Full day runs like the scheduled one - an incremental first, then the synthetic full merging it - so it works on a chain that holds only a full, and the merged full holds today's changes. `--synthetic` still merges the chain as it stands and is refused at once when there is nothing to merge, or when the job's encryption changed since its last backup (a new key, encryption turned on or off): that chain was written with the earlier setting, so run an active full (`--full`) instead. 
• Priority notice: If the current day matches both synthetic and active targets in the job parameters, Active Full always takes priority. 
• Repository capability notice: If today is a job-designated Synthetic Full day, but the target repository does not natively support synthesis, the engine transparently escalates the run execution to an Active Full. nfs repositories are in that group; local, smb and s3 all merge natively. 
• --wait follows the started session's real progress (abr-vmdk-tool's own percent per disk) on stderr as a live bar until it finishes, then prints the final session state; stdout stays clean for scripting either way. When the sessions it starts are all for one VM (the incremental and the synthetic full of a Synthetic Full day), --wait follows them in order and stops at the first one that does not complete. If the job starts sessions for more than one VM, --wait refuses to guess which one to follow and instead prints all of their session IDs so you can watch each with `abr session info --id <id> --watch`. Without --wait, the command returns immediately with the queued session(s), same as before this flag existed. If the followed session does not end completed (failed/stopped/canceled), the command exits with the same non-zero code used elsewhere for an unhealthy state, so `abr job start --wait || alert ...` reacts to the run's real outcome instead of always seeing exit 0. 

SESSION COMMANDS 

Monitor runtime execution state, logs, and telemetry. 

abr session list 

Description: Show active and historical sessions. 

abr session list [--job <name>] [--run <run-id>] [--since <ISO timestamp>] [--output json] 

Notes: 
• TYPE (type in --output json) says what a session does: backup, backup-copy (a session of a backup copy job, its synthetic fulls included), replication, restore, verify or delete. It stays after the job is deleted. GET /api/v1/sessions?type= filters by the same values. 

abr session info 

Description: Display detailed session telemetry. 

abr session info --id <id> [--watch] 

Notes: 
• consistency is quiesced when VMware Tools quiesced the guest for the backup snapshot and crash_consistent when it did not; consistency_reason then says why: tools_not_installed, tools_not_running, quiesce_failed (the job is set to --on-quiesce-failure crash-consistent), not_quiesced, or vm_not_running (the VM was powered off, so nothing ran in it and the backup is consistent; no warning is given). It is empty for sessions that take no snapshot and for backups made before this was recorded. For a Hyper-V VM, consistency is application or crash, the recovery checkpoint the backup was made from; when the job asks for application and gets crash, consistency_reason says why: vss_failed (the application-consistent checkpoint failed, typically the Backup (volume shadow copy) integration service in the guest, and a crash-consistent one was taken instead) or vm_not_running (the VM was not running, so there was no guest to quiesce). It is empty for an application-consistent backup and for a job set to --guest-consistency crash. The restore point carries the same two fields. 
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

Description: Display chain metadata for a single backup identifier. The CONSISTENCY column of its restore points is quiesced or crash_consistent, as described under abr session info. 

abr backup info --id <id> 

abr backup delete 

Description: Delete a backup chain from the repository.  

abr backup delete --id <id> [--force] 

Behavior: 
• Safety enforcement: If deletion would orphan dependent increments, the command fails with a dependency protection error regardless of --force. If deletion would violate GFS protection, the command fails with a dependency protection error unless --force is given. 
• --force: deliberately deletes a chain that carries an active GFS archival mark, discarding data the GFS policy marked for retention. This is the supported way to retire a repository whose only remaining chains are GFS-protected -- without it, such a chain (and the repository holding it) can never be removed. 
• Executing this command physically and permanently deletes data blocks from the storage.
• Interrupted delete: each restore point is marked deleting before its data is removed, newest first, so a delete (or a retention pass) cut short by a server crash never leaves a half-removed point offered for restore. Such a point stays deleting, abr backup verify skips it with a warning, and the server finishes removing it on its own when it starts again (and every BACKUPSERVER_DELETE_RETRY_SECONDS after). The older points the delete had not reached yet are left as they were; run abr backup delete again to remove them.
• S3 Object Lock: restore points whose data the lock still holds, and the older points they need to restore, are not deleted yet. They are marked for deletion (status deleting) and the server removes them by itself once the lock expires, whether or not their job still exists or runs (it tries every BACKUPSERVER_DELETE_RETRY_SECONDS, 900 s by default). The command says how many were kept and until when, and exits non-zero since the chain is not gone yet. Until then they cannot be restored, and no job builds on them, merges them, copies them or extends their lock; abr backup list shows "N point(s) deleting after <date>" in STATUS, and abr backup info shows pending_deletion and each point's status. Running abr backup delete again tries the storage again at once. 
 

abr backup verify 

Description: Verify backup chain readability, logical dependency, and integrity. Exits non-zero unless the result is a clean "valid", so `abr backup verify --id <id> || alert ...` reacts to a "warning" or "invalid" verdict instead of always seeing exit 0. 

abr backup verify --id <id> 

Notes: 
• A restore point whose data the disk tool finds damaged (a data range that fails its checksum) is marked corrupt, and so is every point that builds on it; the points it builds on and the older chains stay as they were. A check that could not be made (a file it could not read, a repository it could not reach, a missing key) fails the verify without marking anything. 
• A corrupt point is not offered for restore: abr backup list shows the chain as "corrupt, N point(s) corrupt" (once a newer point is not corrupt, after the active full that follows, as "available, N point(s) corrupt" until the corrupt points are deleted), backup info and restore list show each point's status, and abr restore start refuses it, naming the damaged point and when it was found. restore start --backup takes the newest point of the chain that can be restored, and when it passes over newer points the restore session warns (newer_points_unavailable), naming each of them and why it cannot be restored; when none can be restored, it refuses with the reason of the newest point, the same as restore start --point-id of that point. 
• The job's next run for that VM takes an active full (reason DamagedChain) instead of building on the damaged chain, and a forced --synthetic is refused until then; a backup copy job copies a full. Scheduled synthetic fulls become active fulls the same way. 
• A later verify that finds the data intact again (the file restored, the storage repaired) clears the marks. Retention deletes corrupt points by age like any other. 
• The same marks come from a job's verify schedule and from abr repo verify. A verify that found damage or could not check every point sends the backup.verify_failed event (see doc/Notifications.md). 

abr backup import 

Description: Import a backup chain from an external path. 

abr backup import --path <path> --repo <name> 

RESTORE COMMANDS 

Logical recovery orchestration to provision data back into working environments. 

abr restore list 

Description: Show restore points that can be used for VM recovery. 

abr restore list [--vm <name-or-id>] [--backup <backup-id>] [--inventory <name-or-id>] [--job <name-or-id>] [--output json] 

--job lists the restore points one job wrote, a backup job or a backup copy job; with --output json a copy carries copy_source: the source job, repository and restore points it was copied from. 

Note: 
This command returns stable restore-point records including point ID, backup ID, VM ID/name, inventory, repository, time, type, and status. Empty results return success with `items: []`. 

abr restore point list 

Description: Show restore points for a specific VM using its unique identifier. CONSISTENCY tells a quiesced restore point from a crash-consistent one, which matters when the VM runs a database. 

abr restore point list --vm-id <id> [--output json] 

Example: 
abr restore point list --vm-id vm-28491-prod 

abr restore context 

Description: Show what a restore point can be mapped to: its disks, network adapters, source datastores and source networks, each with the target it falls back to. 

abr restore context --restore-point <id> [--output json] 

Example: 
abr restore context --restore-point rp-2026-05-16-0200-f1 

Note: 
The SOURCE column holds the exact names the mapping flags take: a datastore name for --datastore-map, a network name for --network-map, a disk label or ID for --disk-datastore, and an adapter label or ID for --nic-network. Two adapters on one source network produce one network row each but separate adapter rows, which is the difference between --network-map and --nic-network. JSON output carries the whole context, including every disk and adapter ID and the targets the restore point can reach. 

abr restore start 

Description: Start a full VM restore workflow from a selected restore point or backup chain. 

abr restore start --restore-point <id> --target-inventory <name> --target-host <host-or-id> --datastore <name-or-id> --new-name <name> [--network-map <src=dst>] [--nic-network <adapter=dst>] [--datastore-map <src=dst>] [--disk-datastore <disk=dst>] [--power-on] [--wait] 

Alternate source resolution:
abr restore start --backup <backup-id> [--vm <name-or-id>] --target-inventory <name> --target-host <host-or-id> --datastore <name-or-id> --new-name <name> [--network-map <src=dst>] [--nic-network <adapter=dst>] [--datastore-map <src=dst>] [--disk-datastore <disk=dst>] [--power-on] [--wait] 

Legacy compatibility:
abr restore start --point-id <id> [--name <new-name>] [--target-inventory <name>] [--target-host <host>] [--target-datastore <name>] [--network-map <src=dst>] [--nic-network <adapter=dst>] [--datastore-map <src=dst>] [--disk-datastore <disk=dst>] [--repo <name>] [--power-on] [--in-place] [--force] [--wait] 

Targeted Restore Scenarios: 
 
1. In-Place Recovery (Replace the original VM) 
Description: The original VM is damaged or unavailable, and the backup must be restored over the existing production object, at its own original host, datastore, and name. 
Syntax: abr restore start --point-id <id> --in-place --force [--power-on] 
Example: abr restore start --point-id rp-2026-05-16-0200-f1 --in-place --force --power-on 

--in-place selects this mode explicitly. It cannot be combined with --target-inventory/--target-host/--datastore/--new-name -- in-place restore always uses the source VM's own placement, never a caller-supplied one. A restore point a backup copy job made is restored in place the same way (scenario 3 below). 

An in-place restore keeps the VM's identity: the restored machine carries the original's SMBIOS uuid and vSphere instanceUuid, so guest licensing, monitoring and this product's own backup jobs and backup chains keep matching it. On VMware the VM being replaced also hands over, before it is deleted, what vCenter keeps on the VM object and not in the backup: its VM folder, each network adapter's MAC address (matched by adapter label, else by order, so DHCP reservations and MAC-bound rules keep matching), its notes, vCenter tags, custom attributes, the permissions set on the VM itself and the alarms defined on it. What cannot be read or given back is named in a warning of the restore session (placement_not_restored), and the restore still completes. Only vCenter's internal moRef changes. A restore to a new name is the opposite by design: it creates a separate VM with its own identity, which is what cloning a machine alongside the original calls for. 
--force authorizes the overwrite once --in-place has been selected; it has no effect and no meaning without --in-place. Omitting --in-place leaves the flag rejected outright, since there is nothing for it to authorize. 

Requires that the backup captured the VM's original host and datastore -- if the source VM's placement is unknown, in-place restore is refused with restore_mode_unsupported. 
 
2. Out-of-Place / Advanced DR (Restore as a new VM on another host) 
Description: A duplicate VM is deployed on a secondary host or recovery site for testing or disaster recovery. 
Syntax: abr restore start --restore-point <id> --new-name <new-name> --target-inventory <name> --target-host <host> --datastore <name> [--network-map <src=dst>] [--nic-network <adapter=dst>] [--datastore-map <src=dst>] [--disk-datastore <disk=dst>] [--power-on] 
Example: abr restore start --restore-point rp-2026-05-16-0200-f1 --new-name VM-SQL-01-DR --target-inventory DR-Inventory --target-host esxi-dr-01.company.local --datastore DR-SSD --network-map Production=DR-Network --datastore-map SATA-BULK=DR-SATA --power-on 
Example (one disk and one adapter placed on their own): abr restore start --restore-point rp-2026-05-16-0200-f1 --new-name VM-SQL-01-DR --target-inventory DR-Inventory --target-host esxi-dr-01.company.local --datastore DR-SSD --disk-datastore "Hard disk 2=DR-SATA" --nic-network "Network adapter 2=DR-DMZ" --power-on 

DATASTORE PLACEMENT 

--datastore is the target for the whole VM: every disk lands there, whatever datastore it came from. --datastore-map overrides that for one source datastore, which is what a VM spread over several datastores needs -- a system disk on SSD and a 2 TB database on bulk storage should not both land on the SSD. Repeat the flag per source datastore; disks whose datastore is not named keep going to --datastore. 

The source datastore is read from the restore point's own manifest, so mapping works for restore points taken before this option existed, and even when the source VM no longer exists. A mapping that names a datastore the VM does not use is rejected rather than silently ignored, and each target is checked for free space against only the disks that actually land on it. 

--disk-datastore places a single disk, named by the disk label or ID that abr restore context lists ("Hard disk 2"). It covers the layout --datastore-map cannot express: all disks on one datastore at the source, where only the data disk should move to faster storage at the target. Disks that shared a source datastore no longer have to travel together. 

Precedence is disk, then source datastore, then whole VM: --disk-datastore wins over --datastore-map, which wins over --datastore. A disk the restore point does not carry is rejected rather than ignored, and free space is still checked against the datastore each disk actually lands on, per-disk placement included. 

In-place restore takes no mapping and no disk placement: it returns every disk to the datastore it came from, so a VM spread over several datastores stays spread. 

NETWORK PLACEMENT 

--network-map is keyed by source network name: every adapter recorded on that network goes to the named target. --nic-network places a single adapter, named by the adapter label or ID that abr restore context lists ("Network adapter 2"), which is what two adapters on one source network need -- a map keyed by network name cannot tell them apart and sends both to the same target. 

Adapters left unnamed keep the automatic same-name match, so naming one adapter or one network does not force you to name the rest: an adapter whose source network exists on the target host under the same name lands there by itself. Only an adapter whose network the target cannot match at all is refused. 

A source named twice with different targets is refused for all four mapping flags instead of silently taking the last value; naming the same source twice with the same target is accepted. 

JSON output:
- `abr --output json restore list` returns `{ "items": [...] }`
- `abr --output json restore start ...` returns the queued restore session ID plus source and target placement details

Tracking progress:
- Pass --wait to follow the restore's real progress (abr-vmdk-tool's own percent per disk) on stderr as a live bar until it finishes, then print the final session state -- no separate command needed. Exits non-zero if the restore session does not end completed, same as `job start --wait`.
- Without --wait: use `abr session list`, then `abr session info --id <restore-session-id> --watch` to attach to it afterward.
- Use `abr session log --id <restore-session-id>`
 
3. Restore from Backup Copy (From remote / disaster storage) 
Description: The source backups are lost or their repository is unreachable, so the VM is restored from the restore points a backup copy job made in its own repository. A copy is a restore point of its own, with its own ID, and is restored in place or to a new VM exactly like any other: no --repo and no encryption key are needed (the copy job's key is used; a key given with --encryption-key that is not the one the copy was written with, such as the source job's, is refused before the restore starts with 409 encryption_key_mismatch), and the source repository and the source backups are never read. 
Find the copies: abr restore list --vm <name> lists them next to the source points (the REPOSITORY column tells them apart); abr restore list --job <copy job> lists only the copies. 
Syntax: abr restore start --restore-point <copy point id> (--in-place --force | --new-name <name> --target-inventory <name> --datastore <name>) [--power-on] 
Example: abr restore start --restore-point 6b0f2c1e-8d4a-4c55-9a1e-3f7d2b9c8e10 --new-name sql-01-dr --target-inventory DR-Inventory --datastore DR-SSD --power-on 
--repo, if given, only checks that the restore point is in that repository; the ID of a source point with the copy's repository is refused, since the copy has an ID of its own. 

SYSTEM COMMANDS 

Inspect health and version state. 

abr settings vsphere show 

Description: Show the server-wide vSphere API and VDDK library, which every vSphere hypervisor (inventory) without its own uses. 

abr settings vsphere show [--output json] 

It shows the API, the library (vddk_library, null when it follows the VDDK directory), the library in effect and the file it resolves to (e.g. libvixDiskLib.so.9.1.0.0), the VDDK directory and where it comes from (environment or default), whether this server can use each API now and why not (apis), every vSphere hypervisor with its own values (hypervisors), the VDDK libraries found on the backup server (detected_vddk_libraries: path, kind ovddk, vmware_vddk or system, and the file it resolves to), and whether an administrator has set the values (confirmed: false while they are the ones the server started with). abr inventory info shows one hypervisor's own values, the values in effect and where each comes from. Needs inventory.view. 

The values the server starts with. On its first start the server stores the server-wide API and VDDK library from ABR_VSPHERE_API (vddk or soap; vddk by default) and ABR_VSPHERE_VDDK_LIB (ovddk by default: the oVDDK library of the altastor-backup-ovddk package, /opt/altastor-backup/tools/ovddk/lib64/libvixDiskLib.so; default for the VDDK directory; or a full path), which /etc/altastor-backup/server.conf can set. A value that cannot be used is not stored: the server log says why, and the API starts as vddk, the library follows the VDDK directory; the same happens when oVDDK is not installed. A server that already has inventories, jobs or backups when it first gets these settings keeps working as before (vddk, the VDDK directory) unless server.conf sets them. Once stored, the values change only through abr settings vsphere set (or the Web UI); restarts and upgrades leave them as they are. 

abr settings vsphere set 

Description: Change the server-wide vSphere API and VDDK library. 

abr settings vsphere set [--api <vddk|soap>] [--vddk-lib <path|ovddk|default>] [--update-existing-hypervisors [--yes]] 

Examples: 
abr settings vsphere set 
abr settings vsphere set --vddk-lib ovddk 
abr settings vsphere set --vddk-lib /opt/vmware-vix-disklib/lib64/libvixDiskLib.so.9.1.0.0 
abr settings vsphere set --vddk-lib default 
abr settings vsphere set --api soap --update-existing-hypervisors --yes 

Without options, in a terminal, it shows the values in effect and asks for the API (Enter keeps it) and, for vddk, the library: it lists the libraries found on the backup server, following the VDDK directory, and another file (asked for as a full path); Enter takes oVDDK when it is installed, otherwise the current library. A VMware VDDK library must stay in the lib64 folder of its bundle: its transport plugins are found only through that folder. An answer that is not one of the choices changes nothing (exit code 2). Without a terminal, at least one option is required (exit code 2). Setting the values, even unchanged, marks them confirmed. 

The library is checked as for inventory add --vddk-lib. ovddk takes the oVDDK library the backup server has, and is refused when it has none. default, or the VDDK directory's own lib64/libvixDiskLib.so, makes it follow the VDDK directory again, so it changes with ABR_VMDK_TOOL_VDDK_DIR. Hypervisors with their own values keep them, unless --update-existing-hypervisors removes the API and VDDK library of each, so they all use the server-wide values from now on. It names those hypervisors and asks first; --yes skips the question, and without a terminal to ask on it is required (exit code 2 otherwise). The command then prints the hypervisors it changed. An API the server cannot use is refused (transport_unavailable), and the server's reason for refusing a value is printed as it is. Needs inventory.edit; recorded in the audit log. 

abr system version 

Description: Show the installed product version. 

abr system version 

abr system health 

Description: Show component health status. 

abr system health [--output json] 

The clock component reads jump_detected while retention is blocked by a forward clock jump (see abr system clock); that degrades the status and the command exits 4. 

abr system clock show 

Description: Show the server time, the last time it was known to be right, and whether retention is blocked by a forward clock jump. 

abr system clock show [--output json] 

The server writes its time to the catalog every 5 minutes. A jump of more than BACKUPSERVER_CLOCK_JUMP_HOURS (24 by default) blocks retention: one seen while the server runs (the wall clock against its monotonic clock), or a gap between the last time written and the next start - the server was off that long or its clock was moved. The block survives a restart. The command exits 4 while it is blocked. 

abr system clock confirm 

Description: Confirm the current server time is right, so retention deletes expired restore points again on its next pass. Administrator only; it is recorded in the audit log. 

abr system clock confirm 

Check the time first: a restore point removed on a wrong clock cannot be brought back. 

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
