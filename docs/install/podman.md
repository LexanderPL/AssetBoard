# Podman: rootless, managed by systemd (Quadlet)

This runs the published container image with rootless Podman, as a systemd user service defined
by a Quadlet file. No daemon, and nothing runs as root.

**Status: documented, not yet tested with Podman.** The image and its settings are the same as
for [Docker](docker.md), which is tested. Please report what works and what does not.

## What you need

- Podman 4.4 or later, for Quadlet (Debian 13 and Ubuntu 24.04 and later have it:
  `sudo apt install podman`).
- A normal user account to run it. A dedicated one keeps it apart from your own files, for
  example `sudo useradd --create-home assetboard` and then work as that user
  (`sudo machinectl shell assetboard@`, which gives the user session systemd needs; plain
  `su` does not).

## Install

As that user:

```bash
# Keep the user's services running after logout and start them at boot.
sudo loginctl enable-linger "$USER"

mkdir -p ~/.config/containers/systemd
curl -fL -o ~/.config/containers/systemd/assetboard.container \
  https://raw.githubusercontent.com/LexanderPL/AssetBoard/v<version>/deploy/podman/assetboard.container
less ~/.config/containers/systemd/assetboard.container   # check the Image= line and settings

systemctl --user daemon-reload
systemctl --user start assetboard
```

Use the same `<version>` as a [release](https://github.com/LexanderPL/AssetBoard/releases); the
file's `Image=` line names that release's image. Quadlet turns the file into the
`assetboard.service` user unit; `systemctl --user status assetboard` shows it. The `[Install]`
section starts it at boot (with lingering on); there is no `systemctl enable` step for Quadlet
units.

## First run

Open <http://localhost:8000> on that machine (or through an SSH tunnel). The setup token is in the
log:

```bash
journalctl --user -u assetboard | grep "setup token"
```

Administrator commands run inside the container, for example
`podman exec -it assetboard assetboard create-admin` (see
[Administrator commands](../deployment.md#administrator-commands)).

## Settings

Simple settings go in the Quadlet file as `Environment=` lines. For secrets (SMTP, OIDC), create
`~/.config/assetboard/assetboard.env` with mode 0600 (`KEY=value` lines, like the
[native env example](../../deploy/assetboard.env.example)) and uncomment the `EnvironmentFile=`
line. After any change:

```bash
systemctl --user daemon-reload
systemctl --user restart assetboard
```

The app is published on `127.0.0.1:8000`. Put a reverse proxy with HTTPS in front of it and set
`ASSETBOARD_BASE_URL` and `ASSETBOARD_TRUSTED_PROXIES`
([Reverse proxies](../deployment.md#reverse-proxies)). Note that with rootless Podman, the
address the app sees for the proxy's connections depends on Podman's network mode (pasta or
slirp4netns) and may differ from the proxy's own address; `ASSETBOARD_TRUSTED_PROXIES` must
name the address the app sees. This has not been checked yet. Rootless containers cannot
publish ports below 1024.

## Data

The database and secret key are in the Podman volume `assetboard-data`
(`podman volume inspect assetboard-data` shows where). For backups, see
[Backups](../deployment.md#backups); with Podman, the simplest consistent copy is:

```bash
systemctl --user stop assetboard
podman volume export assetboard-data > assetboard-data-$(date +%F).tar
systemctl --user start assetboard
```

The tar file holds both `assetboard.db` and `secret_key`; keep it somewhere safe and off this
machine. To restore, stop the service and run `podman volume import assetboard-data <file>`.

## Upgrade

Back up, then change the `Image=` tag in the Quadlet file to the new version, and run
`systemctl --user daemon-reload && systemctl --user restart assetboard`. Migrations are applied
at startup. Read the [Upgrading](../deployment.md#upgrading) notes first.

## Without Quadlet

On Podman older than 4.4, or to try it once:

```bash
podman run -d --name assetboard -p 127.0.0.1:8000:8000 -v assetboard-data:/data \
  --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges \
  -e ASSETBOARD_ENV=prod -e ASSETBOARD_DATA_DIR=/data \
  ghcr.io/lexanderpl/assetboard:<version>
```

Rootful Podman works too: put the Quadlet file in `/etc/containers/systemd/` and use
`systemctl` without `--user`.
