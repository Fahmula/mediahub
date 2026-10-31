// MediaHub service worker, served at /sw.js by web/routes/pwa.py, which fills in VERSION,
// PRECACHE and ICON. Pages and partials are never cached: they show live, signed-in data. Only
// versioned static assets (?v=<hash>, so immutable) and the offline page are kept. It also shows
// push notifications; their text is never stored here either.
"use strict";

const VERSION = "__VERSION__";
const PRECACHE = __PRECACHE__;
const CACHE = `mediahub-${VERSION}`;
const OFFLINE_URL = "/offline";
const ICON = "__ICON__";
const FALLBACK_URL = "/attention";
const PUSH_API = "/settings/notifications";

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(CACHE)
      .then((cache) => cache.addAll(PRECACHE))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      const keys = await caches.keys();
      await Promise.all(
        keys.filter((key) => key.startsWith("mediahub-") && key !== CACHE).map((key) => caches.delete(key)),
      );
      if (self.registration.navigationPreload) await self.registration.navigationPreload.enable();
      await self.clients.claim();
    })(),
  );
});

async function navigate(event) {
  try {
    return (await event.preloadResponse) || (await fetch(event.request));
  } catch (_) {
    return (await caches.match(OFFLINE_URL)) || Response.error();
  }
}

async function staticAsset(request) {
  const cache = await caches.open(CACHE);
  const hit = await cache.match(request);
  if (hit) return hit;
  const response = await fetch(request);
  if (response.ok) cache.put(request, response.clone());
  return response;
}

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  if (request.mode === "navigate") {
    event.respondWith(navigate(event));
  } else if (url.pathname.startsWith("/static/") && url.searchParams.has("v")) {
    event.respondWith(staticAsset(request));
  }
  // Everything else (htmx partials, images, health) goes straight to the network.
});

self.addEventListener("push", (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (_) {
    /* unreadable: still show something, below */
  }
  // Always show a notification: Safari revokes permission from a worker that doesn't.
  event.waitUntil(
    self.registration.showNotification(data.title || "MediaHub", {
      body: data.title ? data.body : "Something needs attention.",
      tag: data.tag,
      renotify: Boolean(data.tag && data.renotify),
      icon: ICON,
      data: { url: data.url },
    }),
  );
});

// Only ever opens this app, whatever the message said.
function notificationTarget(notification) {
  const url = new URL(notification.data?.url || FALLBACK_URL, self.location.origin);
  return url.origin === self.location.origin ? url.href : new URL(FALLBACK_URL, self.location.origin).href;
}

async function openTarget(url) {
  const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
  for (const client of windows) {
    try {
      await client.focus();
      await client.navigate(url);
      return;
    } catch (_) {
      /* not ours to navigate: open a window instead */
    }
  }
  await self.clients.openWindow(url);
}

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil(openTarget(notificationTarget(event.notification)));
});

function keyBytes(key) {
  const padded = `${key}${"=".repeat((4 - (key.length % 4)) % 4)}`.replace(/-/g, "+").replace(/_/g, "/");
  return Uint8Array.from(atob(padded), (c) => c.charCodeAt(0));
}

async function renewSubscription(event) {
  const boot = await fetch(`${PUSH_API}/bootstrap`, { headers: { Accept: "application/json" } });
  if (!boot.ok || boot.redirected) return;
  const { public_key: publicKey, csrf_token: csrfToken } = await boot.json();
  const subscription = await self.registration.pushManager.subscribe({
    userVisibleOnly: true,
    applicationServerKey: keyBytes(publicKey),
  });
  await fetch(`${PUSH_API}/subscribe`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
    body: JSON.stringify({ ...subscription.toJSON(), old_endpoint: event.oldSubscription?.endpoint }),
  });
}

// Not every browser fires this (iOS doesn't); app.js repairs a lost subscription on page load.
self.addEventListener("pushsubscriptionchange", (event) => {
  event.waitUntil(renewSubscription(event).catch(() => {}));
});
