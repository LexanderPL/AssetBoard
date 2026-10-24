# ADR-0017: Email, verified registration and password reset

**Status:** Accepted
**Date:** 2026-10-19

## Context

Every account is tied to an email address, but until now the application sent no email. Open
registration was an environment flag (`ASSETBOARD_ALLOW_REGISTRATION`) with no proof that the
registrant owned the address, and it answered "This email address cannot be registered" for a
taken address, an accepted account-existence signal (ADR-0012). There was no way to recover a
forgotten password without an administrator running `assetboard reset-password`.

The owner decided (pre-release B):

1. Open registration is switched on by an instance administrator in **Instance settings**, not
   by an environment variable, and only with SMTP configured.
2. Self-registered accounts must verify their email address. Accounts created through an
   invitation link, OIDC or proxy authentication, or by an administrator do not.
3. "Forgot password" by email is in scope.
4. SMTP settings come from environment variables; the password is never logged.
5. **No account-existence signal at all**: registration, asking for a new link and forgot
   password answer identically, with similar timing, whether or not the address has an
   account, and their throttles must not leak either.

## Decision

### Mail

- `services/mail.py`: a `Mailer` protocol, `SmtpMailer` on the standard library (`smtplib`,
  `email.message.EmailMessage` with `policy.SMTP`, which refuses line breaks in headers), and
  a fake in the tests; tests never use the network. `ASSETBOARD_SMTP_HOST`, `_PORT` (587/465/25
  by default), `_SECURITY` (`starttls` by default, `tls`, `none`), `_USERNAME`, `_PASSWORD`,
  `_FROM`, `_TIMEOUT_SECONDS`. Certificates are verified (`ssl.create_default_context()`). A
  password over `none` refuses to start outside `ENV=dev`. No new dependency.
- Requests never send mail and never look an address up. Registration's first step and the
  forgot form hand a `MailRequest` (purpose, normalized address, language and the already
  truncated network prefix, the same for every address) to the `MailOutbox` through a
  Starlette `BackgroundTask`, which runs only after the response body was sent. The request
  becomes due after a random 0.5 to 3 seconds (`secrets.SystemRandom`; tests inject 0). The
  delay blocks nothing: due jobs wait in a heap, a scheduler thread sleeps on a condition until
  the earliest is due (new jobs and stop wake it), and a pool of 4 sender threads then, in its
  own `BEGIN IMMEDIATE` transaction with the injected clock, prunes expired links, looks the
  address up, issues the link and writes the audit row with the request's network prefix
  (`resolve_mail_request`), and finally renders and sends. One slow SMTP server holds up only
  its own job. The raw token exists only in that thread's memory and in the mail.
- At most 100 jobs are pending (waiting or in flight). Beyond that a job is dropped, and a
  `mail_dropped count=N reason=queue_full` WARNING, aggregated to one line per minute, tells the
  administrator. A failed job is logged with its kind and the exception class only (never the
  address, a link, the password or the server's reply) and not retried. Stopping the outbox
  (application shutdown) refuses new jobs, drops those not yet due (logged with a count),
  cancels those not started and waits up to 5 seconds for those in flight, so no thread
  outlives the database engine; the next startup accepts jobs again. The person asks again for
  anything lost. A persistent outbox and retries are follow-ups.
- Addresses: a new address (registration, every new account) is refused when it contains
  whitespace or one of `<>()[],;:"\`, so it is always one plain addr-spec; addresses stored
  earlier keep working for sign-in, and the forgot form looks addresses up leniently so their
  owners can still recover. Mail only goes to the exact stored or verified address: `To` is
  `email.headerregistry.Address(addr_spec=...)` and must round-trip unchanged (the email
  package drops comments, so `a(b)@x` would become `a@x`); otherwise the job is skipped and
  logged (`mail_skipped`, with the address's keyed hash). The SMTP envelope names exactly that
  one recipient (`send_message(..., to_addrs=[address])`).
- `Settings` hides input values in validation errors (`hide_input_in_errors`), so the SMTP
  password or the OIDC secret never appear in a startup error or CLI output.
- Emails are plain text, rendered from `web/templates/email/*.txt` with gettext in the
  recipient's language (the account's saved language, else the request's). Every link is built
  from `ASSETBOARD_BASE_URL`, never from request headers; email therefore needs an explicitly
  set `BASE_URL`, `https://` outside `ENV=dev`. The `From` display name is the application name.
- `assetboard mail-test <address>` sends a test email synchronously and reports success or the
  exception's class name only.

### Tokens

- Table `email_tokens` (migration 0014): `purpose` (`register` or `reset`), the SHA-256 of a
  256-bit `secrets.token_urlsafe(32)` token (never the token itself), the normalized address,
  the user (reset only), the mail language, `created_at`, `expires_at`.
- Single use: the row is deleted atomically (`DELETE ... WHERE id = ? AND expires_at > now`,
  checked to remove exactly one row) before the user is changed or created, so two parallel
  requests cannot both use it, on SQLite or PostgreSQL. Registration links live 24
  hours, reset links 60 minutes. At most three live links per address (registration) or per
  account (reset); a fourth deletes the oldest. Expired rows are deleted by every outbox job
  and at startup.
- Every password change (profile, reset, `assetboard reset-password`), deactivation and change
  of the administrator role deletes the account's reset links. A reset requires an active
  account with a password whose address is still the one the link was sent to.
- Links are opened with GET (a form) and used only by POST, so mail scanners that prefetch links
  cannot use them. The token is in the URL path, like invitations: the request log redacts it,
  and these pages send `Referrer-Policy: same-origin` and `Cache-Control: no-store`.

### Registration in two steps

1. `/register` asks only for the email address and always answers "If this address can be
   registered, we sent it a link". The mail differs: a link to finish registering (no account
   has the address), a note that an account exists with a link to sign in (and to reset the
   password when the account has one) (active account), or nothing (deactivated account).
2. `/register/complete/<token>` (signed-in visitors are sent home, so no session is replaced)
   asks for the display name, password and workspace name and
   creates the account, its workspace (with the suggested categories) and its owner membership
   in one transaction that also spends every registration link of the address; the account is
   signed in. An account created meanwhile spends the link too.

No account exists before step 2, so there are no unverified accounts to restrict or clean up,
and nobody can pre-register someone else's address with a password they know (the password is
chosen only after the link proved the address). "Resend the link" is step 1 again. Registration
is effective only while the setting is on, SMTP is configured and `OIDC_ONLY` is off; the admin
page disables the switch otherwise (turning it off always works), and the service refuses it.
`OIDC_ONLY` therefore no longer refuses to start with registration: it simply wins.

### Password reset

`/password/forgot` asks for the address and always answers "If an account uses this address, we
sent it an email". An active account with a password gets a reset link (audited as
`auth.password_reset_requested` by the worker); an active account without a password (identity provider or
proxy) gets a note that there is nothing to reset, so no password can be added to such an
account by email; others get nothing. `/password/reset/<token>` sets the new password (policy
checked, hashed through the hash gate outside the transaction), clears `must_change_password`,
ends every session of the account, spends its reset links and records `auth.password_reset`
with the number of sessions ended. It does not sign anyone in. It is not offered under
`OIDC_ONLY` or without SMTP, nor on the break-glass page.

### Enumeration safety and throttles

- The first step of registration and the forgot form give the same status, page and headers for
  every address, and run the same database statements for every address: validate, count the
  client bucket and the address bucket, answer. A test compares the statements with a
  `before_cursor_execute` spy for unknown, existing, deactivated and passwordless addresses.
  Everything that depends on the account happens in the worker after the response and the
  random delay, so neither the response nor its timing carries a signal.
- `mail_email` counts emails per normalized address (registration and reset together, default 3
  per 60 minutes), the same for every address whether or not an account has it. Past the limit
  the answer stays the same and no mail is sent: it limits mail flooding without a signal. The
  trade-off is a denial of recovery: anyone can use up an address's quota and so delay its
  owner's registration or password reset by up to one window. Each refusal is logged at
  WARNING (`mail_address_limit`) with a 16-hex-digit HMAC-SHA-256 of the address under a
  subkey derived from the ensured secret key (`SECRET_KEY`, or the key file under `DATA_DIR`),
  so an administrator can see a targeted address without the log holding it.
- Residual timing: the request path is identical, but the work it defers is not. The worker's
  transaction for an existing account writes more (a link, an audit row) and briefly holds the
  database write lock; a concurrent request could in principle notice that contention. The
  random delay decorrelates it from the request that caused it, and the volume is bounded by
  the throttles. The mail itself is observable only by the owner of the mailbox.
- The client buckets are the requester's own and may say "try later": the existing `register`
  bucket (both registration steps) and `mail_ip` (forgot requests and new passwords set from a
  link, default 10 per 60 minutes). None of them is reset by success.
- Out of scope of the rule: invitation sign-up still answers "This email address cannot be
  registered" for an address that has an account (owner decision). The invitation link is a
  secret, email-locked, single-use and counted (ADR-0013), so only its holder, who already knows
  the invited address, sees the answer. The second registration step treats an account created
  meanwhile as an invalid link; only the owner of the address holds that link.

### Retiring `ASSETBOARD_ALLOW_REGISTRATION`

`true` refuses to start with a message pointing to Instance settings and SMTP; `false` (the old
default) is ignored with a warning. It does not seed the new setting: that could turn on
registration without SMTP or verification.

## Consequences

- Self-registered accounts have proven their address; administrators decide in the UI whether
  anyone can register, and only with working email.
- A forgotten password no longer needs an administrator, but whoever controls the mailbox
  controls the account (as with every email-based recovery). Accounts that sign in through a
  provider are unaffected.
- Registering takes two steps. Emails can be lost on restart or SMTP failure; people ask again.
- `ASSETBOARD_ALLOW_REGISTRATION=true` deployments must remove the variable before upgrading.
- HTML mail, retries and a persistent outbox, notifications ("your password was changed") and
  changing an account's address are follow-ups.
