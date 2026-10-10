# Container deployment notes

## Release automation credentials

The release-please workflow uses `GITHUB_TOKEN`. Ensure repository Actions settings allow
GitHub Actions to create and approve pull requests. Once a release is created, that workflow calls
the reusable Docker workflow directly with the release tag, so publishing does not depend on a
new workflow being triggered by a tag created with `GITHUB_TOKEN`.

If release automation is changed to rely on a separate `push`-tag workflow, configure a
fine-grained PAT or GitHub App token with repository contents, issues, and pull-request permissions
for release-please. Use that token instead of `GITHUB_TOKEN`; GitHub suppresses most downstream
workflow runs caused by events emitted using `GITHUB_TOKEN`.

## GHCR visibility

The first successful tagged release creates the container package in GitHub Container Registry.
To make images pullable without authentication, open the repository's **Packages** section, open
the container package settings, and change its visibility to **Public**. Confirm the package
belongs to the intended repository before changing visibility. Private packages remain usable by
users who authenticate to GHCR with an account that has package access.

## Data directory ownership

The container runs as UID/GID `10001:10001`. A named Docker volume is initialized for this user.
For a host bind mount, create the directory and run `chown 10001:10001` on it before starting
Compose. The startup check reports this command when `DATA_DIR` is not writable.

## Reverse proxies

The Compose file binds the service to loopback by default. Configure a reverse proxy on the same
Docker network and set `ASSETBOARD_BASE_URL` to the public URL. When the proxy forwards client
addresses, set `ASSETBOARD_TRUSTED_PROXIES` to the proxy's IP or CIDR so Uvicorn accepts forwarded
headers only from that peer range.
