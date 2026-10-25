# Proxmox VE: a Debian LXC container

This guide runs AssetBoard in a small, unprivileged Debian 13 container (CT) on Proxmox VE,
using the [native install](native.md). No Docker is involved. It is written for people who
use Proxmox but are not Linux experts: follow it top to bottom.

**Status: documented, not yet tested on Proxmox.** The steps inside the container are the
native install, which is tested (see [native.md](native.md)); the Proxmox-specific steps
(creating the container, nesting, backups) follow the Proxmox VE documentation but have not been
run end to end. Please report what works and what does not.

## 1. Download the Debian 13 template

In the Proxmox web interface: select your node, then the **local** storage, then
**CT Templates** → **Templates**. Search for `debian-13-standard`, select it and click
**Download**.

Or, in the node's **Shell**:

```bash
pveam update
pveam available --section system | grep debian-13
pveam download local debian-13-standard_<version>_amd64.tar.zst   # the name printed above
```

## 2. Create the container

Click **Create CT** (top right) and fill in:

| Tab | Setting | Value |
| --- | --- | --- |
| General | Hostname | `assetboard` |
| General | Unprivileged container | **checked** (the default) |
| General | Nesting | **checked** (the default; the service's hardening needs it) |
| General | Password or SSH public key | yours, for logging in to the container |
| Template | Template | the `debian-13-standard` template from step 1 |
| Disks | Disk size | 4 GB |
| CPU | Cores | 1 |
| Memory | Memory / Swap | 1024 MB / 512 MB |
| Network | IPv4 | DHCP, or a static address (a fixed address makes the next steps easier) |
| DNS | | use host settings |

On the last tab, tick **Start after created** and click **Finish**. Under the container's
**Options**, set **Start at boot** to Yes.

The same from the node's shell (pick a free ID instead of `120`, and your template and storage
names):

```bash
pct create 120 local:vztmpl/debian-13-standard_<version>_amd64.tar.zst \
  --hostname assetboard --unprivileged 1 --features nesting=1 \
  --cores 1 --memory 1024 --swap 512 --rootfs local-lvm:4 \
  --net0 name=eth0,bridge=vmbr0,ip=dhcp --onboot 1 --start 1
pct enter 120   # a root shell inside the container, no password needed
```

## 3. Install AssetBoard inside the container

Open the container's **Console** in the web interface (or `pct enter 120` from the node's shell)
and log in as `root`. First update the container:

```bash
apt update && apt full-upgrade -y
apt install -y curl ca-certificates
```

Then follow [Install from a release](native.md#install-from-a-release): download
`install-debian.sh` and `SHA256SUMS` for the version you want, check them, read the script, and
run it. When it prints `Ready on http://127.0.0.1:8000`, the app runs inside the container.

## 4. Open it from your network

The app listens only inside the container (`127.0.0.1`). Pick one:

- **You already have a reverse proxy** (Nginx Proxy Manager, Caddy, Traefik, ... on another CT or
  VM): make the app listen on the network and trust that proxy. In the container:

  ```bash
  systemctl edit assetboard
  ```

  and enter, between the comment lines it shows:

  ```ini
  [Service]
  ExecStart=
  ExecStart=/opt/assetboard/current/bin/assetboard serve --host 0.0.0.0 --port 8000
  ```

  Then edit the settings with `nano /etc/assetboard/assetboard.env`:

  ```text
  # The address you open in the browser:
  ASSETBOARD_BASE_URL=https://budget.example.com
  # Your proxy's IP address:
  ASSETBOARD_TRUSTED_PROXIES=192.168.1.20
  ```

  Comments go on their own lines in this file: anything after the `=` is part of the value.

  and run `systemctl restart assetboard`. Point the proxy at `http://<container-ip>:8000`.
  In Proxmox, you can turn on the container's **Firewall** and allow port 8000 only from the
  proxy's address.

- **Home network only, no proxy:** do the same `systemctl edit` step, set
  `ASSETBOARD_BASE_URL=http://<container-ip>:8000` and leave `ASSETBOARD_TRUSTED_PROXIES` empty
  (delete the `127.0.0.1`). Restart, then open `http://<container-ip>:8000`. Do not forward this
  port from your router to the internet; use a reverse proxy with HTTPS for that.

`ip -4 addr show eth0` in the container shows its address.

## 5. First run

Open the app in your browser. It asks for a setup token; print it in the container with:

```bash
journalctl -u assetboard | grep "setup token"
```

Then create your administrator account.

## Backups

Two layers, use both:

- **Proxmox backups of the whole container.** Under **Datacenter** → **Backup**, add a job for
  this container with **Mode: Stop**. Stop mode shuts the container down for the seconds the
  backup takes, so the database file is copied in a consistent state; the container starts
  again afterwards. Store backups on another disk or a Proxmox Backup Server. To restore,
  select the backup under the storage's **Backups** and click **Restore**.
- **The app's data on its own,** for moving to another machine or restoring just the database:
  see [Backups](../deployment.md#backups) (the paths are those of the
  [native install](native.md#files-and-directories)).

## Upgrading

1. Optionally take a snapshot first: the container's **Snapshots** → **Take Snapshot** (needs
   storage with snapshot support, such as LVM-thin or ZFS; the default `local-lvm` has it).
2. In the container, run the new release's `install-debian.sh` as in
   [Upgrade](native.md#upgrade). It backs up the database before it changes anything.
3. Keep the container's Debian up to date: `apt update && apt full-upgrade -y` from time to time.

## Troubleshooting

- **`systemctl status assetboard` shows `status=226/NAMESPACE`:** nesting is off. Shut the
  container down, turn on **Options** → **Features** → **Nesting**, and start it again.
- **The page does not load from another computer:** check the `systemctl edit` step above
  (`--host 0.0.0.0`) and that you use the container's IP address.
- **Sign-in fails over `http://` after setting an `https://` address:** with an `https://`
  `ASSETBOARD_BASE_URL`, cookies only work over HTTPS. Use the proxy's address, or an `http://`
  `BASE_URL` for a home-network-only setup.
- Everything else: [native.md troubleshooting](native.md#troubleshooting) and
  `journalctl -u assetboard -n 50`.

## Other ways on Proxmox

- **Helper scripts.** The community "Proxmox VE Helper-Scripts" (community-scripts) create a
  container and install an app inside it with one command run on the Proxmox host. There is no
  such script for AssetBoard, and this guide does not depend on one: the steps above are what such
  a script would do. Whatever script you use, read it before running it on your Proxmox host, which
  controls every VM and container you have; never pipe it straight from the internet into a root
  shell.
- **Docker in a VM.** Create a Debian VM, install Docker, and follow the
  [Docker guide](docker.md). Docker inside an LXC container also works with nesting, but it is a
  frequent source of problems; prefer a VM for Docker.
- **A container made from the OCI image** (Proxmox VE 9.1 and later, a technology preview):
  Proxmox can create an LXC container directly from `ghcr.io/lexanderpl/assetboard`. This is
  **experimental and untested** with AssetBoard; the image expects a writable `/data` and runs
  as UID 10001.
