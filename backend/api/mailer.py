"""Envoi d'emails transactionnels (V2-08).

Deux implémentations derrière une même interface `Mailer` (injectable via
`deps.get_mailer`) :

  - `SmtpMailer`   : envoi réel via smtplib en SSL (port 465, Infomaniak
                     `mail.infomaniak.com`). Aucun secret en dur — les
                     identifiants viennent de l'environnement (settings).
  - `ConsoleMailer`: développement / tests — journalise l'email au lieu de
                     l'envoyer (jamais de jeton en clair dans les logs : c'est
                     l'appelant qui choisit ce qu'il met dans le corps, et les
                     liens de réinitialisation ne sont journalisés qu'en DEBUG).

Le choix se fait au démarrage (`deps.build_mailer`) : SMTP si configuré, sinon
repli `ConsoleMailer` + avertissement (même motif que `missing_production_config`).
"""
from __future__ import annotations

import logging
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, parseaddr
from typing import Protocol

log = logging.getLogger("casaguide.mail")


@dataclass(frozen=True)
class Email:
    """Un email prêt à partir : sujet + variantes texte et HTML (multipart/alternative)."""
    subject: str
    text: str
    html: str


class Mailer(Protocol):
    """Interface minimale — une seule méthode, testable et remplaçable.

    `bcc` (V2-36 pièce 2) : copie cachée facultative — l'envoi automatique du guide y
    met le propriétaire (email de son compte) pour preuve de service rendu. Kwarg
    optionnel : les appelants qui n'en ont pas besoin (emails owner) l'ignorent."""

    def send(self, to: str, email: Email, *, bcc: str | None = None) -> None: ...


def _build_message(from_addr: str, to: str, email: Email,
                   *, bcc: str | None = None) -> EmailMessage:
    """Construit le message MIME (UTF-8, texte + HTML). Partagé par les deux mailers.
    Un `bcc` renseigné pose un en-tête `Bcc` : `smtplib.send_message` l'inclut dans
    les destinataires de l'enveloppe SMTP mais le **retire** du message transmis (le
    destinataire principal ne voit jamais la copie cachée)."""
    msg = EmailMessage()
    # Normalise l'expéditeur : accepte « Nom <a@b> » comme « a@b ».
    name, addr = parseaddr(from_addr)
    msg["From"] = formataddr((name, addr)) if name else addr
    msg["To"] = to
    if bcc:
        msg["Bcc"] = bcc
    msg["Subject"] = email.subject
    msg.set_content(email.text)
    msg.add_alternative(email.html, subtype="html")
    return msg


class SmtpMailer:
    """Envoi réel via SMTP en SSL (smtplib.SMTP_SSL, port 465 chez Infomaniak)."""

    def __init__(self, *, host: str, port: int, user: str, password: str,
                 from_addr: str, timeout: float = 15.0) -> None:
        self._host = host
        self._port = port
        self._user = user
        self._password = password
        self._from = from_addr
        self._timeout = timeout

    def send(self, to: str, email: Email, *, bcc: str | None = None) -> None:
        msg = _build_message(self._from, to, email, bcc=bcc)
        # SSL direct (implicite) sur 465 — pas de STARTTLS.
        with smtplib.SMTP_SSL(self._host, self._port, timeout=self._timeout) as smtp:
            smtp.login(self._user, self._password)
            smtp.send_message(msg)   # inclut le Bcc dans l'enveloppe, le retire du corps
        log.info("Email « %s » envoyé à %s%s via %s", email.subject, to,
                 " (Cci propriétaire)" if bcc else "", self._host)


class ConsoleMailer:
    """Repli développement/tests : journalise l'email au lieu de l'envoyer.

    Le sujet et le destinataire sont journalisés en INFO ; le corps (qui peut
    contenir un lien à jeton) uniquement en DEBUG, pour ne jamais fuiter de
    jeton dans les logs de production par défaut."""

    def __init__(self, *, from_addr: str = "") -> None:
        self._from = from_addr
        self.sent: list[tuple[str, Email]] = []  # inspectable dans les tests
        self.bccs: list[str | None] = []         # Cci aligné sur `sent` (V2-36)

    def send(self, to: str, email: Email, *, bcc: str | None = None) -> None:
        self.sent.append((to, email))
        self.bccs.append(bcc)
        log.info("[ConsoleMailer] email « %s » → %s%s (non envoyé, repli console)",
                 email.subject, to, " (Cci propriétaire)" if bcc else "")
        log.debug("[ConsoleMailer] corps texte :\n%s", email.text)
