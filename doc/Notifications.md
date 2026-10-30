# E-mail notifications

The appliance e-mails a list of recipients when an event an Administrator chose
happens: a backup job's run failed, a repository is filling up, the server
clock jumped. It sends through the organisation's own SMTP server, signing in
with a user name and password or with OAuth 2.0, over TLS.

Settings are changed in the Web UI (Settings → Notifications → E-mail
notifications), with `abr notification`, or with the API; the three change the
same record and offer the same settings.

## How it works

| Module (`python/srv/services/`) | Holds |
|---|---|
| `notification_events.py` | The events, the `Notification` a component hands over, `DeliveryError` |
| `notification_service.py` | `notify()`, and for each channel a queue, a worker and a history |
| `notification_detectors.py` | What works out that an event happened |
| `email_channel.py` | The e-mail channel: settings, validation, delivery |

`notification_service` is the one place every component hands a notification
to:

```python
notification_service.notify("job.failed", subject, body, severity="error", details={...})
```

`notify` never raises and never waits for a delivery. `body` is plain text and
`details` the same facts for a program to read. Each channel says whether it
wants the notification (it is on, and that event is chosen); for those that do,
the message is recorded as `queued` in that channel's history and put on its
queue (500 deep). A background worker per channel delivers it with the settings
as they are at that moment, so a change of recipients applies to messages still
waiting.

A channel is a module with `ID`, `LABEL`, `RETRY_DELAYS_SECONDS`,
`wants(notification)`, `describe(notification)` for the history entry, and
`send(notification, entry)`, which returns what to add to the entry or raises
`DeliveryError`.

The events are detected in `notification_detectors`, so a component only
reports what happened:

| Detector | Called from | Raises |
|---|---|---|
| `task_finished(task)` | `TaskProcessPool._finish_task` (every final state, a crash included) and `JobScheduler._mark_stale_task_failed` | `job.*` once per job run, when its last VM's task has finished; `restore.*` per restore; `repository.low_space` after a run |
| `clock_jumped(...)` | `clock_guard._block` | `system.clock_jump` |
| `check_certificate()` | a daily timer started with the TLS renewal check | `tls.certificate_expiring`, once per certificate |

A job run is all the tasks with the same `run_id`; the run's result is the
worst VM result (`job_run_summary`), and one notification covers every VM with
its result and, for a failed one, the reason. A run that was stopped sends
nothing.

## Events

| Event ID | Default | Sent when |
|---|---|---|
| `job.failed` | on | A backup job's run finished and the backup of at least one VM failed. |
| `job.warning` | on | A run finished, every VM was backed up, and at least one backup reported a warning. |
| `job.succeeded` | off | A run finished and every VM was backed up without a warning. |
| `restore.failed` | on | A VM restore failed. |
| `restore.succeeded` | off | A VM restore finished. |
| `repository.low_space` | on | After a run, the disk of the job's local or SMB repository is filled to `repository_space_threshold_percent` (85 by default) or beyond. Object storage has no fixed size and is not checked. Sent again only after the disk has dropped below the threshold and filled up again (kept in memory: a restart may send it once more). |
| `system.clock_jump` | on | The server clock jumped forward, so retention is paused until an Administrator confirms the time. |
| `tls.certificate_expiring` | on | An HTTPS certificate an administrator installed expires within 14 days or has expired (checked a minute after start and then daily). The appliance's own certificate renews itself and is not reported. |

`GET /api/v1/notifications/events` and `abr notification events list` list
them. An event a channel's settings do not mention takes its default.

E-mail messages are plain text. The subject starts `[AltaStor]`, then `FAILED:` or
`WARNING:` for those severities. Times are given in UTC and, when the appliance
runs on another zone, also in the appliance's zone, labelled. Each message ends
with the host name and version, and how to change which events send e-mail.
`Auto-Submitted: auto-generated` keeps out-of-office replies away.

## SMTP connection

| Setting | Values |
|---|---|
| Server, port | host name or IP address; 1–65535 |
| Security | `starttls` (connect, then upgrade to TLS before signing in; usually 587), `ssl` (TLS from the first byte; usually 465), `none` (unencrypted; usually 25, for a relay on a trusted network) |
| Verify TLS | on by default: the certificate must chain to the operating system's trust store and name the server. Off accepts any certificate. |
| Timeout | 5–300 seconds (30 by default), for the connection and for the OAuth token request |

`starttls` refuses to go on when the server does not offer STARTTLS rather than
sending in the clear.

## Authentication

| Method | What the appliance does |
|---|---|
| `password` | SMTP AUTH (CRAM-MD5, PLAIN or LOGIN, the first the server offers) with the user name and password. |
| `oauth2` | Gets an access token from the provider's token endpoint and signs in with SASL `XOAUTH2` as the mailbox in `username`. |
| `none` | Sends without signing in, for a relay that trusts the appliance's address. |

Signing in over `security: none` is allowed but logged as a warning.

### OAuth 2.0

Two grants are supported:

- `client_credentials`: an application registered with the provider signs in on
  its own and sends as the mailbox. Settings: client ID and secret (and the
  tenant for Microsoft).
- `refresh_token`: a user consented once, outside the appliance, and the
  appliance exchanges the refresh token for access tokens as needed. Settings:
  client ID, the refresh token, and the client secret when the provider needs
  one. A provider that returns a new refresh token (Microsoft rotates them)
  has it stored in place of the old one.

Access tokens are cached in memory until a minute before they expire, and
dropped when the SMTP settings change.

| Provider | Token endpoint | Default scope |
|---|---|---|
| `microsoft` | `https://login.microsoftonline.com/<tenant>/oauth2/v2.0/token` (`organizations` when no tenant is set) | `https://outlook.office365.com/.default` (client credentials), `https://outlook.office365.com/SMTP.Send offline_access` (refresh token) |
| `google` | `https://oauth2.googleapis.com/token` | `https://mail.google.com/`; refresh token only, as Google issues no client-credentials tokens for mail |
| `custom` | `token_url` (https only) | `scope`, if the provider needs one |

`scope` overrides the default for any provider.

Microsoft 365 with client credentials, in outline: register an application in
Entra ID, add a client secret, grant it the `SMTP.SendAsApp` application
permission of Office 365 Exchange Online with admin consent, register its
service principal in Exchange Online and give it `FullAccess` to the sending
mailbox, and make sure SMTP AUTH is enabled for that mailbox. Use smtp.office365.com,
port 587, STARTTLS.

Google Workspace: create an OAuth client, obtain a refresh token for the sending
user with the `https://mail.google.com/` scope, and use smtp.gmail.com, port 587,
STARTTLS.

## Sender and recipients

The sender address (and display name) is the From of every message; the
signed-in account must be allowed to send as it. Recipients: 1 to 50 addresses;
every message goes to all of them in one delivery.

## Validation

`PUT /api/v1/notifications/email` refuses the whole change when any value is
invalid, and answers `422 invalid_notification_settings` with every problem by
field in `error.details.fields` (`smtp.host`, `smtp.port`, `smtp.username`,
`smtp.password`, `smtp.oauth2.client_id`, `sender.address`, `recipients`,
`events.<id>`, ...). The Web UI shows each one beside its field.

- Values are always checked: host name syntax, port range, security, auth
  method, provider and grant names, https token URL, address syntax, at most
  50 recipients, known event IDs with true or false, threshold 50–99, timeout
  5–300.
- While notifications are on (or for a test) every setting delivery needs must
  be there as well: server, sender, at least one recipient, and the method's
  credentials (user name and password; or mailbox, client ID, tenant for
  Microsoft client credentials, token URL for custom, and the client secret or
  refresh token for the grant). Switched off, an incomplete configuration can
  be kept.

## Secrets

The SMTP password, the OAuth client secret and the refresh token are stored
encrypted with the server's credential key (as inventory passwords are) and
never returned: the API reports `password_set`, `client_secret_set` and
`refresh_token_set`. In a change, a secret left out (or null) keeps the stored
one and an empty text removes it. The audit log masks them.

## Test e-mail

`POST /api/v1/notifications/email/test` (`abr notification test`, the Web UI's
Send test e-mail) sends a message now, synchronously, with the stored settings
and any unsaved changes laid over them; nothing is saved. It goes to the
configured recipients, or to `to`. The answer is `200` with the recipients and
how long it took, or `502` naming why it did not go:

| Code | Stage | Meaning |
|---|---|---|
| `smtp_connection_failed` | connect, send | The name does not resolve, the port refuses or does not answer within the timeout, or the connection broke. |
| `smtp_tls_failed` | tls | The certificate is not trusted, the handshake failed (often `ssl` on a STARTTLS port or the other way round), or the server offers no STARTTLS. |
| `smtp_auth_failed` | auth | The server refused the sign-in, offers none on this connection, or a stored password can no longer be decrypted. |
| `oauth_token_failed` | oauth | The token endpoint could not be reached or refused the client, secret or refresh token; its reason is quoted. |
| `smtp_sender_refused` | send | The server refused the From address. |
| `smtp_recipients_refused` | send | The server refused every recipient. |
| `smtp_error` | send | Another refusal of the message. |

A test is recorded in the delivery history like any other message.

## Delivery, retries and failures

A message whose delivery failed for a reason that can pass (connection,
timeout, an SMTP 4xx, the token endpoint unreachable or answering 5xx) is tried
again after 30 s and after 2 min, three attempts in all; a refusal
(authentication, TLS, sender, recipients, a token request refused) is not
retried. The operation the message reports on is never affected.

Every outcome is recorded:

- in the delivery history (the latest 50, kept in the catalog's
  `system_state` as `email_notification_deliveries`): the event, subject,
  recipients, status (`queued`, `retrying`, `sent`, `failed`, `cancelled`), the
  attempts, the next attempt for a retry, the recipients the server refused
  while taking the message for the others, and the error code, message and
  stage. `GET /api/v1/notifications/email/deliveries`,
  `abr notification history`, and the Web UI's Recent deliveries;
- in the server log: one line per message sent (INFO), per attempt to be
  retried (WARNING) and per final failure (ERROR), with the code and reason.

A message still waiting when notifications are switched off is `cancelled`.

## API

| Method and path | Function | |
|---|---|---|
| `GET /api/v1/notifications/email` | `notification.view` | The settings, the events with whether each is on, and the 20 latest deliveries |
| `PUT /api/v1/notifications/email` | `notification.edit` | Change the settings; fields left out keep their value |
| `POST /api/v1/notifications/email/test` | `notification.test` | `{"settings": {...unsaved changes}, "to": [...]}`, both optional |
| `GET /api/v1/notifications/email/deliveries` | `notification.view` | The latest 50 deliveries |
| `GET /api/v1/notifications/events` | `notification.view` | The events, with whether each sends e-mail |

```json
{
  "enabled": true,
  "smtp": {
    "host": "smtp.office365.com", "port": 587, "security": "starttls", "verify_tls": true,
    "auth": "oauth2", "username": "backup@contoso.com", "timeout_seconds": 30,
    "oauth2": {"provider": "microsoft", "tenant_id": "contoso.onmicrosoft.com", "client_id": "0f1e…",
               "client_secret": "…", "grant": "client_credentials"}
  },
  "sender": {"address": "backup@contoso.com", "name": "AltaStor Backup"},
  "recipients": ["ops@contoso.com"],
  "events": {"job.succeeded": true, "repository.low_space": false},
  "repository_space_threshold_percent": 90
}
```

The Administrator holds all three functions and the Auditor holds
`notification.view` (see `doc/RBAC.md`).

## CLI

```
abr notification show
abr notification set [--enable | --disable] [--smtp-host H] [--smtp-port N] [--security starttls|ssl|none]
                     [--verify-tls | --no-verify-tls] [--timeout S] [--auth password|oauth2|none]
                     [--username U] [--password [V]] [--oauth-provider microsoft|google|custom]
                     [--oauth-tenant T] [--oauth-client-id ID] [--oauth-client-secret [V]]
                     [--oauth-grant client_credentials|refresh_token] [--oauth-refresh-token [V]]
                     [--oauth-token-url URL] [--oauth-scope S] [--from ADDRESS] [--from-name NAME]
                     [--recipients A,B | --add-recipient A --remove-recipient B] [--space-threshold PCT]
abr notification test [--to ADDRESSES] [any `set` option, to test it without saving]
abr notification history
abr notification events list
abr notification events set [--enable IDS] [--disable IDS]
```

A secret flag given without a value reads `ABR_SMTP_PASSWORD`,
`ABR_SMTP_OAUTH_CLIENT_SECRET` or `ABR_SMTP_OAUTH_REFRESH_TOKEN`, or prompts. A
refusal exits 2 with the problems by field; a failed test exits 1 with the code
and reason.

## Limitations

- The appliance connects to the SMTP server and the token endpoint directly;
  there is no proxy setting.
- The settings and the history are not part of `abr system export`; set them
  again after importing a catalog onto a new appliance.
- One recipient list for every event; no per-event recipients, no HTML body.
- The duplicate guards (one e-mail per run, low space once per crossing,
  certificate once per certificate) are kept in memory, so a restart in the
  middle can repeat one message.
