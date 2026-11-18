# Security checklist

This checklist maps AssetBoard to the Level 1 requirements of the [OWASP Application
Security Verification Standard (ASVS) 4.0.3](https://github.com/OWASP/ASVS/tree/v4.0.3).
It is a point-in-time self-assessment, not a certification. Each row says **Done**, **N/A**
(with the reason) or **Gap** (with the follow-up). Code and tests are linked.

ASVS 4.0.3 has no Level 1 requirements in V1 (Architecture); the project's architecture
rules are in [AGENTS.md](../AGENTS.md), [docs/architecture.md](architecture.md) and the
[ADRs](adr/README.md).

## Status at a glance

| Status | Count |
| --- | ---: |
| Done | 87 |
| N/A | 27 |
| Gap | 13 |
| **Total** | **127** |

### Genuine gaps and follow-ups

These are the rows marked **Gap**. They are known limitations, not hidden exceptions.

- **2.1.1** — Minimum is 8 characters (ADR-0012); ASVS L1 wants 12. Follow-up: raise the floor and amend ADR-0012, with the migration message for existing short passwords. [auth.py](../src/assetboard/services/auth.py)
- **2.1.2** — Maximum is 1024 characters; ASVS L1 wants at least 64 permitted and more than 128 denied. Follow-up: cap at 128 and update the forms. [auth.py](../src/assetboard/services/auth.py)
- **2.1.7** — A local common-password list is checked when a password is set or changed, not on sign-in. Follow-up: re-check at sign-in and force a change when the list changes. [auth.py](../src/assetboard/services/auth.py)
- **2.1.8** — No strength meter. Follow-up: add a non-blocking strength hint to the password fields. [auth.py](../src/assetboard/services/auth.py)
- **2.2.3** — Auth changes (password reset, MFA, identity link) are audited but no outbound notification is sent. Follow-up: optional email notice when SMTP is configured. [account_email.py](../src/assetboard/services/account_email.py), [test_email_flows.py](../tests/web/test_email_flows.py)
- **2.3.1** — Administrator-created temporary passwords are random and must be changed at first sign-in, but they do not expire on a timer. Follow-up: add a short unused-password expiry. [auth.py](../src/assetboard/services/auth.py)
- **2.5.5** — MFA and identity changes are audited but the account owner is not notified. Same follow-up as 2.2.3. [auth.py](../src/assetboard/services/auth.py)
- **4.3.1** — TOTP is optional; a workspace can require it, but instance-admin pages do not require MFA by default. Follow-up: an instance setting to require MFA for administrators. [context.py](../src/assetboard/services/context.py), [test_authorization_matrix.py](../tests/web/test_authorization_matrix.py)
- **5.2.6** — Notification and price-provider URLs are user-configured. Schemes are allow-listed, but hosts and private addresses are not. This is an accepted ADR-0021 risk; follow-up: optional host allow-list / egress policy. [deployment.md](deployment.md#security-and-hardening)
- **8.3.1** — Session tokens never appear in a URL, but invite and reset links carry bearer tokens in the path, and /login?next=/invite/<token> puts one in a query. Logs redact them and Referrer-Policy is same-origin; a browser history/URL leak remains. ADR-0013 documents the trade-off; follow-up: a short-lived exchange page. [account_email.py](../src/assetboard/services/account_email.py), [test_email_flows.py](../tests/web/test_email_flows.py)
- **12.4.2** — No antivirus scanning. The archive is structurally validated, but a malicious file inside a legitimate backup is not detected. Self-hosted, single-user risk; follow-up: optional ClamAV scan hook. [archive.py](../src/assetboard/domain/archive.py), [test_archive.py](../tests/unit/test_archive.py)
- **12.6.1** — Outbound requests are limited by URL scheme for notifications and by provider definitions for prices, but not by host. Same follow-up as 5.2.6. [deployment.md](deployment.md#security-and-hardening)
- **14.4.5** — HSTS is sent when BASE_URL is https, but includeSubDomains is omitted: a self-hosted instance may share a parent domain. Follow-up: document includeSubDomains for single-host deployments. [middleware.py](../src/assetboard/middleware.py), [test_security_hardening.py](../tests/web/test_security_hardening.py)

## How this is verified

- `TMPDIR=/home/alex/.cache/assetboard-pytest PYTEST_WORKERS=2 make check`
- `ASSETBOARD_RUN_BROWSER_TESTS=1 TMPDIR=/home/alex/.cache/assetboard-pytest uv run pytest tests/browser -q -n 1 --no-cov`
- `.github/workflows/security.yml`: `pip-audit` and the third-party notice freshness check.
- `.github/workflows/docker.yml`: Trivy image scan (report first, HIGH/CRITICAL gate) and BuildKit SBOM attestation.
- `.github/workflows/release-please.yml` + `deploy/build-release.sh`: CycloneDX SBOM attached to each release.

## V2 Authentication

| # | Requirement (abbreviated) | Status | Evidence / gap |
| --- | --- | --- | --- |
| 2.1.1 | Verify that user set passwords are at least 12 characters in length (after multiple spaces are combined). | Gap | Minimum is 8 characters (ADR-0012); ASVS L1 wants 12. Follow-up: raise the floor and amend ADR-0012, with the migration message for existing short passwords. [auth.py](../src/assetboard/services/auth.py) |
| 2.1.2 | Verify that passwords of at least 64 characters are permitted, and that passwords of more than 128 characters are denied. | Gap | Maximum is 1024 characters; ASVS L1 wants at least 64 permitted and more than 128 denied. Follow-up: cap at 128 and update the forms. [auth.py](../src/assetboard/services/auth.py) |
| 2.1.3 | Verify that password truncation is not performed. However, consecutive multiple spaces may be replaced by a single space. | Done | The submitted password is hashed whole; spaces are not collapsed or truncated. [auth.py](../src/assetboard/services/auth.py) |
| 2.1.4 | Verify that any printable Unicode character, including language neutral characters such as spaces and Emojis are permitted in passwords. | Done | Any printable Unicode is accepted; length is counted in code points and the value is never normalized. [auth.py](../src/assetboard/services/auth.py) |
| 2.1.5 | Verify users can change their password. | Done | Account security page and CLI reset. [auth.py](../src/assetboard/services/auth.py), [test_account_pages.py](../tests/web/test_account_pages.py) |
| 2.1.6 | Verify that password change functionality requires the user's current and new password. | Done | Changing a password asks for the current one; the same page confirms it. [auth.py](../src/assetboard/services/auth.py), [test_account_pages.py](../tests/web/test_account_pages.py) |
| 2.1.7 | Verify that passwords submitted during account registration, login, and password change are checked against a set of breached passwords either… | Gap | A local common-password list is checked when a password is set or changed, not on sign-in. Follow-up: re-check at sign-in and force a change when the list changes. [auth.py](../src/assetboard/services/auth.py) |
| 2.1.8 | Verify that a password strength meter is provided to help users set a stronger password. | Gap | No strength meter. Follow-up: add a non-blocking strength hint to the password fields. [auth.py](../src/assetboard/services/auth.py) |
| 2.1.9 | Verify that there are no password composition rules limiting the type of characters permitted. There should be no requirement for upper or lower… | Done | No composition rules; the common-password and identity checks are the only content rules. [auth.py](../src/assetboard/services/auth.py) |
| 2.1.10 | Verify that there are no periodic credential rotation or password history requirements. | Done | No rotation or history requirements; password change is user-initiated. [auth.py](../src/assetboard/services/auth.py) |
| 2.1.11 | Verify that "paste" functionality, browser password helpers, and external password managers are permitted. | Done | Native password inputs; paste, browser password managers and external managers are not blocked. [test_auth.py](../tests/unit/test_auth.py) |
| 2.1.12 | Verify that the user can choose to either temporarily view the entire masked password, or temporarily view the last typed character of the… | N/A | Reveal is owned by the browser/password manager; the app uses native password inputs and does not disable the platform control. |
| 2.2.1 | Verify that anti-automation controls are effective at mitigating breached credential testing, brute force, and account lockout attacks. Such… | Done | Per-email and per-address throttles with exponential backoff, capped at 15 minutes; blocking happens before hashing. [auth.py](../src/assetboard/services/auth.py), [test_sessions_page.py](../tests/web/test_sessions_page.py) |
| 2.2.2 | Verify that the use of weak authenticators (such as SMS and email) is limited to secondary verification and transaction approval and not as a… | N/A | No SMS/PSTN authenticator. Email is used only to deliver registration and reset links, never as a primary factor. |
| 2.2.3 | Verify that secure notifications are sent to users after updates to authentication details, such as credential resets, email or address changes,… | Gap | Auth changes (password reset, MFA, identity link) are audited but no outbound notification is sent. Follow-up: optional email notice when SMTP is configured. [account_email.py](../src/assetboard/services/account_email.py), [test_email_flows.py](../tests/web/test_email_flows.py) |
| 2.3.1 | Verify system generated initial passwords or activation codes SHOULD be securely randomly generated, SHOULD be at least 6 characters long, and MAY… | Gap | Administrator-created temporary passwords are random and must be changed at first sign-in, but they do not expire on a timer. Follow-up: add a short unused-password expiry. [auth.py](../src/assetboard/services/auth.py) |
| 2.5.1 | Verify that a system generated initial activation or recovery secret is not sent in clear text to the user. | Done | Reset and registration links are random bearer tokens; no generated password is emailed. [account_email.py](../src/assetboard/services/account_email.py), [test_email_flows.py](../tests/web/test_email_flows.py) |
| 2.5.2 | Verify password hints or knowledge-based authentication (so-called "secret questions") are not present. | Done | No password hints or secret questions. [auth.py](../src/assetboard/services/auth.py) |
| 2.5.3 | Verify password credential recovery does not reveal the current password in any way. | Done | Reset only sets a new password and never displays the current one. [account_email.py](../src/assetboard/services/account_email.py), [test_email_flows.py](../tests/web/test_email_flows.py) |
| 2.5.4 | Verify shared or default accounts are not present (e.g. "root", "admin", or "sa"). | Done | No default or shared accounts; first-run setup needs the one-time setup token. [auth.py](../src/assetboard/services/auth.py) |
| 2.5.5 | Verify that if an authentication factor is changed or replaced, that the user is notified of this event. | Gap | MFA and identity changes are audited but the account owner is not notified. Same follow-up as 2.2.3. [auth.py](../src/assetboard/services/auth.py) |
| 2.5.6 | Verify forgotten password, and other recovery paths use a secure recovery mechanism, such as time-based OTP (TOTP) or other soft token, mobile… | Done | Reset link plus optional TOTP/recovery codes; no knowledge-based recovery. [account_email.py](../src/assetboard/services/account_email.py), [test_email_flows.py](../tests/web/test_email_flows.py) |
| 2.7.1 | Verify that clear text out of band (NIST "restricted") authenticators, such as SMS or PSTN, are not offered by default, and stronger alternatives… | N/A | No out-of-band authenticators are offered. |
| 2.7.2 | Verify that the out of band verifier expires out of band authentication requests, codes, or tokens after 10 minutes. | N/A | No out-of-band verifier; TOTP is covered by 2.8.1. |
| 2.7.3 | Verify that the out of band verifier authentication requests, codes, or tokens are only usable once, and only for the original authentication request. | N/A | No out-of-band verifier. |
| 2.7.4 | Verify that the out of band authenticator and verifier communicates over a secure independent channel. | N/A | No out-of-band verifier. |
| 2.8.1 | Verify that time-based OTPs have a defined lifetime before expiring. | Done | TOTP has 30-second steps, one step of drift, and a five-minute two-step ticket. [auth.py](../src/assetboard/services/auth.py) |

## V3 Session Management

| # | Requirement (abbreviated) | Status | Evidence / gap |
| --- | --- | --- | --- |
| 3.1.1 | Verify the application never reveals session tokens in URL parameters. | Done | Session tokens are only in cookies; invite/reset tokens are separate, one-use email links. [test_sessions_page.py](../tests/web/test_sessions_page.py) |
| 3.2.1 | Verify the application generates a new session token on user authentication. | Done | Every successful sign-in creates a new 256-bit session and deletes the previous one. [test_sessions_page.py](../tests/web/test_sessions_page.py) |
| 3.2.2 | Verify that session tokens possess at least 64 bits of entropy. | Done | 32 random bytes from secrets.token_urlsafe. [auth.py](../src/assetboard/services/auth.py) |
| 3.2.3 | Verify the application only stores session tokens in the browser using secure methods such as appropriately secured cookies (see section 3.4) or… | Done | Server-side sessions referenced by a cookie. [test_sessions_page.py](../tests/web/test_sessions_page.py) |
| 3.3.1 | Verify that logout and expiration invalidate the session token, such that the back button or a downstream relying party does not resume an… | Done | Logout deletes the server row; idle/absolute expiry is server-side. [test_sessions_page.py](../tests/web/test_sessions_page.py), [test_security_hardening.py](../tests/web/test_security_hardening.py) |
| 3.4.1 | Verify that cookie-based session tokens have the 'Secure' attribute set. | Done | Secure is set when BASE_URL is https; the cookie is also __Host- prefixed. HTTP is the local-development exception. [test_security_hardening.py](../tests/web/test_security_hardening.py) |
| 3.4.2 | Verify that cookie-based session tokens have the 'HttpOnly' attribute set. | Done | HttpOnly on every app cookie. [test_security_hardening.py](../tests/web/test_security_hardening.py) |
| 3.4.3 | Verify that cookie-based session tokens utilize the 'SameSite' attribute to limit exposure to cross-site request forgery attacks. | Done | SameSite=Lax plus a signed CSRF token and Origin check. [csrf.py](../src/assetboard/services/csrf.py), [test_csrf_origin.py](../tests/web/test_csrf_origin.py) |
| 3.4.4 | Verify that cookie-based session tokens use the "__Host-" prefix so cookies are only sent to the host that initially set the cookie. | Done | __Host- on HTTPS; browsers require Secure for the prefix. [test_security_hardening.py](../tests/web/test_security_hardening.py) |
| 3.4.5 | Verify that if the application is published under a domain name with other applications that set or use session cookies that might disclose the… | N/A | Single application per origin; __Host- prevents domain sharing and the prefix requires Path=/. |
| 3.7.1 | Verify the application ensures a full, valid login session or requires re-authentication or secondary verification before allowing any sensitive… | Done | Sensitive actions ask for the password or a sign-in from the last ten minutes. [context.py](../src/assetboard/services/context.py), [test_authorization_matrix.py](../tests/web/test_authorization_matrix.py) |

## V4 Access Control

| # | Requirement (abbreviated) | Status | Evidence / gap |
| --- | --- | --- | --- |
| 4.1.1 | Verify that the application enforces access control rules on a trusted service layer, especially if client-side access control is present and… | Done | Services enforce roles; routes never decide authorization alone. [context.py](../src/assetboard/services/context.py), [test_authorization_matrix.py](../tests/web/test_authorization_matrix.py) |
| 4.1.2 | Verify that all user and data attributes and policy information used by access controls cannot be manipulated by end users unless specifically… | Done | Role, workspace and membership come from the server-side session/database, never from a form field. [context.py](../src/assetboard/services/context.py), [test_authorization_matrix.py](../tests/web/test_authorization_matrix.py) |
| 4.1.3 | Verify that the principle of least privilege exists - users should only be able to access functions, data files, URLs, controllers, services, and… | Done | Viewer/editor/owner ranks and per-workspace membership; instance administration is separate. [context.py](../src/assetboard/services/context.py), [test_authorization_matrix.py](../tests/web/test_authorization_matrix.py) |
| 4.1.5 | Verify that access controls fail securely including when an exception occurs. | Done | PermissionDenied is handled as 403; missing rows are 404. [app.py](../src/assetboard/app.py), [test_security_hardening.py](../tests/web/test_security_hardening.py) |
| 4.2.1 | Verify that sensitive data and APIs are protected against Insecure Direct Object Reference (IDOR) attacks targeting creation, reading, updating… | Done | Every workspace query is repository-scoped; every workspace-owned model is in the tenant test. [test_tenant_isolation.py](../tests/integration/test_tenant_isolation.py) |
| 4.2.2 | Verify that the application or framework enforces a strong anti-CSRF mechanism to protect authenticated functionality, and effective… | Done | Router-level CSRF dependency on every unsafe route plus the Origin check. [csrf.py](../src/assetboard/services/csrf.py), [test_csrf_origin.py](../tests/web/test_csrf_origin.py) |
| 4.3.1 | Verify administrative interfaces use appropriate multi-factor authentication to prevent unauthorized use. | Gap | TOTP is optional; a workspace can require it, but instance-admin pages do not require MFA by default. Follow-up: an instance setting to require MFA for administrators. [context.py](../src/assetboard/services/context.py), [test_authorization_matrix.py](../tests/web/test_authorization_matrix.py) |
| 4.3.2 | Verify that directory browsing is disabled unless deliberately desired. Additionally, applications should not allow discovery or disclosure of… | Done | Static files are served from one directory; no listing, VCS or backup files. [Dockerfile](../Dockerfile), [compose.example.yaml](../compose.example.yaml), [smoke_container.sh](../scripts/smoke_container.sh) |

## V5 Validation, Sanitization and Encoding

| # | Requirement (abbreviated) | Status | Evidence / gap |
| --- | --- | --- | --- |
| 5.1.1 | Verify that the application has defenses against HTTP parameter pollution attacks, particularly if the application framework makes no distinction… | Done | Forms bind one typed value per field; duplicate values cannot merge privileges. [context.py](../src/assetboard/services/context.py), [test_authorization_matrix.py](../tests/web/test_authorization_matrix.py) |
| 5.1.2 | Verify that frameworks protect against mass parameter assignment attacks, or that the application has countermeasures to protect against unsafe… | Done | Explicit Form fields and Pydantic models; no blind model assignment. [context.py](../src/assetboard/services/context.py), [test_authorization_matrix.py](../tests/web/test_authorization_matrix.py) |
| 5.1.3 | Verify that all input (HTML form fields, REST requests, URL parameters, HTTP headers, cookies, batch files, RSS feeds, etc) is validated using… | Done | Lengths, patterns, enums and typed values are checked on the server. [auth.py](../src/assetboard/services/auth.py), [archive.py](../src/assetboard/domain/archive.py), [test_archive.py](../tests/unit/test_archive.py) |
| 5.1.4 | Verify that structured data is strongly typed and validated against a defined schema including allowed characters, length and pattern (e.g. credit… | Done | Pydantic/FastAPI and the domain parsers validate structured input. [archive.py](../src/assetboard/domain/archive.py), [test_archive.py](../tests/unit/test_archive.py) |
| 5.1.5 | Verify that URL redirects and forwards only allow destinations which appear on an allow list, or show a warning when redirecting to potentially… | Done | safe_next accepts only local absolute paths; tests cover open-redirect forms. [test_sessions_page.py](../tests/web/test_sessions_page.py) |
| 5.2.1 | Verify that all untrusted HTML input from WYSIWYG editors or similar is properly sanitized with an HTML sanitizer library or framework feature. | N/A | No WYSIWYG or user HTML input. |
| 5.2.2 | Verify that unstructured data is sanitized to enforce safety measures such as allowed characters and length. | Done | Control characters, lengths and structure are validated per field. [auth.py](../src/assetboard/services/auth.py) |
| 5.2.3 | Verify that the application sanitizes user input before passing to mail systems to protect against SMTP or IMAP injection. | Done | Addresses are parsed and header-special forms are refused; mail is plain text. [account_email.py](../src/assetboard/services/account_email.py), [test_email_flows.py](../tests/web/test_email_flows.py) |
| 5.2.4 | Verify that the application avoids the use of eval() or other dynamic code execution features. Where there is no alternative, any user input being… | Done | No eval or dynamic code execution; CSP has no unsafe-eval. [middleware.py](../src/assetboard/middleware.py), [test_security_hardening.py](../tests/web/test_security_hardening.py) |
| 5.2.5 | Verify that the application protects against template injection attacks by ensuring that any user input being included is sanitized or sandboxed. | Done | Jinja autoescape; users cannot supply templates. [middleware.py](../src/assetboard/middleware.py), [test_security_hardening.py](../tests/web/test_security_hardening.py) |
| 5.2.6 | Verify that the application protects against SSRF attacks, by validating or sanitizing untrusted data or HTTP file metadata, such as filenames and… | Gap | Notification and price-provider URLs are user-configured. Schemes are allow-listed, but hosts and private addresses are not. This is an accepted ADR-0021 risk; follow-up: optional host allow-list / egress policy. [deployment.md](deployment.md#security-and-hardening) |
| 5.2.7 | Verify that the application sanitizes, disables, or sandboxes user-supplied Scalable Vector Graphics (SVG) scriptable content, especially as they… | N/A | No user-uploaded SVG or images; the only SVG is the bundled favicon. |
| 5.2.8 | Verify that the application sanitizes, disables, or sandboxes user-supplied scriptable or expression template language content, such as Markdown,… | N/A | No user-supplied Markdown, CSS, XSL or template language. |
| 5.3.1 | Verify that output encoding is relevant for the interpreter and context required. For example, use encoders specifically for HTML values, HTML… | Done | Jinja escapes HTML; no manual string interpolation in templates. [middleware.py](../src/assetboard/middleware.py), [test_security_hardening.py](../tests/web/test_security_hardening.py) |
| 5.3.2 | Verify that output encoding preserves the user's chosen character set and locale, such that any Unicode character point is valid and safely handled. | Done | UTF-8 templates and responses; locale-aware formatting is separate from escaping. [docs/](../docs) |
| 5.3.3 | Verify that context-aware, preferably automated - or at worst, manual - output escaping protects against reflected, stored, and DOM based XSS. | Done | Autoescape plus strict CSP; the page tests assert no inline scripts or handlers. [csrf.py](../src/assetboard/services/csrf.py), [test_csrf_origin.py](../tests/web/test_csrf_origin.py), [test_security_hardening.py](../tests/web/test_security_hardening.py) |
| 5.3.4 | Verify that data selection or database queries (e.g. SQL, HQL, ORM, NoSQL) use parameterized queries, ORMs, entity frameworks, or are otherwise… | Done | SQLAlchemy ORM and parameterized statements; no string-built SQL. [test_tenant_isolation.py](../tests/integration/test_tenant_isolation.py) |
| 5.3.5 | Verify that where parameterized or safer mechanisms are not present, context-specific output encoding is used to protect against injection… | N/A | No raw SQL paths that bypass parameters. |
| 5.3.6 | Verify that the application protects against JSON injection attacks, JSON eval attacks, and JavaScript expression evaluation. | Done | Values are passed as JSON data through Jinja's tojson; no eval. [middleware.py](../src/assetboard/middleware.py), [test_security_hardening.py](../tests/web/test_security_hardening.py) |
| 5.3.7 | Verify that the application protects against LDAP injection vulnerabilities, or that specific security controls to prevent LDAP injection have… | N/A | No LDAP integration. |
| 5.3.8 | Verify that the application protects against OS command injection and that operating system calls use parameterized OS queries or use contextual… | Done | No shell invocation in the app; the CLI passes argument lists. [Dockerfile](../Dockerfile), [compose.example.yaml](../compose.example.yaml), [smoke_container.sh](../scripts/smoke_container.sh) |
| 5.3.9 | Verify that the application protects against Local File Inclusion (LFI) or Remote File Inclusion (RFI) attacks. | Done | Only fixed templates/static files are served; archive members are validated and never extracted to arbitrary paths. [archive.py](../src/assetboard/domain/archive.py), [test_archive.py](../tests/unit/test_archive.py) |
| 5.3.10 | Verify that the application protects against XPath injection or XML injection attacks. | N/A | No XML/XPath input. |
| 5.5.1 | Verify that serialized objects use integrity checks or are encrypted to prevent hostile object creation or data tampering. | Done | Signed/encrypted cookies and AES-GCM/Fernet secrets; archive members are checksummed. [archive.py](../src/assetboard/domain/archive.py), [test_archive.py](../tests/unit/test_archive.py) |
| 5.5.2 | Verify that the application correctly restricts XML parsers to only use the most restrictive configuration possible and to ensure that unsafe… | N/A | No XML parser is used for input. |
| 5.5.3 | Verify that deserialization of untrusted data is avoided or is protected in both custom code and third-party libraries (such as JSON, XML and YAML… | Done | JSON and ZIP only; no pickle, YAML or Java-style deserialization. [archive.py](../src/assetboard/domain/archive.py), [test_archive.py](../tests/unit/test_archive.py) |
| 5.5.4 | Verify that when parsing JSON in browsers or JavaScript-based backends, JSON.parse is used to parse the JSON document. Do not use eval() to parse… | Done | The chart/bootstrap scripts use JSON.parse and never eval. [middleware.py](../src/assetboard/middleware.py), [test_security_hardening.py](../tests/web/test_security_hardening.py) |

## V6 Stored Cryptography

| # | Requirement (abbreviated) | Status | Evidence / gap |
| --- | --- | --- | --- |
| 6.2.1 | Verify that all cryptographic modules fail securely, and errors are handled in a way that does not enable Padding Oracle attacks. | Done | Argon2id and authenticated encryption; failed decryption is reported as a generic error, not as padding detail. [auth.py](../src/assetboard/services/auth.py) |

## V7 Error Handling and Logging

| # | Requirement (abbreviated) | Status | Evidence / gap |
| --- | --- | --- | --- |
| 7.1.1 | Verify that the application does not log credentials or payment details. Session tokens should only be stored in logs in an irreversible, hashed form. | Done | Passwords, session ids and CSRF tokens are never logged; request paths redact link tokens. [logging.py](../src/assetboard/logging.py), [test_logging.py](../tests/unit/test_logging.py), [test_security_hardening.py](../tests/web/test_security_hardening.py) |
| 7.1.2 | Verify that the application does not log other sensitive data as defined under local privacy laws or relevant security policy. | Done | Financial amounts are not logged at INFO; mail addresses use a keyed hash. [logging.py](../src/assetboard/logging.py), [test_logging.py](../tests/unit/test_logging.py) |
| 7.4.1 | Verify that a generic message is shown when an unexpected or security sensitive error occurs, potentially with a unique ID which support personnel… | Done | Generic 500 page with a request id; no exception text or traceback. [app.py](../src/assetboard/app.py), [test_security_hardening.py](../tests/web/test_security_hardening.py), [test_security_hardening.py](../tests/web/test_security_hardening.py) |

## V8 Data Protection

| # | Requirement (abbreviated) | Status | Evidence / gap |
| --- | --- | --- | --- |
| 8.2.1 | Verify the application sets sufficient anti-caching headers so that sensitive data is not cached in modern browsers. | Done | Cache-Control: no-store on every non-static response, plus Vary: Accept-Language. [middleware.py](../src/assetboard/middleware.py), [test_security_hardening.py](../tests/web/test_security_hardening.py) |
| 8.2.2 | Verify that data stored in browser storage (such as localStorage, sessionStorage, IndexedDB, or cookies) does not contain sensitive data. | Done | Only the session, CSRF and display-preference cookies are stored; no tokens or amounts in localStorage. [test_security_hardening.py](../tests/web/test_security_hardening.py) |
| 8.2.3 | Verify that authenticated data is cleared from client storage, such as the browser DOM, after the client or session is terminated. | Done | Logout and expiry delete the server row and clear the cookie; pages are no-store. [test_sessions_page.py](../tests/web/test_sessions_page.py) |
| 8.3.1 | Verify that sensitive data is sent to the server in the HTTP message body or headers, and that query string parameters from any HTTP verb do not… | Gap | Session tokens never appear in a URL, but invite and reset links carry bearer tokens in the path, and /login?next=/invite/<token> puts one in a query. Logs redact them and Referrer-Policy is same-origin; a browser history/URL leak remains. ADR-0013 documents the trade-off; follow-up: a short-lived exchange page. [account_email.py](../src/assetboard/services/account_email.py), [test_email_flows.py](../tests/web/test_email_flows.py) |
| 8.3.2 | Verify that users have a method to remove or export their data on demand. | Done | Workspace export/import and deletion are built in; administrators can deactivate accounts. [archive.py](../src/assetboard/domain/archive.py), [test_archive.py](../tests/unit/test_archive.py) |
| 8.3.3 | Verify that users are provided clear language regarding collection and use of supplied personal information and that users have provided opt-in… | N/A | Self-hosted software with no telemetry or third-party collection; the operator is the data controller and provides the notice. |
| 8.3.4 | Verify that all sensitive data created and processed by the application has been identified, and ensure that a policy is in place on how to deal… | Done | Sensitive data and trust boundaries are recorded in SECURITY.md and the ADRs (0012, 0013, 0017, 0021). [docs/](../docs) |

## V9 Communication

| # | Requirement (abbreviated) | Status | Evidence / gap |
| --- | --- | --- | --- |
| 9.1.1 | Verify that TLS is used for all client connectivity, and does not fall back to insecure or unencrypted communications. | Done | TLS terminates at the operator's reverse proxy; deployment requires BASE_URL https and the app never downgrades. [deployment.md](deployment.md#security-and-hardening) |
| 9.1.2 | Verify using up to date TLS testing tools that only strong cipher suites are enabled, with the strongest cipher suites set as preferred. | N/A | Cipher suites are chosen by the reverse proxy/load balancer, outside the app; the deployment guide covers it. |
| 9.1.3 | Verify that only the latest recommended versions of the TLS protocol are enabled, such as TLS 1.2 and TLS 1.3. The latest version of the TLS… | N/A | TLS versions are chosen by the reverse proxy; the deployment guide covers it. |

## V10 Malicious Code

| # | Requirement (abbreviated) | Status | Evidence / gap |
| --- | --- | --- | --- |
| 10.3.1 | Verify that if the application has a client or server auto-update feature, updates should be obtained over secure channels and digitally signed.… | N/A | No auto-update feature; the opt-in check only reads a release tag and never downloads or executes code. |
| 10.3.2 | Verify that the application employs integrity protections, such as code signing or subresource integrity. The application must not load or execute… | Done | All JavaScript, CSS and fonts are vendored and served locally; no CDN or remote script; CSP blocks other origins. [middleware.py](../src/assetboard/middleware.py), [test_security_hardening.py](../tests/web/test_security_hardening.py) |
| 10.3.3 | Verify that the application has protection from subdomain takeovers if the application relies upon DNS entries or DNS subdomains, such as expired… | N/A | The app does not manage DNS or subdomains. |

## V11 Business Logic

| # | Requirement (abbreviated) | Status | Evidence / gap |
| --- | --- | --- | --- |
| 11.1.1 | Verify that the application will only process business logic flows for the same user in sequential step order and without skipping steps. | Done | Multi-step flows (setup, registration, wizards) keep their state server-side and validate each step. [context.py](../src/assetboard/services/context.py), [test_authorization_matrix.py](../tests/web/test_authorization_matrix.py) |
| 11.1.2 | Verify that the application will only process business logic flows with all steps being processed in realistic human time, i.e. transactions are… | N/A | No flow depends on human timing; anti-automation is covered by the throttles. |
| 11.1.3 | Verify the application has appropriate limits for specific business actions or transactions which are correctly enforced on a per user basis. | Done | Per-account/per-address limits, import sizes and archive quotas. [auth.py](../src/assetboard/services/auth.py), [archive.py](../src/assetboard/domain/archive.py), [test_archive.py](../tests/unit/test_archive.py) |
| 11.1.4 | Verify that the application has anti-automation controls to protect against excessive calls such as mass data exfiltration, business logic… | Done | Request-body limits, hash gate, login/registration/mail throttles. [auth.py](../src/assetboard/services/auth.py) |
| 11.1.5 | Verify the application has business logic limits or validation to protect against likely business risks or threats, identified using threat… | Done | Closed months, role checks, archive validation and domain invariants. [archive.py](../src/assetboard/domain/archive.py), [test_archive.py](../tests/unit/test_archive.py) |

## V12 Files and Resources

| # | Requirement (abbreviated) | Status | Evidence / gap |
| --- | --- | --- | --- |
| 12.1.1 | Verify that the application will not accept large files that could fill up storage or cause a denial of service. | Done | MAX_REQUEST_BYTES / IMPORT_MAX_UPLOAD_BYTES and archive member limits. [archive.py](../src/assetboard/domain/archive.py), [test_archive.py](../tests/unit/test_archive.py) |
| 12.3.1 | Verify that user-submitted filename metadata is not used directly by system or framework filesystems and that a URL API is used to protect against… | Done | Uploaded archives are read in memory/validated; user filenames never become filesystem paths. [archive.py](../src/assetboard/domain/archive.py), [test_archive.py](../tests/unit/test_archive.py) |
| 12.3.2 | Verify that user-submitted filename metadata is validated or ignored to prevent the disclosure, creation, updating or removal of local files (LFI). | Done | No user-controlled path reaches open(); archive member names are validated. [archive.py](../src/assetboard/domain/archive.py), [test_archive.py](../tests/unit/test_archive.py) |
| 12.3.3 | Verify that user-submitted filename metadata is validated or ignored to prevent the disclosure or execution of remote files via Remote File… | Done | No filename is used to fetch a remote file; imports are local uploads. [archive.py](../src/assetboard/domain/archive.py), [test_archive.py](../tests/unit/test_archive.py) |
| 12.3.4 | Verify that the application protects against Reflective File Download (RFD) by validating or ignoring user-submitted filenames in a JSON, JSONP,… | Done | Exports set a fixed filename and attachment disposition; no reflection of a user filename. [archive.py](../src/assetboard/domain/archive.py), [test_archive.py](../tests/unit/test_archive.py) |
| 12.3.5 | Verify that untrusted file metadata is not used directly with system API or libraries, to protect against OS command injection. | Done | No shell or OS command receives a user filename. [Dockerfile](../Dockerfile), [compose.example.yaml](../compose.example.yaml), [smoke_container.sh](../scripts/smoke_container.sh) |
| 12.4.1 | Verify that files obtained from untrusted sources are stored outside the web root, with limited permissions. | Done | Uploads live under DATA_DIR, outside the static web root. [archive.py](../src/assetboard/domain/archive.py), [test_archive.py](../tests/unit/test_archive.py) |
| 12.4.2 | Verify that files obtained from untrusted sources are scanned by antivirus scanners to prevent upload and serving of known malicious content. | Gap | No antivirus scanning. The archive is structurally validated, but a malicious file inside a legitimate backup is not detected. Self-hosted, single-user risk; follow-up: optional ClamAV scan hook. [archive.py](../src/assetboard/domain/archive.py), [test_archive.py](../tests/unit/test_archive.py) |
| 12.5.1 | Verify that the web tier is configured to serve only files with specific file extensions to prevent unintentional information and source code… | Done | The app serves only files under its static mount; no directory listing or arbitrary extensions. [Dockerfile](../Dockerfile), [compose.example.yaml](../compose.example.yaml), [smoke_container.sh](../scripts/smoke_container.sh) |
| 12.5.2 | Verify that direct requests to uploaded files will never be executed as HTML/JavaScript content. | Done | Uploads are never served as HTML/JavaScript; downloads use attachment disposition. [archive.py](../src/assetboard/domain/archive.py), [test_archive.py](../tests/unit/test_archive.py) |
| 12.6.1 | Verify that the web or application server is configured with an allow list of resources or systems to which the server can send requests or load… | Gap | Outbound requests are limited by URL scheme for notifications and by provider definitions for prices, but not by host. Same follow-up as 5.2.6. [deployment.md](deployment.md#security-and-hardening) |

## V13 API and Web Service

| # | Requirement (abbreviated) | Status | Evidence / gap |
| --- | --- | --- | --- |
| 13.1.1 | Verify that all application components use the same encodings and parsers to avoid parsing attacks that exploit different URI or file parsing… | Done | One UTF-8 code path; no alternate parsers. [middleware.py](../src/assetboard/middleware.py), [test_security_hardening.py](../tests/web/test_security_hardening.py) |
| 13.1.3 | Verify API URLs do not expose sensitive information, such as the API key, session tokens etc. | N/A | No JSON/REST API. HTML email links use one-use tokens; see the 8.3.1 gap. |
| 13.2.1 | Verify that enabled RESTful HTTP methods are a valid choice for the user or action, such as preventing normal users using DELETE or PUT on… | N/A | No REST API; HTML routes use GET/POST only. |
| 13.2.2 | Verify that JSON schema validation is in place and verified before accepting input. | N/A | No JSON API input; health endpoints take no input. |
| 13.2.3 | Verify that RESTful web services that utilize cookies are protected from Cross-Site Request Forgery via the use of at least one or more of the… | Done | Same CSRF and Origin checks as the HTML forms. [csrf.py](../src/assetboard/services/csrf.py), [test_csrf_origin.py](../tests/web/test_csrf_origin.py) |
| 13.3.1 | Verify that XSD schema validation takes place to ensure a properly formed XML document, followed by validation of each input field before any… | N/A | No XML API. |

## V14 Configuration

| # | Requirement (abbreviated) | Status | Evidence / gap |
| --- | --- | --- | --- |
| 14.2.1 | Verify that all components are up to date, preferably using a dependency checker during build or compile time. | Done | uv.lock, pip-audit and Trivy run in CI; Dependabot-style upgrades are handled by release-please. [security.yml](../.github/workflows/security.yml), [docker.yml](../.github/workflows/docker.yml) |
| 14.2.2 | Verify that all unneeded features, documentation, sample applications and configurations are removed. | Done | Runtime image has no test dependencies, compilers or package manager entry point; docs are not served. [Dockerfile](../Dockerfile), [compose.example.yaml](../compose.example.yaml), [smoke_container.sh](../scripts/smoke_container.sh) |
| 14.2.3 | Verify that if application assets, such as JavaScript libraries, CSS or web fonts, are hosted externally on a Content Delivery Network (CDN) or… | N/A | No external CDN or remote asset. |
| 14.3.2 | Verify that web or application server and application framework debug modes are disabled in production to eliminate debug features, developer… | Done | No debug mode; generic 500 pages in every environment. [app.py](../src/assetboard/app.py), [test_security_hardening.py](../tests/web/test_security_hardening.py) |
| 14.3.3 | Verify that the HTTP headers or any part of the HTTP response do not expose detailed version information of system components. | Done | The Server header is disabled and framework versions are not exposed; the UI intentionally shows the app version for update notices. [middleware.py](../src/assetboard/middleware.py), [test_security_hardening.py](../tests/web/test_security_hardening.py) |
| 14.4.1 | Verify that every HTTP response contains a Content-Type header. Also specify a safe character set (e.g., UTF-8, ISO-8859-1) if the content types… | Done | FastAPI/Starlette set a Content-Type (with charset for text) on every response, including the 413 and plain-text fallback. [middleware.py](../src/assetboard/middleware.py), [test_security_hardening.py](../tests/web/test_security_hardening.py) |
| 14.4.2 | Verify that all API responses contain a Content-Disposition: attachment; filename="api.json" header (or other appropriate filename for the content… | N/A | No API responses; file downloads set attachment with a fixed filename. |
| 14.4.3 | Verify that a Content Security Policy (CSP) response header is in place that helps mitigate impact for XSS attacks like HTML, DOM, JSON, and… | Done | Strict same-origin CSP, no inline script, no eval. [middleware.py](../src/assetboard/middleware.py), [test_security_hardening.py](../tests/web/test_security_hardening.py) |
| 14.4.4 | Verify that all responses contain a X-Content-Type-Options: nosniff header. | Done | X-Content-Type-Options: nosniff on every response. [middleware.py](../src/assetboard/middleware.py), [test_security_hardening.py](../tests/web/test_security_hardening.py) |
| 14.4.5 | Verify that a Strict-Transport-Security header is included on all responses and for all subdomains, such as Strict-Transport-Security:… | Gap | HSTS is sent when BASE_URL is https, but includeSubDomains is omitted: a self-hosted instance may share a parent domain. Follow-up: document includeSubDomains for single-host deployments. [middleware.py](../src/assetboard/middleware.py), [test_security_hardening.py](../tests/web/test_security_hardening.py) |
| 14.4.6 | Verify that a suitable Referrer-Policy header is included to avoid exposing sensitive information in the URL through the Referer header to… | Done | strict-origin-when-cross-origin by default; same-origin on token pages; no-referrer on OIDC pages. [middleware.py](../src/assetboard/middleware.py), [test_security_hardening.py](../tests/web/test_security_hardening.py) |
| 14.4.7 | Verify that the content of a web application cannot be embedded in a third-party site by default and that embedding of the exact resources is only… | Done | frame-ancestors 'none' and X-Frame-Options: DENY. [middleware.py](../src/assetboard/middleware.py), [test_security_hardening.py](../tests/web/test_security_hardening.py) |
| 14.5.1 | Verify that the application server only accepts the HTTP methods in use by the application/API, including pre-flight OPTIONS, and logs/alerts on… | Done | Routes declare GET/POST; unknown methods get 405 and are logged by the request logger. [middleware.py](../src/assetboard/middleware.py), [test_security_hardening.py](../tests/web/test_security_hardening.py) |
| 14.5.2 | Verify that the supplied Origin header is not used for authentication or access control decisions, as the Origin header can easily be changed by… | Done | Origin is only a CSRF signal; authentication is the session cookie and server-side role. [csrf.py](../src/assetboard/services/csrf.py), [test_csrf_origin.py](../tests/web/test_csrf_origin.py) |
| 14.5.3 | Verify that the Cross-Origin Resource Sharing (CORS) Access-Control-Allow-Origin header uses a strict allow list of trusted domains and subdomains… | N/A | No CORS headers are sent; the app is same-origin. |

## Container and deployment controls

- The runtime image runs as UID/GID `10001:10001`, with a read-only root filesystem,
  `cap_drop: [ALL]`, `no-new-privileges:true`, only `/data` and `/tmp` writable, and the
  `Server` header disabled. [Dockerfile](../Dockerfile), [compose.example.yaml](../compose.example.yaml), [smoke_container.sh](../scripts/smoke_container.sh), [ADR-0009](adr/0009-container-security.md).
- `scripts/smoke_container.sh` checks the user, read-only root, `/tmp`, capabilities,
  no-new-privileges, absence of common shell/network tools and `pip`, data persistence and health.
- `compose.example.yaml` documents each control next to the setting; do not add
  `privileged`, host networking or the Docker socket.

## Dependency and license review

Runtime dependencies are permissively licensed (MIT, BSD, Apache-2.0, ISC, MPL-2.0 and
PSF) or otherwise compatible with AGPL-3.0-or-later. The generated
[THIRD_PARTY_LICENSES](../THIRD_PARTY_LICENSES) lists every locked runtime distribution,
its license metadata and the license/notice files from its wheel. Vendored JavaScript is
covered by [vendor/README.md](../src/assetboard/static/vendor/README.md) with versions and
SHA-256 checksums. Regenerate with `make licenses`; CI fails when the file is stale.
