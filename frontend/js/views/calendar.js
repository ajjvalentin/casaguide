/* Calendrier des séjours (V2-23a) : liste chronologique des séjours d'un
   logement (importés des plateformes via iCal + saisies directes), avec alerte
   de chevauchement, mise en évidence des rotations (même jour), et gestion des
   flux iCal (ajout avec validation au collage, synchro, suppression).

   Liste chronologique (pas de grille mensuelle en V1) : plus lisible sur mobile.
   L'anti-chevauchement ALERTE, ne bloque jamais (le propriétaire arbitre). Une
   rotation (départ = arrivée le même jour) n'est pas un chevauchement : elle est
   affichée avec sa fenêtre de préparation. Accessible à tous les plans. */

import { api } from "../api.js";
import {
  el, icon, mount, clear, toast, openModal, confirmDialog, loadingBlock,
  emptyBlock, refreshIcons,
} from "../ui.js";
import { navigate } from "../nav.js";
import { handleQuotaError } from "../quota.js";
import { analyzeCandidate } from "../lib/overlaps.js";
import { suggestEquipment } from "../lib/care.js";
import { publishedLanguageCodes } from "../languages.js";

const PLATFORMS = [
  ["airbnb", "Airbnb"], ["vrbo", "Vrbo / Abritel"], ["booking", "Booking"],
  ["other", "Autre"],
];
const SOURCES = [
  ["direct", "Location directe"], ["airbnb", "Airbnb"], ["vrbo", "Vrbo / Abritel"],
  ["booking", "Booking"], ["other", "Autre"],
];
const SOURCE_LABEL = Object.fromEntries(
  [...SOURCES, ["direct", "Direct"]].map(([k, v]) => [k, v]));
// La NATURE porte la sémantique (elle pilote la préparation) — jamais le statut.
const NATURE_OPTIONS = [
  ["reservation", "Réservation"], ["private", "Séjour privé"],
  ["works", "Travaux"], ["unavailable", "Indisponible"],
  ["unqualified", "À qualifier"],
];
const NATURE_LABEL = Object.fromEntries(NATURE_OPTIONS);
// Libellé de repli EXPLICITE (§0.h) : une nature absente/inconnue ne doit jamais
// imprimer « undefined » à l'écran — elle retombe sur « À qualifier ».
const natureLabel = (n) => NATURE_LABEL[n] || NATURE_LABEL.unqualified;
const OCCUPIED = new Set(["reservation", "private"]);
// Libellés humains des langues du locataire (§3.0). La LISTE proposée n'est jamais
// en dur : elle se lit dans les langues PUBLIÉES du logement + sa langue source
// (offrir une langue non générée créerait une promesse intenable). Ce dictionnaire
// ne fait qu'embellir un code présent ; un code inconnu s'affiche brut.
const LANG_LABELS = {
  fr: "Français", en: "Anglais", es: "Espagnol", de: "Allemand",
  nl: "Néerlandais", it: "Italien", pt: "Portugais",
};
const langLabel = (c) => LANG_LABELS[c] || c;
const MONTHS = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.",
  "août", "sept.", "oct.", "nov.", "déc."];

// Ouverture différée de la modale d'un séjour au prochain rendu du calendrier
// (V2-23c, volet 3) : la fenêtre « Envoyer le guide » (page « Mes logements »)
// s'en sert pour l'invitation « Ajouter un email à ce séjour » — elle pose l'id,
// navigue vers le calendrier, et la modale du séjour s'ouvre là où on l'édite.
let _pendingOpenBooking = null;
export function openBookingOnLoad(bookingId) { _pendingOpenBooking = bookingId; }

function todayISO() {
  const d = new Date();
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

// ── Formatage dates/heures (parsing manuel : aucune surprise de fuseau) ──────
function parseISO(iso) { const [y, m, d] = iso.split("-").map(Number); return { y, m, d }; }
function fmtDay(iso) { const { d, m } = parseISO(iso); return `${pad(d)}.${pad(m)}`; }
function fmtDayLong(iso) { const { d, m, y } = parseISO(iso); return `${d} ${MONTHS[m - 1]} ${y}`; }
function fmtRange(startIso, endIso) {
  const a = parseISO(startIso), b = parseISO(endIso);
  const end = a.y === b.y ? `${fmtDay(endIso)}.${b.y}` : fmtDayLong(endIso);
  return `${fmtDay(startIso)} → ${end}`;
}
function pad(n) { return String(n).padStart(2, "0"); }
// Code technique à partir d'un libellé (catalogue de demandes) : ASCII, minuscules.
function slugCode(label) {
  const base = label.normalize("NFKD").replace(/[\u0300-\u036f]/g, "")
    .toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
  return (base || "demande").slice(0, 60);
}
// V2-23g — dates ajustées à la main : le propriétaire a repris les dates d'un
// séjour importé (dates_overridden) ET le flux annonce désormais AUTRE chose. On ne
// signale que la vraie divergence (protéger sans signaler masquerait une vraie
// modification côté plateforme). Comparaison de chaînes ISO (aucune surprise de
// fuseau — jamais de Date()).
function datesDiverge(b) {
  return !!(b.dates_overridden && b.feed_starts_on && b.feed_ends_on
    && (b.feed_starts_on !== b.starts_on || b.feed_ends_on !== b.ends_on));
}
function fmtTime(t) { return t ? t.slice(0, 5) : ""; }        // "15:00:00" → "15:00"
function fmtGap(min) {
  if (min == null) return "";
  const h = Math.floor(min / 60), m = min % 60;
  if (h && m) return `${h} h ${pad(m)}`;
  if (h) return `${h} h`;
  return `${m} min`;
}
function relSync(iso) {
  if (!iso) return "jamais synchronisé";
  const then = new Date(iso).getTime();
  const diff = Math.max(0, Date.now() - then);
  const min = Math.round(diff / 60000);
  if (min < 1) return "à l'instant";
  if (min < 60) return `il y a ${min} min`;
  const h = Math.round(min / 60);
  if (h < 24) return `il y a ${h} h`;
  return `il y a ${Math.round(h / 24)} j`;
}

export async function renderCalendar(view, pid) {
  mount(view, el("div", { class: "page" }, loadingBlock("Chargement du calendrier…")));

  let property, data, catalog, publishedLangCodes;
  try {
    [property, data, catalog, publishedLangCodes] = await Promise.all([
      api.getProperty(pid), api.calendarView(pid), api.listRequestTypes(pid),
      publishedLanguageCodes()]);
  } catch (err) {
    return mount(view, el("div", { class: "page" },
      el("div", { class: "errbox" }, err.message || "Impossible de charger le calendrier.")));
  }

  const reload = async () => {
    try { data = await api.calendarView(pid); paint(); }
    catch (err) { toast(err.message || "Rechargement impossible.", "err"); }
  };

  const body = el("div", {});
  const page = el("div", { class: "page" },
    el("div", { class: "crumbs" },
      el("a", { href: "#/properties" }, "Mes logements"), icon("chevron-right", 14),
      el("a", { href: `#/properties/${pid}/editor` }, property.name), icon("chevron-right", 14),
      el("span", {}, "Calendrier")),
    el("div", { class: "row", style: { justifyContent: "space-between", alignItems: "flex-start", marginBottom: "14px" } },
      el("div", {}, el("div", { class: "eyebrow" }, "Séjours"),
        el("h1", { class: "page-title", style: { margin: "2px 0 0" } }, "Calendrier des séjours")),
      el("div", { class: "row", style: { gap: "8px" } },
        el("button", { class: "btn btn-sm", onClick: () => openCareSettings() },
          icon("sliders-horizontal", 16), "Réglages d'entretien"),
        el("button", { class: "btn btn-sm", id: "cal-sync", onClick: () => syncNow() },
          icon("refresh-cw", 16), "Synchroniser"),
        el("button", { class: "btn btn-primary btn-sm", onClick: () => openBookingModal() },
          icon("plus", 16), "Nouveau séjour"))),
    body);
  mount(view, page);
  paint();

  // Ouverture différée demandée par la fenêtre d'envoi (invitation « Ajouter un
  // email à ce séjour ») : on ouvre la modale du séjour ciblé une fois la vue prête.
  if (_pendingOpenBooking) {
    const target = (data.bookings || []).find((x) => x.id === _pendingOpenBooking);
    _pendingOpenBooking = null;
    if (target) openBookingModal(target);
  }

  // ── Rendu principal (rejoué après chaque changement) ──────────────────────
  function paint() {
    clear(body);
    const bookings = data.bookings || [];
    const active = bookings.filter((b) => b.status !== "cancelled");
    const cancelled = bookings.filter((b) => b.status === "cancelled");
    // Ancrage sur aujourd'hui (§0.7) : les plateformes exportent aussi l'historique.
    // « À venir » (en cours compris : départ ≥ aujourd'hui) par défaut ; les séjours
    // déjà terminés sont repliés dans une section discrète.
    const today = todayISO();
    const upcoming = active.filter((b) => b.ends_on >= today);
    const past = active.filter((b) => b.ends_on < today);

    // Index chevauchements & rotations pour marquer les lignes.
    const overlapIds = new Set();
    for (const o of data.overlaps || []) { overlapIds.add(o.a); overlapIds.add(o.b); }
    const rotById = new Map();
    for (const r of data.rotations || []) {
      rotById.set(r.departing, { role: "out", r });
      rotById.set(r.arriving, { role: "in", r });
    }

    // Alerte chevauchement (bandeau rouge) — alerte, jamais un blocage.
    if ((data.overlaps || []).length) {
      body.append(el("div", { class: "cal-alert" }, icon("triangle-alert", 18),
        el("div", {},
          el("b", {}, (data.overlaps.length === 1 ? "Un chevauchement détecté"
            : `${data.overlaps.length} chevauchements détectés`)),
          el("div", { class: "muted", style: { fontSize: "13px" } },
            "Deux séjours confirmés se recouvrent. À vous d'arbitrer (dates iCal en journées entières, il s'agit peut-être d'une rotation volontaire)."))));
    }

    // §3.1 — Demandes du voyageur EN ATTENTE, agrégées en tête (le propriétaire est
    // aussi notifié par email). Cliquable → ouvre le premier séjour concerné, où il
    // accepte/refuse ; acceptée, la demande devient une intervention pour l'équipe.
    const withReq = active.filter((b) => b.pending_guest_requests);
    const reqTotal = withReq.reduce((n, b) => n + b.pending_guest_requests, 0);
    if (reqTotal) {
      body.append(el("button", { class: "cal-request-banner", type: "button",
        onClick: () => openBookingModal(withReq[0]) },
        icon("concierge-bell", 18),
        el("div", {},
          el("b", {}, reqTotal === 1 ? "Une demande de voyageur en attente"
            : `${reqTotal} demandes de voyageur en attente`),
          el("div", { class: "muted", style: { fontSize: "13px" } },
            "Ouvrez le séjour pour l'accepter ou la refuser."))));
    }

    // §2.1 — Agrégation de l'incomplétude EN TÊTE (plutôt qu'un badge répété) :
    // un signal toujours allumé cesse d'être un signal. Cliquable → panneau de
    // saisie rapide (le nombre de voyageurs manque sur presque tous les imports).
    const incomplete = upcoming.filter((b) => (b.missing_info || []).length);
    if (incomplete.length) body.append(incompleteBanner(incomplete));

    // Rotations mises en évidence, avec signal de rotation gradué (§2.2). Le
    // triangle n'apparaît QUE pour une rotation infaisable (danger réel) ; sinon
    // la double flèche neutre — cohérent avec la grammaire graduée (§2.1).
    for (const r of data.rotations || []) {
      const out = bookings.find((b) => b.id === r.departing);
      const inn = bookings.find((b) => b.id === r.arriving);
      if (!out || !inn) continue;
      const sig = r.signal;
      const danger = sig && sig.level === "red";
      const amber = sig && sig.level === "amber";
      const sigLine = (danger || amber)
        ? el("div", { class: "cal-rot-signal cal-rot-signal-" + sig.level }, sig.message)
        : null;
      body.append(el("div", {
        class: "cal-rotation" + (danger ? " cal-rotation-red" : amber ? " cal-rotation-amber" : "") },
        icon(danger ? "triangle-alert" : "arrow-right-left", 17),
        el("div", {},
          el("b", {}, `Rotation le ${fmtDayLong(r.on)}`),
          el("div", { class: "muted", style: { fontSize: "13px" } },
            `Sortie ${fmtTime(out.eff_checkout_time)} → entrée ${fmtTime(inn.eff_checkin_time)} — fenêtre ${fmtGap(r.gap_minutes)}`),
          sigLine)));
    }

    // Liste chronologique des séjours À VENIR (en cours compris).
    if (!active.length) {
      body.append(emptyBlock({
        icon: "calendar", title: "Aucun séjour",
        text: "Importez un calendrier de plateforme (Airbnb, Vrbo/Abritel, Booking) ou saisissez une location directe.",
      }));
    } else if (!upcoming.length) {
      body.append(emptyBlock({
        icon: "calendar-check", title: "Aucun séjour à venir",
        text: "Les séjours passés sont repliés plus bas.",
      }));
    } else {
      const list = el("div", { class: "booking-list" });
      for (const b of upcoming) list.append(bookingRow(b, overlapIds, rotById));
      body.append(list);
    }

    // Séjours passés (repliés, discrets — l'historique n'encombre pas la vue).
    if (past.length) {
      const inner = el("div", { class: "booking-list" });
      for (const b of past) inner.append(bookingRow(b, overlapIds, rotById));
      body.append(el("details", { class: "cancelled-block" },
        el("summary", {}, `Séjours passés (${past.length})`), inner));
    }

    // Séjours annulés (repliés, discrets).
    if (cancelled.length) {
      const inner = el("div", { class: "booking-list" });
      for (const b of cancelled) inner.append(bookingRow(b, overlapIds, rotById));
      body.append(el("details", { class: "cancelled-block" },
        el("summary", {}, `Séjours annulés (${cancelled.length})`), inner));
    }

    body.append(fluxSection());
    refreshIcons();
  }

  function bookingRow(b, overlapIds, rotById) {
    const overlap = overlapIds.has(b.id);
    const rot = rotById.get(b.id);
    const toQualify = b.nature === "unqualified" && b.status !== "cancelled";
    const platBadge = el("span", { class: "plat-badge plat-" + b.source },
      SOURCE_LABEL[b.source] || b.source);
    const natureBadge = el("span", { class: "badge cal-nat cal-nat-" + b.nature },
      natureLabel(b.nature));

    const meta = el("div", { class: "row", style: { gap: "8px", flexWrap: "wrap", alignItems: "center" } },
      platBadge, natureBadge,
      overlap ? el("span", { class: "cal-flag cal-flag-overlap", title: "Chevauchement" },
        icon("triangle-alert", 13), "Chevauchement") : null,
      rot ? el("span", { class: "cal-flag cal-flag-rot", title: "Rotation même jour" },
        icon("arrow-right-left", 13), rot.role === "out" ? "Départ le jour d'une arrivée" : "Arrivée le jour d'un départ") : null,
      // V2-23g — dates ajustées à la main ET flux divergent : signal SOBRE (jamais
      // le triangle, réservé au danger), ton neutre-attention — le flux annonce
      // désormais autre chose, à examiner dans la fiche.
      datesDiverge(b)
        ? el("span", { class: "cal-flag cal-flag-datesadj",
          title: `Dates ajustées à la main. Le flux ${SOURCE_LABEL[b.source] || b.source} `
            + `indique désormais ${fmtRange(b.feed_starts_on, b.feed_ends_on)}.` },
          icon("calendar-clock", 13), "Dates ≠ flux")
        : null,
      // §3.1 — demande(s) du voyageur en attente : badge appelant à arbitrer.
      b.pending_guest_requests
        ? el("span", { class: "cal-flag cal-flag-request",
          title: "Demande de service du voyageur à traiter" },
          icon("concierge-bell", 13),
          b.pending_guest_requests > 1
            ? `${b.pending_guest_requests} demandes` : "Demande voyageur")
        : null,
      // V2-23h — succession d'identifiants : ce séjour remplace peut-être un
      // prédécesseur annulé par la plateforme. Ton ATTENTION (une situation à
      // résoudre), jamais le triangle rouge (réservé au danger). Message humain.
      b.succession
        ? el("span", { class: "cal-flag cal-flag-succession",
          title: b.succession.message },
          icon("history", 13), "Fiche à reprendre ?")
        : null);

    const times = el("div", { class: "muted booking-times" },
      `Arrivée ${fmtTime(b.eff_checkin_time)}${b.checkin_time ? " (ajustée)" : ""} · `
      + `Départ ${fmtTime(b.eff_checkout_time)}${b.checkout_time ? " (ajustée)" : ""}`
      + (b.guest_count != null ? ` · ${b.guest_count} voyageur(s)`
        + (b.children_count ? ` (dont ${b.children_count} enfant(s))` : "") : ""));

    // Relance active (§0.6) : ce qui manque encore pour préparer ce séjour.
    // §2.1 — l'incomplétude n'est PAS un danger : une seule pastille SOBRE (jamais
    // le triangle, réservé au chevauchement/rotation infaisable), une seule ligne.
    // Le détail vit dans le title (survol) et dans la fiche ouverte, pas empilé ici.
    const relance = (b.missing_info && b.missing_info.length)
      ? el("div", { class: "booking-relance" },
        el("span", { class: "cal-incomplete",
          title: b.missing_info.map((m) => m.message).join("\n") },
          icon("circle-dashed", 12), "Incomplet"))
      : null;

    const who = b.guest_name
      ? el("span", { class: "booking-guest" }, b.guest_name)
      : el("span", { class: "muted booking-guest" },
        toQualify ? "À qualifier" : (OCCUPIED.has(b.nature) ? "Sans nom" : "—"));

    const cta = toQualify
      ? el("button", { class: "btn btn-sm", onClick: (e) => { e.stopPropagation(); openBookingModal(b); } },
        icon("pencil", 15), "Compléter")
      : el("button", { class: "btn btn-sm btn-ghost", "aria-label": "Ouvrir le séjour",
        onClick: (e) => { e.stopPropagation(); openBookingModal(b); } }, icon("chevron-right", 16));

    return el("div", {
      class: "booking-row" + (overlap ? " row-overlap" : "") + (rot ? " row-rot" : "")
        + (b.status === "cancelled" ? " row-cancelled" : "")
        + (toQualify ? " row-blocked" : ""),
      dataset: { bid: b.id, nature: b.nature, status: b.status, source: b.source },
      onClick: () => openBookingModal(b),
    },
      el("div", { class: "booking-dates" }, fmtRange(b.starts_on, b.ends_on)),
      el("div", { class: "booking-body" }, who, meta, times, relance),
      cta);
  }

  // ── Séjours incomplets : agrégation + saisie rapide (§2.1) ────────────────
  // Un panneau repliable en tête de liste plutôt qu'un badge sur chaque ligne.
  // La saisie du nombre de voyageurs y est faite EN LIGNE (le champ manque sur la
  // quasi-totalité des imports : remplir un par un via la modale serait coûteux).
  function incompleteBanner(list) {
    const panel = el("div", { class: "incomplete-panel", hidden: true });
    for (const b of list) panel.append(quickFixRow(b));
    const chev = el("span", { class: "qf-chev" }, icon("chevron-down", 16));
    const head = el("button", {
      class: "incomplete-head", type: "button", "aria-expanded": "false",
      onClick: () => {
        const open = panel.hidden;
        panel.hidden = !open;
        head.setAttribute("aria-expanded", open ? "true" : "false");
        chev.classList.toggle("qf-open", open);
      },
    },
      icon("circle-dashed", 17),
      el("b", {}, `${list.length} séjour${list.length > 1 ? "s" : ""} incomplet${list.length > 1 ? "s" : ""}`),
      el("span", { class: "muted qf-sub" }, "à compléter (voyageurs, coordonnées)"),
      chev);
    return el("div", { class: "incomplete-box" }, head, panel);
  }

  function quickFixRow(b) {
    const missCodes = new Set((b.missing_info || []).map((m) => m.code));
    const info = el("div", { class: "qf-info" },
      el("div", { class: "qf-label" },
        el("b", {}, fmtRange(b.starts_on, b.ends_on)),
        b.guest_name ? el("span", { class: "muted" }, " · " + b.guest_name) : null),
      el("div", { class: "qf-tags" },
        ...(b.missing_info || []).map((m) => el("span", { class: "qf-tag" }, m.message))));

    const actions = el("div", { class: "qf-actions" });
    // Saisie rapide du nombre de voyageurs (entier ≥ 1, §2.1.5).
    if (missCodes.has("guest_count_missing")) {
      const input = el("input", { type: "number", min: "1", step: "1",
        class: "qf-count", placeholder: "voyageurs", "aria-label": "Nombre de voyageurs" });
      const save = el("button", { class: "btn btn-sm btn-primary", type: "button" }, "OK");
      const commit = async () => {
        const n = Number(input.value);
        if (!Number.isInteger(n) || n < 1) {
          toast("Nombre de voyageurs : un entier ≥ 1.", "err"); return;
        }
        save.disabled = true; input.disabled = true;
        try {
          await api.updateBooking(pid, b.id, { guest_count: n });
          toast("Nombre de voyageurs enregistré.", "ok");
          await reload();
        } catch (e) {
          if (!handleQuotaError(e)) toast(e.message || "Enregistrement impossible.", "err");
          save.disabled = false; input.disabled = false;
        }
      };
      save.onclick = commit;
      input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); commit(); } });
      actions.append(input, save);
    }
    actions.append(el("button", { class: "btn btn-sm btn-ghost", type: "button",
      onClick: () => openBookingModal(b) }, "Ouvrir la fiche"));
    return el("div", { class: "qf-row" }, info, actions);
  }

  // ── Flux de calendrier (iCal) ─────────────────────────────────────────────
  function fluxSection() {
    const wrap = el("div", { class: "card flux-card" },
      el("h3", { style: { marginTop: 0 } }, icon("rss", 18), " Flux de calendrier"));

    const cals = data.calendars || [];
    if (!cals.length) {
      wrap.append(el("p", { class: "muted", style: { fontSize: "13px" } },
        "Aucun flux importé. Collez l'URL iCal de votre annonce (Airbnb, Vrbo/Abritel, Booking) pour importer automatiquement vos séjours."));
    } else {
      const list = el("div", { class: "flux-list" });
      for (const c of cals) list.append(fluxRow(c));
      wrap.append(list);
    }
    wrap.append(addFluxForm());
    return wrap;
  }

  function fluxRow(c) {
    const platLabel = (PLATFORMS.find((p) => p[0] === c.platform) || [, c.platform])[1];
    const state = c.last_sync_status === "error"
      ? el("span", { class: "flux-state flux-err" }, icon("circle-alert", 14),
        c.sync_error || "erreur de synchro")
      : el("span", { class: "flux-state flux-ok" }, icon("circle-check", 14),
        `synchronisé ${relSync(c.last_sync_at)}`);
    return el("div", { class: "flux-row", dataset: { cid: c.id } },
      el("div", {},
        el("div", { class: "row", style: { gap: "8px", alignItems: "center" } },
          el("span", { class: "plat-badge plat-" + c.platform }, platLabel),
          el("code", { class: "flux-url" }, c.masked_url)),
        state),
      el("button", { class: "btn btn-sm btn-ghost", "aria-label": "Supprimer le flux",
        onClick: () => removeFlux(c) }, icon("trash-2", 15)));
  }

  function addFluxForm() {
    const sel = el("select", { name: "platform" },
      ...PLATFORMS.map(([k, v]) => el("option", { value: k }, v)));
    const url = el("input", { type: "url", name: "ical_url", class: "flux-input",
      placeholder: "https://…/calendar/ical/….ics" });
    const feedback = el("div", { class: "flux-feedback" });
    const submit = el("button", { class: "btn btn-sm btn-primary", type: "submit" },
      icon("plus", 15), "Ajouter & vérifier");
    const form = el("form", { class: "flux-add", onSubmit: onAdd }, sel, url, submit);

    async function onAdd(e) {
      e.preventDefault();
      clear(feedback); feedback.className = "flux-feedback";
      const value = url.value.trim();
      if (!value) { feedback.classList.add("flux-err"); feedback.textContent = "Collez l'URL du flux iCal."; return; }
      submit.disabled = true; submit.textContent = "Vérification…";
      try {
        const res = await api.addCalendar(pid, { platform: sel.value, ical_url: value });
        if (res.sync.status === "ok") {
          toast(`${res.sync.created} séjour(s) importé(s).`, "ok");
          url.value = "";
        } else {
          toast("Flux ajouté, mais la synchro a échoué : " + (res.sync.error || ""), "err");
        }
        await reload();
      } catch (err) {
        if (handleQuotaError(err)) return;
        feedback.classList.add("flux-err");
        feedback.textContent = err.message || "Ajout impossible.";
      } finally {
        submit.disabled = false;
        mount(submit, icon("plus", 15), "Ajouter & vérifier");
      }
    }
    return el("div", {}, form, feedback);
  }

  async function removeFlux(c) {
    if (!(await confirmDialog(
      "Supprimer ce flux ? Ses séjours importés seront conservés mais marqués « annulés » (jamais supprimés).",
      { title: "Supprimer le flux", okLabel: "Supprimer", danger: true }))) return;
    try {
      await api.deleteCalendar(pid, c.id);
      toast("Flux supprimé.", "ok");
      await reload();
    } catch (err) { if (!handleQuotaError(err)) toast(err.message || "Suppression impossible.", "err"); }
  }

  async function syncNow() {
    const btn = document.getElementById("cal-sync");
    if (btn) { btn.disabled = true; }
    try {
      const r = await api.syncCalendars(pid);
      toast(`Synchro : ${r.created} créé(s), ${r.updated} à jour, ${r.cancelled} annulé(s).`, "ok");
      await reload();
    } catch (err) {
      if (err.status === 429) toast("Déjà synchronisé à l'instant. Réessayez dans quelques secondes.", "");
      else if (!handleQuotaError(err)) toast(err.message || "Synchronisation impossible.", "err");
    } finally { if (btn) btn.disabled = false; }
  }

  // ── Réglages d'entretien : règles + catalogue de demandes (§1.1/§1.2) ──────
  function openCareSettings() {
    const cr = property.care_rules || {};
    const t = cr.turnaround || {};
    const field = (label, node, help) => el("div", { class: "field" },
      el("label", {}, label), node, help ? el("div", { class: "help" }, help) : null);
    const numOrBlank = (v) => (v == null ? "" : String(v));

    const linenDay = el("input", { type: "number", min: "0", max: "60",
      value: numOrBlank(cr.linen_change_from_day ?? 8) });
    const midstay = el("select", {},
      ...[["included", "Inclus"], ["on_request", "Sur demande"], ["none", "Non proposé"]]
        .map(([k, v]) => el("option", { value: k, selected: (cr.midstay_cleaning || "on_request") === k }, v)));
    const pack = el("select", {},
      ...[["free", "Offert"], ["paid", "Payant"], ["none", "Aucun"]]
        .map(([k, v]) => el("option", { value: k, selected: (cr.welcome_pack || "free") === k }, v)));
    const packNote = el("textarea", { rows: "2" }, cr.welcome_pack_note || "");

    // Rotation. Les HEURES acceptent des décimales (step 0.5) ; les PERSONNES
    // (voyageurs, effectif de ménage) sont des ENTIERS ≥ 1 (§2.1.5 : « 6,5
    // voyageurs » n'a aucun sens) — step "1" + validation à l'enregistrement.
    const phMin = el("input", { type: "number", min: "0", step: "0.5", value: numOrBlank(t.person_hours_min_occupancy) });
    const phFull = el("input", { type: "number", min: "0", step: "0.5", value: numOrBlank(t.person_hours_full_occupancy) });
    const gMin = el("input", { type: "number", min: "1", step: "1", value: numOrBlank(t.min_occupancy_guests) });
    const gFull = el("input", { type: "number", min: "1", step: "1", value: numOrBlank(t.full_occupancy_guests) });
    const maxCleaners = el("input", { type: "number", min: "1", max: "6", step: "1", value: numOrBlank(t.max_cleaners ?? 2) });
    const margin = el("input", { type: "number", min: "0", step: "0.5", value: numOrBlank(t.comfort_margin_hours ?? 1) });
    // §2.3 — rendement de la 2ᵉ personne (le travail à plusieurs n'est pas
    // parfaitement divisible). Explicite et configurable, jamais appliqué en silence.
    const parEff = el("input", { type: "number", min: "0.1", max: "1", step: "0.05",
      value: numOrBlank(t.parallel_efficiency ?? 0.75) });
    const numVal = (inp) => (inp.value.trim() === "" ? null : Number(inp.value));
    // Champs comptant des PERSONNES : entier ≥ 1 (ou vide). Renvoie un message
    // d'erreur FR sur la première violation, sinon null.
    const validatePersonFields = () => {
      for (const [label, inp] of [["Voyageurs à faible occupation", gMin],
        ["Capacité (pleine occupation)", gFull],
        ["Nombre max de personnes au ménage", maxCleaners]]) {
        const raw = inp.value.trim();
        if (raw === "") continue;
        const n = Number(raw);
        if (!Number.isInteger(n) || n < 1) {
          return `« ${label} » doit être un nombre entier de personnes (≥ 1).`;
        }
      }
      return null;
    };

    const err = el("div", { class: "errbox hidden" });
    const catBox = el("div", { class: "cat-box" });
    renderCatalog();

    const save = el("button", { class: "btn btn-primary" }, "Enregistrer les règles");
    const modal = openModal({
      title: "Réglages d'entretien",
      body: el("form", { onSubmit: onSave },
        el("h3", { class: "care-h" }, "Règles d'entretien"),
        el("div", { class: "grid-2" },
          field("Draps changés à partir du (jour)", linenDay,
            "En cours de séjour. 0 = jamais. Villa Ballarin : 8."),
          field("Ménage en cours de séjour", midstay)),
        el("div", { class: "grid-2" },
          field("Welcome pack", pack),
          field("Note du welcome pack", packNote)),
        el("details", { class: "care-adv" },
          el("summary", {}, "Rotation — effort de nettoyage (avancé)"),
          el("p", { class: "help" },
            "La charge se mesure en hommes-heures et suit l'occupation ; l'effectif "
            + "est une décision d'équipe. Laissez vide tant que ce n'est pas mesuré."),
          el("div", { class: "grid-2" },
            field("Hommes-heures à faible occupation", phMin),
            field("Hommes-heures à pleine occupation", phFull)),
          el("div", { class: "grid-2" },
            field("Voyageurs à faible occupation", gMin, "Nombre entier."),
            field("Capacité (pleine occupation)", gFull, "Nombre entier.")),
          el("div", { class: "grid-2" },
            field("Nombre max de personnes au ménage", maxCleaners, "Nombre entier."),
            field("Marge de confort (heures)", margin)),
          field("Rendement de la 2ᵉ personne", parEff,
            "Entre 0 et 1 (défaut 0,75). Le travail à plusieurs n'est pas "
            + "parfaitement divisible : on se coordonne, certaines tâches ne se "
            + "partagent pas. À 0,75, deux personnes valent 1,75 fois une seule — "
            + "pas deux. Diviser par le nombre de personnes promettrait des "
            + "rotations intenables.")),
        el("h3", { class: "care-h" }, "Catalogue de demandes particulières"),
        el("p", { class: "help" }, "Lit bébé, chaise haute, parasol… proposés à la préparation d'un séjour."),
        catBox,
        addTypeRow(),
        err),
      footer: [
        el("button", { class: "btn btn-ghost", type: "button", onClick: () => modal.close() }, "Fermer"),
        save,
      ],
    });
    save.addEventListener("click", () => onSave());

    function renderCatalog() {
      clear(catBox);
      if (!catalog.length) {
        catBox.append(el("div", { class: "muted", style: { fontSize: "13px" } }, "Aucun type pour l'instant."));
      }
      for (const ty of catalog) {
        const toggle = el("button", { class: "btn btn-sm btn-ghost", type: "button",
          onClick: async () => {
            try {
              await api.updateRequestType(pid, ty.id, { is_active: !ty.is_active });
              catalog = await api.listRequestTypes(pid);
              renderCatalog();
            } catch (e) { toast(e.message || "Modification impossible.", "err"); }
          } },
          icon(ty.is_active ? "eye" : "eye-off", 14), ty.is_active ? "Actif" : "Masqué");
        catBox.append(el("div", { class: "cat-row" + (ty.is_active ? "" : " cat-off") },
          el("span", { class: "cat-label" }, ty.label),
          el("code", { class: "cat-code" }, ty.code), toggle));
      }
      refreshIcons();
    }

    function addTypeRow() {
      const label = el("input", { type: "text", maxlength: "120", placeholder: "Libellé (ex. Réhausseur)" });
      const btn = el("button", { class: "btn btn-sm", type: "button", onClick: onAddType },
        icon("plus", 14), "Ajouter au catalogue");
      async function onAddType() {
        const lv = label.value.trim();
        if (!lv) return;
        const code = slugCode(lv);
        btn.disabled = true;
        try {
          await api.createRequestType(pid, { code, label: lv });
          catalog = await api.listRequestTypes(pid);
          label.value = ""; renderCatalog();
        } catch (e) {
          if (e.status === 409) toast("Un type avec ce code existe déjà.", "err");
          else toast(e.message || "Ajout impossible.", "err");
        } finally { btn.disabled = false; }
      }
      return el("div", { class: "cat-add row" }, label, btn);
    }

    async function onSave(e) {
      if (e && e.preventDefault) e.preventDefault();
      err.classList.add("hidden");
      const personErr = validatePersonFields();
      if (personErr) {
        err.textContent = personErr; err.classList.remove("hidden"); return;
      }
      const care_rules = {
        ...cr,
        linen_change_from_day: linenDay.value.trim() === "" ? 0 : Math.max(0, parseInt(linenDay.value, 10) || 0),
        midstay_cleaning: midstay.value,
        welcome_pack: pack.value,
        welcome_pack_note: packNote.value.trim(),
        turnaround: {
          ...(cr.turnaround || {}),
          person_hours_min_occupancy: numVal(phMin),
          person_hours_full_occupancy: numVal(phFull),
          min_occupancy_guests: numVal(gMin),
          full_occupancy_guests: numVal(gFull),
          max_cleaners: numVal(maxCleaners) || 2,
          comfort_margin_hours: numVal(margin) ?? 1,
          parallel_efficiency: numVal(parEff) ?? 0.75,
        },
      };
      save.disabled = true; save.textContent = "Enregistrement…";
      try {
        property = await api.updateProperty(pid, { care_rules });
        modal.close();
        toast("Règles d'entretien enregistrées.", "ok");
        await reload();
      } catch (e2) {
        if (handleQuotaError(e2)) { modal.close(); return; }
        err.textContent = e2.message || "Enregistrement impossible."; err.classList.remove("hidden");
        save.disabled = false; save.textContent = "Enregistrer les règles";
      }
    }
  }

  // ── Modale séjour (saisie directe, complétion, qualification, suppression) ──
  function openBookingModal(b) {
    const isNew = !b;
    const inherited = data;
    const isImported = b && !b.is_direct;
    // Blocs miroirs choisis pour être rattachés au séjour saisi (§0.5), et
    // acquittement de l'avertissement rouge (§0.4 : la double résa = choix conscient).
    const attachIds = new Set();
    let redAck = false;
    let lastAnalysis = { red: [], neutral: [], rotations: [] };

    const field = (label, node, help) => el("div", { class: "field" },
      el("label", {}, label), node, help ? el("div", { class: "help" }, help) : null);

    const start = el("input", { type: "date", value: b ? b.starts_on : "" });
    const end = el("input", { type: "date", value: b ? b.ends_on : "" });
    const ci = el("input", { type: "time", value: b && b.checkin_time ? fmtTime(b.checkin_time) : "" });
    const co = el("input", { type: "time", value: b && b.checkout_time ? fmtTime(b.checkout_time) : "" });
    const luggageDrop = el("input", { type: "time",
      value: b && b.luggage_drop_time ? fmtTime(b.luggage_drop_time) : "" });
    const guest = el("input", { type: "text", maxlength: "200", value: b?.guest_name || "" });
    // §3.0 — coordonnées SÉPARÉES : le téléphone est une ACTION (liens Appel/
    // WhatsApp du planning), l'email en est une autre (mailto:, lien du guide). Un
    // champ fourre-tout ne pouvait devenir ni l'un ni l'autre.
    const phone = el("input", { type: "tel", maxlength: "60", inputmode: "tel",
      value: b?.guest_phone || "", placeholder: "+33 6 12 34 56 78" });
    const email = el("input", { type: "email", maxlength: "200",
      value: b?.guest_email || "", placeholder: "prenom@exemple.com" });
    // Langue du locataire : source du logement + langues PUBLIÉES du logement,
    // mais bornées au REGISTRE (V2-21a, langues publiées du produit) — une langue
    // dépubliée en base disparaît d'ici sans redéploiement. Jamais de liste en
    // dur. « non précisée » reste le défaut.
    const langOptions = [property.default_lang, ...(property.published_langs || [])]
      .filter((c, i, a) => c && a.indexOf(c) === i)
      .filter((c) => c === property.default_lang || publishedLangCodes.includes(c));
    const langSel = el("select", {},
      el("option", { value: "", selected: !b?.guest_lang }, "— non précisée —"),
      ...langOptions.map((c) => el("option",
        { value: c, selected: b?.guest_lang === c }, langLabel(c))));
    // Repli : un ancien contact fourre-tout non encore réparti reste affiché en aide.
    const legacyContact = (b && b.guest_contact && !b.guest_phone && !b.guest_email)
      ? b.guest_contact : "";
    // Surcharge du code de boîte à clés pour CE séjour (V2-23c volet 2) : vide =
    // code du logement. La valeur (chiffrée en base) n'est jamais dans BookingOut ;
    // on la charge à l'ouverture par le chemin d'édition dédié (comme les secrets).
    const keybox = el("input", { type: "text", maxlength: "100", autocomplete: "off",
      placeholder: "Code du logement par défaut" });
    // Ne jamais ÉCRASER une surcharge existante tant qu'on ne l'a pas lue : un
    // nouveau séjour part « chargé » (rien à préserver) ; un séjour existant n'est
    // « chargé » qu'après la lecture réussie (sinon on omet le champ à la sauvegarde).
    let keyboxLoaded = isNew;
    if (b) {
      api.bookingKeybox(pid, b.id)
        .then((k) => { keybox.value = k.keybox_code || ""; keyboxLoaded = true; })
        .catch(() => { /* pas de surcharge lisible : ne pas la clobber en sauvant */ });
    }
    const notes = el("textarea", { rows: "2" }, b?.notes || "");

    // ── Voyageurs (§1.0) — quantifient toute la préparation de l'équipe ───────
    const guestCount = el("input", { type: "number", min: "1", max: "100", step: "1",
      value: b && b.guest_count != null ? String(b.guest_count) : "",
      placeholder: "ex. 6" });
    let childAges = b && Array.isArray(b.children_ages) ? [...b.children_ages] : [];
    const chipRow = el("div", { class: "child-ages" });
    const ageInput = el("input", { type: "number", min: "0", max: "17",
      class: "child-age-input", placeholder: "âge" });
    const suggestLine = el("div", { class: "help care-suggest" });
    const catLabel = (code) =>
      (catalog.find((t) => t.code === code) || {}).label || code;

    function renderAges() {
      clear(chipRow);
      childAges.forEach((age, i) => {
        chipRow.append(el("span", { class: "child-chip" }, `${age} an${age > 1 ? "s" : ""}`,
          el("button", { type: "button", class: "child-chip-x", "aria-label": "Retirer",
            onClick: () => { childAges.splice(i, 1); renderAges(); } }, "×")));
      });
      chipRow.append(el("span", { class: "child-add-inline" }, ageInput,
        el("button", { type: "button", class: "btn btn-sm btn-ghost",
          onClick: addAge }, icon("plus", 13))));
      // Suggestions d'équipement d'après les âges (propose, n'ajoute jamais).
      clear(suggestLine);
      const codes = suggestEquipment(childAges, property.care_rules);
      if (codes.length) {
        suggestLine.append("Suggéré d'après les âges : " + codes.map(catLabel).join(", ")
          + (b ? " — ajoutez-les en demandes ci-dessous." : "."));
      }
      refreshIcons();
    }
    function addAge() {
      const v = ageInput.value.trim();
      if (v === "") return;
      const n = Math.max(0, Math.min(17, parseInt(v, 10) || 0));
      childAges.push(n); ageInput.value = ""; renderAges(); ageInput.focus();
    }
    ageInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); addAge(); }
    });
    renderAges();

    const sourceSel = el("select", {},
      ...SOURCES.map(([k, v]) => el("option", { value: k, selected: (b?.source || "direct") === k }, v)));
    // Nature : la sémantique du séjour (elle pilote la préparation, jamais le statut).
    // Repli PRUDENT (§0.h) : un NOUVEAU séjour saisi à la main est une réservation
    // par défaut (intention du propriétaire, cohérent avec BookingIn) ; mais un
    // séjour EXISTANT dont la nature manquerait ne doit JAMAIS retomber sur la
    // valeur la plus conséquente — il retombe sur « à qualifier », le moins actif.
    const natureDefault = isNew ? "reservation" : (b.nature || "unqualified");
    const natureSel = el("select", {},
      ...NATURE_OPTIONS.map(([k, v]) =>
        el("option", { value: k, selected: natureDefault === k }, v)));
    const natureHelp = el("div", { class: "help" },
      "Un séjour occupé (réservation, séjour privé) déclenche les alertes de "
      + "chevauchement et la préparation par l'équipe.");

    const warnBox = el("div", { class: "cal-warn-box" });
    const err = el("div", { class: "errbox hidden" });
    const inDefault = fmtTime(inherited.default_checkin_time);
    const outDefault = fmtTime(inherited.default_checkout_time);
    ci.placeholder = inDefault; co.placeholder = outDefault;

    // §0.3 — auto-promotion : dès qu'un nom est saisi sur un séjour « à qualifier »,
    // la nature bascule VISIBLEMENT sur « Réservation » (réversible d'un clic).
    guest.addEventListener("input", () => {
      if (guest.value.trim() && natureSel.value === "unqualified") {
        natureSel.value = "reservation";
        natureSel.classList.add("cal-nat-flash");
        setTimeout(() => natureSel.classList.remove("cal-nat-flash"), 900);
      }
    });
    // §0.4 — l'avertissement se recalcule à chaque changement de dates/heures.
    for (const inp of [start, end, ci, co]) {
      inp.addEventListener("input", () => { redAck = false; recomputeWarnings(); });
    }

    const rows = [
      successionNote(),
      el("div", { class: "grid-2" }, field("Arrivée", start), field("Départ", end)),
      datesFeedNote(),
      warnBox,
      field("Nature du séjour", natureSel), natureHelp,
      el("div", { class: "grid-2" },
        field("Heure d'arrivée", ci, `Par défaut ${inDefault} (heure standard)`),
        field("Heure de départ", co, `Par défaut ${outDefault}`)),
      field("Dépôt de bagages avant l'entrée (facultatif)", luggageDrop,
        "Heure à laquelle la maison doit être accessible et présentable."),
      el("div", { class: "grid-2" },
        field("Nombre de voyageurs", guestCount,
          "Total, enfants compris. Quantifie draps, serviettes et welcome pack."),
        field("Âges des enfants à l'arrivée",
          el("div", {}, chipRow), "Le nombre d'enfants s'en déduit.")),
      suggestLine,
      field("Nom du locataire", guest),
      el("div", { class: "grid-2" },
        field("Téléphone du locataire", phone,
          "Forme internationale (+33…) : l'équipe compose depuis l'Espagne. Sert aux liens Appel et WhatsApp du planning."),
        field("Email du locataire", email)),
      legacyContact
        ? el("div", { class: "help", style: { marginTop: "-6px" } },
          `Ancien contact enregistré : ${legacyContact} — répartissez-le ci-dessus.`)
        : null,
      field("Langue du locataire", langSel,
        "L'équipe sait comment l'aborder ; le lien du guide part dans cette langue."),
      field("Code de boîte à clés pour ce séjour", keybox,
        "Laissez vide pour utiliser celui du logement. Il n'est visible que par le "
        + "lien de séjour et expire avec lui."),
      field("Notes", notes),
    ];
    // Demandes particulières : uniquement sur un séjour déjà enregistré (il faut
    // son id pour rattacher). Chargées à l'ouverture (§1.2).
    if (b) rows.push(requestsSection(b));
    // La source n'est éditable que pour une saisie directe (un import garde la sienne).
    if (isNew || (b && b.is_direct)) rows.splice(2, 0, field("Origine", sourceSel));

    const save = el("button", { class: "btn btn-primary" }, isNew ? "Créer le séjour" : "Enregistrer");
    const footer = [
      el("button", { class: "btn btn-ghost", type: "button", onClick: () => modal.close() }, "Annuler"),
    ];
    if (b) footer.unshift(el("button", {
      class: "btn btn-danger btn-ghost", type: "button", style: { marginRight: "auto" },
      onClick: () => removeBooking(b),
    }, icon("trash-2", 15), b.is_direct ? "Supprimer" : "Annuler ce séjour"));
    footer.push(save);

    const modal = openModal({
      title: isNew ? "Nouveau séjour" : (isImported ? "Compléter le séjour" : "Séjour"),
      body: el("form", { onSubmit: onSave }, ...rows, err),
      footer,
    });
    save.addEventListener("click", () => onSave());
    recomputeWarnings();

    // ── Dates ajustées à la main vs flux (V2-23g) ────────────────────────────
    // Badge SOBRE « Dates ajustées » sur la fiche dès que le propriétaire a repris
    // les dates d'un séjour importé (même sans divergence) ; quand le flux annonce
    // désormais autre chose, on l'EXPOSE (protéger sans signaler masquerait une
    // vraie modification côté plateforme) + action « Reprendre les dates du flux »
    // qui rend la main à la synchro. Langage humain, jamais d'UUID (principe 0.4).
    // ── Succession d'identifiants (V2-23h) ───────────────────────────────────
    // Ce séjour (nouvel import vierge) remplace peut-être un prédécesseur ANNULÉ par
    // une modification de réservation côté plateforme. On PROPOSE la reprise de sa
    // fiche (jamais l'automatisme) : le message est construit côté serveur (langage
    // humain, jamais d'uid) ; le bouton cible source_id. La reprise n'hérite JAMAIS
    // du registre d'envois → le nouveau lien /b/ sera VIVANT.
    function successionNote() {
      if (!b || !b.succession) return null;
      const box = el("div", { class: "cal-succession" });
      box.append(el("div", { class: "cal-succession-head" },
        icon("history", 15),
        el("b", {}, "Reprise de fiche possible")));
      box.append(el("div", { class: "cal-succession-msg" }, b.succession.message));
      const btn = el("button", { class: "btn btn-sm btn-primary", type: "button" },
        icon("history", 14), "Reprendre la fiche");
      btn.onclick = async () => {
        btn.disabled = true;
        try {
          await api.inheritBooking(pid, b.id, b.succession.source_id);
          modal.close();
          toast("Fiche reprise du séjour précédent.", "ok");
          await reload();
        } catch (e) {
          btn.disabled = false;
          if (!handleQuotaError(e)) toast(e.message || "Reprise impossible.", "err");
        }
      };
      box.append(btn);
      return box;
    }

    function datesFeedNote() {
      if (!b || !b.dates_overridden) return null;
      const diverge = datesDiverge(b);
      const box = el("div",
        { class: "cal-datesadj" + (diverge ? " cal-datesadj-diverge" : "") });
      box.append(el("div", { class: "cal-datesadj-head" },
        el("span", { class: "cal-datesadj-badge" },
          icon("calendar-clock", 13), "Dates ajustées"),
        el("span", { class: "muted", style: { fontSize: "12.5px" } },
          "La synchronisation ne les remplacera plus.")));
      if (diverge) {
        box.append(el("div", { class: "cal-datesadj-msg" },
          `Le flux ${SOURCE_LABEL[b.source] || b.source} indique désormais `,
          el("b", {}, fmtRange(b.feed_starts_on, b.feed_ends_on)),
          ` (vous avez saisi ${fmtRange(b.starts_on, b.ends_on)}).`));
      }
      if (b.feed_starts_on) {
        const resetBtn = el("button",
          { class: "btn btn-sm btn-ghost", type: "button" },
          icon("rotate-ccw", 14), "Reprendre les dates du flux");
        resetBtn.onclick = async () => {
          resetBtn.disabled = true;
          try {
            await api.resetBookingDates(pid, b.id);
            modal.close();
            toast("Dates du flux reprises.", "ok");
            await reload();
          } catch (e) {
            resetBtn.disabled = false;
            if (!handleQuotaError(e)) toast(e.message || "Reprise impossible.", "err");
          }
        };
        box.append(resetBtn);
      }
      return box;
    }

    // ── Avertissement de chevauchement à la saisie (§0.4/§0.5) ───────────────
    function recomputeWarnings() {
      clear(warnBox);
      const candidate = {
        id: b?.id, starts_on: start.value, ends_on: end.value,
        checkin_time: ci.value || null, checkout_time: co.value || null,
      };
      const a = analyzeCandidate(candidate, data.bookings, {
        defaultCheckin: inDefault, defaultCheckout: outDefault });
      lastAnalysis = a;
      const effectiveRed = a.red.filter((x) => !attachIds.has(x.id));

      if (effectiveRed.length) {
        warnBox.append(warnBlock("cal-warn-red", "triangle-alert",
          effectiveRed.length === 1 ? "Chevauchement avec une occupation"
            : `${effectiveRed.length} chevauchements avec des occupations`,
          effectiveRed));
      }
      if (a.neutral.length) {
        warnBox.append(warnBlock("cal-warn-neutral", "info",
          "Recouvre une période non occupée", a.neutral));
      }
      for (const r of a.rotations) {
        warnBox.append(el("div", { class: "cal-warn cal-warn-info" },
          icon("arrow-right-left", 15),
          el("div", {},
            el("b", {}, `Rotation le ${fmtDayLong(r.on)}`),
            el("div", { class: "muted", style: { fontSize: "12.5px" } },
              `Fenêtre de préparation ${fmtGap(r.gap_minutes)} avec ${describe(r.booking)}.`))));
      }
      // Le libellé du bouton reste neutre ; le 2e clic n'est demandé qu'au moment
      // de l'enregistrement (voir onSave) pour ne pas dévoiler l'issue trop tôt.
      if (!save.disabled) save.textContent = isNew ? "Créer le séjour" : "Enregistrer";
    }

    function warnBlock(cls, ic, title, list) {
      const items = el("div", { class: "cal-warn-items" });
      for (const x of list) {
        const line = el("div", { class: "cal-warn-item" },
          el("span", {}, describe(x)));
        const att = attachBtn(x);
        if (att) line.append(att);
        items.append(line);
      }
      return el("div", { class: "cal-warn " + cls }, icon(ic, 15),
        el("div", {}, el("b", {}, title), items));
    }

    function describe(x) {
      const who = x.guest_name ? ` · ${x.guest_name}` : "";
      const src = x.is_direct ? "" : ` · ${SOURCE_LABEL[x.source] || x.source}`;
      return `${fmtRange(x.starts_on, x.ends_on)} — ${natureLabel(x.nature)}${who}${src}`;
    }

    // Rattacher un bloc miroir importé au séjour saisi (§0.5). Le bloc n'est jamais
    // supprimé : il sera masqué (linked_booking_id) après l'enregistrement.
    function attachBtn(x) {
      if (x.is_direct) return null;
      const on = attachIds.has(x.id);
      const btn = el("button", { class: "btn btn-sm btn-ghost", type: "button" },
        icon(on ? "link-2" : "link", 13), on ? "Rattaché ✓" : "Rattacher ce bloc");
      btn.onclick = () => {
        if (attachIds.has(x.id)) attachIds.delete(x.id);
        else { attachIds.add(x.id); redAck = false; }
        recomputeWarnings();
      };
      return btn;
    }

    // ── Demandes particulières du séjour (§1.2) ──────────────────────────────
    function requestsSection(bk) {
      const list = el("div", { class: "req-list" }, el("div", { class: "muted", style: { fontSize: "13px" } }, "Chargement…"));
      const active = catalog.filter((t) => t.is_active);
      const typeSel = el("select", {},
        ...active.map((t) => el("option", { value: t.id }, t.label)));
      const qty = el("input", { type: "number", min: "1", max: "50", value: "1", class: "req-qty" });
      const addBtn = el("button", { class: "btn btn-sm", type: "button", onClick: onAdd },
        icon("plus", 14), "Ajouter");
      const adder = active.length
        ? el("div", { class: "req-add row" }, typeSel, qty, addBtn)
        : el("div", { class: "muted", style: { fontSize: "13px" } },
          "Aucun type de demande actif. Ajoutez-en dans « Réglages d'entretien ».");

      loadRequests();
      async function loadRequests() {
        try {
          const reqs = await api.listBookingRequests(pid, bk.id);
          clear(list);
          if (!reqs.length) {
            list.append(el("div", { class: "muted", style: { fontSize: "13px" } },
              "Aucune demande pour ce séjour."));
          }
          for (const r of reqs) list.append(reqRow(r));
          refreshIcons();
        } catch { clear(list); list.append(el("div", { class: "muted" }, "Chargement impossible.")); }
      }
      function reqRow(r) {
        const org = r.origin === "guest"
          ? el("span", { class: "badge req-guest", title: "Demande du voyageur" }, "voyageur")
          : null;
        // §3.1 — une demande EN ATTENTE (typiquement du voyageur) s'accepte ou se
        // refuse : acceptée, elle devient une intervention visible par l'équipe.
        const setStatus = async (status) => {
          try { await api.updateBookingRequest(pid, r.id, { status }); await loadRequests(); }
          catch (e) { toast(e.message || "Mise à jour impossible.", "err"); }
        };
        const actions = [];
        if (r.status === "pending") {
          actions.push(
            el("button", { class: "btn btn-sm btn-primary", type: "button",
              onClick: () => setStatus("accepted") }, "Accepter"),
            el("button", { class: "btn btn-sm btn-ghost", type: "button",
              onClick: () => setStatus("declined") }, "Refuser"));
        }
        actions.push(el("button", { class: "btn btn-sm btn-ghost", type: "button",
          "aria-label": "Retirer",
          onClick: async () => {
            try { await api.deleteBookingRequest(pid, r.id); await loadRequests(); }
            catch (e) { toast(e.message || "Suppression impossible.", "err"); }
          } }, icon("trash-2", 14)));
        // Note du voyageur affichée sous la ligne (le « pourquoi » de la demande).
        const note = r.note
          ? el("div", { class: "req-note muted", style: { fontSize: "12.5px" } }, `« ${r.note} »`)
          : null;
        return el("div", { class: "req-row-wrap" },
          el("div", { class: "req-row" },
            el("span", { class: "req-label" }, `${r.label}${r.quantity > 1 ? " ×" + r.quantity : ""}`),
            org,
            el("span", { class: "req-status req-" + r.status }, statusLabel(r.status)),
            ...actions),
          note);
      }
      async function onAdd() {
        if (!active.length) return;
        addBtn.disabled = true;
        try {
          await api.createBookingRequest(pid, bk.id, {
            request_type_id: typeSel.value,
            quantity: Math.max(1, parseInt(qty.value, 10) || 1),
            status: "accepted" });
          qty.value = "1";
          await loadRequests();
        } catch (e) { if (!handleQuotaError(e)) toast(e.message || "Ajout impossible.", "err"); }
        finally { addBtn.disabled = false; }
      }
      return el("div", { class: "req-section" },
        el("label", {}, "Demandes particulières"), list, adder);
    }

    function statusLabel(s) {
      return { pending: "en attente", accepted: "acceptée", declined: "refusée" }[s] || s;
    }

    async function onSave(e) {
      if (e && e.preventDefault) e.preventDefault();
      err.classList.add("hidden");
      if (!start.value || !end.value) {
        err.textContent = "Renseignez l'arrivée et le départ."; err.classList.remove("hidden"); return;
      }
      if (end.value <= start.value) {
        err.textContent = "Le départ doit être postérieur à l'arrivée."; err.classList.remove("hidden"); return;
      }
      // §2.1.5 — le nombre de voyageurs compte des PERSONNES : entier ≥ 1 (ou vide).
      if (guestCount.value.trim() !== "") {
        const gc = Number(guestCount.value);
        if (!Number.isInteger(gc) || gc < 1) {
          err.textContent = "Le nombre de voyageurs doit être un entier ≥ 1 (ou vide).";
          err.classList.remove("hidden"); return;
        }
      }
      // §0.4 — le cas ROUGE (double réservation) demande un second clic conscient.
      const effectiveRed = lastAnalysis.red.filter((x) => !attachIds.has(x.id));
      if (effectiveRed.length && !redAck) {
        redAck = true;
        save.textContent = "Enregistrer quand même";
        err.textContent = "Double réservation avec une occupation existante. "
          + "Cliquez à nouveau pour l'enregistrer volontairement.";
        err.classList.remove("hidden");
        return;
      }
      const payload = {
        starts_on: start.value, ends_on: end.value,
        checkin_time: ci.value || null, checkout_time: co.value || null,
        luggage_drop_time: luggageDrop.value || null,
        guest_name: guest.value.trim() || null,
        guest_phone: phone.value.trim() || null,
        guest_email: email.value.trim() || null,
        guest_lang: langSel.value || null,
        notes: notes.value.trim() || null,
        nature: natureSel.value,
        guest_count: guestCount.value.trim() === "" ? null : parseInt(guestCount.value, 10),
        children_ages: childAges,
      };
      // La surcharge n'est envoyée que si on a pu la lire (sinon on ne la touche
      // pas : omettre le champ laisse le PATCH la préserver côté serveur).
      if (keyboxLoaded) payload.keybox_code = keybox.value.trim() || null;
      save.disabled = true; save.textContent = "Enregistrement…";
      try {
        let bookingId;
        if (isNew) {
          payload.source = sourceSel.value;
          bookingId = (await api.createBooking(pid, payload)).id;
        } else {
          if (b.is_direct) payload.source = sourceSel.value;
          await api.updateBooking(pid, b.id, payload);
          bookingId = b.id;
        }
        // Rattachement des blocs miroirs choisis (best-effort : un échec ne perd
        // pas le séjour déjà enregistré).
        const toAttach = [...attachIds].filter((id) =>
          lastAnalysis.red.concat(lastAnalysis.neutral).some((x) => x.id === id));
        for (const id of toAttach) {
          try { await api.updateBooking(pid, id, { linked_booking_id: bookingId }); }
          catch { /* le bloc reviendra à la prochaine synchro : non bloquant */ }
        }
        modal.close();
        toast(isNew ? "Séjour créé." : "Séjour enregistré.", "ok");
        await reload();
      } catch (e2) {
        if (handleQuotaError(e2)) { modal.close(); return; }
        err.textContent = e2.message || "Enregistrement impossible."; err.classList.remove("hidden");
        save.disabled = false; save.textContent = isNew ? "Créer le séjour" : "Enregistrer";
      }
    }
  }

  async function removeBooking(b) {
    const direct = b.is_direct;
    if (!(await confirmDialog(
      direct ? "Supprimer définitivement cette location directe ?"
        : "Marquer ce séjour importé comme annulé ? (il est conservé, jamais supprimé)",
      { title: direct ? "Supprimer le séjour" : "Annuler le séjour",
        okLabel: direct ? "Supprimer" : "Annuler ce séjour", danger: true }))) return;
    try {
      const r = await api.deleteBooking(pid, b.id);
      toast(r.outcome === "deleted" ? "Séjour supprimé." : "Séjour annulé.", "ok");
      // Ferme la modale du séjour restée ouverte puis recharge.
      document.querySelectorAll(".modal-back").forEach((n) => n.remove());
      await reload();
    } catch (err) { if (!handleQuotaError(err)) toast(err.message || "Suppression impossible.", "err"); }
  }
}
