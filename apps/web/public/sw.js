/* Zagent service worker — app-shell cache only. API + WS traffic is ALWAYS
   network (a stale run state is worse than no run state). */
const SHELL = "zagent-shell-v2";
const ASSETS = ["/", "/index.html", "/manifest.webmanifest", "/icon.svg"];

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
  const isApi =
    url.pathname.startsWith("/ws") ||
    ["/auth", "/runs", "/lanes", "/sessions", "/repos", "/modes",
     "/hydration", "/approvals", "/team", "/health"].some((p) => url.pathname.startsWith(p));
  if (isApi || e.request.method !== "GET") return; // network, untouched
  e.respondWith(
    caches.match(e.request).then((hit) => hit || fetch(e.request))
  );
});

/* Push (Phase 4): the payload carries a deep link to the specific action
   card — a tap lands on the approval, not the inbox. */
self.addEventListener("push", (e) => {
  let data = { title: "zagent", body: "", url: "/" };
  try {
    if (e.data) data = { ...data, ...e.data.json() };
  } catch { /* keep defaults */ }
  e.waitUntil(
    self.registration.showNotification(data.title, {
      body: data.body,
      icon: "/icon.svg",
      data: { url: data.url },
      tag: "zagent-ask",
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
