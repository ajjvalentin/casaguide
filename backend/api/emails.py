"""Gabarits d'emails transactionnels (V2-08) — français, sobres.

Chaque fonction renvoie un `Email` (sujet + texte + HTML léger aux couleurs du
produit : sable `#FAF7F2`, encre `#1E2A32`, mer `#0E5A73`). Aucun secret n'est
inséré : seul le lien à usage unique (jeton à haute entropie) figure dans le
corps. Le HTML reste minimal (compatible clients mail) et se double toujours
d'une version texte lisible sans HTML.
"""
from __future__ import annotations

import html as _html

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


def _shell(title: str, body_html: str) -> str:
    """Enveloppe HTML commune (identité sable/encre/mer)."""
    return (
        f'<div style="margin:0;padding:24px;background:{_SAND};">'
        f'<div style="max-width:520px;margin:0 auto;background:#ffffff;'
        f'border-radius:12px;padding:32px;'
        f'font-family:Helvetica,Arial,sans-serif;color:{_INK};line-height:1.55;">'
        f'<div style="font-size:20px;font-weight:700;color:{_SEA};'
        f'margin-bottom:20px;">{_html.escape(_BRAND)}</div>'
        f'<h1 style="font-size:19px;margin:0 0 14px;color:{_INK};">{_html.escape(title)}</h1>'
        f"{body_html}"
        f'<p style="font-size:13px;color:{_MUTED};margin-top:28px;">'
        f"Ce message vous est envoyé automatiquement par {_html.escape(_BRAND)}. "
        f"Merci de ne pas y répondre.</p>"
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
