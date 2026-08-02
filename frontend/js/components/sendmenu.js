/* Fenêtre « Envoyer le guide » (V2-23c, volet 3) — LA surface d'envoi du produit.

   Ouverte depuis la carte du logement (« Mes logements »), elle réunit les TROIS
   liens (grammaire actée du guide) et leurs canaux :

     · SÉJOUR  (`/b/{stay_token}`)  — un locataire réservé : à venir + en cours
       (jamais l'historique). Langue pré-sélectionnée depuis `guest_lang`, email
       et téléphone lus de la fiche. Le `guide_token` éternel ne voyage jamais.
     · VITRINE (`/v/{showcase_token}`) — prospect/annonce : secrets d'exemple.
     · MAISON  (`/g/{guide_token}`)  — le QR imprimé (partage multilingue actuel).

   Le token de séjour/vitrine est généré À LA DEMANDE côté serveur (idempotent),
   jamais par une route publique. Les gabarits email/WhatsApp sont localisés via
   l'inventaire i18n (`ui.send_*`) — aucun libellé n'est codé ici.

   Le manque devient une invitation (§3.3) : un séjour sans email/téléphone désactive
   le canal et propose « Ajouter un email à ce séjour » (ouvre la modale du séjour),
   jamais un `mailto:` vide. */

import { el, icon, clear, openModal, toast, refreshIcons } from "../ui.js";
import { api } from "../api.js";
import { navigate } from "../nav.js";
import { qrCanvas } from "../../guide/qr.js";
import { slugify, guideShareUrl } from "../share.js";
import { publishedLanguages } from "../languages.js";
import { openBookingOnLoad } from "../views/calendar.js";

// Drapeau décoratif d'un code langue (repli : rien — le nom natif suffit).
const FLAGS = { fr: "🇫🇷", en: "🇬🇧", es: "🇪🇸", de: "🇩🇪", nl: "🇳🇱",
  it: "🇮🇹", pt: "🇵🇹", sq: "🇦🇱" };

function todayISO() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}
const pad = (n) => String(n).padStart(2, "0");

/* « 08–22.08 » (même mois) ou « 28.07–03.08 » (mois différents). */
function fmtRange(startIso, endIso) {
  const [, sm, sd] = startIso.split("-");
  const [, em, ed] = endIso.split("-");
  return sm === em ? `${sd}–${ed}.${em}` : `${sd}.${sm}–${ed}.${em}`;
}

function firstName(name) {
  return (name || "").trim().split(/\s+/)[0] || "";
}

// Chiffres d'un numéro pour wa.me (jamais d'espaces ni de signe). Vide si absent.
function waDigits(phone) { return String(phone || "").replace(/\D/g, ""); }

export async function openSendMenu(property) {
  const pid = property.id;
  const defLang = (property.default_lang || "fr").toLowerCase();

  // Langues offertes par CE guide : source + publiées, croisées au registre
  // (publishedLanguages fait foi — jamais de liste en dur, invariant 15). Défaut
  // en tête.
  let offered;
  try {
    const registry = await publishedLanguages();          // [{code, name_native}]
    const byCode = new Map(registry.map((l) => [l.code, l]));
    const wanted = [defLang, ...(property.published_langs || []).map((c) => c.toLowerCase())]
      .filter((c, i, a) => a.indexOf(c) === i && byCode.has(c));
    offered = wanted.map((c) => byCode.get(c));
    if (!offered.length) offered = [{ code: defLang, name_native: defLang.toUpperCase() }];
  } catch (_) {
    offered = [{ code: defLang, name_native: defLang.toUpperCase() }];
  }
  const offeredCodes = new Set(offered.map((l) => l.code));

  // Séjours envoyables : à venir + en cours (départ ≥ aujourd'hui), jamais annulés,
  // jamais l'historique, et jamais les natures SANS locataire — works/unavailable
  // exclus (amendé 02/08 après relecture de 7b810a3). `unqualified` reste listé
  // (import brut souvent un vrai locataire pas encore qualifié), au même titre que
  // reservation/private (taxonomie nature de V2-23a, invariant 14).
  const NATURES_SANS_LOCATAIRE = new Set(["works", "unavailable"]);
  let bookings = [];
  try {
    const cal = await api.calendarView(pid);
    const today = todayISO();
    bookings = (cal.bookings || [])
      .filter((b) => b.status !== "cancelled" && b.ends_on >= today
        && !NATURES_SANS_LOCATAIRE.has(b.nature))
      .sort((a, b) => a.starts_on.localeCompare(b.starts_on));
  } catch (_) { /* la vitrine et la maison restent disponibles sans calendrier */ }

  // ── État de la fenêtre ────────────────────────────────────────────────────
  let target = bookings.length ? "stay" : "showcase";   // séjour par défaut s'il y en a
  let booking = bookings[0] || null;
  let lang = pickLang();
  const linkCache = {};   // baseUrl mémorisé par cible (token généré une fois)
  const tplCache = {};    // gabarits d'envoi mémorisés par langue
  const channels = el("div", { class: "send-channels" });  // canaux (rerendus)

  function pickLang() {
    if (target === "stay" && booking) {
      const gl = (booking.guest_lang || "").toLowerCase();
      return offeredCodes.has(gl) ? gl : defLang;
    }
    return defLang;
  }

  // ── Squelette ─────────────────────────────────────────────────────────────
  const targetBar = el("div", { class: "send-targets" });
  const panel = el("div", { class: "send-panel" });
  const body = el("div", { class: "send-menu" }, targetBar, panel);
  const modal = openModal({
    title: "Envoyer le guide", body,
    footer: [el("button", { class: "btn btn-ghost", type: "button", onClick: () => modal.close() }, "Fermer")],
  });

  renderTargets();
  renderPanel();

  function renderTargets() {
    clear(targetBar);
    const opt = (t, ic, label) => el("button", {
      class: "send-target" + (target === t ? " active" : ""), type: "button",
      onClick: () => { if (target !== t) { target = t; if (t === "stay") booking = bookings[0] || null; lang = pickLang(); renderTargets(); renderPanel(); } },
    }, icon(ic, 16), label);
    targetBar.append(
      opt("stay", "user-round", "Un séjour"),
      opt("showcase", "eye", "Vitrine"),
      opt("house", "printer", "Lien maison"));
    refreshIcons();
  }

  function renderPanel() {
    clear(panel);
    if (target === "stay") {
      if (!bookings.length) {
        panel.append(el("div", { class: "notice notice-info" }, icon("info", 18),
          el("div", {}, "Aucun séjour à venir. ",
            el("a", { href: `#/properties/${pid}/calendrier`, onClick: () => modal.close() },
              "Ajoutez un séjour au calendrier"), " pour lui envoyer un lien personnalisé.")));
        refreshIcons();
        return;
      }
      panel.append(bookingSelector());
    } else {
      panel.append(el("p", { class: "muted", style: { marginTop: 0 } },
        target === "showcase"
          ? "Lien de démonstration à montrer à un prospect ou dans une annonce : les vrais codes (wifi, boîte à clés) sont remplacés par des exemples."
          : "Le lien de la maison — celui du QR imprimé, à réutiliser tel quel. Choisissez la langue à partager."));
    }
    panel.append(langRow(), channelsBox());
    refreshIcons();
    refreshChannels();
  }

  // Sélecteur de séjour (destinataire = le séjour, rien à retaper).
  function bookingSelector() {
    const sel = el("select", { class: "send-booking",
      onChange: (e) => { booking = bookings.find((b) => b.id === e.target.value) || null; lang = pickLang(); renderPanel(); } });
    for (const b of bookings) {
      const who = b.guest_name || "Séjour sans nom";
      const flag = FLAGS[(b.guest_lang || "").toLowerCase()] || "";
      sel.append(el("option", { value: b.id, selected: booking && b.id === booking.id },
        `${who} · ${fmtRange(b.starts_on, b.ends_on)}${flag ? " · " + flag : ""}`));
    }
    return el("div", { class: "field" }, el("label", {}, "Séjour à qui envoyer le guide"), sel);
  }

  // Pastilles de langue — pré-sélection depuis guest_lang (séjour), modifiable.
  function langRow() {
    const row = el("div", { class: "send-langs" });
    for (const l of offered) {
      row.append(el("button", {
        class: "send-lang" + (l.code === lang ? " active" : ""), type: "button",
        onClick: () => { if (lang !== l.code) { lang = l.code; renderPanel(); } },
      }, (FLAGS[l.code] ? FLAGS[l.code] + " " : "") + l.name_native));
    }
    return el("div", { class: "field" },
      el("label", {}, "Langue du guide envoyé"), row);
  }

  // ── Canaux (lien / QR / email / WhatsApp) ────────────────────────────────
  function channelsBox() { return channels; }

  async function refreshChannels() {
    clear(channels);
    channels.append(el("div", { class: "muted", style: { fontSize: "13px" } }, "Préparation du lien…"));
    let baseUrl;
    try {
      baseUrl = await currentBaseUrl();
    } catch (err) {
      clear(channels);
      channels.append(el("div", { class: "errbox" }, err.message || "Lien indisponible."));
      return;
    }
    // La langue choisie force le rendu du guide (`?lang=`) sauf si c'est déjà la
    // langue par défaut du lien (guest_lang pour un séjour, langue du logement sinon).
    const natural = target === "stay" && booking && offeredCodes.has((booking.guest_lang || "").toLowerCase())
      ? (booking.guest_lang || "").toLowerCase() : defLang;
    const url = target === "house"
      ? guideShareUrl(property, lang)                         // slug + ?lang gérés par share.js
      : (lang === natural ? baseUrl : `${baseUrl}?lang=${encodeURIComponent(lang)}`);

    const tpl = await templates(lang);

    clear(channels);
    channels.append(linkRow(url), qrRow(url), emailRow(url, tpl), whatsappRow(url, tpl));
    refreshIcons();
  }

  // Base du lien (token généré une seule fois côté serveur, puis mémorisé).
  async function currentBaseUrl() {
    if (target === "house") return guideShareUrl(property, defLang);
    const key = target === "stay" ? `stay:${booking.id}` : "showcase";
    if (linkCache[key]) return linkCache[key];
    const r = target === "stay"
      ? await api.stayLink(pid, booking.id)
      : await api.showcaseLink(pid);
    linkCache[key] = r.url;
    return r.url;
  }

  async function templates(lg) {
    if (tplCache[lg]) return tplCache[lg];
    try { tplCache[lg] = await api.sendTemplates(lg); }
    catch (_) { tplCache[lg] = await api.sendTemplates(defLang).catch(() => null); }
    return tplCache[lg];
  }

  function linkRow(url) {
    const input = el("input", { type: "text", value: url, readonly: true,
      onFocus: (e) => e.target.select() });
    const copy = el("button", { class: "btn btn-sm", type: "button",
      onClick: async () => {
        try { await navigator.clipboard.writeText(url); toast("Lien copié.", "ok"); }
        catch (_) { toast("Copie impossible — copiez le lien manuellement.", "err"); }
      } }, icon("link", 15), "Copier");
    const open = el("a", { class: "btn btn-sm", href: url, target: "_blank", rel: "noopener" },
      icon("external-link", 15), "Ouvrir");
    return el("div", { class: "field" }, el("label", {}, "Lien à partager"),
      el("div", { class: "row", style: { gap: "8px", flexWrap: "wrap" } }, input, copy, open));
  }

  function qrRow(url) {
    const canvas = qrCanvas(url, { scale: 6, label: "QR du guide" });
    if (!canvas) return el("div", {});   // lien trop long pour un QR (rare) → pas de bloc
    const dl = el("button", { class: "btn btn-sm", type: "button",
      onClick: () => downloadQr(canvas) }, icon("download", 15), "Télécharger le QR (PNG)");
    return el("div", { class: "qr-share send-qr" }, canvas, dl);
  }

  function downloadQr(canvas) {
    const who = target === "stay" ? slugify(firstName(booking.guest_name) || booking.id)
      : target === "showcase" ? "vitrine" : null;
    const name = "guide-" + slugify(property.name) + (who ? "-" + who : "") + ".png";
    try {
      const a = el("a", { href: canvas.toDataURL("image/png"), download: name });
      document.body.appendChild(a); a.click(); a.remove();
      toast("QR téléchargé.", "ok");
    } catch (_) { toast("Téléchargement impossible.", "err"); }
  }

  // Message localisé (le greeting réutilise ui.send_hello / _generic ; jamais de
  // libellé en dur). {name} = prénom du locataire (séjour), sinon salutation neutre.
  function compose(url, tpl) {
    const who = target === "stay" ? firstName(booking.guest_name) : "";
    const greeting = tpl
      ? (who ? tpl.hello.replace("{name}", who) : tpl.hello_generic)
      : (who ? `Bonjour ${who},` : "Bonjour,");
    const intro = tpl ? tpl.intro : "Voici le lien de votre guide :";
    const signoff = tpl ? tpl.signoff : "";
    const subject = (tpl ? tpl.subject : "Votre guide — {property}").replace("{property}", property.name);
    const emailBody = `${greeting}\n\n${intro}\n${url}${signoff ? "\n\n" + signoff : ""}`;
    const shortMsg = `${greeting} ${intro} ${url}`;
    return { subject, emailBody, shortMsg };
  }

  function emailRow(url, tpl) {
    const { subject, emailBody } = compose(url, tpl);
    // Séjour sans email → invitation (§3.3), jamais un mailto vide. Vitrine/maison
    // n'ont pas de destinataire de fiche → mailto sans « À » (l'hôte le saisit).
    const email = target === "stay" ? (booking.guest_email || "") : "";
    if (target === "stay" && !email) {
      return inviteRow("mail", "Ajouter un email à ce séjour",
        "Ce séjour n'a pas d'email — ajoutez-en un pour l'envoyer par courriel.");
    }
    const href = `mailto:${encodeURIComponent(email)}`
      + `?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(emailBody)}`;
    return el("a", { class: "btn btn-sm send-chan", href }, icon("mail", 15),
      target === "stay" ? `Envoyer par email (${email})` : "Rédiger l'email");
  }

  function whatsappRow(url, tpl) {
    const { shortMsg } = compose(url, tpl);
    const digits = target === "stay" ? waDigits(booking.guest_phone) : "";
    if (target === "stay" && !digits) {
      return inviteRow("message-circle", "Ajouter un téléphone à ce séjour",
        "Ce séjour n'a pas de téléphone — ajoutez-en un pour l'envoyer par WhatsApp.");
    }
    // Sans numéro (vitrine/maison), wa.me/?text= ouvre WhatsApp et laisse choisir
    // le contact — jamais un envoi à personne.
    const href = `https://wa.me/${digits}?text=${encodeURIComponent(shortMsg)}`;
    return el("a", { class: "btn btn-sm send-chan", href, target: "_blank", rel: "noopener" },
      icon("message-circle", 15), "Envoyer par WhatsApp");
  }

  // Invitation (§3.3) : le manque montré au moment où il coûte. Ouvre la modale du
  // séjour (dans le calendrier) pour compléter la coordonnée manquante.
  function inviteRow(ic, label, help) {
    const btn = el("button", { class: "btn btn-sm send-invite", type: "button",
      onClick: () => { openBookingOnLoad(booking.id); modal.close(); navigate(`#/properties/${pid}/calendrier`); } },
      icon(ic, 15), label);
    return el("div", { class: "send-invite-wrap" }, btn,
      el("div", { class: "help" }, help));
  }
}
