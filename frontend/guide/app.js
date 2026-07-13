/* Guide voyageur — enrichissement progressif de la page rendue côté serveur
   (M-08). Le contenu (sections, POI, area_facts, contacts) est déjà dans le
   HTML ; ce module ajoute l'interactif : carte Leaflet, filtres par chapitre
   synchronisés liste/carte, visionneuse photo, secrets (wifi/boîte à clés) +
   QR de connexion, et l'enregistrement du service worker (hors-ligne / PWA).

   Aucune dépendance lourde : Leaflet est fourni par la page, le QR est généré
   localement (qr.js). Tout échoue proprement — sans JS, la page reste lisible. */

import { qrMatrix } from "./qr.js";

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

  // QR de connexion automatique (norme WIFI:…)
  if (sec.wifi_ssid && sec.wifi_pass) {
    const payload = `WIFI:T:WPA;S:${wifiEscape(sec.wifi_ssid)};P:${wifiEscape(sec.wifi_pass)};;`;
    const canvas = renderQr(payload);
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

/* Échappement des caractères spéciaux pour la charge utile WIFI: (\ ; , : "). */
function wifiEscape(s) { return String(s).replace(/([\\;,:"])/g, "\\$1"); }

function renderQr(text) {
  const matrix = qrMatrix(text);
  if (!matrix) return null;
  const n = matrix.length, quiet = 4, scale = 4, dim = (n + quiet * 2) * scale;
  const canvas = el("canvas", { width: dim, height: dim, "aria-label": "QR code de connexion Wifi" });
  const ctx = canvas.getContext("2d");
  ctx.fillStyle = "#fff"; ctx.fillRect(0, 0, dim, dim);
  ctx.fillStyle = "#1E2A32";
  for (let y = 0; y < n; y++) for (let x = 0; x < n; x++) {
    if (matrix[y][x]) ctx.fillRect((x + quiet) * scale, (y + quiet) * scale, scale, scale);
  }
  return canvas;
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
initMap();
initChips();
initLightbox();
initSecrets();
initPwa();
