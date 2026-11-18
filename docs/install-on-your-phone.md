# Install AssetBoard on your phone

AssetBoard is an installable web app (a PWA). Adding it to your phone's home screen gives it its
own icon and opens it without the browser's address bar, like a native app. There is no separate
mobile app to download, and the app keeps using the same address, account and data.

The app still needs a network connection: the offline page explains that you are offline, but it
never shows cached financial data. Reminders keep going through the notification channels set up
in AssetBoard; web push is not used.

## Before you start

- **Use HTTPS.** Installing and the service worker only work on a secure origin. On a phone this
  means a valid certificate that the phone trusts, served by your reverse proxy or tunnel — an
  `http://` address on your LAN is not enough. (Browsers treat `localhost` as secure, but a phone
  is not localhost.) The same HTTPS requirement applies to OIDC sign-in.
- Use the exact public address configured in `ASSETBOARD_BASE_URL`. Serve AssetBoard at the
  origin root: the manifest, service worker and offline page honour an ASGI root path, but the
  sign-in, sign-out and OIDC redirects currently assume the app is mounted at `/`. If you sign
  in through Authelia, a tunnel or another identity provider, use that same address so the
  redirect after sign-in returns to the installed app.
- Do not use a private/incognito window: install prompts are hidden there.
- iOS 16.4 or later is required for the standalone experience. Android Chrome (or another
  Chromium-based browser) works on current Android versions.

## Android (Chrome)

1. Open your AssetBoard address in Chrome and sign in.
2. Open the browser menu (⋮) and choose **Add to Home screen** / **Install app**. If Chrome
   offers an install banner, you can use that instead.
3. Confirm. The AssetBoard icon appears on your home screen and in the app drawer.
4. Open it from the icon. It starts at Home, in standalone mode, without the address bar.

If the menu has no install entry, check the HTTPS address, reload once, and make sure you are not
in incognito mode.

## iOS and iPadOS (Safari, 16.4+)

1. Open your AssetBoard address in Safari and sign in.
2. Tap the **Share** button.
3. Choose **Add to Home Screen**, then **Add**.
4. Open AssetBoard from the new icon. It opens in standalone mode.

On iOS, use Safari for the installation. Other browsers may offer their own "Add to Home
Screen", but only Safari's produces the standalone app experience. With AssetBoard at the origin
root, sign-in, sign-out and OIDC redirects return to the installed app; if an identity provider
insists on opening a browser tab, complete the sign-in there and return to the AssetBoard icon.

## Signing in, signing out and updates

- Sign-in, sign-out and OIDC/Authelia redirects stay on the app's address, so they return to the
  installed app. Signing out returns to the sign-in page; sign in again to continue.
- The app checks for a new release when it loads. After an upgrade, reload (or reopen the app):
  the new version replaces the old static cache, and the version in the sidebar and the update
  notice keep working as before.
- To remove the app, delete its icon like any other home-screen app. Your data stays on the
  server; removing the icon does not delete anything.

## What is cached

The service worker caches only versioned static files (CSS, JavaScript, icons) and the one
translated offline page. It never caches HTML pages, HTMX fragments, exports or workspace data.
When the phone is offline, opening the app shows **You are offline** with a retry link and no
financial data. The offline page and the manifest's language follow the browser's
`Accept-Language` when the app is installed or updated, not a profile language chosen later.

## Troubleshooting

- **No install option:** confirm the page is HTTPS, not incognito, and reload. On iOS, use Safari.
- **The icon opens a browser tab:** the app's scope may not match the address you installed from.
  Reinstall from the exact `ASSETBOARD_BASE_URL` at the origin root.
- **Stale styles or scripts after an upgrade:** close and reopen the app, or reload the page. A
  release changes the service worker and its cache automatically.
- **Sign-in does not return to the app:** use the same host name in `ASSETBOARD_BASE_URL` and in
  the identity provider's redirect URI, over HTTPS. See [OIDC sign-in](auth/oidc.md) and the
  [deployment guide](deployment.md#reverse-proxies).
