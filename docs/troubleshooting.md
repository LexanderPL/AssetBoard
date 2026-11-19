# Troubleshooting

Start with the **log**. `docker compose logs -f assetboard` (or `journalctl -u assetboard -f`
for a native install) shows one line per request and a short error code for most failures. Set
`ASSETBOARD_LOG_LEVEL=DEBUG` temporarily for more detail; never paste a full log with secrets,
notification URLs or financial amounts into an issue.

Health endpoints answer without signing in:

```console
curl -fsS http://127.0.0.1:8000/healthz   # process is up
curl -fsS http://127.0.0.1:8000/readyz    # database and schema are ready
```

`/readyz` returns 503 and names the reason (`database`, `schema_outdated`,
`schema_newer`); `/healthz` stays green. The rest of this page is grouped by where you see the
problem. Detailed behaviour lives in the linked guides.

## First run and the setup token

- **The setup form asks for a token and the log has none.** The token is logged once at
  `WARNING` on every start while no user exists, unless `ASSETBOARD_SETUP_TOKEN` is set (then it
  is never logged). Search for `Generated first-run setup token`. Restarting prints a new random
  token; a configured token stays the same.
- **`/setup` returns 404.** Setup is already complete. Go to `/login`; use
  `assetboard reset-password <email>` if the password is lost.
- **`assetboard create-admin` says a user exists.** It only creates the first administrator.
  Use **Instance settings → Users** or `assetboard reset-password` instead.
- **The page redirects to `/setup` forever.** Every page redirects while the user table is
  empty. Check that the app points at the database you expect (`ASSETBOARD_DATA_DIR`,
  `ASSETBOARD_DATABASE_URL`) and that the data volume is writable by UID/GID `10001` in the
  container.

## Sign-in, sessions and two-factor

- **"Too many attempts" / throttling.** Limits are per email and per client address, and a
  successful sign-in does not reset them. Wait for the shown delay, or restart with a larger
  `ASSETBOARD_AUTH_*` allowance only while you recover access. Through a reverse proxy, set
  `ASSETBOARD_TRUSTED_PROXIES` or every visitor shares the proxy's address (see
  [Reverse proxies](deployment.md#reverse-proxies)).
- **Sign-in succeeds, then every page asks again.** Cookies are `Secure` and `__Host-` prefixed
  when `ASSETBOARD_BASE_URL` starts with `https://`. Reach the app over HTTPS at exactly that
  host name; plain HTTP to a LAN address will not keep the session. Check that the proxy passes
  the original `Host` header.
- **The authenticator code is rejected after moving the data.** The TOTP secret is encrypted
  with the instance secret key. Restore `secret_key` (or `ASSETBOARD_SECRET_KEY`) together with
  the database. A recovery code still works; an administrator can run
  `assetboard reset-mfa <email>`.
- **A password reset or account change did not arrive.** Email is optional. The page says that
  a link was sent either way, so check the SMTP settings and the log for `mail_send_failed`
  (the log has the error class, never the message content). Run
  `assetboard mail-test you@example.com`.
- **OIDC button missing or sign-in fails.** The provider name, issuer URL, client ID and secret
  must all be set, and the redirect URI must match `<BASE_URL>/auth/oidc/callback` exactly.
  `email_verified` must be true for a new account. See [OIDC sign-in](auth/oidc.md#logs) for the
  audit reasons and issuer-change procedure.
- **Proxy sign-in does not work.** The request must arrive from an address in
  `ASSETBOARD_TRUSTED_PROXIES`, and the proxy must overwrite the identity headers. Test with the
  procedure in [proxy authentication](auth/proxy-auth.md#test-it). Headers from other addresses
  are ignored and logged at WARNING.

## Reverse proxy and HTTPS

- **Redirect loop, wrong links or CSRF/origin errors.** Set `ASSETBOARD_BASE_URL` to the exact
  public URL (scheme and host, no trailing slash) and pass the original `Host` header. Forms
  are accepted only from that origin or the address the request reached.
- **413 on an import.** The proxy body cap is lower than the app's
  `ASSETBOARD_IMPORT_MAX_UPLOAD_BYTES` (20 MiB by default). Raise `client_max_body_size` in
  nginx, `request_body` in Caddy or the Traefik buffering limit; see the examples under
  [Reverse proxies](deployment.md#reverse-proxies).
- **Wrong client addresses in the log or throttling.** Forwarded headers are trusted only from
  `ASSETBOARD_TRUSTED_PROXIES`. Do not add networks an untrusted client can reach directly.
- **HSTS or an old certificate sticks in the browser.** The app sends HSTS on HTTPS responses.
  Test a new certificate in a private window and remove the HSTS entry from the browser if a
  self-signed certificate was used on that host name.

## Notifications

- **No reminders.** Check that `ASSETBOARD_SCHEDULER_ENABLED` is on, the channel is enabled,
  its time in the workspace time zone has passed, and the scheme is in
  `ASSETBOARD_NOTIFICATION_SCHEMES`. **Send test** bypasses the daily schedule and reports a
  short error. The server must be able to reach the URL's host, including a local ntfy/Gotify.
- **"The channel shows an error" after restoring a backup.** The URL cannot be decrypted with
  the new secret key. Re-enter the Apprise URL; it is never shown again. See
  [Notifications](deployment.md#notifications).

## Prices and investments

- **A price is stale or shows an error badge.** Providers are opt-in per workspace and need a
  key for EODHD or GoldAPI.io. Check **Investments → Providers** for the last success/error and
  the calls used today. A failed refresh keeps the last known price; manual prices always win.
  See [Price providers](investments.md#price-providers).
- **An instrument has no provider fields.** Bond-model and currency instruments do not use a
  provider. Use manual prices, or a provider that publishes the instrument's currency.
- **FX looks wrong.** The newest date wins; on the same date the order is manual, provider,
  transaction. Provider rates are converted into the workspace base currency only when the
  provider publishes it.

## Imports, archives and months

- **A CSV row is invalid.** The dry-run report names the row and column and lists at most 1 000
  errors; the counts cover every row. Fix the file or map the category, then upload it again.
  See [Import and export](import-export.md#the-dry-run-report).
- **The import disappeared after a restart.** Uploads are temporary and are removed after 24
  hours or when cancelled. Apply the import to keep its transactions.
- **An archive restore refuses the workspace.** Restore only into an empty workspace (an
  untouched suggested category set counts as empty). Use **Merge** to add an archive to a
  workspace that already has data.
- **A month is read-only.** It is closed. An owner can reopen it under **Months**; imports into
  a closed month are skipped or refused. See [Months](months.md).
- **Large file rejected.** Uploads are limited to 20 MiB by default, archives unpack to at most
  200 MiB, and checking may take at most 60 seconds. Split the data by year.

## Backups and restores

- **No scheduled backup.** Scheduled jobs need the process running, the scheduler enabled and
  the last successful copy to be at least 24 hours old. **Back up now** always works.
- **A restore says the schema is newer.** The backup was written by a newer AssetBoard than
  the one you started. Install that version (or a later one), then restore.
- **The database is locked or a copy looks incomplete.** Never copy `assetboard.db` alone while
  the app runs; WAL changes may still be in `assetboard.db-wal`. Stop the app, or use the
  built-in backup/`sqlite3` backup API. See [Backups and restore](backup.md).
- **Two-factor or notifications broke after a restore.** The secret key did not travel with
  the database. Restore the matching key file or value; users re-enroll MFA if it is lost.

## Upgrades and migrations

- **Startup says the database needs an upgrade.** With `ASSETBOARD_AUTO_MIGRATE=false`, run
  `assetboard migrate` and restart. With automatic migrations on, check the log for the failed
  revision and take a backup before retrying.
- **A downgrade is refused or loses data.** Some migrations are lossy or irreversible; read the
  notes for the versions you skip under [Upgrading](deployment.md#upgrading). Restore a backup
  from before the upgrade when in doubt.
- **Only one process/worker.** The scheduler, setup token and some caches live in the process;
  `WEB_CONCURRENCY` above 1 logs a warning and can duplicate jobs.

## Demo mode

- **The demo button is missing.** `ASSETBOARD_DEMO_ENABLED` must be on and the demo bootstrap
  must have succeeded. Check **Instance settings → Demo mode** and the log for
  `demo_bootstrap_failed`.
- **The demo looks stale or the reset failed.** The panel shows the last reset and its error;
  **Reset now** runs it again with reason `manual`. A failed reset keeps the old data and never
  blocks startup. See [Demo workspace](demo-mode.md#reset).
- **Turning demo mode off did not delete data.** It intentionally keeps the workspace and user
  so an administrator can inspect them; use **Delete demo data** on Instance settings.

## Phone, PWA and the browser

- **"Add to home screen" is missing.** The installable app needs HTTPS (except on localhost) and
  a supported browser (Android Chrome, iOS 16.4+ Safari). See
  [Install on your phone](install-on-your-phone.md).
- **A page scrolls sideways on a phone.** That is a bug; note the page and width and open an
  issue. The supported phone width is 360–430 px.
- **Charts are blank.** They are drawn by the vendored ECharts bundle. A strict Content
  Security Policy added by a proxy must allow the app's own scripts; the app ships no inline
  scripts. Check the browser console for blocked URLs.

## Getting more help

- Run `assetboard --help` for the operator commands (`create-admin`, `reset-password`,
  `reset-mfa`, `mail-test`, `restore`, `migrate`, `check-migrations`, `demo-seed`).
- Before opening an issue, note the AssetBoard version (sidebar footer), install method,
  `ASSETBOARD_BASE_URL` scheme/host, and the short error code or request id from the page. Do
  not include secrets, full logs, database files or amounts.
- Security reports follow [SECURITY.md](../SECURITY.md), not the public issue tracker.
