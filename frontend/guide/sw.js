/* Service worker du guide voyageur (M-08, §3.2 : consultable hors-ligne après
   la première visite — crucial avant la configuration du wifi).

   Portée « / » (accordée par l'entête Service-Worker-Allowed du backend) pour
   intercepter à la fois la page `/g/<token>` et l'app shell `/guide/…`. Le
   handler ne traite QUE les URL du guide : tout le reste (back-office…) passe
   au réseau sans modification.

   Stratégies :
     · app shell (/guide/*)      → pré-cache à l'installation, cache-first
     · CDN Leaflet / polices     → cache-first (mis en cache à la 1re rencontre)
     · page /g/<token> + /data   → network-first, repli sur le cache (hors-ligne)
     · /g/<token>/secrets        → network-first (wifi dispo hors-ligne ensuite)
     · /g/<token>/media/<id>     → cache-first (vignettes déjà vues)
     · tuiles OSM                → réseau seul (cache des tuiles = M-10, exclu)
*/

const VERSION = "casaguide-guide-v2"; // ⚠ incrémenter à CHAQUE modification de frontend/guide/*
const SHELL = `${VERSION}-shell`;
const RUNTIME = `${VERSION}-runtime`;

const SHELL_ASSETS = [
  "/guide/app.js",
  "/guide/qr.js",
  "/guide/guide.css",
  "/guide/icon-192.png",
  "/guide/icon-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(SHELL)
      .then((cache) => cache.addAll(SHELL_ASSETS.map((u) => new Request(u, { cache: "reload" }))))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((k) => k !== SHELL && k !== RUNTIME).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);

  // Tuiles cartographiques : réseau seul (hors-ligne = M-10, hors périmètre).
  if (/tile\.openstreetmap\.org$/.test(url.hostname)) return;

  // CDN de l'app shell (Leaflet, polices) : cache-first.
  const isShellCdn = /(^|\.)unpkg\.com$/.test(url.hostname) ||
    /(^|\.)(fonts\.googleapis\.com|fonts\.gstatic\.com)$/.test(url.hostname);
  if (isShellCdn) { event.respondWith(cacheFirst(req, RUNTIME)); return; }

  if (url.origin !== self.location.origin) return; // autre origine : on ne touche pas

  // App shell local : cache-first.
  if (url.pathname.startsWith("/guide/")) { event.respondWith(cacheFirst(req, SHELL)); return; }

  // Espace du guide voyageur.
  if (url.pathname.startsWith("/g/")) {
    if (/\/media\/[^/]+$/.test(url.pathname)) {
      event.respondWith(cacheFirst(req, RUNTIME)); // vignettes déjà vues
    } else {
      event.respondWith(networkFirst(req, RUNTIME)); // page, /data, /secrets
    }
    return;
  }
  // Tout le reste (back-office, API privée…) : comportement navigateur par défaut.
});

async function cacheFirst(req, cacheName) {
  const cache = await caches.open(cacheName);
  const hit = await cache.match(req);
  if (hit) return hit;
  try {
    const resp = await fetch(req);
    if (resp && (resp.ok || resp.type === "opaque")) cache.put(req, resp.clone());
    return resp;
  } catch (err) {
    return hit || Response.error();
  }
}

async function networkFirst(req, cacheName) {
  const cache = await caches.open(cacheName);
  try {
    const resp = await fetch(req);
    if (resp && resp.ok) cache.put(req, resp.clone());
    return resp;
  } catch (err) {
    const hit = await cache.match(req);
    if (hit) return hit;
    throw err;
  }
}
