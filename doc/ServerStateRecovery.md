# Backing Up And Restoring Server`s State


## Prerequisites

- `abr` installed, pointed at a running server
- somewhere to store the export that isn't the server itself

## 1. Export the catalog

```bash
abr system export --file <path>
```

Prompts for a password twice - this encrypts the file and is
required on import.

For scheduled/scripted exports:

```bash
export ABR_EXPORT_PASSWORD='<password>'
abr system export --file <path>
```

### Inventory credentials

Inventory passwords are stored encrypted in the catalog database, with a key
kept at `/var/lib/altastor-backup/credentials.key` (`ABR_CREDENTIAL_KEY_FILE`).
The export above does not depend on that key: it carries the credentials inside
its own password-protected file, and the new host encrypts them again with its
own key on import.

The key matters only if you copy the raw database files instead of using
export/import. Copy the key file with them; without it the server starts and
logs, for each inventory, that its stored password cannot be decrypted. Such an
inventory does not try that password against vCenter; it reports the same
reason on every connection check, rescan or backup until its password is
entered again (`--password` reads `ABR_INVENTORY_PASSWORD` or prompts for it):

```bash
abr inventory edit --name <name> --password
```

A key file that is there but cannot be used (unreadable by the server's user,
empty, or not a key) is never replaced. The server still starts, logs the path
and the reason, leaves every stored credential as it is and refuses to store a
new password until the file is put back and the server restarted.

Session tokens are stored only as SHA-256 digests, so they need no key. Server
versions before this change wrote inventory passwords and session tokens in
clear, and MongoDB's slow-query log may still hold copies from that time in
`/var/log/mongodb/mongod.log*`; remove or rotate those files after upgrading.

## 2. Provision the replacement host

Install `abr` and a running server on the replacement host. Catalog is
empty at this point.

## 3. Import the catalog

```bash
abr system import --file <path>
```

Prompts for the password from step 1.

- The users in the export replace those of the same name on this server, such
  as the `admin` the fresh install created. Their sessions end, so log in again
  with the password the user had on the exported server. Users the export does
  not name are kept.
- An export that cannot be imported whole (two different users of the same
  name, or a credential key this server cannot use) is refused before anything
  is written.

- Recovered server configuration (if any) is written to a file next to the
  export, not applied automatically - paths/ports can differ on the new
  host. Review and merge into `/etc/default/altastor-backup-server` by hand.

## 4. Re-verify repositories

```bash
abr repo list
abr repo verify --name <name>   # for each repository
```

Restore points already show up in `abr restore list` right after import -
`verify` is what confirms the physical storage is actually reachable from
the new host before you rely on it.

## 5. Confirm a restore works

```bash
abr restore list
abr restore start --point-id <id> --target-inventory <name> --target-host <host> --datastore <name> --new-name <name>
```