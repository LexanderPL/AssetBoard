# ADR-0023: Opt-in update check

**Status:** Accepted
**Date:** 2026-10-31

## Context

AssetBoard is self-hosted. Administrators learn about new releases only by watching the GitHub
repository or the container registry, so instances stay on old versions. A built-in check would
help, but the project rules say there is no telemetry and no external call at runtime except
for features the user explicitly configures (price providers, OIDC, notifications). A check
that runs by default would break that rule and tell GitHub that an instance exists.

## Decision

- A new instance setting, **Check for updates** (`instance_settings.check_for_updates`,
  migration `0030_update_check`), is **off by default**, also for existing instances. Only an
  instance administrator can turn it on (Instance settings, confirmed with their password); the
  change is audited as `instance.settings_updated` like the other instance settings.
- While it is on, the in-process scheduler (the one that runs recurring items and reminders)
  asks `https://api.github.com/repos/LexanderPL/AssetBoard/releases/latest` at most once a day,
  and once about ten seconds after the setting is turned on. A failed attempt also waits a day.
  The repository slug lives in `branding.py`.
- The request is a plain HTTPS GET through the standard library (`urllib.request`, as the OIDC
  client already does; no new dependency): 5 second timeout, response capped at 512 KiB,
  `User-Agent: AssetBoard`, the GitHub API version and `Accept` headers, no cookies, no token,
  no instance URL, version, user or workspace data. GitHub still sees the server's IP address.
- `releases/latest` excludes drafts and prereleases. The answer is accepted only when its
  `tag_name` is a plain version number (`v1.2.3`, up to four numeric parts); anything else is
  treated as a failure. The tag and the time of the check are stored on the instance settings
  row; turning the setting off clears them.
- Versions are compared by their numeric parts (`packaging` is not a dependency). The running
  version comes from the installed package metadata.
- Every signed-in user sees the running version (`v0.3.6`) at the bottom of the sidebar.
  Instance administrators also see "Update available: X.Y.Z", linking to that release's GitHub
  page, while the check is on and the stored release is newer than the running version.
- Failures are logged at INFO with a short reason code (`update_check_failed reason=http_403`),
  never with the response body. They never affect a page: pages read only the stored result.
- External I/O sits behind the `ReleaseSource` interface; tests use a fake and never reach the
  network.

## Consequences

- Nothing leaves the instance unless an administrator asks for it, so the "no external calls"
  rule still holds: the check is a feature the user explicitly configures.
- With the check on, GitHub learns the server's IP address once a day and that it runs
  AssetBoard. The setting's help text says so in short ("nothing else is sent").
- Behind an egress firewall the check simply fails every day; nothing else changes.
- With more than one worker (unsupported) each worker could check once a day.
- Unauthenticated GitHub API calls are rate-limited per IP (60 an hour); one call a day is far
  below that, but many instances behind one NAT could still meet the limit and see failures.
