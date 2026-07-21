"""Tests unitaires de l'infrastructure email (V2-08, volet 1).

Aucun réseau : on vérifie la construction du message MIME, les gabarits FR
(sujet + lien présent en texte ET en HTML) et le repli ConsoleMailer.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # racine backend/

from api import emails  # noqa: E402
from api.mailer import ConsoleMailer, Email, _build_message  # noqa: E402


def test_reset_email_contains_link_in_both_parts():
    url = "https://holaguia.com/#/reset/deadbeef"
    e = emails.reset_password_email(url, "André")
    assert isinstance(e, Email)
    assert "Holaguia" in e.subject
    assert url in e.text
    assert url in e.html
    assert "60 minutes" in e.text


def test_verify_email_contains_link_and_greeting():
    url = "https://holaguia.com/#/verify/cafe"
    e = emails.verify_email(url, None)
    assert url in e.text and url in e.html
    assert "Bonjour," in e.text  # sans nom → salutation neutre


def test_build_message_is_multipart_with_from_and_subject():
    e = emails.reset_password_email("https://x/y", None)
    msg = _build_message("Holaguia <no-reply@holaguia.com>", "user@example.com", e)
    assert msg.is_multipart()
    assert msg["To"] == "user@example.com"
    assert msg["From"] == "Holaguia <no-reply@holaguia.com>"
    assert msg["Subject"] == e.subject
    # texte + html
    subtypes = {p.get_content_subtype() for p in msg.iter_parts()}
    assert {"plain", "html"} <= subtypes


def test_build_message_accepts_bare_from_address():
    e = emails.verify_email("https://x/y", None)
    msg = _build_message("no-reply@holaguia.com", "u@e.com", e)
    assert msg["From"] == "no-reply@holaguia.com"


def test_console_mailer_records_without_sending():
    m = ConsoleMailer(from_addr="Holaguia <no-reply@holaguia.com>")
    e = emails.reset_password_email("https://x/y", None)
    m.send("dest@example.com", e)
    assert m.sent == [("dest@example.com", e)]
