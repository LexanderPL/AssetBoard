# ADR-0021: Reminders through Apprise channels

**Status:** Proposed
**Date:** 2026-10-25

## Context

F3.6 sends reminders (occurrences due soon, overdue occurrences, work months without a work log)
to services people already use: ntfy, Gotify, Telegram, email and many others. Each member
configures their own channels. A channel's address is an Apprise URL, which often carries a
bearer secret (a bot token, an access token, an SMTP password), and its host is chosen by the
user, so the server makes outgoing requests to user-chosen hosts. The application runs one
worker with the in-process APScheduler of [ADR-0018](0018-recurring-items.md).

## Decision

- **Dependency.** Add **Apprise** (`apprise>=2.1,<3`, BSD-2-Clause, actively maintained, typed),
  the library the stack names for notifications. It turns one URL syntax into deliveries to
  more than 100 services; the standard library and the existing dependencies offer nothing
  comparable. It adds requests (Apache-2.0), requests-oauthlib (ISC), oauthlib (BSD-3-Clause),
  Markdown (BSD-3-Clause), charset-normalizer (MIT) and urllib3 (MIT); certifi, click and
  PyYAML were already locked. All are compatible with AGPL-3.0-or-later. Apprise installs about
  13 MB (its service plugins). Only the adapter `notify_apprise.py` (outside the strictly typed
  service layer, like the scheduler adapter) imports it; everything else
  uses the `Notifier` protocol, and tests use a fake that never touches the network.
- **Channels per member and workspace.** `NotificationChannel` belongs to one user in one
  workspace. Every member, viewers included, manages only their own channels; nobody (owners
  included) sees or changes another member's channels. Removing a member deletes their channels
  in that workspace. Channels are not part of the workspace archive (F3.8).
- **Secret storage.** The URL is encrypted with Fernet under
  `derive_subkey(instance_secret, "notification-url")`, the pattern of
  [ADR-0019](0019-local-totp.md). The plaintext is `"<channel_id>|<url>"`, so a ciphertext
  copied to another row is refused. The URL is never logged, never written to audit data and
  never rendered after saving: pages show the service name and a fixed mask. Editing keeps the
  stored URL unless a new one is typed. A ciphertext that no longer decrypts (the instance
  secret changed) fails that channel with `decrypt_failed`, logged without secret data.
- **Outgoing requests (SSRF).** A URL must be a single Apprise URL whose scheme is on an
  allowlist, checked when saving and again before every send. The default list holds services
  with a fixed protocol: `ntfy(s)`, `gotify(s)`, `tgram`, `mailto(s)`, `discord`, `slack`,
  `matrix(s)`, `pover`, `signal(s)`, `rocket(s)`, `mmost(s)`, `apprise(s)`. Generic webhooks
  (`json`, `xml`, `form` and their secure variants) and schemes that act on the server itself
  (desktop notifications, D-Bus, syslog, ...) are left out. Operators can replace the list with
  `ASSETBOARD_NOTIFICATION_SCHEMES` and switch all deliveries off with
  `ASSETBOARD_NOTIFICATIONS_ENABLED=false`. Private and loopback addresses are **not**
  blocked: a ntfy or Gotify server on the local network is the main homelab use. *Residual
  risk:* any member can make the server send requests to hosts it can reach, with a body they
  only partly control, in the protocol of an allowed service. Responses are never shown (the
  result is "sent" or a failure code), which limits it to blind requests. Instances that admit
  untrusted members should narrow the allowlist or turn notifications off.
- **Timeouts and runtime bounds.** Each send overrides the plugin's socket connect and read
  timeouts with `ASSETBOARD_NOTIFICATION_TIMEOUT_SECONDS` (default 10) and runs on a daemon
  thread that the caller stops waiting for after that time, so a hung service holds neither a
  scheduler tick nor a "Send test" request. A tick starts no new channel after
  `ASSETBOARD_NOTIFICATION_TICK_SECONDS` (default 45); the channels it did not reach are picked
  up by the next tick.
- **Schedule and idempotency.** One scheduler job runs every minute. A channel runs once per
  local day (workspace time zone; users have none) at or after its `digest_time`, recorded in
  `last_run_on`, which also makes a restart or downtime catch up. In one immediate transaction
  the run decides what to send, claims each reminder as a `NotificationLog` row (`sending`)
  under the unique key `(channel_id, kind, subject_key, sent_on)` and sets `last_run_on`; it
  then sends outside the transaction and records `sent` or `failed`. `subject_key` replaces the
  spec's nullable `occurrence_id` in the key (SQL treats NULLs as distinct, so work-log
  reminders could repeat). A row left `sending` for 10 minutes counts as failed. Failed
  reminders are retried by the next daily run. Rows older than 60 days are pruned.
- **Content.** Messages name the item, the date and the work month, translated into the
  recipient's language (their saved language, else the workspace's). Amounts are included only
  when the member ticks "Include amounts" on the channel (default off). Messages pass through
  third-party services (the notification provider, push gateways, mail servers), which the
  channel form says.

## Consequences

Reminders work with the services people already run, without new operator settings. The
instance secret must be backed up with the database, as for authenticator secrets; replacing
it makes stored channel URLs unreadable and members re-enter them. A daily run that fails is
retried a day later, and a reminder claimed just before a crash may be lost rather than sent
twice. The application can be made to contact hosts on its network; the allowlist, the kill
switch and the documentation above are the mitigation.

References: [Apprise](https://github.com/caronc/apprise),
[Fernet](https://cryptography.io/en/latest/fernet/).
