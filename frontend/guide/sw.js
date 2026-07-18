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
     · tuiles OSM                → cache-first + pré-chargement de la zone (M-10)

   Hors-ligne complet (M-10) : à la première visite EN LIGNE, l'app envoie la
   position du logement (message 'prefetch-tiles') ; le SW pré-charge alors, en
   tâche de fond, les tuiles OSM de la zone (zooms 13→16, grille bornée autour du
   point) de façon SÉQUENTIELLE et espacée (politesse envers tile.openstreetmap.org,
   pas de rafale). Le cache de tuiles est plafonné (éviction FIFO). Résultat :
   carte consultable hors-ligne sur la zone du logement ; au-delà, message discret
   côté app.
*/

// ⚠ Incrémenter la partie « vN » à CHAQUE modification de frontend/guide/* (dev).
// `__ASSET_VERSION__` est remplacé à la volée par le SHA git du déploiement
// (route /guide/sw.js, M-11) : chaque déploiement change le nom des caches → le
// SW se réactive et purge les anciens, sans bump manuel en production.
const VERSION = "casaguide-guide-v12-__ASSET_VERSION__";
const SHELL = `${VERSION}-shell`;
const RUNTIME = `${VERSION}-runtime`;
const TILES = `${VERSION}-tiles`;

// Pré-chargement des tuiles de la zone (M-10). Rayon = demi-côté de la grille
// (2r+1)² par zoom → total = 25+25+49+49 = 148 tuiles (~3–4 Mo), sous le budget.
const TILE_HOST = /(^|\.)tile\.openstreetmap\.org$/;
const PREFETCH_PLAN = { 13: 2, 14: 2, 15: 3, 16: 3 };
const MAX_TILES = 220;      // plafond du cache de tuiles (prefetch + navigation)
const PREFETCH_DELAY_MS = 60;  // pause entre tuiles : séquentiel, jamais en rafale

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
  const keep = new Set([SHELL, RUNTIME, TILES]);
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((k) => !keep.has(k)).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

// Pré-chargement des tuiles de la zone (M-10) : déclenché par l'app une fois en
// ligne, avec la position du logement. Best-effort, séquentiel, poli.
self.addEventListener("message", (event) => {
  const d = event.data || {};
  if (d.type === "prefetch-tiles" && typeof d.lat === "number" && typeof d.lon === "number") {
    event.waitUntil(prefetchTiles(d.lat, d.lon));
  }
});

function lon2tile(lon, z) { return Math.floor(((lon + 180) / 360) * 2 ** z); }
function lat2tile(lat, z) {
  const r = (lat * Math.PI) / 180;
  return Math.floor(((1 - Math.log(Math.tan(r) + 1 / Math.cos(r)) / Math.PI) / 2) * 2 ** z);
}
function sleep(ms) { return new Promise((res) => setTimeout(res, ms)); }

let prefetching = false;
async function prefetchTiles(lat, lon) {
  if (prefetching) return;      // une seule campagne à la fois
  prefetching = true;
  try {
    const cache = await caches.open(TILES);
    const urls = [];
    for (const [z, rad] of Object.entries(PREFETCH_PLAN)) {
      const zoom = +z, n = 2 ** zoom;
      const cx = lon2tile(lon, zoom), cy = lat2tile(lat, zoom);
      for (let dx = -rad; dx <= rad; dx++) {
        for (let dy = -rad; dy <= rad; dy++) {
          const x = cx + dx, y = cy + dy;
          if (x < 0 || y < 0 || x >= n || y >= n) continue;
          urls.push(`https://tile.openstreetmap.org/${zoom}/${x}/${y}.png`);
        }
      }
    }
    for (const u of urls) {
      const req = new Request(u, { mode: "cors" });
      if (await cache.match(req)) continue;        // déjà pré-chargée
      try {
        const resp = await fetch(req);
        if (resp && resp.ok) await cache.put(req, resp.clone());
      } catch (_) { /* réseau : on saute cette tuile (best-effort) */ }
      await sleep(PREFETCH_DELAY_MS);              // politesse : pas de rafale
    }
    await trimCache(TILES, MAX_TILES);
  } finally {
    prefetching = false;
  }
}

async function trimCache(cacheName, max) {
  const cache = await caches.open(cacheName);
  const keys = await cache.keys();
  if (keys.length <= max) return;
  // Éviction FIFO (les plus anciennes entrées d'abord).
  for (const k of keys.slice(0, keys.length - max)) await cache.delete(k);
}

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);

  // Tuiles cartographiques : cache-first (M-10). Les tuiles de la zone sont
  // pré-chargées ; celles rencontrées à la navigation sont mises en cache (borné).
  if (TILE_HOST.test(url.hostname)) { event.respondWith(tileCacheFirst(req)); return; }

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

// Tuiles OSM (M-10) : servies depuis le cache si présentes (zone pré-chargée),
// sinon réseau puis mise en cache bornée. Hors-ligne + non caché → Response.error
// (l'app affiche une tuile transparente + un message discret « hors zone »).
async function tileCacheFirst(req) {
  const cache = await caches.open(TILES);
  const hit = await cache.match(req);
  if (hit) return hit;
  try {
    const resp = await fetch(req);
    if (resp && (resp.ok || resp.type === "opaque")) {
      await cache.put(req, resp.clone());
      trimCache(TILES, MAX_TILES);   // éviction souple (non bloquante)
    }
    return resp;
  } catch (err) {
    return Response.error();
  }
}

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
