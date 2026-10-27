# Your account

The profile page (`/account`, "Profile" in the account menu) holds your details, your
language and theme, and the ways you sign in. Password changes and signing out other devices
are on "Account security".

## Signing in

- A wrong email or password shows "The email or password is not correct." The page never says
  whether an account exists.
- By default, 5 sign-in attempts for an email per hour are free, successful ones included (and
  20 failed attempts from one network address); the 6th (or the 21st) starts a pause, and
  attempts during it say "Too many attempts. Try again in N minutes." The pause starts at 1
  minute and doubles with each further attempt, up to 15 minutes. The count covers every
  sign-in attempt for that email in the hour since the first one, and signing in successfully
  does not clear it: it expires after the hour. If you sign in more than 5 times within an
  hour (for example on several devices), expect a short pause. Your administrator can change
  these limits.
- Wrong passwords when changing your password or confirming a change are counted separately
  (5 free, then the same pause); they never block signing in, and a correct password clears
  them.

## Creating an account and forgotten passwords

These need email, which your administrator sets up ([Email](deployment.md#email)).

- **Create an account** (on the sign-in page, when an instance administrator turned open
  registration on): enter your email address. The page always says "Check your inbox", whether
  or not the address can be registered. The email holds a link, valid for 24 hours and usable
  once, where you choose your display name, password and workspace name; the account and its
  workspace are created only then, and you are signed in. If the address already has an
  account, the email says so instead and links to sign-in and to "Forgot your password?".
  Nothing arrived? Check your spam folder, or ask again a little later.
- **Forgot your password?** (on the sign-in page): enter your email address. The page always
  gives the same answer. If your account has a password, the email holds a link, valid for
  60 minutes and usable once, to choose a new one; doing so signs out every device signed in to
  your account, and you then sign in with the new password. It does not remove two-factor
  authentication: you still need your authenticator or a recovery code. If your account signs in through
  an identity provider or a proxy, the email says there is no password to reset.
- At most 3 such emails are sent to one address per hour; further requests get the same answer
  but no email. Changing your password, or an administrator deactivating the account, makes
  every reset link sent earlier stop working.

## Two-factor authentication

Choose **Two-factor authentication** in your profile. Confirm your password, scan the QR code
with an authenticator app (or enter its manual key), then enter a six-digit code within ten
minutes. Nothing is enabled until that code succeeds. Enrollment ends other password sessions.
Save the ten recovery codes shown on completion: this is the only time they are displayed.
Each code can replace the authenticator code for one sign-in. Store them separately from your
password. The enrollment code has already been used; wait for the next code before signing in
on another device.

After enrollment, enter your email and password on the first sign-in page. If two-factor
authentication is enabled, the next page asks for a code from your authenticator app or a
recovery code. A wrong or expired code shows the same generic failure as an incorrect password;
the ticket expires after five minutes and you can retry while it is valid. Attempts use the same
sign-in throttle as incorrect passwords. The emergency local administrator login uses the same
two steps.
When linking a new identity provider or proxy identity by email during sign-in, enter the code
alongside the existing account's password too. Already-linked external sign-ins rely on their
provider's MFA instead. Accounts without passwords configure MFA at their identity provider.

Workspace owners can select **Require two-factor authentication for password-based access to
this workspace** in workspace settings. It applies to every member, including owners, and to
existing password sessions immediately. To avoid locking yourself out, an owner signed in with a
password must first enable two-factor authentication and sign in with it before turning this on. A member without verified enrollment is sent to the
security page before accessing workspace data. They can still manage their account, enroll,
sign out, or switch to an unrelated workspace. Other workspaces and OIDC/proxy sessions are
unaffected.

Disabling on the same security page requires your password and a current authenticator code or
an unused recovery code; it removes the authenticator and all recovery codes, and signs out other password sessions. A workspace requiring MFA stays protected
and will ask you to enroll again. To replace a lost authenticator, sign in with a recovery code,
disable, then enroll the replacement. You can also generate a replacement set of recovery codes
on the security page; confirm your password and enter your current authenticator code or an
unused recovery code. This immediately invalidates every previous recovery code, and the new
codes are shown only once. Recovery codes are shown in groups such as `1a2b-3c4d-…`; you can
type them with or without the dashes.

If you lose your authenticator and every recovery code, contact an instance administrator. In
**Users**, the administrator can reset your two-factor authentication after confirming their own
account. This removes your authenticator and recovery codes and ends all your sessions. You can
then sign in with your password and enroll again; a workspace that requires two-factor
authentication remains inaccessible until you do. Administrators cannot use this action on
their own account; use the account security page instead. An operator with shell access can
also run `assetboard reset-mfa <email>` (see [administrator commands](deployment.md#administrator-commands)).
A CLI password reset does not remove your second factor.

## Details, language and theme

- **Display name**: 1 to 120 characters, no control or invisible characters.
- **Language**: "Browser language" (the default) follows your browser's language setting;
  English or Polish pins that language on every device.
- **Theme**: the saved theme applies on every device. The dark-mode button in the menu changes
  the theme on one device only; "Use the saved theme on this device" undoes that.

## Sign-in methods

The profile lists the identity providers (OIDC) and the proxy sign-in linked to your account.

- **Link your identity provider**: choose "Link <provider>", confirm, and sign in at the
  provider. You come back to the profile; nothing else about your session changes. The profile
  does not link a second account of a provider that is already linked: remove the old link
  first. (Linking by email at sign-in can still add one; the list shows every linked account
  and each can be removed.)
- **Link the proxy sign-in**: shown when you browse through a reverse proxy that signs you in
  (see [docs/auth/proxy-auth.md](auth/proxy-auth.md)) and that proxy user is not linked yet.
- **Remove a method**: allowed only if another way to sign in remains: a password the current
  settings allow, your identity provider, or the proxy. Removing an identity provider also
  signs out the other devices that signed in with it.

## Confirming changes

Linking, removing a sign-in method and signing out other devices ask for confirmation:

- **With a password**, you enter it. Wrong passwords are counted (separately from sign-in);
  after too many, the page says how many minutes to wait.
- **Without a password** (your account was created through an identity provider or the proxy),
  a sign-in from the last 10 minutes confirms it. After that, the page says "sign in again":
  - with an identity provider: sign out, then "Continue with <provider>" on the sign-in page;
  - through a proxy: sign out, then open any page; the proxy signs you in again.

  You then have 10 minutes for the change. For linking an identity provider, the 10 minutes
  are for starting the link; finishing the sign-in at the provider may take up to 10 more
  minutes. This is weaker than a password: anyone holding your signed-in browser during those
  minutes could make the change.

## Managing accounts (instance administrators)

"Users" in the account menu lists every account with its sign-in methods (password, identity
provider, proxy), its status and its last sign-in. Each change has its own confirmation page
and is confirmed like a profile change: your password, or a sign-in from the last 10 minutes
if your account has none.

- **Deactivate**: the person can no longer sign in, and every session of the account ends at
  once (password, identity provider and proxy sessions alike). **Activate** undoes it.
  Deactivating the only owner of a workspace leaves that workspace without an active owner
  until the account is activated again.
- **Make administrator / Remove administrator**: instance administrators manage every account.
  With `ASSETBOARD_OIDC_ADMIN_GROUP` set, the provider's group decides again at each OIDC
  sign-in of that person ([docs/auth/oidc.md](auth/oidc.md#administrators-from-a-group)), so a
  change made here lasts only until then; the Users page says so and marks those accounts
  "(from the identity provider)". Deactivation does not depend on the group: a deactivated
  account stays deactivated whatever the provider says.
- You cannot deactivate yourself or remove your own administrator role, and the last active
  administrator always stays.
- **Create an account** (not under `ASSETBOARD_OIDC_ONLY`): enter an email and a display name.
  A temporary password is generated and shown **once**, on the page that confirms the account;
  it is not stored in readable form, logged or sent anywhere. Give it to the person. At the
  first sign-in they can only change it (or sign out) until they choose a new password, which
  must differ from the temporary one. An unused temporary password stays valid until it is
  used or the account is deactivated. The new account has no workspace: it sees "Your account
  is not a member of any workspace" until a workspace owner invites it, or until it creates
  its own (see below).

## Creating a workspace

Any signed-in account can create a workspace and becomes its owner, unless an instance
administrator has turned this off. "Create workspace" appears on the "No workspace" page, and
"New workspace" in the account menu (bottom left) and on the workspace settings page. The form asks
for the same details as first-run setup: a name (your display name unless you change it),
base currency, country, time zone and language. The new workspace starts with the suggested
categories in your language ([categories](categories.md#suggested-categories)), and you switch
to it straight away. Its log records "Workspace created".

**Instance settings** (instance administrators, "Instance settings" in the account menu or on
the Users page): "Who can create workspaces" is "Every signed-in user" (the default) or "Only
instance administrators". Instance administrators can always create workspaces. The setting
does not affect open registration or accounts created at the
first sign-in through an identity provider or a proxy (`ASSETBOARD_OIDC_AUTO_CREATE`,
`ASSETBOARD_PROXY_AUTH_AUTO_CREATE`): those keep creating their own workspace. A change is
confirmed like the other administrator actions and appears in the instance audit log
("Instance settings changed").

"Open registration" (Instance settings, off by default) lets anyone create an account with an
email address they confirm through a link (see above); each such account gets its own
workspace. It can be turned on only when email is set up and password sign-in is not turned off
(`ASSETBOARD_OIDC_ONLY`); the page says why when it cannot. Accounts from invitations, identity
providers, the proxy or an administrator do not depend on it.

## Audit log

The audit log lists who did what and when (in UTC, to the second), newest first, in pages of
50. Each log has its own entry in the account menu; the address `/audit` on its own opens the
workspace log for the owner of the current workspace, otherwise the instance log for an
instance administrator.

- **Workspace** ("Audit log: workspace", for the owner of the current workspace): that
  workspace's events: setup, member role changes and removals, invitations, settings. Each row
  shows who acted and which member it is about, by display name only ("Former member" if that
  account no longer exists).
- **Instance** ("Audit log: instance", for instance administrators): sign-ins and sign-outs,
  failed and refused sign-ins, linked and removed sign-in methods, password changes and account
  administration. Each row also shows the account it is about, the acting account's email and
  the network (an IPv4 /24 or IPv6 /48 prefix, never the full address). It never shows any
  workspace's events: those are only in that workspace's log.

Filters: the kind of event and a date range in UTC (both days included). In the instance log,
"History" next to an account on the Users page shows only the events about that account.
Details are shown only for known events and known fields: reasons, methods, roles and counts.
Names are shown as they are now, not as they were when the event happened. Identity-provider
subjects, tokens and the email addresses a provider reported are never recorded, so they never
appear.
