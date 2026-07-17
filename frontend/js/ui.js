/* Boîte à outils UI : constructeur de DOM, icônes Lucide, toasts, modales.
   Aucun framework — DOM natif, pensé pour rester lisible et léger. */

// ── Construction de DOM ──────────────────────────────────────────────────────
// el("div", {class, onClick, dataset, style, html, text, ...attrs}, ...enfants)
export function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k === "class" || k === "className") node.className = v;
    else if (k === "html") node.innerHTML = v;
    else if (k === "text") node.textContent = v;
    else if (k === "dataset") Object.assign(node.dataset, v);
    else if (k === "style" && typeof v === "object") Object.assign(node.style, v);
    else if (k.startsWith("on") && typeof v === "function")
      node.addEventListener(k.slice(2).toLowerCase(), v);
    else if (v === true) node.setAttribute(k, "");
    else node.setAttribute(k, v);
  }
  append(node, children);
  return node;
}

function append(node, children) {
  for (const c of children.flat(Infinity)) {
    if (c == null || c === false || c === true) continue;
    node.append(c.nodeType ? c : document.createTextNode(String(c)));
  }
}

export function clear(node) { while (node.firstChild) node.firstChild.remove(); return node; }
export function mount(node, ...children) { clear(node); append(node, children); refreshIcons(); return node; }

// ── Icônes Lucide (dégradation propre si le CDN est absent) ──────────────────
export function icon(name, size = 18) {
  const i = document.createElement("i");
  i.setAttribute("data-lucide", name);
  i.setAttribute("width", size);
  i.setAttribute("height", size);
  i.style.display = "inline-flex";
  i.style.flex = "none";
  return i;
}
export function refreshIcons() {
  try { if (window.lucide) window.lucide.createIcons(); } catch (_) { /* non bloquant */ }
}

// ── i18n / formatage ─────────────────────────────────────────────────────────
export function t(i18n, fallback = "") {
  if (!i18n) return fallback;
  if (typeof i18n === "string") return i18n;
  return i18n.fr || i18n.en || i18n.es || fallback;
}

/** Distance « voyageur » : à pied si ≤ 30 min, sinon en voiture (cf. §M-01). */
export function fmtDist(p) {
  if (p.walk_min != null && p.walk_min <= 30) return { n: p.walk_min, u: "min à pied" };
  if (p.drive_min != null) return { n: p.drive_min, u: "min en voiture" };
  return { n: "–", u: "" };
}

// ── Toasts ───────────────────────────────────────────────────────────────────
export function toast(message, type = "") {
  const box = el("div", { class: "toast " + type },
    type === "ok" ? icon("check", 18) : type === "err" ? icon("triangle-alert", 18) : null,
    el("span", {}, message));
  document.getElementById("toasts").append(box);
  refreshIcons();
  setTimeout(() => {
    box.style.transition = "opacity .3s"; box.style.opacity = "0";
    setTimeout(() => box.remove(), 300);
  }, type === "err" ? 5200 : 3000);
}

// ── Modales ──────────────────────────────────────────────────────────────────
export function openModal({ title, body, footer, size, onClose }) {
  const back = el("div", { class: "modal-back" });
  const close = () => { document.removeEventListener("keydown", onKey); back.remove(); if (onClose) onClose(); };
  const modal = el("div", { class: "modal" + (size === "lg" ? " modal-lg" : "") },
    el("div", { class: "modal-head" },
      el("h3", {}, title),
      el("button", { class: "close", "aria-label": "Fermer", onClick: close }, icon("x", 20))),
    el("div", { class: "modal-body" }, body),
    footer ? el("div", { class: "modal-foot" }, footer) : null);
  back.append(modal);
  back.addEventListener("mousedown", (e) => { if (e.target === back) close(); });
  const onKey = (e) => { if (e.key === "Escape") close(); };
  document.addEventListener("keydown", onKey);
  document.body.append(back);
  refreshIcons();
  return { close, root: modal };
}

export function confirmDialog(message, { title = "Confirmer", okLabel = "Confirmer", danger = false } = {}) {
  return new Promise((resolve) => {
    const ok = el("button", { class: "btn " + (danger ? "btn-danger" : "btn-primary") }, okLabel);
    const cancel = el("button", { class: "btn btn-ghost" }, "Annuler");
    const m = openModal({
      title,
      body: el("p", { class: "muted", style: { margin: "0" } }, message),
      footer: [cancel, ok],
      onClose: () => resolve(false),
    });
    ok.addEventListener("click", () => { resolve(true); m.close(); });
    cancel.addEventListener("click", () => { resolve(false); m.close(); });
  });
}

// ── Presse-papiers : repli en contexte non sécurisé (HTTP) ────────────────────
// navigator.clipboard n'existe qu'en HTTPS/localhost. Tant que la production
// est servie par IP en HTTP (avant le domaine), on fournit un équivalent via
// l'ancienne API execCommand — même signature, donc transparent pour les vues.
if (!navigator.clipboard) {
  const writeText = (text) => new Promise((resolve, reject) => {
    const ta = document.createElement("textarea");
    ta.value = String(text);
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.append(ta);
    ta.select();
    ta.setSelectionRange(0, ta.value.length); // iOS
    let ok = false;
    try { ok = document.execCommand("copy"); } catch (_) { ok = false; }
    ta.remove();
    ok ? resolve() : reject(new Error("copy failed"));
  });
  try {
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
  } catch (_) { /* navigateur récalcitrant : le message d'erreur existant reste le repli */ }
}

// ── États génériques ─────────────────────────────────────────────────────────
export function loadingBlock(label = "Chargement…") {
  return el("div", { class: "loading" }, el("div", {}, icon("loader-circle", 30), el("div", { style: { marginTop: "8px" } }, label)));
}
export function emptyBlock({ icon: ic = "inbox", title, text, action } = {}) {
  return el("div", { class: "empty" }, icon(ic, 36),
    title ? el("h3", {}, title) : null,
    text ? el("p", { style: { margin: "0 auto", maxWidth: "42ch" } }, text) : null,
    action ? el("div", { style: { marginTop: "16px" } }, action) : null);
}
