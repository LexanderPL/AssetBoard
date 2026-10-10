# AssetBoard

A self-hosted personal finance web app for budgeting, net worth, investment, loan, and FIRE planning.

## License

This project is licensed under the GNU Affero General Public License v3.0 or later. See [LICENSE](LICENSE).

## Development

```bash
make setup
make dev
```

`make dev` builds the local CSS, compiles the translations, and starts the reload-enabled
development server at `http://127.0.0.1:8000`. Override the bind address with `DEV_HOST` and
`DEV_PORT`, for example `make dev DEV_PORT=8080`.

The web shell serves HTMX, Alpine.js (CSP build), and ECharts from pinned local vendor files.
Jinja2 provides server-rendered templates; Babel provides locale-aware number parsing, formatting,
and gettext catalog tooling.

Run the complete local checks with:

```bash
make check
```

## Run with Docker Compose

From a checkout, build and start the example deployment with:

```bash
docker compose -f compose.example.yaml up -d --build
```

Open <http://localhost:8000>. Compose keeps the database and generated secret key in the named
`assetboard-data` volume. To use a published image instead, set `ASSETBOARD_IMAGE` to its GHCR
reference and run Compose with `--no-build`:

```bash
ASSETBOARD_IMAGE=ghcr.io/your-github-owner/your-repository:latest \
  docker compose -f compose.example.yaml up -d --no-build
```

The example runs as UID 10001 with a read-only root filesystem. `/data` is persistent and `/tmp`
is writable. See the environment section in `compose.example.yaml` to customize the locale, base
currency, log format, and public URL. The first start creates a secret key in the data volume and
applies database migrations automatically.
