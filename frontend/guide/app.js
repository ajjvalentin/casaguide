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
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png",
    { attribution: "© OpenStreetMap", maxZoom: 19 }).addTo(map);
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

// ── Filtres par chapitre (chips ↔ sections ↔ marqueurs) ──────────────────────
function initChips() {
  const chips = [...document.querySelectorAll(".chip")];
  if (!chips.length) return;
  chips.forEach((chip) => chip.addEventListener("click", () => {
    chips.forEach((c) => c.classList.remove("on"));
    chip.classList.add("on");
    const ch = chip.dataset.chapter || "";
    document.querySelectorAll(".chapter[data-chapter]").forEach((sec) => {
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
  if (!sec.wifi_pass && !sec.wifi_ssid) return;
  const card = el("div", { class: "secret-card" }, el("div", { class: "sc-title" }, "📶 Connexion Wifi"));
  if (sec.wifi_ssid) card.appendChild(secretRow("Réseau", sec.wifi_ssid));
  if (sec.wifi_pass) card.appendChild(secretRow("Mot de passe", sec.wifi_pass, { mono: true }));

  // QR de connexion automatique (norme WIFI:…), généré via le module mutualisé
  if (sec.wifi_ssid && sec.wifi_pass) {
    const canvas = qrCanvas(wifiPayload(sec.wifi_ssid, sec.wifi_pass),
      { label: "QR code de connexion Wifi" });
    if (canvas) {
      card.appendChild(el("div", { class: "qr-wrap" }, canvas,
        el("div", { class: "qr-cap" }, "Scannez pour vous connecter automatiquement au Wifi.")));
    }
  }
  slot.replaceChildren(card);
  slot.hidden = false;
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
    navigator.serviceWorker.register("/guide/sw.js", { scope: "/" }).catch(() => { /* non bloquant */ });
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
initChips();
initCuisineFilter();
initCopy();
initLightbox();
initSecrets();
initPwa();
