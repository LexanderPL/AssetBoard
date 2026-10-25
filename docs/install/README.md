# Installing AssetBoard

AssetBoard is one small web app with one SQLite database file. Pick the way that matches what
you already run; you can move between them later by moving the data directory (see
[Backups](../deployment.md#backups)).

| Method | Status | Choose it when |
| --- | --- | --- |
| [Docker Compose](docker.md) | **Recommended**, supported | You already run Docker, or you have no strong preference. |
| [Native with systemd](native.md) | Supported | You want no container runtime: a Debian or Ubuntu machine or VM. |
| [Proxmox LXC](proxmox-lxc.md) | Documented, not yet tested on Proxmox | You run Proxmox VE: a small Debian container running the native install. |
| [Podman](podman.md) | Documented, not yet tested with Podman | You prefer rootless containers managed by systemd (Quadlet). |

Not covered yet: Kubernetes and Helm, Nix, NAS app stores. These come after 1.0.

## What every method has in common

- **One process.** Run a single instance with a single worker: the recurring-item scheduler, the
  first-run setup token and the sign-in limits live in the process.
- **A data directory** holding `assetboard.db` and `secret_key`. Back up both, together
  ([Backups](../deployment.md#backups)). Losing `secret_key` breaks two-factor sign-in.
- **Loopback by default.** Every method listens on `127.0.0.1:8000` only. To reach the app from
  other machines, put a reverse proxy with HTTPS in front of it (or, on a trusted home network
  only, listen on the LAN); see [Reverse proxies](../deployment.md#reverse-proxies).
- **Settings** are `ASSETBOARD_*` environment variables, listed in the
  [deployment guide](../deployment.md#environment-variables). Set `ASSETBOARD_BASE_URL` to the
  address people type in the browser.
- **First run.** Open the app and create the administrator. The setup form asks for a one-time
  token printed in the log at startup ("Generated first-run setup token"); each guide says where
  to find that log.
- **Upgrades** migrate the database automatically (`ASSETBOARD_AUTO_MIGRATE=true`, the default),
  or with `assetboard migrate`. Take a backup first; the native install script does that for you.
