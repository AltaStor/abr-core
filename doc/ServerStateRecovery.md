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

## 2. Provision the replacement host

Install `abr` and a running server on the replacement host. Catalog is
empty at this point.

## 3. Import the catalog

```bash
abr system import --file <path>
```

Prompts for the password from step 1.

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