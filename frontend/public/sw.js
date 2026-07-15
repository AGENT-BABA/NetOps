/* NetOps Service Worker · v3
 * Enables install as PWA + offline fallback support.
 * Handles FCM push notifications + forwarded in-app notifications.
 */
const CACHE = "netops-v3";
const APP_SHELL = ["/", "/index.html", "/manifest.json", "/icon-192.png", "/icon-512.png", "/offline.html"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(APP_SHELL).catch(() => null)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
  );
  self.clients.claim();
});

// Simple network-first for API, cache-first for shell
self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  // API requests: network-first, fallback to cache
  if (url.pathname.startsWith("/api/")) {
    event.respondWith(fetch(req).catch(() => caches.match(req)));
    return;
  }
  // Navigation requests: network-first, fallback to offline page
  if (req.mode === "navigate") {
    event.respondWith(
      fetch(req)
        .then((r) => {
          if (r && r.ok) {
            const copy = r.clone();
            caches.open(CACHE).then((c) => c.put(req, copy));
          }
          return r;
        })
        .catch(() =>
          caches.match("/offline.html").then((r) => r || new Response("Offline", { status: 503, headers: { "Content-Type": "text/plain" } }))
        )
    );
    return;
  }
  // Other requests: cache-first, fallback to network
  event.respondWith(
    caches.match(req).then((cached) =>
      cached ||
      fetch(req)
        .then((r) => {
          if (r && r.ok && url.origin === location.origin) {
            const copy = r.clone();
            caches.open(CACHE).then((c) => c.put(req, copy));
          }
          return r;
        })
        .catch(() => cached)
    )
  );
});

// FCM push notifications — fired when app is closed/backgrounded
self.addEventListener("push", (event) => {
  if (!event.data) return;
  let payload;
  try {
    payload = event.data.json();
  } catch {
    payload = { notification: { title: "NetOps", body: event.data.text() } };
  }

  const title = payload.notification?.title || payload.data?.title || "NetOps";
  const body = payload.notification?.body || payload.data?.body || "";
  const tag = payload.notification?.tag || payload.data?.tag || "netops-push";

  event.waitUntil(
    self.registration.showNotification(title, {
      body,
      icon: "/icon-192.png",
      badge: "/icon-192.png",
      tag,
      data: payload.data || {},
      renotify: true,
    })
  );
});

// Receive messages from the page: { type: "notify", title, body, tag, data }
self.addEventListener("message", (event) => {
  const msg = event.data || {};
  if (msg.type === "notify") {
    self.registration.showNotification(msg.title || "NetOps", {
      body: msg.body || "",
      icon: "/icon-192.png",
      badge: "/icon-192.png",
      tag: msg.tag || undefined,
      data: msg.data || {},
      renotify: true,
    });
  }
});

// Click a notification → focus the app window (or open it)
self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((wins) => {
      for (const w of wins) {
        if ("focus" in w) return w.focus();
      }
      if (self.clients.openWindow) return self.clients.openWindow("/");
    })
  );
});
