# Notifications

When an event an Administrator chose happens (a backup job's run failed, a
repository is filling up, the server clock jumped), the appliance tells people
by e-mail and on the messaging platforms an Administrator adds:

- **E-mail**, to a list of recipients, through the organisation's own SMTP
  server, signing in with a user name and password, over TLS.
- **Telegram**, a bot posting to up to ten chats.
- **Slack**, an incoming webhook posting to a channel.
- **Discord**, a webhook posting to a channel.
- **ntfy**, a topic on ntfy.sh or a server of your own, shown as push
  notifications by the ntfy apps.

Messaging platforms go out through Apprise. See
[Messaging platforms](#messaging-platforms). Each channel has its own choice of
events, and each works without the others.

Settings are changed in the Web UI (Settings → Notifications), with
`abr notification`, or with the API; the three change the same records and
offer the same settings.

## How it works

| Module (`python/srv/services/`) | Holds |
|---|---|
| `notification_events.py` | The events, the `Notification` a component hands over, `DeliveryError` |
| `notification_service.py` | `notify()`, and for each channel a queue, a worker and a history |
| `notification_detectors.py` | What works out that an event happened |
| `email_channel.py` | The e-mail channel: settings, validation, delivery |
| `chat_channels.py` | The messaging platforms: settings, validation, delivery |
| `apprise_runner.py` | Running Apprise for one message and target |

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
waiting, and a slow chat service does not hold up e-mail.

A channel is a module with `ID`, `LABEL`, `RETRY_DELAYS_SECONDS`,
`REPEAT_AFTER_RESTART` (whether a delivery the server stopped in the middle of
is tried again when it starts), `wants(notification)`, `describe(notification)` for the history entry, and
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
| `backup.verify_failed` | on | A backup verification - abr backup verify, a job's verify schedule or abr repo verify - found damaged data, which marks those restore points and the ones that build on them corrupt, or could not check every restore point. |
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
| Timeout | 5–300 seconds (30 by default), for the connection |

`starttls` refuses to go on when the server does not offer STARTTLS rather than
sending in the clear.

## Authentication

| Method | What the appliance does |
|---|---|
| `password` | SMTP AUTH (CRAM-MD5, PLAIN or LOGIN, the first the server offers) with the user name and password. |
| `none` | Sends without signing in, for a relay that trusts the appliance's address. |

Signing in over `security: none` is allowed but logged as a warning.

Earlier builds also offered OAuth 2.0 sign-in. It was removed: a stored
`oauth2` setting reads as `password` with no password, so delivery fails with
`smtp_auth_failed` until a password is entered, and its OAuth secrets leave the
catalog with the next change.

## Sender and recipients

The sender address (and display name) is the From of every message; the
signed-in account must be allowed to send as it. The display name is one line
of at most 100 characters: a line break or another control character in it is
refused, so it can never add a header to the message. Recipients: 1 to 50 addresses;
every message goes to all of them in one delivery.

## Validation

`PUT /api/v1/notifications/email` refuses the whole change when any value is
invalid, and answers `422 invalid_notification_settings` with every problem by
field in `error.details.fields` (`smtp.host`, `smtp.port`, `smtp.username`,
`smtp.password`, `sender.address`, `recipients`,
`events.<id>`, ...). The Web UI shows each one beside its field.

- Values are always checked: host name syntax (at most 253 characters) or an
  IPv4 or IPv6 address, port range, security, auth method, address syntax, a
  sender name of one line and at most 100 characters, at most 50 recipients,
  known event IDs with true or false, threshold 50–99, timeout 5–300.
- While notifications are on (or for a test) every setting delivery needs must
  be there as well: server, sender, at least one recipient, and for `password`
  the user name and password. Switched off, an incomplete configuration can be
  kept.

## Secrets

The SMTP password is stored encrypted with the server's credential key (as
inventory passwords are) and never returned: the API reports `password_set`.
In a change, a password left out (or null) keeps the stored one and an empty
text removes it. The audit log masks it. The Telegram bot token, the webhook
URLs of Slack and Discord, and ntfy's topic URL and access token are kept the
same way (`bot_token_set`, `webhook_url_set`, `access_token_set`).

## Test e-mail

`POST /api/v1/notifications/email/test` (`abr notification test`, the Web UI's
Send test e-mail) sends a message now, synchronously, with the stored settings
and any unsaved changes laid over them; nothing is saved. It goes to the
configured recipients, or to `to`, a list of addresses (anything else is
refused with `422 invalid_notification_settings` naming `to`). With `to` no recipients need to be saved, so
the server, sign-in and TLS can be tried before a recipient list exists; without it, at least one
recipient must be configured. The answer is `200` with the recipients and
how long it took, or `502` naming why it did not go:

| Code | Stage | Meaning |
|---|---|---|
| `smtp_connection_failed` | connect, send | The name does not resolve, the port refuses or does not answer within the timeout, or the connection broke. |
| `smtp_tls_failed` | tls | The certificate is not trusted, the handshake failed (often `ssl` on a STARTTLS port or the other way round), or the server offers no STARTTLS. |
| `smtp_auth_failed` | auth | The server refused the sign-in, offers none on this connection, no password is stored, or the stored one can no longer be decrypted. |
| `smtp_sender_refused` | send | The server refused the From address. |
| `smtp_recipients_refused` | send | The server refused every recipient. |
| `smtp_error` | send | Another refusal of the message. |

A test is recorded in the delivery history like any other message.

## Delivery, retries and failures

A message whose delivery failed for a reason that can pass (connection,
timeout, an SMTP 4xx) is tried again after 30 s and after 2 min, three attempts
in all; a refusal (authentication, TLS, sender, recipients) is not retried. The
operation the message reports on is never affected.

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

A message still waiting when the server stops, `queued` or `retrying`, is kept
in the catalog (`system_state` `email_notification_deliveries_waiting`, until
it is sent, fails or is cancelled) and taken up again when the server starts: a
retry at its next attempt, or at once when that time passed while the server
was down, with its attempts counting on; a `queued` message at once. A message
the server was handing to the SMTP server at the moment it stopped may arrive
twice. A message left `queued` or `retrying` by an earlier version, which did
not keep it, ends `failed` with the code `interrupted` at the first start.

## Messaging platforms

The platforms are Telegram, Slack, Discord and ntfy. A platform is
**added** once its settings are first saved (`added: true`); the Web UI's
**Add a platform** offers the others. **Removing** one (`DELETE
/notifications/{platform}`, `abr notification <platform> remove`) puts its
record back to the defaults, its secret with it, so it sends nothing until it is
added again; its delivery history stays, and the record keeps who removed it
and when. Each added platform has its own switch, events, test message and
history.

### Apprise

Messaging platforms go out through Apprise
(https://github.com/caronc/apprise, BSD-2-Clause). It runs as a program of its
own, `/usr/bin/apprise` from the Debian package `apprise` (`ABR_APPRISE` names
another one), once per message and target. Its libraries never load into the
server, and a run that hangs is stopped after 60 seconds with whatever it
started. The server package needs `apprise (>= 1.9)`. Debian 13 has 1.9.3.
Without Apprise, platforms can be set up but not switched on, tested or sent,
and the API reports `apprise.available: false`.

A target's URL holds its secrets. It is never put on a command line, where
any local user could read it: it is written to a private (0600) configuration
file for the run, which is removed afterwards. Apprise is kept from loading
anything else: no plugins from its plugin folders (`--plugin-path` names an
empty folder), no configuration from `/etc` or the home folder, no `include`
(`--recursion-depth 0`), no cache on disk (`--storage-mode memory`), no
attachments, and an environment built from nothing (`PATH`, `LANG`, and `HOME`
and `TMPDIR` set to the run's folder). The message body goes in on standard
input. Apprise's debug output carries the URL and the message, so only its
warnings, errors and the reason a connection failed are read, with every
secret of the target masked.

Apprise reaches the services over HTTPS (port 443) directly; there is no proxy
setting. It checks their certificates against the system trust store.

### Telegram

1. Make a bot with @BotFather in Telegram (`/newbot`); it gives the bot's
   token, `123456789:AAE...`.
2. Add the bot to each group or channel it should post to (in a channel, as an
   administrator that may post). For a private chat, start a chat with the bot.
3. Find each chat's ID: a group's or channel's is negative, such as
   `-1001234567890` (send a message there and read `chat.id` from
   `https://api.telegram.org/bot<token>/getUpdates`). A public channel can
   also be given as `@name`. A forum topic follows a colon: `-1001234567890:42`.

The settings are the bot token and up to ten chats. Each chat is sent to on
its own, so a retry goes only to the chats that did not get the message, and
the history's `delivered_to` lists those that did. A token pasted with the
`bot` prefix of the API URL is taken without it. The number before the colon
(`bot_id`) is shown, so people can tell which bot is set up. Messages longer
than Telegram's 4,096 characters are split.

### Slack

In Slack, open the app at api.slack.com/apps (or create one), turn on
**Incoming Webhooks** and add a webhook to the channel. Its URL,
`https://hooks.slack.com/services/T.../B.../...`, is the one setting; another
form is refused. Messages are posted as "AltaStor" (a webhook made by a Slack
app posts under the app's name whatever the message says), without Apprise's
icon and footer.

### Discord

In Discord, open the channel's settings, **Integrations**, **Webhooks**, make a
webhook and use **Copy Webhook URL**. Its URL,
`https://discord.com/api/webhooks/<id>/<token>`, is the one setting; one on
`discordapp.com`, `ptb.discord.com` or `canary.discord.com`, or with an API
version (`/api/v10/`), is the same webhook and is stored in that form.
Messages are posted under the webhook's own name and picture, and those longer
than Discord's 2,000 characters are split.

### ntfy

ntfy publishes to a **topic**, on the public ntfy.sh or on a server of your
own, and its apps (phone, desktop, web) show each message as a push
notification. The settings are the topic's URL, `https://ntfy.sh/<topic>` or
`https://<your server>/<topic>` (the topic of letters, digits, `-` and `_`, at
most 64), and, for a server with access control, an **access token** (`tk_...`,
from `ntfy token add <user>` or the web app's Account page) of a user that may
publish to the topic. Both are kept encrypted: on ntfy.sh whoever knows a
topic's name can read it, so the name should be long and hard to guess.
Messages carry the title and text; a failure goes at ntfy's high priority,
which phones show more prominently. Apprise's icon is left out.

### Settings and validation

As for e-mail: values are always checked, and while a platform is on (or for a
test) every setting a message needs must be there, and Apprise must read them
(a `--dry-run`, which sends nothing). The whole change is refused with
`422 invalid_notification_settings` and every problem by field (`bot_token`,
`chat_ids`, `webhook_url`, `enabled` when Apprise is not installed,
`events.<id>`). Each channel has its own event choice, apart from e-mail's;
an event it does not mention takes its default.

The message's title is the e-mail's subject (`[AltaStor] FAILED: ...`), its
text the e-mail's body and a line naming the product, version and host.

### Delivery, retries and failures

As for e-mail: a failure that can pass is tried again after 30 s and after
2 min, a refusal is not, and a message still waiting when the server stops is
taken up again when it starts (one cut off in the middle may arrive twice).
The latest 50 deliveries are kept (`<platform>_notification_deliveries` in
`system_state`).

| Code | Stage | Meaning | Retried |
|---|---|---|---|
| `connection_failed` | connect | The service's name does not resolve, it refused the connection, did not answer, or cannot be reached. | yes |
| `tls_failed` | tls | Its certificate is not trusted (a TLS-inspecting proxy whose certificate is not in the trust store, for one), or the handshake failed. | not for an untrusted certificate |
| `delivery_refused` | send | The service answered 4xx: a wrong bot token or access token (401, 403), a chat the bot is not in (400, 403), a webhook that was deleted (404, 410). Its reason is quoted. | no |
| `delivery_failed` | send | The service answered 5xx, 408 or 429. | yes |
| `timed_out` | send | Apprise did not finish within 60 seconds and was stopped. | yes |
| `apprise_missing` | start | Apprise is not installed. | no |
| `apprise_not_started` | start | Apprise could not be started. | no |
| `apprise_url_refused` | start | Apprise cannot read the settings. | no |
| `apprise_failed` | start | Apprise ended with another exit code. | no |

`POST /notifications/{platform}/test` sends a test
message now, synchronously, with the stored settings and any unsaved changes;
nothing is saved. The answer is `200` with where it went and how long it took,
or `502` with the code, the stage and `delivered_to`. A test is recorded in the
history like any other message.

## API

| Method and path | Function | |
|---|---|---|
| `GET /api/v1/notifications/email` | `notification.view` | The settings, the events with whether each is on, and the 20 latest deliveries |
| `PUT /api/v1/notifications/email` | `notification.edit` | Change the settings; fields left out keep their value |
| `POST /api/v1/notifications/email/test` | `notification.test` | `{"settings": {...unsaved changes}, "to": [...]}`, both optional |
| `GET /api/v1/notifications/email/deliveries` | `notification.view` | The latest 50 deliveries |
| `GET /api/v1/notifications/events` | `notification.view` | The events, with whether each sends e-mail |
| `GET /api/v1/notifications/messaging` | `notification.view` | Every platform, added or not, as below, and `apprise` (`available`, `version`) |
| `GET /api/v1/notifications/{platform}` | `notification.view` | One platform: `id`, `name`, `added`, `enabled`, Telegram's `bot_token_set`, `bot_id` and `chat_ids` or the others' `webhook_url_set` and `webhook_host` (and ntfy's `access_token_set`), `apprise`, the events and the 20 latest deliveries |
| `PUT /api/v1/notifications/{platform}` | `notification.edit` | `{"enabled", "events"}` and Telegram's `"bot_token", "chat_ids"`, or the others' `"webhook_url"` (ntfy: the topic URL, and `"access_token"`); fields left out keep their value. Adds the platform when it was not |
| `DELETE /api/v1/notifications/{platform}` | `notification.edit` | Remove the platform: its settings and secret go, its history stays |
| `POST /api/v1/notifications/{platform}/test` | `notification.test` | `{"settings": {...unsaved changes}}`, optional |
| `GET /api/v1/notifications/{platform}/deliveries` | `notification.view` | The latest 50 deliveries |

`{platform}` is `telegram`, `slack`, `discord` or `ntfy`; another answers
`404 notification_platform_not_found`.

The e-mail settings:

```json
{
  "enabled": true,
  "smtp": {
    "host": "smtp.office365.com", "port": 587, "security": "starttls", "verify_tls": true,
    "auth": "password", "username": "backup@contoso.com", "password": "…", "timeout_seconds": 30
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
                     [--verify-tls | --no-verify-tls] [--timeout S] [--auth password|none]
                     [--username U] [--password [V]] [--from ADDRESS] [--from-name NAME]
                     [--recipients A,B | --add-recipient A --remove-recipient B] [--space-threshold PCT]
abr notification test [--to ADDRESSES] [any `set` option, to test it without saving]
abr notification history
abr notification events list
abr notification events set [--enable IDS] [--disable IDS]
abr notification platforms
abr notification telegram show|history|remove
abr notification telegram set [--enable | --disable] [--bot-token [V]] [--chats IDS]
                              [--event-on IDS] [--event-off IDS]
abr notification telegram test [--bot-token [V]] [--chats IDS]
abr notification slack|discord|ntfy show|history|remove
abr notification slack|discord set [--enable | --disable] [--webhook-url [V]]
                                   [--event-on IDS] [--event-off IDS]
abr notification slack|discord test [--webhook-url [V]]
abr notification ntfy set [--enable | --disable] [--topic-url [V]] [--access-token [V]]
                          [--event-on IDS] [--event-off IDS]
abr notification ntfy test [--topic-url [V]] [--access-token [V]]
```

A secret flag given without a value reads `ABR_SMTP_PASSWORD`,
`ABR_TELEGRAM_BOT_TOKEN`, `ABR_SLACK_WEBHOOK_URL`, `ABR_DISCORD_WEBHOOK_URL`,
`ABR_NTFY_TOPIC_URL` or `ABR_NTFY_ACCESS_TOKEN`, or prompts. A refusal
exits 2 with the problems by field; a failed test exits 1 with the code and
reason.

## Limitations

- The appliance connects to the SMTP server and the messaging platforms
  directly; there is no proxy setting.
- The settings and the histories are not part of `abr system export`; set them
  again after importing a catalog onto a new appliance.
- One recipient list for every event; no per-event recipients, no HTML body.
- Microsoft Teams is not offered: Apprise 1.9.3, Debian 13's, cannot read
  the workflow URLs Teams gives now (they need Apprise 1.13.0 or later).
- The duplicate guards (one message per run, low space once per crossing,
  certificate once per certificate) are kept in memory, so a restart in the
  middle can repeat one message.
