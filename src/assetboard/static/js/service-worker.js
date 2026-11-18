// AssetBoard's minimal service worker (F9.10).
//
// Privacy rules: only versioned static assets under /static/ and the one offline page are ever
// cached. HTML pages, HTMX fragments, exports and every other workspace response are network
// only. A release changes the ?v= query of this script, which installs a fresh cache and drops
// the previous one.
//
// This file is served from the app root (see pwa_routes.py), so self.registration.scope is the
// app's root path and every precached URL below resolves inside it.

"use strict";

const CACHE_PREFIX = "assetboard-pwa-";
const rawVersion = new URL(self.location.href).searchParams.get("v") || "dev";
const VERSION = /^[A-Za-z0-9._+-]{1,64}$/.test(rawVersion) ? rawVersion : "dev";
const CACHE_NAME = `${CACHE_PREFIX}${VERSION}`;
const SCOPE = self.registration.scope;
const OFFLINE_URL = new URL("offline", SCOPE).href;
const STATIC_PATH = new URL("static/", SCOPE).pathname;

const PRECACHE_PATHS = [
  "static/css/app.css",
  "static/js/theme.js",
  "static/js/app.js",
  "static/img/favicon.svg",
  "static/img/icon-192.png",
  "static/img/icon-512.png",
  "static/img/icon-192-maskable.png",
  "static/img/icon-512-maskable.png",
  "static/img/apple-touch-icon.png",
];

function versionedAsset(path) {
  const url = new URL(path, SCOPE);
  url.searchParams.set("v", VERSION);
  return url.href;
}

const PRECACHE_URLS = [OFFLINE_URL, ...PRECACHE_PATHS.map(versionedAsset)];

function isVersionedStatic(url) {
  return (
    url.origin === self.location.origin &&
    url.pathname.startsWith(STATIC_PATH) &&
    url.searchParams.get("v") === VERSION
  );
}

self.addEventListener("install", (event) => {
  event.waitUntil(
    (async () => {
      const cache = await caches.open(CACHE_NAME);
      // One failed asset must not stop the worker: the rest of the app still works, and the
      // next online visit caches what it loads. The offline page is retried on the next install
      // after an upgrade.
      await Promise.all(
        PRECACHE_URLS.map(async (url) => {
          try {
            const response = await fetch(url, { cache: "reload" });
            if (response.ok) await cache.put(url, response);
          } catch {
            // Offline during installation: keep what was cached and finish.
          }
        }),
      );
      await self.skipWaiting();
    })(),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      const keys = await caches.keys();
      await Promise.all(
        keys
          .filter((key) => key.startsWith(CACHE_PREFIX) && key !== CACHE_NAME)
          .map((key) => caches.delete(key)),
      );
      await self.clients.claim();
    })(),
  );
});

async function cachedStatic(request) {
  const cache = await caches.open(CACHE_NAME);
  const cached = await cache.match(request);
  if (cached) return cached;
  const response = await fetch(request);
  if (response.ok && response.type === "basic") {
    await cache.put(request, response.clone());
  }
  return response;
}

async function networkOrOffline(request) {
  try {
    return await fetch(request);
  } catch {
    const cache = await caches.open(CACHE_NAME);
    // The offline page is stored with the server's Vary: Accept-Language header; matching a
    // navigation request (which may carry a different header) must ignore Vary.
    const offline = await cache.match(OFFLINE_URL, { ignoreVary: true });
    return offline || Response.error();
  }
}

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  if (isVersionedStatic(url) && request.destination !== "document") {
    event.respondWith(cachedStatic(request));
    return;
  }
  if (request.mode === "navigate") {
    event.respondWith(networkOrOffline(request));
  }
  // Anything else (HTMX fragments, exports, APIs, workspace data) is never intercepted or
  // cached: the browser goes to the network directly.
});
