# Two-factor sign-in

A user can add a second step to signing in: after the password, a 6-digit code
from an authenticator app on their phone. Any app that does standard TOTP
(RFC 6238) works, such as Google Authenticator or Microsoft Authenticator. The
appliance needs no internet access: the phone makes the codes from a secret it
got when the QR code was scanned, and its clock.

It is optional and per user, and set up and managed in the Web UI: Settings ->
Security -> Two-factor sign-in. The CLI only signs in with it. Scripts that sign
in with `ABR_USERNAME` and `ABR_PASSWORD` keep working for a user who has not
turned it on.

## Turning it on

The Web UI asks for the password, shows a QR code and the same secret as text,
and turns two-factor sign-in on once a code from the app proves the app holds
the secret. Until then sign-in does not change. Turning it on signs the user out
everywhere else.

Keep the ten recovery codes that come back. They are shown only then. Each signs
in once, in place of a code from the app; the appliance keeps only their
digests, never the codes.

If one person signs in to several appliances, set `ABR_TOTP_ISSUER` differently
on each: it is the name the app shows. It applies to setups made after the
change.

## Signing in

In the Web UI the code is asked for after the password; "Use a recovery code
instead" takes a recovery code.

```bash
abr auth login --name jdoe    # then asks: Authentication code:
```

The CLI asks for the code at a prompt that does not echo it. It never takes the
code from a flag or an environment variable, which a shell keeps in its
history, and it takes no recovery code: use the Web UI for that. A refused code
exits with code 5 (`UNAUTHORIZED`) and says why.

## Which codes are accepted

- A code is accepted within `ABR_TOTP_VALID_WINDOW` 30-second steps either side
  of the server's time: 1 by default, so about 30 seconds either way for a phone
  whose clock is a little off. 0 accepts only the current step; each step added
  gives someone guessing one more valid code at a time.
- A code works once: a code the server has accepted, and anything older, is
  refused as already used (`totp_code_used`). Wait for the next one.
- A code from before the window, up to five minutes old, is refused as expired
  (`totp_code_expired`). If that keeps happening, check the clocks on the phone
  and the appliance; the phone should set its time automatically.
- Anything else is invalid (`invalid_totp_code`), and an unknown or used
  recovery code too (`invalid_recovery_code`).

Every refused code counts towards the same lockout as a wrong password
(`ABR_LOGIN_MAX_ATTEMPTS`, `ABR_LOGIN_LOCKOUT_SECONDS`). With the defaults,
someone who already has the password gets 1,440 guesses a day.

## While it is on

Settings -> Security shows that it is on and how many recovery codes are left,
and offers three things. Each asks for the password and a code from the app or
a recovery code: a session alone is never enough, so a stolen session cannot
change or remove the second step.

- **Set up again**, for a new phone: a new QR code and secret. The current app
  keeps working until a code from the new one is accepted; then the previous
  secret and the earlier recovery codes stop working, and new recovery codes
  are shown.
- **New recovery codes**: every earlier recovery code stops working.
- **Turn off**.

The secret is shown only while setting up, never afterwards.

## A lost phone

The user signs in with a recovery code and sets it up again. Without recovery
codes, an Administrator resets it in Settings -> Users -> edit the user (or
`totp_reset` on `PATCH /api/v1/users/{id}`), which also ends that user's
sessions. Nobody resets their own.

## The last Administrator

If no other Administrator can reset it and the recovery codes are gone, reset it
on the appliance itself. This needs shell access and the catalog database,
which is the point: stop the server, run this with `MONGODB_URI`,
`MONGODB_DB_NAME` and `ABR_USER` set from the server's configuration, and start
the server again. It answers how many users it changed, which must be 1.

```python
import os
from pymongo import MongoClient

users = MongoClient(os.environ["MONGODB_URI"])[os.environ["MONGODB_DB_NAME"]]["users"]
result = users.update_one(
    {"name": os.environ["ABR_USER"]},
    {"$set": {"totp_enabled": False, "totp_secret": None, "totp_pending_secret": None,
              "totp_last_step": None, "totp_recovery": []}},
)
print(result.modified_count)
```

Save it as a file and run it with the server's own Python, which has `pymongo`.

## Logs and storage

Secrets, codes and recovery codes reach no log, no audit record and no error
message. The secret, and a new one being set up, are stored encrypted with the
credential key, like inventory passwords; recovery codes only as digests.

A catalog export carries the secret inside its own password-protected file,
and the new server encrypts it again with its own key, so the same
authenticator app and recovery codes keep working.

If you copy the raw database files without the key file, the server cannot read
the secret: a code from the app is refused with `totp_secret_unreadable` and the
log names the user. Recovery codes still work, so put the key file back, or sign
in with one and set two-factor sign-in up again. See `ServerStateRecovery.md`.
