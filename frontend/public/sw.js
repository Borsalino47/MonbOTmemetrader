/* Crypto Pulse service worker.
 *
 * THE ONE RULE THAT MATTERS
 *
 * `/api/` is NEVER cached. Not stale-while-revalidate, not network-first with a
 * cached fallback — never. Every number this app shows is a price, a score or a
 * data age, and serving yesterday's from a cache would break the guarantee the
 * whole project is built on: a value on screen is either current or labelled as
 * old. A cached API response would be neither.
 *
 * Only the shell is cached — the HTML, the bundle, the icons. Those are static
 * build artifacts, so serving them from disk is the same bytes, not an
 * approximation. That is what makes the app open instantly on a phone while the
 * first scan is still in flight; the warm-start path on the server then fills it
 * with real rows.
 *
 * When the network is genuinely gone, an API request fails as it always would.
 * The UI already handles that and says so. It does not get a comforting lie.
 */

const VERSION = 'cryptopulse-shell-v1';
const SHELL = [
  '/',
  '/manifest.webmanifest',
  '/icons/icon-192.png',
  '/icons/icon-512.png',
  '/icons/apple-touch-icon.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(VERSION)
      // Individually, so one missing file cannot fail the whole install.
      .then((cache) => Promise.allSettled(SHELL.map((url) => cache.add(url))))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== VERSION).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener('fetch', (event) => {
  const { request } = event;
  if (request.method !== 'GET') return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  // Live data: straight to the network, always. No cache, no fallback.
  if (url.pathname.startsWith('/api/')) return;

  // Navigations: network first so a rebuilt app is picked up, shell as the
  // offline fallback so the window opens instead of showing a browser error.
  if (request.mode === 'navigate') {
    event.respondWith(
      fetch(request)
        .then((response) => {
          const copy = response.clone();
          caches.open(VERSION).then((cache) => cache.put('/', copy));
          return response;
        })
        .catch(() => caches.match('/').then((hit) => hit ?? Response.error())),
    );
    return;
  }

  // Build artifacts are content-hashed, so a hit is byte-identical by construction.
  event.respondWith(
    caches.match(request).then((hit) => {
      if (hit) return hit;
      return fetch(request).then((response) => {
        if (response.ok && response.type === 'basic') {
          const copy = response.clone();
          caches.open(VERSION).then((cache) => cache.put(request, copy));
        }
        return response;
      });
    }),
  );
});
