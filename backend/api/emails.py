"""Gabarits d'emails transactionnels (V2-08) — français, sobres.

Chaque fonction renvoie un `Email` (sujet + texte + HTML léger aux couleurs du
produit : sable `#FAF7F2`, encre `#1E2A32`, mer `#0E5A73`). Aucun secret n'est
inséré : seul le lien à usage unique (jeton à haute entropie) figure dans le
corps. Le HTML reste minimal (compatible clients mail) et se double toujours
d'une version texte lisible sans HTML.
"""
from __future__ import annotations

import html as _html

from . import i18n as _i18n
from .mailer import Email

# Palette produit (guide_preview.html)
_SAND = "#FAF7F2"
_INK = "#1E2A32"
_SEA = "#0E5A73"
_MUTED = "#5B6B72"

_BRAND = "Holaguia"


def _button(url: str, label: str) -> str:
    """Bouton d'action (table-based pour la compatibilité clients mail)."""
    safe = _html.escape(url, quote=True)
    return (
        f'<table role="presentation" cellspacing="0" cellpadding="0" '
        f'style="margin:24px 0;"><tr><td '
        f'style="border-radius:8px;background:{_SEA};">'
        f'<a href="{safe}" style="display:inline-block;padding:12px 22px;'
        f'font-family:Helvetica,Arial,sans-serif;font-size:15px;font-weight:600;'
        f'color:#ffffff;text-decoration:none;border-radius:8px;">{_html.escape(label)}</a>'
        f"</td></tr></table>"
    )


def _hero(image_url: str) -> str:
    """Photo d'en-tête (vignette du logement) — arrondie, pleine largeur de la
    carte. Table-based pour la compatibilité clients mail."""
    safe = _html.escape(image_url, quote=True)
    return (
        f'<img src="{safe}" alt="" width="100%" '
        f'style="display:block;width:100%;max-width:100%;height:auto;'
        f'border-radius:10px;margin:0 0 20px;object-fit:cover;">'
    )


def _shell(title: str, body_html: str, *, footer: str | None = None,
           hero_url: str | None = None) -> str:
    """Enveloppe HTML commune (identité sable/encre/mer). `hero_url` insère une
    vignette sous l'en-tête ; `footer` remplace le pied par défaut (« envoyé
    automatiquement… ») — les envois de guide ont un pied plus chaleureux."""
    hero = _hero(hero_url) if hero_url else ""
    foot = footer if footer is not None else (
        f"Ce message vous est envoyé automatiquement par {_html.escape(_BRAND)}. "
        f"Merci de ne pas y répondre.")
    return (
        f'<div style="margin:0;padding:24px;background:{_SAND};">'
        f'<div style="max-width:520px;margin:0 auto;background:#ffffff;'
        f'border-radius:12px;padding:32px;'
        f'font-family:Helvetica,Arial,sans-serif;color:{_INK};line-height:1.55;">'
        f'<div style="font-size:20px;font-weight:700;color:{_SEA};'
        f'margin-bottom:20px;">{_html.escape(_BRAND)}</div>'
        f"{hero}"
        f'<h1 style="font-size:19px;margin:0 0 14px;color:{_INK};">{_html.escape(title)}</h1>'
        f"{body_html}"
        f'<p style="font-size:13px;color:{_MUTED};margin-top:28px;">{foot}</p>'
        f"</div></div>"
    )


def _greeting(full_name: str | None) -> str:
    return f"Bonjour {full_name}," if full_name else "Bonjour,"


def reset_password_email(reset_url: str, full_name: str | None = None) -> Email:
    """Email de réinitialisation de mot de passe (lien valable 60 min, usage unique)."""
    hello = _greeting(full_name)
    subject = f"{_BRAND} — réinitialisation de votre mot de passe"

    text = (
        f"{hello}\n\n"
        "Vous avez demandé à réinitialiser le mot de passe de votre compte "
        f"{_BRAND}. Cliquez sur le lien ci-dessous pour choisir un nouveau mot "
        "de passe :\n\n"
        f"{reset_url}\n\n"
        "Ce lien est valable 60 minutes et ne peut servir qu'une seule fois.\n\n"
        "Si vous n'êtes pas à l'origine de cette demande, ignorez simplement cet "
        "email : votre mot de passe reste inchangé.\n\n"
        f"— L'équipe {_BRAND}"
    )

    body_html = (
        f'<p style="margin:0 0 12px;">{_html.escape(hello)}</p>'
        f'<p style="margin:0 0 12px;">Vous avez demandé à réinitialiser le mot de '
        f"passe de votre compte {_html.escape(_BRAND)}. Cliquez sur le bouton "
        "ci-dessous pour en choisir un nouveau :</p>"
        f"{_button(reset_url, 'Réinitialiser mon mot de passe')}"
        f'<p style="margin:0 0 12px;font-size:14px;color:{_MUTED};">Ce lien est '
        "valable <strong>60 minutes</strong> et ne peut servir qu'une seule fois.</p>"
        f'<p style="margin:0;font-size:14px;color:{_MUTED};">Si vous n\'êtes pas à '
        "l'origine de cette demande, ignorez cet email : votre mot de passe reste "
        "inchangé.</p>"
    )
    return Email(subject=subject, text=text, html=_shell(
        "Réinitialisation de votre mot de passe", body_html))


def verify_email(verify_url: str, full_name: str | None = None) -> Email:
    """Email de vérification d'adresse à l'inscription (lien valable 60 min)."""
    hello = _greeting(full_name)
    subject = f"{_BRAND} — confirmez votre adresse email"

    text = (
        f"{hello}\n\n"
        f"Bienvenue sur {_BRAND} ! Pour confirmer votre adresse email, cliquez "
        "sur le lien ci-dessous :\n\n"
        f"{verify_url}\n\n"
        "Ce lien est valable 60 minutes.\n\n"
        "Vous pouvez utiliser votre espace propriétaire dès maintenant : cette "
        "vérification nous aide simplement à garder votre compte en sécurité.\n\n"
        f"— L'équipe {_BRAND}"
    )

    body_html = (
        f'<p style="margin:0 0 12px;">{_html.escape(hello)}</p>'
        f'<p style="margin:0 0 12px;">Bienvenue sur {_html.escape(_BRAND)} ! '
        "Confirmez votre adresse email en cliquant sur le bouton ci-dessous :</p>"
        f"{_button(verify_url, 'Confirmer mon adresse')}"
        f'<p style="margin:0 0 12px;font-size:14px;color:{_MUTED};">Ce lien est '
        "valable <strong>60 minutes</strong>.</p>"
        f'<p style="margin:0;font-size:14px;color:{_MUTED};">Vous pouvez utiliser '
        "votre espace propriétaire dès maintenant : cette vérification nous aide "
        "simplement à garder votre compte en sécurité.</p>"
    )
    return Email(subject=subject, text=text, html=_shell(
        "Confirmez votre adresse email", body_html))


def _days_label(days_left: int) -> str:
    if days_left <= 1:
        return "demain" if days_left == 1 else "aujourd'hui"
    return f"dans {days_left} jours"


def trial_reminder_email(days_left: int, dashboard_url: str,
                         full_name: str | None = None) -> Email:
    """Relance de fin d'essai (V2-18a) : J-7 / J-2. Ton sobre, sans alarmisme —
    l'essai continue et les guides restent en ligne quoi qu'il arrive. `days_left`
    est le nombre de jours restants (7 ou 2)."""
    hello = _greeting(full_name)
    when = _days_label(days_left)
    subject = f"{_BRAND} — votre essai se termine {when}"

    text = (
        f"{hello}\n\n"
        f"Votre essai {_BRAND} se termine {when}. Vos guides d'accueil resteront "
        "en ligne, mais la modification de vos logements sera suspendue tant "
        "qu'une offre n'est pas choisie.\n\n"
        "Pour continuer à éditer vos guides sans interruption, choisissez votre "
        "offre ici :\n\n"
        f"{dashboard_url}\n\n"
        "Rien n'est perdu : à tout moment, souscrire réactive immédiatement "
        "l'accès complet à vos logements.\n\n"
        f"— L'équipe {_BRAND}"
    )

    body_html = (
        f'<p style="margin:0 0 12px;">{_html.escape(hello)}</p>'
        f'<p style="margin:0 0 12px;">Votre essai {_html.escape(_BRAND)} se '
        f"termine <strong>{_html.escape(when)}</strong>. Vos guides d'accueil "
        "resteront en ligne, mais la modification de vos logements sera suspendue "
        "tant qu'une offre n'est pas choisie.</p>"
        f"{_button(dashboard_url, 'Choisir mon offre')}"
        f'<p style="margin:0;font-size:14px;color:{_MUTED};">Rien n\'est perdu : '
        "souscrire réactive immédiatement l'accès complet à vos logements.</p>"
    )
    return Email(subject=subject, text=text, html=_shell(
        f"Votre essai se termine {when}", body_html))


def guest_service_request_email(*, property_name: str, service_label: str,
                                note: str | None, guest_name: str | None,
                                stay_label: str | None,
                                calendar_url: str,
                                full_name: str | None = None) -> Email:
    """Notification au propriétaire d'une demande de service du voyageur (V2-23b,
    §3.1). Sobre : la demande est **en attente** — le propriétaire l'accepte ou la
    refuse depuis son calendrier, où elle devient (si acceptée) une intervention
    visible par l'équipe. Aucun secret, aucune coordonnée dans le corps."""
    hello = _greeting(full_name)
    who = f" (séjour de {guest_name})" if guest_name else ""
    stay = f" — {stay_label}" if stay_label else ""
    subject = f"{_BRAND} — nouvelle demande d'un voyageur : {service_label}"

    note_text = f'\n\nMessage du voyageur :\n« {note} »' if note else ""
    text = (
        f"{hello}\n\n"
        f"Un voyageur de « {property_name} »{who}{stay} vient de demander : "
        f"{service_label}.{note_text}\n\n"
        "Cette demande est en attente. Vous pouvez l'accepter ou la refuser depuis "
        "votre calendrier ; une demande acceptée devient une intervention visible "
        "par votre équipe d'entretien :\n\n"
        f"{calendar_url}\n\n"
        f"— {_BRAND}"
    )

    note_html = (f'<p style="margin:0 0 12px;padding:12px 14px;background:{_SAND};'
                 f'border-radius:8px;font-style:italic;">'
                 f"« {_html.escape(note)} »</p>") if note else ""
    body_html = (
        f'<p style="margin:0 0 12px;">{_html.escape(hello)}</p>'
        f'<p style="margin:0 0 12px;">Un voyageur de <strong>'
        f'{_html.escape(property_name)}</strong>{_html.escape(who)}'
        f'{_html.escape(stay)} vient de demander : '
        f'<strong>{_html.escape(service_label)}</strong>.</p>'
        f"{note_html}"
        f"{_button(calendar_url, 'Voir la demande')}"
        f'<p style="margin:0;font-size:14px;color:{_MUTED};">Cette demande est '
        "<strong>en attente</strong>. Acceptez-la ou refusez-la depuis votre "
        "calendrier ; acceptée, elle devient une intervention visible par votre "
        "équipe d'entretien.</p>"
    )
    return Email(subject=subject, text=text, html=_shell(
        "Nouvelle demande d'un voyageur", body_html))


# ── Envoi du guide au voyageur / au prospect (V2-23d, volet 1) ───────────────
# Contrairement aux emails ci-dessus (transactionnels, propriétaire, restés FR),
# ces deux emails partent VERS le voyageur/prospect → ils sont LOCALISÉS. Les
# copies FR/EN/ES vivent ici (source du code) ; les langues supplémentaires
# (nl/de/it/sq) sont superposées depuis `ui_translations` via l'overlay i18n
# (clés `email.*`, générées plus tard — V2-29), avec repli FR sans trou. Les
# placeholders {property}/{name}/{start}/{end} sont substitués par le builder.
_EMAIL: dict[str, dict[str, str]] = {
    "fr": {
        "stay_subject": "Votre guide pour {property} — séjour du {start} au {end}",
        "stay_title": "Votre guide de séjour",
        "stay_hello": "Bonjour {name},",
        "stay_hello_generic": "Bonjour,",
        "stay_intro": "voici votre guide personnel pour votre séjour : arrivée et "
                      "accès, wifi, bonnes adresses, plages, numéros utiles — tout "
                      "y est, consultable hors connexion.",
        "showcase_subject": "Découvrez {property} en visite guidée",
        "showcase_title": "Votre visite guidée",
        "showcase_hello": "Bonjour,",
        "showcase_intro": "avant même de réserver, visitez {property} comme si vous "
                          "y étiez : le quartier, l'accès et le parking, les plages "
                          "et restaurants à proximité, les équipements — le guide "
                          "complet que reçoivent nos voyageurs.",
        "showcase_signoff": "À votre disposition pour toute question — au plaisir "
                            "de vous accueillir !",
        "button": "Ouvrir le guide",
        "footer": "Guide propulsé par Holaguia. Bon séjour !",
    },
    "en": {
        "stay_subject": "Your guide for {property} — stay from {start} to {end}",
        "stay_title": "Your stay guide",
        "stay_hello": "Hello {name},",
        "stay_hello_generic": "Hello,",
        "stay_intro": "here is your personal guide for your stay: arrival and "
                      "access, wifi, great local spots, beaches, useful numbers — "
                      "everything is here, available offline.",
        "showcase_subject": "Discover {property} on a guided tour",
        "showcase_title": "Your guided tour",
        "showcase_hello": "Hello,",
        "showcase_intro": "even before booking, explore {property} as if you were "
                          "already there: the neighbourhood, access and parking, "
                          "nearby beaches and restaurants, the amenities — the full "
                          "guide our guests receive.",
        "showcase_signoff": "I'm here for any questions — looking forward to "
                            "welcoming you!",
        "button": "Open the guide",
        "footer": "Guide powered by Holaguia. Enjoy your stay!",
    },
    "es": {
        "stay_subject": "Tu guía para {property} — estancia del {start} al {end}",
        "stay_title": "Tu guía de estancia",
        "stay_hello": "Hola {name}:",
        "stay_hello_generic": "Hola:",
        "stay_intro": "aquí tienes tu guía personal para tu estancia: llegada y "
                      "acceso, wifi, buenas direcciones, playas, números útiles — "
                      "todo está aquí, disponible sin conexión.",
        "showcase_subject": "Descubre {property} en una visita guiada",
        "showcase_title": "Tu visita guiada",
        "showcase_hello": "Hola:",
        "showcase_intro": "antes incluso de reservar, visita {property} como si ya "
                          "estuvieras allí: el barrio, el acceso y el aparcamiento, "
                          "las playas y restaurantes cercanos, los equipamientos — "
                          "la guía completa que reciben nuestros viajeros.",
        "showcase_signoff": "Quedo a tu disposición para cualquier pregunta. "
                            "¡Un placer darte la bienvenida!",
        "button": "Abrir la guía",
        "footer": "Guía con tecnología de Holaguia. ¡Feliz estancia!",
    },
}


def _et(lang: str, key: str) -> str:
    """Libellé d'email dans `lang` : overlay des langues publiées supplémentaires
    (V2-21a, `ui_translations`, clé `email.*`) d'abord, puis le code (FR/EN/ES),
    puis le FR (repli, jamais de trou). Même mécanique que `guide_page._t`."""
    return (_i18n.overlaid(_i18n.email_key(key))
            or _EMAIL.get(lang, {}).get(key) or _EMAIL["fr"][key])


def guide_stay_email(*, property_name: str, guest_name: str | None,
                     start: str, end: str, url: str, image_url: str,
                     lang: str = "fr") -> Email:
    """Email « Envoyer par Holaguia » pour un SÉJOUR (V2-23d). Accueil personnalisé
    (prénom si connu), vignette du logement, bouton vers le lien de séjour `/b/`
    (le `guide_token` éternel n'y figure jamais). `start`/`end` déjà formatés."""
    name = (guest_name or "").strip()
    hello = (_et(lang, "stay_hello").replace("{name}", name) if name
             else _et(lang, "stay_hello_generic"))
    lead = f"{hello} {_et(lang, 'stay_intro')}"
    button = _et(lang, "button")
    subject = (_et(lang, "stay_subject").replace("{property}", property_name)
               .replace("{start}", start).replace("{end}", end))
    text = f"{lead}\n\n{url}\n\n— {_BRAND}"
    body_html = (f'<p style="margin:0 0 12px;">{_html.escape(lead)}</p>'
                 f"{_button(url, button)}")
    return Email(subject=subject, text=text, html=_shell(
        _et(lang, "stay_title"), body_html,
        footer=_html.escape(_et(lang, "footer")), hero_url=image_url))


def guide_showcase_email(*, property_name: str, url: str, image_url: str,
                         lang: str = "fr") -> Email:
    """Email « Envoyer par Holaguia » pour la VITRINE (V2-23d) : outil de vente
    adressé à un prospect. Bouton vers le lien vitrine `/v/` (secrets d'exemple)."""
    intro = _et(lang, "showcase_intro").replace("{property}", property_name)
    lead = f"{_et(lang, 'showcase_hello')} {intro}"
    signoff = _et(lang, "showcase_signoff")
    button = _et(lang, "button")
    subject = _et(lang, "showcase_subject").replace("{property}", property_name)
    text = f"{lead}\n\n{url}\n\n{signoff}\n\n— {_BRAND}"
    body_html = (f'<p style="margin:0 0 12px;">{_html.escape(lead)}</p>'
                 f"{_button(url, button)}"
                 f'<p style="margin:0;">{_html.escape(signoff)}</p>')
    return Email(subject=subject, text=text, html=_shell(
        _et(lang, "showcase_title"), body_html,
        footer=_html.escape(_et(lang, "footer")), hero_url=image_url))
