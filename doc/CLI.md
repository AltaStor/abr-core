# AltaStor CLI

The supported AltaStor Backup & Replication CLI is `abr`.

Use [AltaStorCLI.md](./AltaStorCLI.md) as the command reference. The public
command tree follows ABR CLI v4.1:

```text
abr
  auth
  user
  repo
  inventory
  job
  session
  backup
  restore
  system
```

Global options:

```bash
abr [--output table|json] [--log-level debug|info|warn|error] <command>
```

Important notes:

- Every `/api/v1` call requires a session: run `abr auth login` first (see
  [AltaStorCLI.md](./AltaStorCLI.md#authentication-commands)).
- After `abr inventory add`, run `abr inventory rescan` before using VM names
  in `abr job create`.
- `abr job start --full` is the canonical full-backup flag.
  `--force-full` remains accepted as a compatibility alias.

Pre-v4.1 command examples are obsolete.
