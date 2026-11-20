# Backups and restore

AssetBoard keeps everything in one SQLite database. This page explains the automatic
backups, what they contain, how to restore one, and what a backup strategy for the rest of the
machine should look like.

> **A backup holds all your financial data.** Store copies encrypted, keep them off the machine
> and test a restore before you need one.

## What is backed up

The automatic job copies the database with SQLite's online backup API. The copy is consistent
even while the application is running and includes changes that are still in the write-ahead
log; only committed transactions are visible.

Two things are stored together:

- `<DATA_DIR>/backups/assetboard-<timestamp>.db` – the database.
- `<DATA_DIR>/backups/assetboard-<timestamp>.db.secret_key` – the instance secret key, when
  the key is stored in `<DATA_DIR>/secret_key`. The key decrypts two-factor secrets,
  notification URLs and price-provider credentials. Without it those values cannot be read
  after a restore.

If `ASSETBOARD_SECRET_KEY` (or a `*_FILE` setting) supplies a key that differs from
`<DATA_DIR>/secret_key`, the job does not copy the file: keep the environment or secret file in
your own configuration backup. A copy failure is recorded as a failed backup rather than a
success without a readable key.

Settings such as `BASE_URL`, SMTP credentials, OIDC client secrets and `TRUSTED_PROXIES` are
environment or configuration values, not database rows. Back them up separately.

## Schedule and retention

- One scheduled copy a day at **03:30 UTC**.
- The job starts the first time the application runs and then daily. A restart makes a backup
  if the last successful scheduled one is at least 24 hours old; the daily tick skips a copy
  made within the previous hour, so a restart around 03:30 does not make two backups.
- `ASSETBOARD_SCHEDULER_ENABLED=false` turns off every scheduled job, including backups.
  "Back up now" still works.
- Failures are recorded on Instance settings and sent to every enabled notification channel of
  active instance administrators. A failure with no such channel is only written to the log.

Retention is **grandfather-father-son**, counted by bucket rather than by age. Days, ISO weeks
and months are UTC, like the job:

| Setting | Default | Keeps |
| --- | ---: | --- |
| Daily backups | 7 | The newest scheduled copy of each of the 7 most recent days |
| Weekly backups | 4 | The newest scheduled copy of each of the 4 most recent ISO weeks |
| Monthly backups | 6 | The newest scheduled copy of each of the 6 most recent months |

One file can be the representative of a day, a week and a month at the same time, so the
defaults do not mean 17 separate files. After a few weeks a typical directory holds 7 daily
files, 3–4 older weekly files and up to 5 older monthly files.

Only **scheduled** copies are pruned. "Back up now" copies and the automatic pre-restore copy
are kept until you remove them yourself, which makes them reliable safety nets. Pruning
removes a database, its secret-key sidecar and its history row together.

Scheduled backups run in the application process, so the process (or the container) must be
running. The directory is created with mode 0700 and each file with mode 0600.

## Instance settings

Instance administrators open **User menu → Instance settings**:

- **Last backup**, **Last failure**, **Next scheduled backup** and the backup directory.
- **Back up now** opens a confirmation page; after your password (or a recent external
  sign-in) it makes a manual copy and records a `backup.created` audit event.
- **Download** next to a successful copy opens the same confirmation page; after confirmation
  it serves the database file and records a `backup.downloaded` audit event. The secret key is
  not included in the download; copy the `.secret_key` file from `<DATA_DIR>/backups/` as well
  when you need a complete standalone copy.
- **Backup retention** changes the three counts (0–365). Saving asks for your password, like
  the other instance settings.

The page lists the ten most recent attempts. A failed attempt shows its short error code; the
log has the exception class and stack trace.

Instance administrators can download every successful copy, and that file contains **all
workspaces** on the instance. Instance administration is therefore a whole-database privilege,
not a per-workspace one; give it only to people who may see every workspace's financial data.

## Restore a backup

A restore replaces the whole database. **Stop the application first**; a running instance has
the database, `-wal` and `-shm` files open and its writes would be lost or mixed with the
restored file.

### Native install

Run the command with the service's environment and data directory, not from a shell whose
working directory happens to contain a `data/` folder:

```bash
sudo systemctl stop assetboard
sudo systemd-run --quiet --wait --pty --collect \
  -p User=assetboard -p Group=assetboard \
  -p EnvironmentFile=/etc/assetboard/assetboard.env \
  -E ASSETBOARD_ENV=prod -E ASSETBOARD_DATA_DIR=/var/lib/assetboard \
  /opt/assetboard/current/bin/assetboard restore \
    --data-dir /var/lib/assetboard \
    /var/lib/assetboard/backups/assetboard-20261117T033000Z.db
sudo systemctl start assetboard
```

`assetboard restore` refuses to run against the default relative `./data` directory when
neither `--data-dir`/`--database-url` nor `ASSETBOARD_DATA_DIR` is given, so a forgotten
environment cannot silently create and "restore" into a new database beside your shell.

### Container

The file must be reachable inside the container. Either use a backup in the mounted data
directory, or copy one in temporarily:

```bash
docker compose -f compose.example.yaml stop assetboard
docker compose -f compose.example.yaml cp ./assetboard-20261117T033000Z.db \
    assetboard:/data/restore.db
docker compose -f compose.example.yaml run --rm assetboard \
    python -m assetboard restore /data/restore.db
docker compose -f compose.example.yaml start assetboard
```

(`run` builds a one-off container with the same volume; adjust the service name to yours.)

### What the command checks

Before touching the live database, `assetboard restore`:

1. refuses a non-SQLite configuration (PostgreSQL deployments use `pg_restore`, see below);
2. refuses a missing file, the live database itself, a backup with a non-empty `-wal` file
   beside it (copy a live database with the SQLite backup API instead) and a live database
   with a `-wal` file (stop the application first; a stale zero-length file left by a crash may
   be removed after you are sure no process is using the database);
3. runs `PRAGMA integrity_check` and `PRAGMA foreign_key_check`;
4. requires the `users`, `workspaces` and `alembic_version` tables and compares the schema
   revision with the migrations shipped in this version:
   - **older** – the backup is accepted and migrated to the current schema after the copy;
   - **current** – accepted as is;
   - **newer or unknown** – refused, because an older application cannot safely read a newer
     schema;
5. makes an automatic **pre-restore backup** in `<DATA_DIR>/backups/` (origin
   “pre-restore”), with the secret key sidecar, before replacing anything. If the current
   database is too damaged for SQLite's online copy or its integrity check, a raw file copy is
   kept instead and the command warns that the copy is unverified; a damaged database
   therefore never blocks its own recovery.

If the schema migration fails after the copy, the pre-restore backup is put back and the
command exits with an error. The pre-restore copy is recorded on Instance settings after a
successful restore, or after the original database is put back when migrations fail (when the
history can still be written); it is never pruned automatically.

If `<backup>.secret_key` exists and the instance key comes from `<DATA_DIR>/secret_key`, the
key is restored too. If the key comes from the environment, the existing environment value is
kept and the command warns you to check that it is the key the backup was made with.

Finally, start the application and sign in. A backup from an older release is migrated at
startup as usual; the restore command already applies the migrations, so it starts current.

## Volume snapshots

A filesystem snapshot is a good second layer, but it is not automatically an
application-consistent SQLite backup while the app is running:

- **Stop the application, then snapshot.** This is the simplest reliable method. All data is
  in the volume and SQLite closes cleanly.
- **Snapshot a running container only if the snapshot is atomic for the whole volume.** SQLite
  recovers a database after a crash, but copying only `assetboard.db` while the app is running
  can miss commits that are still in `assetboard.db-wal`. If you snapshot live, include the
  `-wal` and `-shm` files and restore them together; the backup API job above is safer.
- **Docker/Podman volume snapshots** depend on the storage driver. Stop the container first,
  or use the built-in backup and copy `<DATA_DIR>/backups` out of the volume.
- **Proxmox LXC / VM backups** are reliable in **stop** mode. Snapshot mode captures the disk
  consistently at the block level but the app is still running; treat it as crash-consistent
  and keep the in-app backups as well. See the
  [Proxmox guide](install/proxmox-lxc.md#backups).
- **ZFS/Btrfs/LVM snapshots** are atomic for the filesystem and therefore consistent at the
  block level. Take them with the app stopped when you can, and keep the secret key in the same
  snapshot.

A volume snapshot protects against a bad upgrade or a deleted volume; it does not protect
against a lost disk, a lost host or ransomware. That is what off-site copies are for.

## Off-site copies

Follow the 3-2-1 rule: at least three copies, on two kinds of media, with one off-site.

- Copy `<DATA_DIR>/backups/` (database and `.secret_key` sidecars together) to another machine,
  a NAS or object storage on a schedule. `restic`, `borg`, `rclone` or your NAS's snapshot
  replication all work; nothing in AssetBoard is tied to one tool.
- Encrypt the destination. The database contains all financial records and the key sidecar
  decrypts other secrets stored in it.
- Do not sync the live database file with a file-sync client while the app runs. Sync the
  finished files in `<DATA_DIR>/backups`, or stop the app and sync the whole data directory.
- Keep at least one copy offline or on write-once storage, so a compromised host cannot delete
  every backup.
- Test a restore regularly, for example once a quarter, in a scratch data directory. A backup
  that has never been restored is only a hope.
- Keep the configuration (Compose file, environment file, systemd unit, reverse-proxy config)
  and the secret key outside the data disk too.

The application makes no network connections for the backup copies themselves. Backup failure
notifications use the notification channels you configured (Apprise), and copying backups off
the machine is your responsibility and is never automated.

## PostgreSQL

The built-in job, the download button and `assetboard restore` are SQLite-only. When a
PostgreSQL database is configured (not yet supported by the configuration layer), the Instance
settings page explains that backups are managed by PostgreSQL and disables the controls. Use
the standard tools instead:

```bash
pg_dump --format=custom --file=assetboard-$(date -u +%Y%m%dT%H%M%SZ).dump assetboard
pg_restore --clean --if-exists --dbname=assetboard assetboard-20261117T033000Z.dump
```

Store the dump and the database credentials together; `pg_dump` does not contain the
application secret key either.
