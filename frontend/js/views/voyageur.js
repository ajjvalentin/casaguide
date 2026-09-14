/* Offre « Guide Voyageur » (V2-54 Mission C) — tunnel PUBLIC, sans compte.

   Routes (toutes publiques, cf. app.js, AVANT la porte propriétaire) :
     #/voyageur                     → page de l'offre (prix lu de la config)
     #/voyageur/adresse             → adresse + géocodage + AJUSTEMENT du point sur carte
                                       → récapitulatif → Stripe Checkout
     #/voyageur/merci/{token}       → paiement reçu ; polling ; lien + installation PWA
     #/voyageur/reprise/{token}     → reprise après échec (ré-ajuste le point, sans repayer)
     #/voyageur/renvoi              → « Retrouver mon guide » par e-mail

   i18n FR/EN/ES (structure prête pour les 7) — chaînes locales à cette vue (le
   back-office n'a pas de dictionnaire d'UI ; le guide livré est nativement 7 langues). */

import { api, ApiError } from "../api.js";
import { el, icon, mount, clear, toast, refreshIcons } from "../ui.js";
import { navigate } from "../nav.js";
import { redirect } from "../redirect.js";

// ── i18n ─────────────────────────────────────────────────────────────────────
const STRINGS = {
  fr: {
    offer_eyebrow: "Voyageurs", offer_title: "Le guide des environs de votre lieu de vacances",
    offer_promise: "À vie · hors-ligne · 7 langues",
    offer_desc: "Restaurants, plages, commerces, urgences — le guide complet du secteur de votre location, généré pour votre adresse.",
    offer_cta: "Créer mon guide", offer_have: "J'ai déjà acheté un guide",
    addr_title: "Où allez-vous séjourner ?",
    addr_intro: "Saisissez l'adresse de votre location — vous ajusterez le point exact sur la carte.",
    f_address: "Adresse (rue et numéro)", f_postal: "Code postal", f_city: "Ville / commune",
    f_country: "Pays", f_email: "Votre e-mail",
    locate: "Situer sur la carte",
    map_hint: "Déplacez le point (ou touchez la carte) pour marquer précisément votre lieu de séjour.",
    map_mismatch: "Emplacement incertain — vérifiez et déplacez le point sur votre location.",
    map_notfound: "Adresse introuvable — placez le point manuellement sur la carte.",
    need_precise: "Emplacement imprécis — indiquez votre rue ou déplacez le point sur votre lieu de séjour.",
    geo_error: "Localisation momentanément indisponible — placez le point manuellement sur la carte.",
    choose_area: "Grande ville : choisissez votre quartier de séjour.",
    recap: "Guide des environs de {city}", pay: "Payer {price}",
    paying: "Redirection vers le paiement sécurisé…",
    err_fields: "Renseignez l'adresse, la ville, le pays et un e-mail valide.",
    err_locate: "Situez d'abord votre location sur la carte.",
    merci_title: "Paiement reçu, merci !",
    merci_prep: "Votre guide est en préparation. Vous le recevrez aussi par e-mail dès qu'il est prêt.",
    merci_slow: "La préparation peut prendre 20 à 30 minutes selon la région. Votre guide arrivera par e-mail — vous pouvez fermer cette page en toute tranquillité.",
    merci_ready: "Votre guide est prêt !",
    merci_open: "Ouvrir mon guide", merci_install: "Installer votre guide",
    merci_install_hint: "Ouvrez le guide puis « Ajouter à l'écran d'accueil » pour l'avoir hors-ligne, à vie.",
    merci_failed: "La préparation a rencontré un souci. Un e-mail vous permet de reprendre — sans repayer.",
    merci_reprise: "Reprendre",
    exit_another: "Créer un autre guide", exit_host: "Je suis hôte — créer mon espace",
    reprise_title: "Reprenons votre guide",
    reprise_intro: "Votre paiement est acquis. Ajustez le point de votre lieu de séjour, nous relançons la génération (aucun nouveau paiement).",
    reprise_cta: "Relancer la génération",
    reprise_done: "C'est reparti ! Vous recevrez votre guide par e-mail sous peu.",
    renvoi_title: "Retrouver mon guide",
    renvoi_intro: "Saisissez l'e-mail utilisé lors de l'achat, nous vous renvoyons le lien de votre guide.",
    renvoi_cta: "Me renvoyer le lien",
    renvoi_done: "Si un guide est associé à cet e-mail, le lien vient de partir.",
    back: "Retour", generic_err: "Une erreur est survenue. Réessayez.",
  },
  en: {
    offer_eyebrow: "Travellers", offer_title: "The guide to the area around your holiday spot",
    offer_promise: "For life · offline · 7 languages",
    offer_desc: "Restaurants, beaches, shops, emergencies — the complete guide to your rental's area, generated for your address.",
    offer_cta: "Create my guide", offer_have: "I already bought a guide",
    addr_title: "Where are you staying?",
    addr_intro: "Enter your rental's address — you'll fine-tune the exact point on the map.",
    f_address: "Address (street and number)", f_postal: "Postcode", f_city: "Town / city",
    f_country: "Country", f_email: "Your e-mail",
    locate: "Locate on the map",
    map_hint: "Drag the point (or tap the map) to mark exactly where you're staying.",
    map_mismatch: "Location uncertain — check and move the point to your rental.",
    map_notfound: "Address not found — place the point manually on the map.",
    need_precise: "Imprecise location — enter your street or move the point to where you're staying.",
    geo_error: "Location temporarily unavailable — place the point manually on the map.",
    choose_area: "Large city: choose the district where you're staying.",
    recap: "Guide to the area around {city}", pay: "Pay {price}",
    paying: "Redirecting to secure payment…",
    err_fields: "Enter the address, town, country and a valid e-mail.",
    err_locate: "First locate your rental on the map.",
    merci_title: "Payment received, thank you!",
    merci_prep: "Your guide is being prepared. You'll also receive it by e-mail as soon as it's ready.",
    merci_slow: "Preparation can take 20 to 30 minutes depending on the area. Your guide will arrive by e-mail — you can safely close this page.",
    merci_ready: "Your guide is ready!",
    merci_open: "Open my guide", merci_install: "Install your guide",
    merci_install_hint: "Open the guide, then “Add to Home Screen” to keep it offline, for life.",
    merci_failed: "Preparation hit a snag. An e-mail lets you resume — no new payment.",
    merci_reprise: "Resume",
    exit_another: "Create another guide", exit_host: "I'm a host — create my space",
    reprise_title: "Let's finish your guide",
    reprise_intro: "Your payment is secured. Adjust the point of your stay and we'll restart generation (no new payment).",
    reprise_cta: "Restart generation",
    reprise_done: "Off we go! You'll receive your guide by e-mail shortly.",
    renvoi_title: "Find my guide",
    renvoi_intro: "Enter the e-mail you used at purchase and we'll resend your guide link.",
    renvoi_cta: "Resend me the link",
    renvoi_done: "If a guide is linked to this e-mail, the link is on its way.",
    back: "Back", generic_err: "Something went wrong. Please try again.",
  },
  es: {
    offer_eyebrow: "Viajeros", offer_title: "La guía de los alrededores de tu lugar de vacaciones",
    offer_promise: "De por vida · sin conexión · 7 idiomas",
    offer_desc: "Restaurantes, playas, comercios, emergencias — la guía completa de la zona de tu alojamiento, generada para tu dirección.",
    offer_cta: "Crear mi guía", offer_have: "Ya he comprado una guía",
    addr_title: "¿Dónde te vas a alojar?",
    addr_intro: "Introduce la dirección de tu alojamiento — ajustarás el punto exacto en el mapa.",
    f_address: "Dirección (calle y número)", f_postal: "Código postal", f_city: "Ciudad / municipio",
    f_country: "País", f_email: "Tu correo electrónico",
    locate: "Situar en el mapa",
    map_hint: "Mueve el punto (o toca el mapa) para marcar exactamente dónde te alojas.",
    map_mismatch: "Ubicación incierta — comprueba y mueve el punto a tu alojamiento.",
    map_notfound: "Dirección no encontrada — coloca el punto manualmente en el mapa.",
    need_precise: "Ubicación imprecisa — indica tu calle o mueve el punto a tu lugar de alojamiento.",
    geo_error: "Localización no disponible por el momento — coloca el punto manualmente en el mapa.",
    choose_area: "Ciudad grande: elige el barrio donde te alojas.",
    recap: "Guía de los alrededores de {city}", pay: "Pagar {price}",
    paying: "Redirigiendo al pago seguro…",
    err_fields: "Indica la dirección, la ciudad, el país y un correo válido.",
    err_locate: "Sitúa primero tu alojamiento en el mapa.",
    merci_title: "¡Pago recibido, gracias!",
    merci_prep: "Tu guía se está preparando. También la recibirás por correo electrónico en cuanto esté lista.",
    merci_slow: "La preparación puede tardar de 20 a 30 minutos según la zona. Tu guía llegará por correo — puedes cerrar esta página con tranquilidad.",
    merci_ready: "¡Tu guía está lista!",
    merci_open: "Abrir mi guía", merci_install: "Instalar tu guía",
    merci_install_hint: "Abre la guía y pulsa “Añadir a pantalla de inicio” para tenerla sin conexión, de por vida.",
    merci_failed: "La preparación tuvo un problema. Un correo te permite retomar — sin pagar de nuevo.",
    merci_reprise: "Retomar",
    exit_another: "Crear otra guía", exit_host: "Soy anfitrión — crear mi espacio",
    reprise_title: "Terminemos tu guía",
    reprise_intro: "Tu pago está asegurado. Ajusta el punto de tu alojamiento y reiniciamos la generación (sin nuevo pago).",
    reprise_cta: "Reiniciar la generación",
    reprise_done: "¡En marcha! Recibirás tu guía por correo en breve.",
    renvoi_title: "Recuperar mi guía",
    renvoi_intro: "Introduce el correo que usaste en la compra y te reenviamos el enlace de tu guía.",
    renvoi_cta: "Reenviarme el enlace",
    renvoi_done: "Si hay una guía asociada a este correo, el enlace acaba de salir.",
    back: "Atrás", generic_err: "Ha ocurrido un error. Inténtalo de nuevo.",
  },
};

let LANG = "fr";
function resolveLang(params) {
  const cand = (params.get("lang") || localStorage.getItem("casaguide:lang")
    || (navigator.language || "fr").slice(0, 2)).toLowerCase();
  LANG = ["fr", "en", "es"].includes(cand) ? cand : "fr";
  return LANG;
}
function tr(key, subs) {
  let s = (STRINGS[LANG] && STRINGS[LANG][key]) || STRINGS.fr[key] || key;
  if (subs) for (const k in subs) s = s.replaceAll(`{${k}}`, subs[k]);
  return s;
}
function money(cts, currency) {
  try {
    return new Intl.NumberFormat(LANG, { style: "currency", currency: (currency || "eur").toUpperCase() })
      .format((cts || 0) / 100);
  } catch (_) { return `${((cts || 0) / 100).toFixed(2)} €`; }
}

// ── Ossature plein-écran (identité, sans session) ────────────────────────────
function shell(...children) {
  return el("div", { class: "auth-wrap" },
    el("div", { class: "card auth-card voyageur-card" },
      el("a", { class: "brand", href: "#/voyageur" },
        el("span", { class: "mark" }, icon("map-pinned", 20)), "Holaguia"),
      ...children));
}
function backLink(hash) {
  return el("a", { class: "muted-link", href: hash },
    icon("arrow-left", 14), " ", tr("back"));
}
// Portes de sortie (V2-64) : depuis la page merci (et le pied du parcours), toujours un
// chemin — refaire un guide voyageur, ou passer côté hôte (créer son espace).
function exitDoors() {
  return el("div", { class: "voyageur-exits" },
    el("a", { class: "muted-link", href: "#/voyageur" },
      icon("plus", 14), " ", tr("exit_another")),
    el("a", { class: "muted-link", href: "#/login" },
      icon("home", 14), " ", tr("exit_host")));
}

// ── Carte d'ajustement du point (Leaflet global sur le SPA) ──────────────────
// Renvoie un getter () -> {lat, lon} ; `onSet(lat,lon)` notifié à chaque changement.
function mountAdjustMap(container, { lat, lon }, onSet) {
  if (!window.L) { container.textContent = "(carte indisponible)"; return () => null; }
  const hasPoint = lat != null && lon != null;
  let la = hasPoint ? lat : 40.0, lo = hasPoint ? lon : -3.7;
  const map = window.L.map(container).setView([la, lo], hasPoint ? 16 : 5);
  window.L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png",
    { attribution: "© OpenStreetMap", maxZoom: 19 }).addTo(map);
  const marker = window.L.marker([la, lo], { draggable: true }).addTo(map);
  const set = (a, o) => { la = a; lo = o; if (onSet) onSet(la, lo); };
  marker.on("dragend", () => { const p = marker.getLatLng(); set(p.lat, p.lng); });
  map.on("click", (e) => { marker.setLatLng(e.latlng); set(e.latlng.lat, e.latlng.lng); });
  setTimeout(() => map.invalidateSize(), 80);
  if (hasPoint && onSet) onSet(la, lo);
  return () => ({ lat: la, lon: lo });
}

// ── Dispatcher ────────────────────────────────────────────────────────────────
export function renderVoyageur(root, seg, params) {
  resolveLang(params);
  const step = seg[0] || "";
  if (step === "adresse") return renderAdresse(root);
  if (step === "merci") return renderMerci(root, seg[1]);
  if (step === "reprise") return renderReprise(root, seg[1]);
  if (step === "renvoi") return renderRenvoi(root);
  return renderOffer(root);
}

// ── 1. Offre ──────────────────────────────────────────────────────────────────
async function renderOffer(root) {
  let price = "";
  try { const o = await api.guestOffer(); price = money(o.price_cts, o.currency); }
  catch (_) { /* prix indisponible : le CTA reste sans montant */ }
  mount(root, shell(
    el("p", { class: "eyebrow" }, tr("offer_eyebrow")),
    el("h1", {}, tr("offer_title")),
    el("p", { class: "voyageur-promise" }, tr("offer_promise")),
    el("p", { class: "muted" }, tr("offer_desc")),
    el("div", { class: "voyageur-price" }, price),
    el("button", { class: "btn btn-primary btn-block",
      onClick: () => navigate("#/voyageur/adresse") },
      tr("offer_cta") + (price ? ` — ${price}` : "")),
    el("a", { class: "muted-link center", href: "#/voyageur/renvoi" }, tr("offer_have")),
  ));
}

// ── 2. Adresse + carte + récapitulatif → Checkout ────────────────────────────
function renderAdresse(root) {
  const fields = {
    address_line1: el("input", { type: "text", autocomplete: "street-address" }),
    postal_code: el("input", { type: "text", autocomplete: "postal-code" }),
    city: el("input", { type: "text", required: true }),
    country_code: el("input", { type: "text", value: "ES", maxlength: "2",
      style: "text-transform:uppercase" }),
    email: el("input", { type: "email", required: true, autocomplete: "email" }),
  };
  const errBox = el("div", { class: "errbox hidden" });
  const mapWrap = el("div", { class: "voyageur-mapwrap hidden" });
  const hoodsBox = el("div", { class: "voyageur-hoods hidden" });
  const mapEl = el("div", { class: "voyageur-map" });
  const mapMsg = el("p", { class: "muted small" });
  const recap = el("div", { class: "voyageur-recap hidden" });
  let getPoint = () => null;

  const field = (key, label) =>
    el("label", { class: "field" }, el("span", {}, label), fields[key]);

  const locateBtn = el("button", { class: "btn btn-block", type: "button" }, tr("locate"));
  const showErr = (m) => { errBox.textContent = m; errBox.classList.remove("hidden"); };
  const hideErr = () => errBox.classList.add("hidden");

  locateBtn.onclick = async () => {
    hideErr();
    const city = fields.city.value.trim();
    const cc = fields.country_code.value.trim().toUpperCase();
    const email = fields.email.value.trim();
    if (!city || cc.length !== 2 || !/.+@.+\..+/.test(email)) {
      return showErr(tr("err_fields"));
    }
    const addr1 = fields.address_line1.value.trim() || null;
    const pc = fields.postal_code.value.trim() || null;
    locateBtn.disabled = true;
    try {
      let geo = { found: false };
      let geoErr = false;
      try {
        geo = await api.guestGeocode({ address_line1: addr1, postal_code: pc, city, country_code: cc });
      } catch (_) { geo = { found: false }; geoErr = true; }   // réseau/timeout → placement manuel
      // Précision (V2-68 p1) : rue/quartier OK ; « city »/mismatch/introuvable = imprécis
      // → on IMPOSE un ancrage (choix de quartier pour une grande ville, sinon ajustement).
      const precise = geo.found && (geo.accuracy === "rooftop" || geo.accuracy === "street");
      let hoods = [];
      if (!precise && geo.found) {
        try { hoods = await api.guestNeighborhoods({ address_line1: addr1, postal_code: pc, city, country_code: cc }); }
        catch (_) { hoods = []; }
      }

      // État d'ancrage : « validé » si PRÉCIS, ou AJUSTÉ à la main, ou quartier CHOISI.
      // `anchorCity` porte le quartier retenu (titre « Shibuya »). syncPay et payBtn sont
      // définis AVANT `mountAdjustMap` (V2-68b) : `mountAdjustMap` appelle `onSet` de
      // façon SYNCHRONE au montage quand un point est fourni → référencer syncPay après
      // le levait en zone morte (le tunnel gelait). On ignore ce 1er appel de montage
      // (`mounted`) pour ne PAS débloquer le paiement sans ajustement réel du client.
      let anchored = precise;
      let anchorCity = city;
      const payBtn = el("button", { class: "btn btn-primary btn-block", type: "button" },
        tr("pay", { price: "" }).trim());
      let priceLabel = tr("pay", { price: "" }).trim();
      const syncPay = () => {
        payBtn.disabled = !anchored;
        payBtn.textContent = priceLabel;
      };
      api.guestOffer().then((o) => { priceLabel = tr("pay", { price: money(o.price_cts, o.currency) }); syncPay(); })
        .catch(() => {});
      payBtn.onclick = async () => {
        const pt = getPoint();
        if (!anchored || !pt || pt.lat == null) return showErr(tr("err_locate"));
        payBtn.disabled = true; payBtn.textContent = tr("paying");
        try {
          const r = await api.guestCheckout({
            email, city: anchorCity, country_code: cc,
            address_line1: addr1, postal_code: pc,
            region: anchorCity !== city ? city : null,   // quartier choisi → ville en région
            lat: pt.lat, lon: pt.lon, lang: LANG });
          redirect(r.url);
        } catch (e) {
          payBtn.disabled = false;
          showErr(e instanceof ApiError ? e.message : tr("generic_err"));
        }
      };

      // Carte d'ajustement, CENTRÉE sur le point retourné (même imprécis). Le 1er `onSet`
      // (appel synchrone du montage) est ignoré → un point imprécis ne débloque PAS le
      // paiement tant que le client ne l'a pas déplacé.
      mapWrap.classList.remove("hidden");
      clear(mapEl);
      const start = geo.found ? { lat: geo.lat, lon: geo.lon } : { lat: null, lon: null };
      let mounted = false;
      getPoint = mountAdjustMap(mapEl, start, () => {
        if (!mounted) return;              // montage : pas une interaction du client
        anchored = true; syncPay();
      });
      mounted = true;

      // Message + choix de quartier selon la précision.
      clear(hoodsBox);
      if (precise) {
        hoodsBox.classList.add("hidden");
        mapMsg.textContent = tr("map_hint");
      } else if (hoods.length) {
        // Grande ville : boutons de quartier (ancrage précis en un tap) — V2-68 p2.
        mapMsg.textContent = tr("need_precise");
        hoodsBox.appendChild(el("p", { class: "muted small" }, tr("choose_area")));
        const chips = el("div", { class: "hood-chips" });
        hoods.forEach((h) => {
          const b = el("button", { class: "btn hood-chip", type: "button" }, h.name);
          b.onclick = () => {
            let m2 = false;
            getPoint = mountAdjustMap(mapEl, { lat: h.lat, lon: h.lon }, () => {
              if (!m2) return; anchored = true; syncPay();
            });
            m2 = true;
            anchorCity = h.name;            // le guide s'ancre et se titre sur le quartier
            anchored = true;                // choisir un quartier VAUT ancrage
            [...chips.children].forEach((c) => c.classList.remove("on"));
            b.classList.add("on");
            const h2 = recap.querySelector("h2");
            if (h2) h2.textContent = tr("recap", { city: anchorCity });
            mapMsg.textContent = tr("map_hint");
            syncPay();
          };
          chips.appendChild(b);
        });
        hoodsBox.appendChild(chips);
        hoodsBox.classList.remove("hidden");
      } else {
        // Imprécis sans quartier (petite commune), introuvable, ou erreur réseau :
        // ajustement du point imposé, message explicite — jamais d'impasse (V2-68b p3).
        hoodsBox.classList.add("hidden");
        mapMsg.textContent = geo.found ? tr("need_precise")
          : (geoErr ? tr("geo_error") : tr("map_notfound"));
      }

      clear(recap);
      mount(recap, el("h2", {}, tr("recap", { city: anchorCity })), payBtn);
      recap.classList.remove("hidden");
      syncPay();
      recap.scrollIntoView({ behavior: "smooth", block: "nearest" });
    } finally {
      locateBtn.disabled = false;          // TOUJOURS réarmé — jamais grisé sans issue
    }
  };

  mount(root, shell(
    el("h1", {}, tr("addr_title")),
    el("p", { class: "muted" }, tr("addr_intro")),
    errBox,
    field("address_line1", tr("f_address")),
    el("div", { class: "field-row" },
      field("postal_code", tr("f_postal")), field("city", tr("f_city"))),
    el("div", { class: "field-row" },
      field("country_code", tr("f_country")), field("email", tr("f_email"))),
    locateBtn,
    mapWrap,
    backLink("#/voyageur"),
  ));
  mount(mapWrap, hoodsBox, mapEl, mapMsg, recap);
}

// ── 3. Merci (polling + installation) ────────────────────────────────────────
function renderMerci(root, token) {
  const status = el("div", { class: "voyageur-status" });
  // Portes de sortie TOUJOURS présentes (V2-64) : quel que soit l'état, l'utilisateur
  // n'est jamais bloqué sur cette page.
  mount(root, shell(el("h1", {}, tr("merci_title")), status, exitDoors()));
  refreshIcons();
  if (!token) { status.textContent = tr("generic_err"); return; }

  let stopped = false;
  let shownSlow = false;
  const startedAt = Date.now();
  // Au-delà de ce délai, message honnête (20-30 min selon la région, arrive par e-mail,
  // « vous pouvez fermer la page ») — l'ancien « ~10 minutes » était faux hors zones denses.
  const SLOW_AFTER_MS = 12 * 60 * 1000;
  const preparing = (slow) => {
    mount(status,
      el("div", { class: "spinner-inline" }, icon("loader", 22)),
      el("p", { class: "muted" }, tr(slow ? "merci_slow" : "merci_prep")));
    refreshIcons();
  };
  preparing(false);

  const ready = (order) => {
    stopped = true;
    const guideUrl = order.guide_url;
    const openBtn = el("a", { class: "btn btn-primary btn-block", href: guideUrl },
      tr("merci_open"));
    const installBtn = el("a", { class: "btn btn-block", href: guideUrl },
      icon("download", 16), " ", tr("merci_install"));
    mount(status,
      el("div", { class: "okbox" }, tr("merci_ready")),
      openBtn, installBtn,
      el("p", { class: "muted small" }, tr("merci_install_hint")));
    refreshIcons();
  };
  const failed = () => {
    stopped = true;
    mount(status,
      el("div", { class: "errbox" }, tr("merci_failed")),
      el("a", { class: "btn btn-primary btn-block", href: `#/voyageur/reprise/${token}` },
        tr("merci_reprise")));
  };

  // La page CONCLUT toujours : 'done' → arrêt du polling + lien du guide ; 'failed' →
  // lien de reprise ; au-delà de N minutes → message honnête, on ralentit le polling mais
  // on continue (si la génération aboutit encore, on affiche le guide). Le backend garantit
  // par ailleurs qu'aucune commande payée ne reste orpheline (chien de garde V2-64).
  const poll = async () => {
    if (stopped) return;
    try {
      const o = await api.guestOrder(token);
      if (o.status === "done" && o.guide_url) return ready(o);
      if (o.status === "failed") return failed();
    } catch (_) { /* transitoire : on re-tente */ }
    if (stopped) return;
    if (!shownSlow && Date.now() - startedAt > SLOW_AFTER_MS) {
      shownSlow = true;
      preparing(true);
    }
    setTimeout(poll, shownSlow ? 15000 : 4000);
  };
  poll();
}

// ── 4. Reprise (ré-ajuste le point, sans repayer) ────────────────────────────
function renderReprise(root, token) {
  const errBox = el("div", { class: "errbox hidden" });
  const okBox = el("div", { class: "okbox hidden" });
  const mapEl = el("div", { class: "voyageur-map" });
  let getPoint = () => null;
  const cta = el("button", { class: "btn btn-primary btn-block", type: "button" },
    tr("reprise_cta"));
  cta.onclick = async () => {
    const pt = getPoint();
    cta.disabled = true;
    try {
      await api.guestRetry(token, pt && pt.lat != null ? { lat: pt.lat, lon: pt.lon } : {});
      okBox.textContent = tr("reprise_done"); okBox.classList.remove("hidden");
      errBox.classList.add("hidden"); cta.classList.add("hidden");
    } catch (e) {
      cta.disabled = false;
      errBox.textContent = e instanceof ApiError ? e.message : tr("generic_err");
      errBox.classList.remove("hidden");
    }
  };
  mount(root, shell(
    el("h1", {}, tr("reprise_title")),
    el("p", { class: "muted" }, tr("reprise_intro")),
    okBox, errBox, mapEl,
    el("p", { class: "muted small" }, tr("map_hint")),
    cta,
  ));
  getPoint = mountAdjustMap(mapEl, { lat: null, lon: null }, () => {});
}

// ── 5. Renvoi (« Retrouver mon guide ») ──────────────────────────────────────
function renderRenvoi(root) {
  const emailInput = el("input", { type: "email", required: true, autocomplete: "email" });
  const okBox = el("div", { class: "okbox hidden" });
  const btn = el("button", { class: "btn btn-primary btn-block", type: "submit" },
    tr("renvoi_cta"));
  const form = el("form", {
    onsubmit: async (e) => {
      e.preventDefault();
      btn.disabled = true;
      try { await api.guestResend({ email: emailInput.value.trim() }); } catch (_) {}
      okBox.textContent = tr("renvoi_done"); okBox.classList.remove("hidden");
      form.classList.add("hidden");
    },
  }, el("label", { class: "field" }, el("span", {}, tr("f_email")), emailInput), btn);
  mount(root, shell(
    el("h1", {}, tr("renvoi_title")),
    el("p", { class: "muted" }, tr("renvoi_intro")),
    okBox, form,
    backLink("#/voyageur"),
  ));
}
