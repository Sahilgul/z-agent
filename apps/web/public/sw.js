/* Collegium service worker — app-shell cache for offline boot only. API + WS
   traffic is ALWAYS network (a stale run state is worse than no run state). */
const SHELL = "collegium-shell-v3";
const ASSETS = ["/manifest.webmanifest", "/icon.svg"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(SHELL).then((c) => c.addAll(ASSETS)));
  self.skipWaiting();
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== SHELL).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  // W10-#7: keep in sync with API_PREFIXES in vite.config.ts — /lanes never
  // existed (threads are /threads); the missing prefixes here meant the SW
  // cache-first served STALE JSON for knowledge/ideas/proposals/push/etc.
  const isApi =
    url.pathname.startsWith("/ws") ||
    ["/auth", "/runs", "/threads", "/sessions", "/repos", "/modes",
     "/hydration", "/approvals", "/team", "/knowledge", "/ideas", "/proposals",
     "/push", "/me", "/campaigns", "/deliveries", "/stats", "/bench",
     "/webhooks", "/models", "/health"].some((p) => url.pathname.startsWith(p));
  if (isApi || e.request.method !== "GET") return; // network, untouched
  // index.html / navigations are NETWORK-FIRST: they were cache-first, so a
  // redeploy left the browser running the previous bundle (with yesterday's
  // bugs) until the SW happened to update. Cache is now only an offline
  // fallback — new deploys reach users on the next load, no hard refresh.
  if (e.request.mode === "navigate" || url.pathname === "/" || url.pathname === "/index.html") {
    e.respondWith(
      fetch(e.request)
        .then((res) => {
          const copy = res.clone();
          caches.open(SHELL).then((c) => c.put(e.request, copy));
          return res;
        })
        .catch(() => caches.match(e.request).then((hit) => hit || caches.match("/index.html")))
    );
    return;
  }
  // Hashed build assets (/assets/index-<hash>.js) are immutable — cache-first
  // is safe and keeps repeat loads instant. W10-#3: a cache MISS must still
  // populate the cache — the old code fetched but never `put`, so an
  // offline cold boot had no bundle to serve and the PWA was a white screen.
  e.respondWith(
    caches.match(e.request).then(
      (hit) =>
        hit ||
        fetch(e.request).then((res) => {
          if (res.ok && url.pathname.startsWith("/assets/")) {
            const copy = res.clone();
            caches.open(SHELL).then((c) => c.put(e.request, copy));
          }
          return res;
        })
    )
  );
});

/* Push: the payload carries a deep link to the specific action
   card — a tap lands on the approval, not the inbox. */
self.addEventListener("push", (e) => {
  let data = { title: "Collegium", body: "", url: "/" };
  try {
    if (e.data) data = { ...data, ...e.data.json() };
  } catch { /* keep defaults */ }
  e.waitUntil(
    self.registration.showNotification(data.title, {
      body: data.body,
      icon: "/icon.svg",
      data: { url: data.url },
      // W10-#4: tag per deep-link — the constant "collegium-ask" collapsed
      // EVERY concurrent approval card into one notification, so tapping
      // only ever opened the latest. Distinct cards stay distinct.
      tag: data.url && data.url !== "/" ? `collegium:${data.url}` : "collegium-ask",
      renotify: true,
    })
  );
});

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || "/";
  e.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((clients) => {
      const existing = clients.find((c) => "focus" in c);
      if (existing) {
        existing.focus();
        existing.navigate(url);
        return;
      }
      return self.clients.openWindow(url);
    })
  );
});
