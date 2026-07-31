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
const OCCUPIED = new Set(["reservation", "private"]);
const MONTHS = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.",
  "août", "sept.", "oct.", "nov.", "déc."];

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

  let property, data;
  try {
    [property, data] = await Promise.all([
      api.getProperty(pid), api.calendarView(pid)]);
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
        el("button", { class: "btn btn-sm", id: "cal-sync", onClick: () => syncNow() },
          icon("refresh-cw", 16), "Synchroniser"),
        el("button", { class: "btn btn-primary btn-sm", onClick: () => openBookingModal() },
          icon("plus", 16), "Nouveau séjour"))),
    body);
  mount(view, page);
  paint();

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

    // Rotations mises en évidence.
    for (const r of data.rotations || []) {
      const out = bookings.find((b) => b.id === r.departing);
      const inn = bookings.find((b) => b.id === r.arriving);
      if (!out || !inn) continue;
      body.append(el("div", { class: "cal-rotation" }, icon("arrow-right-left", 17),
        el("div", {},
          el("b", {}, `Rotation le ${fmtDayLong(r.on)}`),
          el("div", { class: "muted", style: { fontSize: "13px" } },
            `Sortie ${fmtTime(out.eff_checkout_time)} → entrée ${fmtTime(inn.eff_checkin_time)} — fenêtre ${fmtGap(r.gap_minutes)}`))));
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
      NATURE_LABEL[b.nature] || b.nature);

    const meta = el("div", { class: "row", style: { gap: "8px", flexWrap: "wrap", alignItems: "center" } },
      platBadge, natureBadge,
      overlap ? el("span", { class: "cal-flag cal-flag-overlap", title: "Chevauchement" },
        icon("triangle-alert", 13), "Chevauchement") : null,
      rot ? el("span", { class: "cal-flag cal-flag-rot", title: "Rotation même jour" },
        icon("arrow-right-left", 13), rot.role === "out" ? "Départ le jour d'une arrivée" : "Arrivée le jour d'un départ") : null);

    const times = el("div", { class: "muted booking-times" },
      `Arrivée ${fmtTime(b.eff_checkin_time)}${b.checkin_time ? " (ajustée)" : ""} · `
      + `Départ ${fmtTime(b.eff_checkout_time)}${b.checkout_time ? " (ajustée)" : ""}`);

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
      el("div", { class: "booking-body" }, who, meta, times),
      cta);
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
    const contact = el("input", { type: "text", maxlength: "200", value: b?.guest_contact || "" });
    const notes = el("textarea", { rows: "2" }, b?.notes || "");
    const sourceSel = el("select", {},
      ...SOURCES.map(([k, v]) => el("option", { value: k, selected: (b?.source || "direct") === k }, v)));
    // Nature : la sémantique du séjour (elle pilote la préparation, jamais le statut).
    const natureSel = el("select", {},
      ...NATURE_OPTIONS.map(([k, v]) =>
        el("option", { value: k, selected: (b?.nature || "reservation") === k }, v)));
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
      el("div", { class: "grid-2" }, field("Arrivée", start), field("Départ", end)),
      warnBox,
      field("Nature du séjour", natureSel), natureHelp,
      el("div", { class: "grid-2" },
        field("Heure d'arrivée", ci, `Par défaut ${inDefault} (heure standard)`),
        field("Heure de départ", co, `Par défaut ${outDefault}`)),
      field("Dépôt de bagages avant l'entrée (facultatif)", luggageDrop,
        "Heure à laquelle la maison doit être accessible et présentable."),
      field("Nom du locataire", guest),
      field("Contact (téléphone / email)", contact),
      field("Notes", notes),
    ];
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
      return `${fmtRange(x.starts_on, x.ends_on)} — ${NATURE_LABEL[x.nature] || x.nature}${who}${src}`;
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

    async function onSave(e) {
      if (e && e.preventDefault) e.preventDefault();
      err.classList.add("hidden");
      if (!start.value || !end.value) {
        err.textContent = "Renseignez l'arrivée et le départ."; err.classList.remove("hidden"); return;
      }
      if (end.value <= start.value) {
        err.textContent = "Le départ doit être postérieur à l'arrivée."; err.classList.remove("hidden"); return;
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
        guest_contact: contact.value.trim() || null,
        notes: notes.value.trim() || null,
        nature: natureSel.value,
      };
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
