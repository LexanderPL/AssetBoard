# Install with Docker Compose (recommended)

This is the recommended and best-tested way to run AssetBoard. You need Docker Engine and Docker
Compose v2. The [deployment guide](../deployment.md) has the details; this page is the short
path.

## Start

From a copy of the repository (only `compose.example.yaml` is needed):

```bash
# Use a published release image instead of building one locally:
export ASSETBOARD_IMAGE=ghcr.io/lexanderpl/assetboard:<version>
docker compose -f compose.example.yaml up -d --no-build
```

Pick `<version>` from the [releases](https://github.com/LexanderPL/AssetBoard/releases) (see
[Image tags](../deployment.md#image-tags)). To build the image from the checkout instead, run
`docker compose -f compose.example.yaml up -d --build`.

The database and the generated secret key live in the named volume mounted at `/data`.

## First run

Open <http://localhost:8000>. The setup form asks for a one-time token from the log:

```bash
docker compose -f compose.example.yaml logs assetboard | grep "setup token"
```

Or create the administrator from the command line:
`docker compose -f compose.example.yaml exec assetboard assetboard create-admin`.

## Settings

`compose.example.yaml` reads `ASSETBOARD_IMAGE`, `ASSETBOARD_BASE_URL`,
`ASSETBOARD_TRUSTED_PROXIES`, `ASSETBOARD_DEFAULT_LOCALE`, `ASSETBOARD_DEFAULT_CURRENCY`,
`ASSETBOARD_BIND_ADDRESS` and `ASSETBOARD_HOST_PORT` from your shell or from a `.env` file next
to it. For any other setting (SMTP, OIDC and so on), copy the file to `compose.yaml` and add the
variable under `environment:`. Compose publishes the app on
`127.0.0.1:8000`; put a reverse proxy in front before exposing it, and set
`ASSETBOARD_BASE_URL` and `ASSETBOARD_TRUSTED_PROXIES`
([Reverse proxies](../deployment.md#reverse-proxies)).

## Upgrade

Back up first ([Backups](../deployment.md#backups)), then point `ASSETBOARD_IMAGE` at the new
version and run `docker compose -f compose.example.yaml up -d --no-build`. Pending database
migrations are applied at startup. Read the [Upgrading](../deployment.md#upgrading) notes for the
versions you skip.

## Proxmox

Docker runs well in a Proxmox **VM**. Docker inside an LXC container needs nesting and is a common
source of trouble; on Proxmox the [LXC guide](proxmox-lxc.md) (native install, no Docker) is
simpler.
