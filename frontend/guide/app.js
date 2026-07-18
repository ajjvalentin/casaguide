/* Guide voyageur — enrichissement progressif de la page rendue côté serveur
   (M-08). Le contenu (sections, POI, area_facts, contacts) est déjà dans le
   HTML ; ce module ajoute l'interactif : carte Leaflet, filtres par chapitre
   synchronisés liste/carte, visionneuse photo, secrets (wifi/boîte à clés) +
   QR de connexion, et l'enregistrement du service worker (hors-ligne / PWA).

   Aucune dépendance lourde : Leaflet est fourni par la page, le QR est généré
   localement (qr.js). Tout échoue proprement — sans JS, la page reste lisible. */

import { qrCanvas, wifiPayload } from "./qr.js";

const token = document.body.dataset.token || location.pathname.split("/")[2] || "";
let GUIDE = { property: {}, pois: [] };
try { GUIDE = JSON.parse(document.getElementById("guide-data")?.textContent || "{}"); }
catch (_) { /* données de carte absentes : la page reste utilisable */ }

/* Distance « voyageur » : à pied si ≤ 30 min, sinon en voiture (§M-01). */
function fmtDist(p) {
  if (p.walk_min != null && p.walk_min <= 30) return `${p.walk_min} min à pied`;
  if (p.drive_min != null) return `${p.drive_min} min en voiture`;
  return "";
}
function tel(raw) { return (String(raw).trim().startsWith("+") ? "+" : "") + String(raw).replace(/\D/g, ""); }

// ── Carte Leaflet ────────────────────────────────────────────────────────────
const markersByChapter = {};
function initMap() {
  const mapEl = document.getElementById("map");
  const P = GUIDE.property || {};
  if (!mapEl || !window.L || P.lat == null || P.lon == null) return;

  const map = L.map(mapEl, { scrollWheelZoom: false }).setView([P.lat, P.lon], 14);
  // Tuile transparente en cas d'échec (hors-ligne, hors zone pré-chargée) : la
  // carte reste sobre au lieu d'afficher des vignettes cassées (M-10).
  const TRANSPARENT_TILE = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==";
  const tiles = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png",
    { attribution: "© OpenStreetMap", maxZoom: 19, errorTileUrl: TRANSPARENT_TILE });
  // Hors-ligne : si des tuiles manquent (au-delà de la zone pré-chargée), afficher
  // un message discret. Se retire au retour en ligne.
  tiles.on("tileerror", () => { if (!navigator.onLine) showMapOffline(mapEl); });
  tiles.addTo(map);
  L.marker([P.lat, P.lon], {
    icon: L.divIcon({ className: "", html: '<div class="home-pin">🏠</div>', iconAnchor: [13, 13] }),
    keyboard: false,
  }).addTo(map).bindPopup(`<b>${escapeHtml(P.name || "Votre logement")}</b>`);

  const bounds = [[P.lat, P.lon]];
  for (const p of GUIDE.pois || []) {
    const m = L.circleMarker([p.lat, p.lon], {
      radius: 7, weight: 2, color: "#fff", fillColor: p.color || "#0E5A73", fillOpacity: 0.95,
    });
    const dist = fmtDist(p);
    let html = `<b>${escapeHtml(p.name)}</b><br>${escapeHtml(p.category || "")}`;
    if (dist) html += ` · ${dist}`;
    if (p.phone) html += `<br>📞 <a href="tel:${tel(p.phone)}">${escapeHtml(p.phone)}</a>`;
    html += `<br><a href="https://www.google.com/maps/dir/?api=1&destination=${p.lat},${p.lon}" target="_blank" rel="noopener">Itinéraire ↗</a>`;
    m.bindPopup(html);
    m.addTo(map);
    (markersByChapter[p.chapter] ||= []).push(m);
    bounds.push([p.lat, p.lon]);
  }
  if (bounds.length > 1) map.fitBounds(bounds, { padding: [30, 30], maxZoom: 15 });
  // La carte est créée avant la mise en page finale : recalage.
  setTimeout(() => map.invalidateSize(), 80);
  window._guideMap = map;
}

// Message discret « hors ligne / hors zone » sur la carte (M-10).
function showMapOffline(mapEl) {
  if (mapEl.querySelector(".map-offline")) return;
  const note = el("div", { class: "map-offline" },
    "Hors ligne — carte limitée à la zone du logement");
  mapEl.appendChild(note);
  window.addEventListener("online", () => note.remove(), { once: true });
}

// ── Onglets « Le logement / Urgences / Autour de vous » (V2-09) ───────────────
// Une seule page : navigation sans rechargement, état dans l'URL (#logement /
// #urgences / #autour) pour que liens profonds + retour arrière fonctionnent.
// Les ancres de section (#<code>) mènent au bon onglet et défilent jusqu'à elle.
const TAB_HASH = { home: "logement", emergency: "urgences", around: "autour" };
const HASH_TAB = { logement: "home", urgences: "emergency", autour: "around" };

function initTabs() {
  const tabs = [...document.querySelectorAll(".guide-tabs .tab[data-tab]")];
  const panels = [...document.querySelectorAll(".tab-panel[data-tab]")];
  if (!tabs.length || !panels.length) return;

  function activate(tabKey, { push = true } = {}) {
    if (!TAB_HASH[tabKey]) tabKey = "home";
    tabs.forEach((t) => {
      const on = t.dataset.tab === tabKey;
      t.classList.toggle("on", on);
      t.setAttribute("aria-selected", on ? "true" : "false");
    });
    panels.forEach((p) => p.classList.toggle("tab-active", p.dataset.tab === tabKey));
    if (push) {
      const h = "#" + TAB_HASH[tabKey];
      if (location.hash !== h) history.pushState(null, "", h);
    }
    // La carte est créée dans l'onglet « Autour » (masqué au départ) : recalage.
    if (tabKey === "around" && window._guideMap) {
      setTimeout(() => window._guideMap.invalidateSize(), 30);
    }
    updateLangHash();
  }

  // Résout le hash courant : onglet fixe (#logement…) OU ancre de section
  // (#B_wifi → l'onglet qui contient cette section, puis défilement).
  function applyHash({ push = false } = {}) {
    const raw = decodeURIComponent(location.hash.replace(/^#/, ""));
    let tabKey = HASH_TAB[raw];
    let scrollEl = null;
    if (!tabKey && raw) {
      const elt = document.getElementById(raw);
      const panel = elt && elt.closest(".tab-panel[data-tab]");
      if (panel) { tabKey = panel.dataset.tab; scrollEl = elt; }
    }
    activate(tabKey || "home", { push });
    if (scrollEl) setTimeout(() => scrollEl.scrollIntoView({ behavior: "smooth", block: "start" }), 80);
  }

  tabs.forEach((t) => t.addEventListener("click", () => activate(t.dataset.tab)));
  window.addEventListener("hashchange", () => applyHash({ push: false }));
  window.addEventListener("popstate", () => applyHash({ push: false }));
  applyHash({ push: false });   // état initial (deep link / retour arrière)
  window._activateTab = (k) => activate(k);
}

// Le sélecteur de langue recharge la page (?lang=xx) : on lui joint le hash
// courant pour conserver l'onglet actif après changement de langue (V2-09).
function updateLangHash() {
  document.querySelectorAll(".langs a[data-lang]").forEach((a) => {
    a.setAttribute("href", "?lang=" + encodeURIComponent(a.dataset.lang) + location.hash);
  });
}

// ── Filtres par chapitre (chips ↔ sections ↔ marqueurs) ──────────────────────
// Scopés à l'onglet « Autour de vous » : ne masque jamais les chapitres des
// autres onglets (qui ne portent pas de puce).
function initChips() {
  const around = document.querySelector('.tab-panel[data-tab="around"]');
  if (!around) return;
  const chips = [...around.querySelectorAll(".chip")];
  if (!chips.length) return;
  chips.forEach((chip) => chip.addEventListener("click", () => {
    chips.forEach((c) => c.classList.remove("on"));
    chip.classList.add("on");
    const ch = chip.dataset.chapter || "";
    around.querySelectorAll(".chapter[data-chapter]").forEach((sec) => {
      sec.style.display = (!ch || sec.dataset.chapter === ch) ? "" : "none";
    });
    const map = window._guideMap;
    if (map) {
      Object.entries(markersByChapter).forEach(([c, list]) => {
        list.forEach((m) => (!ch || c === ch) ? m.addTo(map) : map.removeLayer(m));
      });
    }
  }));
}

// ── Filtre par cuisine (restaurants, M-16) ───────────────────────────────────
// Les puces sont rendues côté serveur depuis les cuisines réellement présentes ;
// ici on ne fait que masquer/afficher les cartes concernées, sans rechargement.
function initCuisineFilter() {
  document.querySelectorAll(".cuisines[data-cat]").forEach((bar) => {
    const group = bar.nextElementSibling && bar.nextElementSibling.classList.contains("poi-group")
      ? bar.nextElementSibling
      : bar.parentElement.querySelector('.poi-group[data-cat="restaurant"]');
    if (!group) return;
    const chips = [...bar.querySelectorAll(".cchip")];
    chips.forEach((chip) => chip.addEventListener("click", () => {
      chips.forEach((c) => c.classList.remove("on"));
      chip.classList.add("on");
      const cui = chip.dataset.cuisine || "";
      group.querySelectorAll(".poi-card").forEach((card) => {
        card.style.display = (!cui || card.dataset.cuisine === cui) ? "" : "none";
      });
    }));
  });
}

// ── Adresse & GPS copiables (M-19) ───────────────────────────────────────────
// Boutons rendus côté serveur (data-copy = texte à copier, data-copied = libellé
// de confirmation localisé). Presse-papiers si dispo, sinon repli : on sélectionne
// le texte pour que le voyageur puisse le copier à la main.
function initCopy() {
  document.querySelectorAll(".copy-btn[data-copy]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const text = btn.dataset.copy || "";
      const done = btn.dataset.copied || "✓";
      const original = btn.textContent;
      let ok = false;
      try {
        await navigator.clipboard.writeText(text);
        ok = true;
      } catch (_) {
        ok = selectValue(btn);   // repli : sélection du texte affiché
      }
      if (ok) {
        btn.textContent = done;
        btn.classList.add("done");
        setTimeout(() => { btn.textContent = original; btn.classList.remove("done"); }, 1600);
      }
    });
  });
}

function selectValue(btn) {
  const row = btn.closest(".copy-row");
  const val = row && row.querySelector("[data-copy-value]");
  if (!val) return false;
  try {
    const range = document.createRange();
    range.selectNodeContents(val);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
    return true;
  } catch (_) { return false; }
}

// ── Visionneuse plein écran ──────────────────────────────────────────────────
function initLightbox() {
  const figures = [...document.querySelectorAll(".gphoto")];
  if (!figures.length) return;
  let box = null;
  const open = (src, caption) => {
    close();
    const img = el("img", { src, alt: caption || "Photo" });
    const cap = caption ? el("div", { class: "lb-cap" }, caption) : null;
    const btn = el("button", { class: "lb-close", "aria-label": "Fermer" }, "×");
    box = el("div", { class: "lightbox", role: "dialog", "aria-modal": "true" }, btn, img, cap);
    box.addEventListener("click", (e) => { if (e.target === box || e.target === btn) close(); });
    document.body.appendChild(box);
    document.addEventListener("keydown", onKey);
  };
  const close = () => { if (box) { box.remove(); box = null; document.removeEventListener("keydown", onKey); } };
  const onKey = (e) => { if (e.key === "Escape") close(); };
  figures.forEach((fig) => {
    const act = () => open(fig.dataset.full, fig.dataset.caption || "");
    fig.addEventListener("click", act);
    fig.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); act(); } });
  });
}

// ── Secrets : wifi + boîte à clés (mode 'link'), chargés à la demande ────────
async function initSecrets() {
  const slots = [...document.querySelectorAll(".secret-slot")];
  if (!slots.length) return;
  let sec;
  try {
    const resp = await fetch(`/g/${encodeURIComponent(token)}/secrets`);
    if (!resp.ok) return;
    sec = await resp.json();
  } catch (_) { return; } // hors-ligne au premier chargement : blocs simplement masqués

  for (const slot of slots) {
    if (slot.dataset.secret === "wifi") fillWifi(slot, sec);
    else if (slot.dataset.secret === "keybox") fillKeybox(slot, sec);
  }
}

function fillWifi(slot, sec) {
  // Multi-wifi (M-15) : une carte par réseau (nom d'usage, SSID, mot de passe, QR).
  // Repli si un ancien guide ne renvoie encore que les champs simples.
  let networks = Array.isArray(sec.wifi_networks) ? sec.wifi_networks : [];
  if (!networks.length && (sec.wifi_ssid || sec.wifi_pass)) {
    networks = [{ label: "Wifi", ssid: sec.wifi_ssid, pass: sec.wifi_pass }];
  }
  if (!networks.length) return;

  const cards = networks.map((n) => wifiCard(n, networks.length > 1));
  slot.replaceChildren(...cards.filter(Boolean));
  slot.hidden = false;
}

function wifiCard(net, showLabel) {
  const ssid = net.ssid || "";
  const pass = net.pass || "";
  if (!ssid && !pass) return null;
  const title = showLabel && net.label ? `📶 ${net.label}` : "📶 Connexion Wifi";
  const card = el("div", { class: "secret-card" }, el("div", { class: "sc-title" }, title));
  if (ssid) card.appendChild(secretRow("Réseau", ssid));
  if (pass) card.appendChild(secretRow("Mot de passe", pass, { mono: true }));

  // QR de connexion automatique (norme WIFI:…), généré via le module mutualisé
  if (ssid && pass) {
    const canvas = qrCanvas(wifiPayload(ssid, pass),
      { label: `QR de connexion Wifi ${net.label || ""}`.trim() });
    if (canvas) {
      card.appendChild(el("div", { class: "qr-wrap" }, canvas,
        el("div", { class: "qr-cap" }, "Scannez pour vous connecter automatiquement au Wifi.")));
    }
  }
  return card;
}

function fillKeybox(slot, sec) {
  if (!sec.keybox_code && !sec.keybox_notes) return;
  const card = el("div", { class: "secret-card" }, el("div", { class: "sc-title" }, "🔑 Boîte à clés"));
  if (sec.keybox_code) card.appendChild(secretRow("Code", sec.keybox_code, { mono: true }));
  if (sec.keybox_notes) card.appendChild(el("p", { class: "sc-notes" }, sec.keybox_notes));
  slot.replaceChildren(card);
  slot.hidden = false;
}

function secretRow(label, value, { mono } = {}) {
  const btn = el("button", { class: "copy-btn", type: "button" }, "Copier");
  btn.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(value);
      btn.textContent = "Copié ✓"; btn.classList.add("done");
      setTimeout(() => { btn.textContent = "Copier"; btn.classList.remove("done"); }, 1600);
    } catch (_) { btn.textContent = "—"; }
  });
  return el("div", { class: "sc-row" },
    el("span", { class: "k" }, label),
    el("span", { class: "v", style: mono ? "font-family:ui-monospace,monospace" : "" }, value),
    btn);
}

// ── Langue (M-09) : mémorisation du choix + détection initiale ────────────────
// Le sélecteur est rendu côté serveur (liens ?lang=xx qui rechargent la page
// dans la bonne langue). Ici : on mémorise le dernier choix (localStorage) et,
// au tout premier chargement sans ?lang explicite, on redirige vers la langue
// préférée (choix mémorisé, sinon navigator.language) si elle est disponible.
const LANG_KEY = "casaguide:lang";
function initLang() {
  const current = document.body.dataset.lang || "fr";
  const opts = Array.from(document.querySelectorAll(".langs a[data-lang]"));
  opts.forEach((a) => a.addEventListener("click", () => {
    try { localStorage.setItem(LANG_KEY, a.dataset.lang); } catch (_) { /* privé */ }
  }));

  const hasExplicit = new URLSearchParams(location.search).has("lang");
  if (hasExplicit) {                       // l'URL fixe la langue → on la mémorise
    try { localStorage.setItem(LANG_KEY, current); } catch (_) { /* privé */ }
    return;
  }
  const available = new Set(opts.map((a) => a.dataset.lang));
  if (!available.size) return;

  let pref = null;
  try { pref = localStorage.getItem(LANG_KEY); } catch (_) { /* privé */ }
  if (!pref) {
    const nav = (navigator.language || "").slice(0, 2).toLowerCase();
    if (available.has(nav)) pref = nav;
  }
  if (pref && available.has(pref) && pref !== current) {
    location.replace("?lang=" + encodeURIComponent(pref));  // recharge en SSR
  }
}

// ── Service worker (hors-ligne + PWA) ────────────────────────────────────────
function initPwa() {
  if (!("serviceWorker" in navigator)) return;
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/guide/sw.js", { scope: "/" })
      .then(() => navigator.serviceWorker.ready)
      .then((reg) => {
        // Hors-ligne complet (M-10) : à la 1re visite EN LIGNE, demander au SW
        // de pré-charger les tuiles de la zone du logement (en tâche de fond).
        const P = GUIDE.property || {};
        if (navigator.onLine && reg.active && P.lat != null && P.lon != null) {
          reg.active.postMessage({ type: "prefetch-tiles", lat: P.lat, lon: P.lon });
        }
      })
      .catch(() => { /* non bloquant */ });
  });
}

// ── Utilitaires DOM ──────────────────────────────────────────────────────────
function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (v == null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "style") node.setAttribute("style", v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children) if (c != null) node.append(c.nodeType ? c : document.createTextNode(c));
  return node;
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ── Démarrage ────────────────────────────────────────────────────────────────
initLang();
initMap();
initTabs();
initChips();
initCuisineFilter();
initCopy();
initLightbox();
initSecrets();
initPwa();
