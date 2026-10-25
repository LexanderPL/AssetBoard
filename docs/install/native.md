# Native install with systemd (Debian and Ubuntu)

This runs AssetBoard directly on a Debian or Ubuntu machine, VM or Proxmox container, as a
hardened systemd service, without Docker. One script, `deploy/install-debian.sh`, installs it
and upgrades it later.

**What has been tested.** The script was run end to end (fresh install, re-run, upgrade with a
backup, rollback) on Ubuntu 26.04 with its own Python 3.14, and on Ubuntu 24.04 with a
uv-managed Python 3.13 (see [Older distributions](#older-distributions)), each in a throwaway
systemd container. Debian 13 ships the same pieces (Python 3.13, systemd 257) and is the
recommended system, but it has not been run through the script yet.

## What you need

- Debian 13 or Ubuntu 24.04 or later, with systemd, on amd64 or arm64.
- Python 3.13 or later. Debian 13 and Ubuntu 25.04 and later have it; for Debian 12 and
  Ubuntu 24.04 see [Older distributions](#older-distributions).
- Root access (`sudo`), and about 200 MB of disk per installed release, plus room for your data.
- Memory: 512 MB is enough for a household; 1 GB leaves headroom (each password check uses about
  64 MB for a moment).

## What the script does

Run as root, it:

1. installs `python3`, `python3-venv`, `curl` and `ca-certificates` if missing (apt);
2. downloads the release files and checks them against the release's `SHA256SUMS`;
3. creates the system user `assetboard` (no login shell, no home directory);
4. creates a virtual environment for that release under `/opt/assetboard/releases/<version>` and
   installs the exact, hash-checked dependency versions the release was tested with (prebuilt
   wheels only; nothing is compiled);
5. creates `/etc/assetboard/assetboard.env` from the example, once; it never overwrites it;
6. creates the data directory `/var/lib/assetboard` (mode 0700) and the secret key
   `/var/lib/assetboard/secret_key` (mode 0600), once;
7. on an upgrade: stops the service, backs up the database and the secret key to
   `/var/backups/assetboard`, switches `/opt/assetboard/current` to the new release and runs the
   database migrations;
8. installs and enables the systemd unit `assetboard.service` and starts it.

Running it again with the same version changes nothing and does not restart the app. Options:
`--version X.Y.Z` (default: the latest release), `--python PATH` (another Python 3.13+),
`--release-url URL` (a mirror or a local directory, see [From a git checkout](#from-a-git-checkout)),
`--no-start`. `--help` lists them.

## Install from a release

Release files for the native install are attached to every GitHub release after 0.3.0. Until
the first such release, use [From a git checkout](#from-a-git-checkout).

Pick the version from the [releases page](https://github.com/LexanderPL/AssetBoard/releases),
then, as root (`sudo -i`):

```bash
VERSION=X.Y.Z   # the release you picked
BASE=https://github.com/LexanderPL/AssetBoard/releases/download/v$VERSION
curl -fLO "$BASE/install-debian.sh"
curl -fLO "$BASE/SHA256SUMS"
sha256sum --check --ignore-missing SHA256SUMS   # must print "install-debian.sh: OK"
less install-debian.sh                          # read what you are about to run as root
bash install-debian.sh --version "$VERSION"
```

Never pipe a script from the internet straight into a root shell (`curl ... | sh`); download it,
check it and read it first, as above.

At the end the script prints `Ready on http://127.0.0.1:8000`. Continue with
[First run](#first-run).

## From a git checkout

A git checkout has no compiled translations (`.mo` files are build output); building the wheel
compiles them. `deploy/build-release.sh` builds the same files a release has, and the install
script installs them from that directory. You need [uv](#installing-uv) and git, as a normal
user:

```bash
git clone https://github.com/LexanderPL/AssetBoard.git
cd AssetBoard
git checkout v0.3.0                     # or stay on main for the development version
deploy/build-release.sh ~/assetboard-release
sudo ~/assetboard-release/install-debian.sh --version 0.3.0 \
  --release-url "file://$HOME/assetboard-release"
```

`build-release.sh` prints the exact install command with the right version at the end. For
development (`make dev`), follow [CONTRIBUTING.md](../../CONTRIBUTING.md) instead; `uv sync`
plus `make i18n-compile` prepares a checkout for that, not for a service.

## First run

The app listens on `127.0.0.1:8000` on the machine itself. Open it there, or through an SSH
tunnel from your computer: `ssh -L 8000:127.0.0.1:8000 you@server`, then
<http://localhost:8000>. To reach it from other machines, see
[Reach it from your network](#reach-it-from-your-network) first.

The setup form asks for a one-time setup token. Read it from the log:

```bash
journalctl -u assetboard | grep "setup token"
```

Or create the administrator from the command line, which needs no token (see
[Administrator commands](#administrator-commands)).

## Settings

Settings are in `/etc/assetboard/assetboard.env` (owner root, group `assetboard`, mode 0640,
because it can hold SMTP and OIDC secrets). Every variable is in the
[deployment guide](../deployment.md#environment-variables). After a change:

```bash
sudo systemctl restart assetboard
```

The unit itself sets `ASSETBOARD_ENV=prod`, `ASSETBOARD_DATA_DIR=/var/lib/assetboard` and
`WEB_CONCURRENCY=1` (one process: the recurring-item scheduler needs it). Do not edit
`/etc/systemd/system/assetboard.service`, because upgrades replace it; use a drop-in
(`sudo systemctl edit assetboard`) for local changes.

`ASSETBOARD_ALLOW_REGISTRATION` no longer exists: the app refuses to start with it set to `true`,
and the install script stops before changing anything. Open registration is turned on in the app
(**Instance settings**) and needs SMTP (`ASSETBOARD_SMTP_*`, see [Email](../deployment.md#email)).

## Reach it from your network

**With a reverse proxy (recommended).** Run Caddy, nginx, Traefik or Nginx Proxy Manager in front
of the app with HTTPS, and:

- set `ASSETBOARD_BASE_URL=https://budget.example.com` (your address);
- set `ASSETBOARD_TRUSTED_PROXIES` to the proxy's IP address (`127.0.0.1` when the proxy runs on
  the same machine, which is the default in the env file);
- if the proxy runs on **another** machine, make the app listen on the network too (below), and
  allow port 8000 only from the proxy (a firewall rule, or the Proxmox firewall).

With Caddy on the same machine, the whole Caddyfile can be:

```text
budget.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

Read [Reverse proxies](../deployment.md#reverse-proxies) for rate limits and body-size limits.

**Listening on the network.** Create a drop-in with `sudo systemctl edit assetboard` and enter:

```ini
[Service]
ExecStart=
ExecStart=/opt/assetboard/current/bin/assetboard serve --host 0.0.0.0 --port 8000
```

The empty `ExecStart=` line is required: it clears the original command. Then
`sudo systemctl restart assetboard`. On a trusted home network without a proxy you can then open
`http://<server-ip>:8000` directly; set `ASSETBOARD_BASE_URL=http://<server-ip>:8000` to match.
Do not expose plain HTTP to the internet.

## Administrator commands

The [administrator commands](../deployment.md#administrator-commands) (`create-admin`,
`reset-password`, `reset-mfa`, `mail-test`, `migrate`) must run as the `assetboard` user with the
service's settings. `systemd-run` does that:

```bash
sudo systemd-run --quiet --wait --pty --collect \
  -p User=assetboard -p Group=assetboard \
  -p EnvironmentFile=/etc/assetboard/assetboard.env \
  -E ASSETBOARD_ENV=prod -E ASSETBOARD_DATA_DIR=/var/lib/assetboard \
  /opt/assetboard/current/bin/assetboard create-admin
```

Replace `create-admin` with the command you need. For scripts, use `--pipe` instead of `--pty`
and the command's `--password-stdin` option.

## Files and directories

| Path | What | Back it up? |
| --- | --- | --- |
| `/var/lib/assetboard/assetboard.db` | The database (SQLite, WAL mode) | **Yes** |
| `/var/lib/assetboard/secret_key` | The secret key; two-factor secrets are encrypted with it | **Yes, with the database** |
| `/etc/assetboard/assetboard.env` | Your settings (may hold SMTP/OIDC secrets) | Yes |
| `/var/backups/assetboard/` | Database and key copies taken before each upgrade | Copy elsewhere |
| `/opt/assetboard/releases/<version>/` | One virtual environment per installed release | No |
| `/opt/assetboard/current` | Link to the active release | No |
| `/etc/systemd/system/assetboard.service` | The unit; replaced on upgrades | No |

## Upgrade

Read the [Upgrading](../deployment.md#upgrading) notes, then run the new release's script the same
way as the first install (download, check, read), with the new version:

```bash
bash install-debian.sh --version X.Y.Z
```

It installs the new release next to the old one while the app keeps running, then stops the
app, backs up the database and secret key to `/var/backups/assetboard`, switches to the new
release, applies the database migrations and starts the app again. If the migration fails, it
switches back to the previous release and leaves the app stopped, so that you can look at the
error; nothing else is changed.

Old releases stay in `/opt/assetboard/releases`, which makes a rollback quick. Remove the ones
you no longer need (`sudo rm -r /opt/assetboard/releases/<old-version>`), never the one
`/opt/assetboard/current` points at.

## Rolling back

Use the backup taken just before the upgrade: a database migrated by a newer release cannot be
opened by an older one. As root:

```bash
systemctl stop assetboard
ls /var/backups/assetboard/          # find the newest "*-before-<new-version>.db"
BACKUP=/var/backups/assetboard/assetboard-<timestamp>-before-<new-version>.db
rm -f /var/lib/assetboard/assetboard.db-wal /var/lib/assetboard/assetboard.db-shm
install -o assetboard -g assetboard -m 0600 "$BACKUP" /var/lib/assetboard/assetboard.db
install -o assetboard -g assetboard -m 0600 "$BACKUP.secret_key" /var/lib/assetboard/secret_key
ln -sfn /opt/assetboard/releases/<old-version> /opt/assetboard/current
systemctl start assetboard
```

Anything entered after the upgrade is lost with this, so roll back soon or not at all.

## Backups

Back up `/var/lib/assetboard/assetboard.db` and `/var/lib/assetboard/secret_key` together, to
another machine, on a schedule. [Backups](../deployment.md#backups) shows how to take a consistent
copy while the app runs and how to restore one. The copies the script takes before upgrades are
not a backup by themselves: they live on the same disk.

## Older distributions

Debian 12 and Ubuntu 24.04 ship Python 3.11 and 3.12; the script stops with a message there. Use
a Python 3.13 managed by [uv](#installing-uv), installed **outside `/root` and `/home`**, because
the service is not allowed to read those (`ProtectHome=yes`). As root:

```bash
umask 022
UV_PYTHON_INSTALL_DIR=/opt/uv-python uv python install 3.13
bash install-debian.sh --version X.Y.Z \
  --python "$(UV_PYTHON_INSTALL_DIR=/opt/uv-python uv python find 3.13)"
```

The script refuses an interpreter that other users can write to. uv does not update this Python
by itself. To move the app to a newer 3.13 patch release: install it the same way, run
`systemctl stop assetboard`, remove the active release's directory under
`/opt/assetboard/releases`, and run the install script for the same version with the new
`--python`; it rebuilds the release and starts the app. Upgrading the distribution is the
simpler long-term fix.

## Installing uv

You need uv only to build from a git checkout or for [Older distributions](#older-distributions).
Prefer your distribution's package if it has one. Otherwise use the official installer, pinned
to a version, downloaded and read before running it:

```bash
UV_VERSION=0.8.22   # the version this project's CI uses; newer ones work too
curl -fLO "https://astral.sh/uv/$UV_VERSION/install.sh"
less install.sh
sudo env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh install.sh
```

See the [uv installation guide](https://docs.astral.sh/uv/getting-started/installation/) for
other methods (pipx, standalone binaries).

## How the service is hardened

The unit runs the app as the unprivileged `assetboard` user with no capabilities, a read-only
view of the system (`ProtectSystem=strict`) except `/var/lib/assetboard`, no access to home
directories, a private `/tmp`, no new privileges, a system-call filter and restricted address
families. Files it creates are readable by that user only (`UMask=0077`).
`systemd-analyze security assetboard` rates it about 1.5 ("OK"). `MemoryDenyWriteExecute` is
left off on purpose (see the comment in the unit).

## Troubleshooting

- **Status and logs:** `systemctl status assetboard`, `journalctl -u assetboard -n 50`.
- **"Not ready yet" after install:** the log says why. Common causes are a setting that fails
  validation (the app prints the variable name) or a port already in use.
- **`status=226/NAMESPACE` in a container:** the container cannot create the namespaces the
  hardening needs. On Proxmox, turn on **Nesting** for the container (see the
  [Proxmox guide](proxmox-lxc.md#troubleshooting)).
- **The database needs an upgrade:** with `ASSETBOARD_AUTO_MIGRATE=false`, run the `migrate`
  [administrator command](#administrator-commands), then restart.

## Uninstall

```bash
sudo systemctl disable --now assetboard
sudo rm /etc/systemd/system/assetboard.service && sudo systemctl daemon-reload
sudo rm -r /opt/assetboard
# Your data and settings; keep a backup before removing them:
sudo rm -r /var/lib/assetboard /var/backups/assetboard /etc/assetboard
sudo userdel assetboard
```
