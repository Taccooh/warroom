// Minimal service worker — makes the app installable. Network-first, app shell
// only as an offline fallback. Deliberately lean; data comes live from the server.
// On asset changes bump the SHELL version → the old cache is deleted on activate.
const SHELL = "warroom-v5";
const ASSETS = ["/", "/static/style.css", "/static/fonts/germania-one.woff2",
  "/static/vendor/leaflet/leaflet.css", "/static/vendor/leaflet/leaflet.js"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(SHELL).then((c) => c.addAll(ASSETS)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((ks) =>
    Promise.all(ks.filter((k) => k !== SHELL).map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.pathname.startsWith("/api/")) return; // never cache live data
  // Network first; only when offline → cache (ignore the ?v= query when matching).
  e.respondWith(fetch(e.request).catch(() => caches.match(e.request, {ignoreSearch: true})));
});

// The raven brings tidings: bundled watcher report from the poller.
self.addEventListener("push", (e) => {
  let d = {};
  try { d = e.data.json(); } catch (err) {}
  e.waitUntil(self.registration.showNotification(d.title || "Warroom", {
    body: d.body || "",
    tag: d.tag || "warroom",
    renotify: true,
    icon: "/static/icon-raider.png",
    badge: "/static/icon-raider.png",
    data: { url: d.url || "/?tab=waechter" },
  }));
});
self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || "/";
  e.waitUntil(clients.matchAll({ type: "window", includeUncontrolled: true }).then((cs) => {
    for (const c of cs) {
      if ("focus" in c) { c.navigate(url); return c.focus(); }
    }
    return clients.openWindow(url);
  }));
});
