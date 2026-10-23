# Roles, permissions and the audit log

Every user of AltaStor Backup & Replication holds exactly one role, and a role
decides which product functions the user may run. The server checks the role
before a function runs, for every interface: the Web UI, the `abr` CLI,
`--output json` automation and direct API calls. Every command, and every user
or role change, is written to the audit log with the role the user held at the
time.

## Functions

A function is one thing the product does, named by a stable function ID of the
form `area.action`: `job.start`, `backup.delete`, `user.edit`. Every API
operation runs exactly one function; the OpenAPI document (`/docs`) names it
on each operation (`x-function-id`), and every `abr` command names it at the
end of its `--help` ("Permission: job.start"). `abr role functions` and
`GET /api/v1/functions` list them all.

Each function has a category: `read`, `operate`, `configure`, `destructive`,
`administer`, or `self-service` (signing in and out and changing one's own
password, which every role may do).

## Roles

The five roles are built in. They live in the server's code
(`python/srv/services/rbac.py`), not in the catalog, so no interface can
create, change or delete them.

| Role | ID | For |
|---|---|---|
| Administrator | `administrator` | Everything, including users, roles, the HTTPS certificate, catalog export and import, and support bundles. |
| Backup Administrator | `backup_administrator` | Repositories, inventories, backup jobs and backups. |
| Backup Operator | `backup_operator` | Running and monitoring backup jobs, and verifying backups. |
| Restore Operator | `restore_operator` | Restoring VMs, with the inventory operations a recovery needs (rescan, test). |
| Auditor | `auditor` | Read-only: configuration, operational history, users, roles and the audit log. |

## Permission matrix

A role holds an explicit allowed or disabled permission for every function. A
function a role does not list is disabled, so access is denied unless granted.
The Administrator is allowed every function, including ones added later; every
other role is allowed exactly what it lists.

| Function | Category | Admin | Backup Admin | Backup Op | Restore Op | Auditor |
|---|---|:-:|:-:|:-:|:-:|:-:|
| `auth.login` | self-service | ✓ | ✓ | ✓ | ✓ | ✓ |
| `auth.logout` | self-service | ✓ | ✓ | ✓ | ✓ | ✓ |
| `auth.whoami` | read | ✓ | ✓ | ✓ | ✓ | ✓ |
| `auth.change_password` | self-service | ✓ | ✓ | ✓ | ✓ | ✓ |
| `auth.extend_session` | self-service | ✓ | ✓ | ✓ | ✓ | ✓ |
| `auth.touch_session` | self-service | ✓ | ✓ | ✓ | ✓ | ✓ |
| `dashboard.view` | read | ✓ | ✓ | ✓ | ✓ | ✓ |
| `system.view` | read | ✓ | ✓ | ✓ | ✓ | ✓ |
| `system.logs` | read | ✓ | ✓ | – | – | ✓ |
| `system.support_bundle` | administer | ✓ | – | – | – | – |
| `system.export` | administer | ✓ | – | – | – | – |
| `system.import` | administer | ✓ | – | – | – | – |
| `system.clock_confirm` | administer | ✓ | – | – | – | – |
| `tls.view` | read | ✓ | – | – | – | ✓ |
| `tls.edit` | administer | ✓ | – | – | – | – |
| `user.view` | read | ✓ | – | – | – | ✓ |
| `user.create` | administer | ✓ | – | – | – | – |
| `user.edit` | administer | ✓ | – | – | – | – |
| `user.delete` | administer | ✓ | – | – | – | – |
| `role.view` | read | ✓ | – | – | – | ✓ |
| `audit.view` | read | ✓ | – | – | – | ✓ |
| `repository.view` | read | ✓ | ✓ | ✓ | ✓ | ✓ |
| `repository.create` | configure | ✓ | ✓ | – | – | – |
| `repository.edit` | configure | ✓ | ✓ | – | – | – |
| `repository.delete` | destructive | ✓ | ✓ | – | – | – |
| `repository.rescan` | operate | ✓ | ✓ | – | – | – |
| `repository.verify` | operate | ✓ | ✓ | – | – | – |
| `repository.apply_immutability` | configure | ✓ | ✓ | – | – | – |
| `inventory.view` | read | ✓ | ✓ | ✓ | ✓ | ✓ |
| `inventory.create` | configure | ✓ | ✓ | – | – | – |
| `inventory.edit` | configure | ✓ | ✓ | – | – | – |
| `inventory.delete` | destructive | ✓ | ✓ | – | – | – |
| `inventory.rescan` | operate | ✓ | ✓ | – | ✓ | – |
| `inventory.test` | operate | ✓ | ✓ | – | ✓ | – |
| `job.view` | read | ✓ | ✓ | ✓ | – | ✓ |
| `job.create` | configure | ✓ | ✓ | – | – | – |
| `job.edit` | configure | ✓ | ✓ | – | – | – |
| `job.delete` | destructive | ✓ | ✓ | – | – | – |
| `job.start` | operate | ✓ | ✓ | ✓ | – | – |
| `job.retention` | destructive | ✓ | ✓ | – | – | – |
| `session.view` | read | ✓ | ✓ | ✓ | ✓ | ✓ |
| `session.stop` | operate | ✓ | ✓ | ✓ | ✓ | – |
| `backup.view` | read | ✓ | ✓ | ✓ | ✓ | ✓ |
| `backup.verify` | operate | ✓ | ✓ | ✓ | ✓ | – |
| `backup.delete` | destructive | ✓ | ✓ | – | – | – |
| `backup.import` | configure | ✓ | ✓ | – | – | – |
| `restore.view` | read | ✓ | – | – | ✓ | – |
| `restore.start` | operate | ✓ | – | – | ✓ | – |

The automated test `python/tests/srv/rbac/test_permission_matrix.py` holds the
same matrix written out, so any change to a role has to be made in both places.

## How a request is authorized

1. The session token is checked (`401 unauthorized` without a valid one). A
   disabled user's sessions end when they are disabled.
2. The route's function ID is checked against the user's role: the user must
   be enabled, hold a known role, and the role must allow the function.
3. Only then does the operation read or change anything. A denied request
   changes nothing.

A denied request answers `403` with one error shape on every route:

```json
{"error": {"code": "permission_denied",
            "message": "Permission denied: The Auditor role does not allow job.start (start a job).",
            "details": {"function_id": "job.start", "role": "auditor", "reason": "not_permitted"}}}
```

`reason` is `not_permitted`, `no_role` (the user's role is missing or unknown),
`account_disabled`, or `unknown_function`. The CLI exits with code 7 and, with
`--output json`, prints this error on stderr. A disabled user, or one without a
valid role, cannot sign in: `403` with code `account_disabled` or
`role_invalid`.

`GET /api/v1/auth/me` (`abr auth whoami`) answers the signed-in user, their
role and the function IDs it allows; the sign-in answer carries the same list.
The Web UI uses it to hide or disable what the role does not allow and never
requests a read the role does not allow, but it is not what protects anything:
the server refuses the request either way.

## Users and the last Administrator

- `abr user add --role <role>` / `POST /api/v1/users` creates a user; the role
  is required.
- `abr user edit` / `PATCH /api/v1/users/{id}` changes the login name, the
  full name, the role, the enabled state (`enabled: false` disables) or the
  password. A role change applies to the user's next request. A new login name
  keeps the password; a new password (`--reset-password`, `password`) ends the
  user's sessions, and is refused with `409 own_password` for the caller's own,
  which `abr auth change-password` changes with the current one.
- At least one enabled Administrator always remains. Demoting, disabling or
  deleting the last one is refused with `409 last_administrator`, and so is a
  catalog import (`system import`) that would leave none.
- Users from before roles existed become enabled Administrators when the
  server starts, which is what they could do before, and so do such users in an
  imported catalog.

## The audit log

The server writes one record per API request that runs a function, whether it
was allowed, denied, or failed, from every interface. Each record holds:

| Field | |
|---|---|
| `timestamp` | UTC, `2026-10-18T21:05:00.997Z` |
| `user`, `user_id` | Who; for a sign-in, the name that was tried |
| `role` | The role the user held at the time |
| `function_id` | What |
| `target` | What it acted on, when there is one: `{"type": "job", "id": "...", "name": "nightly"}` |
| `result` | `allowed`, `denied` or `failed` |
| `session_id` | The backup, restore, verify or delete session the command started; `details.session_ids` lists them all when there are several |
| `auth_session_id` | A reference to the sign-in session that made the request (not usable to sign in) |
| `interface` | `web`, `cli` or `api`; `command` names the CLI command |
| `status`, `error` | The HTTP status, and the error code and message when not allowed |
| `parameters` | The request, for functions that change something |
| `details` | For a user change, what changed: `{"role": {"from": "auditor", "to": "backup_operator"}}` |

Passwords, tokens, secret and access keys, encryption keys, passphrases and
other credentials are masked (`********`) before a record is written: by key
name in the parameters, and in any text that reads like one (`password=...`,
`Bearer ...`, `https://user:pass@...`).

Reads that a client repeats on its own are not recorded: the Web UI's
background refresh and the CLI's progress polling send `X-ABR-Refresh: 1`. Such
a read is still recorded when it is denied, and every action is recorded. The
Web UI's keep-alive (`auth.touch_session`) is not recorded either.

Records are stored in the catalog's `audit` collection (in memory when the
server runs without MongoDB), and each one is also written as a line to the
server log. Read them with `abr audit list`, `GET /api/v1/audit`, or the Web
UI's Logs page; the `audit.view` function (Administrator, Auditor) is needed.

## Adding a function

1. Add it to `FUNCTIONS` in `python/srv/services/rbac.py` with its category
   (and `starts_session=True` if it starts a session).
2. Grant it to the roles that need it in `_GRANTS`; the Administrator has it
   already.
3. Register the route with the function ID (`register(router, [(path,
   handler, methods, "area.action")])`).
4. Map the CLI command to it in `python/cli/command_functions.py`.
5. Add it to the matrix in `test_permission_matrix.py` and to the table above.
