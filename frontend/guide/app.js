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

/* Distance « voyageur » (V2-24) : le mode préféré de la catégorie (travel_mode)
   prime sur l'auto — 'driving' toujours voiture, 'walking' à pied tant que
   raisonnable, sinon à pied si ≤ 30 min puis voiture (§M-01). */
const WALK_MODE_MAX = 45;
function fmtDist(p) {
  const mode = p.travel_mode;
  if (mode === "driving" && p.drive_min != null) return `${p.drive_min} min en voiture`;
  if (mode === "walking" && p.walk_min != null && (p.drive_min == null || p.walk_min <= WALK_MODE_MAX))
    return `${p.walk_min} min à pied`;
  if (p.walk_min != null && p.walk_min <= 30) return `${p.walk_min} min à pied`;
  if (p.drive_min != null) return `${p.drive_min} min en voiture`;
  if (p.walk_min != null) return `${p.walk_min} min à pied`;
  return "";
}
function tel(raw) { return (String(raw).trim().startsWith("+") ? "+" : "") + String(raw).replace(/\D/g, ""); }

// ── Carte Leaflet ────────────────────────────────────────────────────────────
// UNE SEULE instance de carte (window._guideMap), la plus robuste avec Leaflet :
// la carte vit toujours à la même place DOM (en tête de l'onglet « Autour », sous
// les onglets, avant la liste). Le mode filtré (V2-12e) ne déplace ni ne recrée
// rien — il masque simplement la grille au-dessus (CSS) et recadre les marqueurs.
// Marqueurs POI conservés à plat (`_allMarkers`, tagués `_catCode`/`_chapter`) pour
// filtrer par catégorie (mode filtré) ou par chapitre (puces) sans les recréer.
let allMarkers = [];      // marqueurs POI (jamais le logement)
let allBounds = null;     // cadrage d'origine (logement + tous les POI)
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
  allMarkers = [];
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
    m._catCode = p.category_code || "";
    m._chapter = p.chapter || "";
    m.addTo(map);
    allMarkers.push(m);
    bounds.push([p.lat, p.lon]);
  }
  allBounds = bounds;
  if (bounds.length > 1) map.fitBounds(bounds, { padding: [30, 30], maxZoom: 15 });
  // La carte est créée avant la mise en page finale : recalage.
  setTimeout(() => map.invalidateSize(), 80);
  window._guideMap = map;
}

// Chapitre actif de la puce de filtre (vide = « Tout ») — source de vérité du
// filtrage par chapitre, partagée entre les puces et la sortie du mode filtré.
function activeChapter() {
  const chip = document.querySelector('.tab-panel[data-tab="around"] .chip.on');
  return chip ? (chip.dataset.chapter || "") : "";
}

// Un SEUL point de décision de la visibilité des marqueurs (jamais de recréation,
// donc jamais de doublon même après N aller-retours). Priorité au mode filtré
// (catégorie choisie) ; sinon repli sur le filtre chapitre des puces.
function syncMarkers() {
  const map = window._guideMap;
  if (!map) return;
  const cat = window._filterCat || "";
  const ch = cat ? "" : activeChapter();
  for (const m of allMarkers) {
    const show = cat ? (m._catCode === cat) : (!ch || m._chapter === ch);
    if (show) m.addTo(map); else map.removeLayer(m);   // addTo est idempotent
  }
}

// Recadrage sur le sous-ensemble d'une catégorie (mode filtré) : logement + POI
// de la catégorie. Un seul POI → zoom plafonné (sinon fitBounds sur 2 points très
// proches zoomerait à l'excès). Reste dans la zone pré-cachée (invariant M-10 :
// aucun nouveau téléchargement de tuile).
function fitFiltered(code) {
  const map = window._guideMap;
  const P = GUIDE.property || {};
  if (!map || P.lat == null) return;
  const pts = [[P.lat, P.lon]];
  for (const m of allMarkers) if (m._catCode === code) { const ll = m.getLatLng(); pts.push([ll.lat, ll.lng]); }
  map.fitBounds(pts, { padding: [40, 40], maxZoom: pts.length <= 2 ? 15 : 16 });
}

// Retour au cadrage d'origine (grille) : logement + tous les POI.
function fitAll() {
  const map = window._guideMap;
  if (map && allBounds && allBounds.length > 1) map.fitBounds(allBounds, { padding: [30, 30], maxZoom: 15 });
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

// ── Mode filtré « une catégorie = un écran » (V2-12b) ────────────────────────
// Un tap sur une tuile de la grille (#autour/{code}) n'est plus un simple
// défilement : on entre dans un mode où seuls le bloc de la catégorie choisie
// et le titre de son chapitre restent affichés — la grille, la carte, les puces
// et tous les autres blocs sont masqués. Le retour à la grille (#autour) sort du
// mode. Piloté PAR DES CLASSES (CSS gated sur `html.js`) : sans JS, la classe
// n'est jamais posée et tout reste visible (ancres natives, dégradation propre).
function aroundPanel() { return document.querySelector('.tab-panel[data-tab="around"]'); }

function setServiceFilter(code) {
  const around = aroundPanel();
  if (!around) return;
  code = code || "";
  const on = !!code;
  const changed = (window._filterCat || "") !== code;
  window._filterCat = code;
  around.classList.toggle("cat-filtered", on);
  // La barre d'urgences d'en-tête vit HORS du panneau (dans <header>) : une classe
  // au niveau du <body> pilote son retrait en mode filtré (V2-12e).
  document.body.classList.toggle("cat-filtered", on);
  // Blocs de catégorie : seul le choisi reste visible en mode filtré.
  around.querySelectorAll(".cat").forEach((cat) => {
    cat.classList.toggle("cat-off", on && cat.dataset.cat !== code);
  });
  // Chapitres : on ne garde (avec son titre) que celui qui contient la catégorie
  // choisie ; les autres sont masqués (le titre du chapitre hôte donne le contexte).
  around.querySelectorAll(".chapter[data-chapter]").forEach((ch) => {
    const holds = [...ch.querySelectorAll(".cat")].some((c) => c.dataset.cat === code);
    ch.classList.toggle("chapter-off", on && !holds);
  });
  // Carte (V2-12e) : filtrer les marqueurs sur la catégorie choisie et recadrer.
  // On n'agit QUE sur une vraie transition (setServiceFilter("") est appelé à
  // chaque navigation, y compris hors « Autour » : ne pas re-cadrer inutilement).
  if (!changed) return;
  syncMarkers();
  const map = window._guideMap;
  if (!map) return;
  // La carte peut être masquée juste avant (changement d'onglet) ou remonter (grille
  // masquée) : invalider la taille avant de recadrer sur le nouvel ensemble.
  setTimeout(() => {
    map.invalidateSize();
    if (on) fitFiltered(code); else fitAll();
  }, 60);
}

function initTabs() {
  const tabs = [...document.querySelectorAll(".guide-tabs .tab[data-tab]")];
  const panels = [...document.querySelectorAll(".tab-panel[data-tab]")];
  if (!tabs.length || !panels.length) return;

  function activate(tabKey, { push = true } = {}) {
    if (!TAB_HASH[tabKey]) tabKey = "home";
    // Quitter « Autour » (tap d'onglet → pushState, pas de hashchange) doit lever
    // le mode filtré, sinon `body.cat-filtered` masquerait la barre d'urgences sur
    // les autres onglets (V2-12e). Sur #autour/{code}, applyHash a déjà posé le
    // filtre AVANT d'appeler activate("around") → on ne le touche pas ici.
    if (tabKey !== "around") setServiceFilter("");
    tabs.forEach((t) => {
      const on = t.dataset.tab === tabKey;
      t.classList.toggle("on", on);
      t.setAttribute("aria-selected", on ? "true" : "false");
    });
    panels.forEach((p) => p.classList.toggle("tab-active", p.dataset.tab === tabKey));
    // V2-12f : classe d'onglet au niveau body → l'onglet Urgences masque la barre
    // SOS d'en-tête (doublon avec les cartes du contenu). Même mécanique que
    // `body.cat-filtered` (V2-12e). Les autres onglets gardent la barre.
    Object.values(TAB_HASH).forEach((h) => document.body.classList.toggle("tab-" + h, h === TAB_HASH[tabKey]));
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
    let catCode = "";   // V2-12b : catégorie du mode filtré (#autour/{code}), sinon ""
    if (!tabKey && raw) {
      // Ancre directe : section (#B_wifi) OU catégorie de la grille (#autour/{code},
      // l'id du bloc `.cat` est « autour/{code} », V2-12).
      const elt = document.getElementById(raw);
      const panel = elt && elt.closest(".tab-panel[data-tab]");
      if (panel) { tabKey = panel.dataset.tab; scrollEl = elt; }
      // V2-12b : une tuile de la grille (#autour/{code}) ouvre le MODE FILTRÉ
      // (une catégorie = un écran), pas un simple défilement vers le bloc.
      if (elt && tabKey === "around" && elt.classList.contains("cat")) {
        catCode = elt.dataset.cat || "";
      }
    }
    // V2-12 : si la cible est un bloc de catégorie masqué par une puce de filtre
    // (chapitre), on rétablit « Tout » pour la rendre visible avant le défilement.
    if (scrollEl && scrollEl.classList.contains("cat")) {
      const around = scrollEl.closest('.tab-panel[data-tab="around"]');
      const activeChip = around && around.querySelector(".chip.on");
      if (activeChip && activeChip.dataset.chapter) {
        const allChip = around.querySelector('.chip[data-chapter=""]');
        if (allChip) allChip.click();
      }
    }
    // V2-12b : (dés)active le mode filtré. Hors #autour/{code} (onglet fixe,
    // grille #autour, ancre de section) → catCode="" → grille + tous les blocs.
    setServiceFilter(catCode);
    activate(tabKey || "home", { push });
    if (catCode) {
      // Mode filtré : une catégorie = un écran → défilement remis EN HAUT (la
      // grille et les autres blocs ont disparu, le bloc choisi occupe l'écran).
      setTimeout(() => window.scrollTo({ top: 0, behavior: "auto" }), 0);
    } else if (scrollEl) {
      setTimeout(() => scrollEl.scrollIntoView({ behavior: "smooth", block: "start" }), 80);
    }
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

// ── Retour aux services (V2-27) : ramener le voyageur à la grille V2-12 ──────
// Chemin de retour visible depuis une liste de catégorie profonde. Les liens de
// fin de liste sont rendus SSR (ancre native `#autour` → grille, sans JS) ; ici
// on garantit l'onglet actif + le défilement doux, et on révèle un bouton
// flottant discret pendant le défilement dans « Autour de vous ».
function initBackToServices() {
  const grid = document.querySelector(".svc-grid");
  const links = [...document.querySelectorAll(".back-services")];
  if (!grid || !links.length) return;
  const aroundHash = "#" + TAB_HASH.around;

  links.forEach((a) => a.addEventListener("click", (e) => {
    e.preventDefault();
    if (window._activateTab) window._activateTab("around");
    // V2-12b : sortie du mode filtré → grille + tous les blocs de nouveau visibles.
    // (pushState ne déclenche pas de hashchange → on désactive le mode à la main.)
    setServiceFilter("");
    // Nettoie le hash vers l'onglet (#autour) → historique cohérent avec V2-12.
    if (location.hash !== aroundHash) history.pushState(null, "", aroundHash);
    grid.scrollIntoView({ behavior: "smooth", block: "start" });
  }));

  // Bouton flottant : visible seulement dans l'onglet « Autour » et une fois
  // défilé au-delà d'un seuil raisonnable (disparaît en haut de page). Bonus JS.
  const float = document.querySelector(".back-float");
  if (!float) return;
  const around = document.querySelector('.tab-panel[data-tab="around"]');
  let ticking = false;
  const evaluate = () => {
    ticking = false;
    const active = around && around.classList.contains("tab-active");
    // V2-12b : en mode filtré, le flottant est TOUJOURS offert (chemin de retour
    // à la grille) ; hors mode filtré, seulement une fois défilé assez bas.
    const filtered = around && around.classList.contains("cat-filtered");
    float.classList.toggle("show", !!(active && (filtered || window.scrollY > 480)));
  };
  const onScroll = () => { if (!ticking) { ticking = true; requestAnimationFrame(evaluate); } };
  window.addEventListener("scroll", onScroll, { passive: true });
  // Réévalue à tout changement d'onglet, quel que soit son déclencheur (bouton,
  // hash, retour arrière) : l'onglet actif se lit sur la classe du panneau.
  if (around) new MutationObserver(evaluate).observe(around, {
    attributes: true, attributeFilter: ["class"],
  });
  evaluate();
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
    // Les puces ne sont visibles qu'en grille (masquées en mode filtré) : la
    // synchro des marqueurs lit le chapitre actif via activeChapter().
    syncMarkers();
  }));
}

// ── Listes de lieux : repli à 4 + filtre par cuisine (V2-09, M-16) ───────────
// Chaque catégorie (`.cat`) affiche 4 cartes (coups de cœur ❤ d'abord — tri
// serveur — puis les plus proches) et un bouton « Voir les N autres » qui déplie
// le reste (puis « Réduire »). Repli PUREMENT client : le HTML contient tout
// (sans JS, tout est visible). Pour les restaurants, le filtre par cuisine et le
// repli coopèrent : le compte du bouton suit les cartes réellement éligibles.
const POI_LIMIT = 4;
function initCategoryLists() {
  document.querySelectorAll(".cat").forEach((cat) => {
    const group = cat.querySelector(".poi-group");
    if (!group) return;
    const cards = [...group.querySelectorAll(":scope > .poi-card")];
    const moreBtn = cat.querySelector(".more-btn");
    const chips = [...cat.querySelectorAll(".cuisines .cchip")];
    const state = { cuisine: "", expanded: false };

    const eligible = (card) => !state.cuisine || card.dataset.cuisine === state.cuisine;

    function apply() {
      let shown = 0;
      cards.forEach((card) => {
        if (!eligible(card)) { card.style.display = "none"; return; }
        shown += 1;
        card.style.display = (state.expanded || shown <= POI_LIMIT) ? "" : "none";
      });
      if (moreBtn) {
        const total = cards.filter(eligible).length;
        if (total > POI_LIMIT) {
          moreBtn.classList.add("show");
          moreBtn.textContent = state.expanded
            ? (moreBtn.dataset.less || "")
            : (moreBtn.dataset.moreTpl || "").replace("{n}", total - POI_LIMIT);
        } else {
          moreBtn.classList.remove("show");   // filtre laissant ≤ 4 : rien à replier
        }
      }
    }

    if (moreBtn) {
      moreBtn.addEventListener("click", () => { state.expanded = !state.expanded; apply(); });
    }
    chips.forEach((chip) => chip.addEventListener("click", () => {
      chips.forEach((c) => c.classList.remove("on"));
      chip.classList.add("on");
      state.cuisine = chip.dataset.cuisine || "";
      state.expanded = false;
      apply();
    }));
    apply();   // repli initial à 4
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
initBackToServices();
initCategoryLists();
initCopy();
initLightbox();
initSecrets();
initPwa();
