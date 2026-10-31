# ADR-0019: Local TOTP and workspace enforcement

**Status:** Accepted (the owner accepted the full scope for 1.0 on 2026-10-24: optional TOTP,
recovery codes, per-workspace enforcement, administrator reset and CLI reset)
**Date:** 2026-10-20

## Context

F1.6 was deferred in PR #93. This contribution implements the optional local second factor
from issue #26 and resolves the previously undecided owner policy as workspace-specific.
Global users may belong to unrelated workspaces; a workspace owner must not change another
workspace's access policy. External identity providers remain responsible for their MFA.

## Decision

- Use PyOTP for six-digit SHA-1 TOTP with 30-second steps and one step of drift in either
  direction. These are the authenticator defaults; the one-step tolerance handles modest
  clock drift without accepting arbitrarily old codes. Persist the last accepted step and
  serialize factor updates to reject replay, including simultaneous requests.
- Ask for password and code together on password sign-in, including `/login/local`
  (amended: since the two-step sign-in change the code is asked for on a second page after a
  correct password, backed by a five-minute single-use login ticket; see F1.6). Reuse the
  existing reserved account/address attempts; never create a partial login session. A missing
  or invalid code uses the existing generic failure. Password-confirmed identity linking also
  verifies the factor before clearing its confirmation throttle or linking the new identity.
- Enrollment requires password confirmation through the existing confirmation entry point.
  Its pending secret is bound to that session for ten minutes (the existing sensitive-action
  freshness interval); completion requires a code. Enrollment attempts have a separate per-user
  bucket using existing throttle settings, unaffected by restarting enrollment.
- Encrypt pending and enabled secrets with Fernet using `derive_subkey(instance_secret,
  "totp")`, derived once per instance secret. The plaintext is `"<user_id>|<secret>"` and
  decryption checks the prefix, so a ciphertext copied to another account's row is refused.
  The instance already persists this secret outside the database; there is no new operator
  setting. A ciphertext that cannot be decrypted (the instance secret changed) logs a warning
  without secret data that points to the key-change note; there is no key rotation
  (`MultiFernet`). Generate QR SVG locally through Segno; no enrollment data leaves the app.
- Generate ten 128-bit random recovery codes and show them once after enrollment. Store SHA-256
  hashes, sufficient for random high-entropy values rather than user passwords. Lock the factor
  and consume a code in the same transaction as issuing a session. Both SQLite's existing
  immediate transaction and PostgreSQL row locks serialize consumption.
- `Workspace.require_two_factor` defaults off. Context construction checks it for password
  sessions, and write role guards re-read it. Every member, owners included, needs a verified
  second factor to access that workspace. An owner cannot turn it on from a password session
  without a verified second factor (that would lock them out); external sessions can. Profile/security routes and the workspace switcher
  remain available; unrelated workspaces and OIDC/proxy sessions are unaffected.
- Disabling requires the password and a current authenticator code or an unused recovery code,
  checked together by the service (a wrong code is a failed confirmation). Enabling and
  disabling revoke other password sessions. The current local
  session records whether the second factor was verified. Audit these changes without secrets.
- Regenerating recovery codes requires the current password and TOTP or an unused recovery code,
  verified in the service as for disabling; old codes are invalidated and the replacement set is
  shown once, grouped as `xxxx-xxxx-…` (dashes are ignored on input). An instance administrator can
  reset another user's factor after confirming their own account; this removes the factor and all
  recovery codes, revokes every target session, and is audited without secret data. An operator
  with shell access can run `assetboard reset-mfa <email>`, which removes the factor and recovery
  codes, revokes the user's password sessions and writes a `cli.reset_mfa` audit event.
- A correct password with a missing or wrong code fails exactly like a wrong password (same
  response); the `auth.login_failure` event records the internal reason `second_factor`. A stale
  password hash is upgraded only after the whole sign-in succeeds, second factor included, so
  rehashing time never reveals a correct password to someone without the second factor.

## Consequences

Backups must preserve the persistent instance secret as well as the database. Replacing it
makes authenticator secrets unreadable and authenticator sign-in fails closed; recovery codes
remain usable. Changing passwords does not clear MFA. Users who lose all recovery methods need an
instance administrator to reset the factor through the Users page, or an operator to run
`assetboard reset-mfa`.

Accepted residual risk: after a correct password, sign-in decrypts the secret and computes a few
HMACs before deciding, which a wrong password skips. The difference is well under a millisecond,
far below network and argon2 jitter, and every attempt counts in the existing sign-in throttle,
so it is not practical to measure; it is accepted rather than padded with dummy work.

New direct dependencies are PyOTP (MIT, the spec's TOTP implementation; releases are infrequent,
the latest being 2.9.0 in 2023, which suits a small, stable RFC 6238 implementation), Segno
(BSD-3-Clause, a maintained pure-Python QR encoder with no dependencies), and cryptography
(Apache-2.0 OR BSD-3-Clause, maintained by PyCA and already locked transitively through
Authlib). The standard library and existing app dependencies do not supply a TOTP provisioning
API or QR encoder. Declaring cryptography directly supports using its authenticated-encryption
recipe without implementing encryption ourselves. These licenses are compatible with AGPL-3.0.

References: [PyOTP](https://pyauth.github.io/pyotp/),
[Segno](https://segno.readthedocs.io/en/latest/),
[Fernet](https://cryptography.io/en/latest/fernet/),
[RFC 6238](https://www.rfc-editor.org/rfc/rfc6238).
