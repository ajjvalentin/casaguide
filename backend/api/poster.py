"""Affiche « QR code à imprimer » du guide voyageur (M-07, §3.2).

À la publication d'un logement, le propriétaire télécharge un PDF élégant (A5 ou
A4) reprenant l'identité CasaGuide (sable/mer, titres façon Fraunces) : nom du
logement, QR code du lien du guide, et un court mot d'accueil FR/EN. Il est
imprimé et laissé dans le logement pour que les voyageurs scannent et ouvrent
leur guide en un geste.

Génération 100 % côté serveur avec reportlab (le QR natif de reportlab évite
toute dépendance supplémentaire). Aucun secret n'y figure : seul le lien public
`/g/{guide_token}` (déjà non secret au sens où il tient lieu de clé d'accès) est
encodé — jamais le wifi ni le code de la boîte à clés.
"""
from __future__ import annotations

import io

from reportlab.graphics import renderPDF
from reportlab.graphics.barcode import qr
from reportlab.graphics.shapes import Drawing
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4, A5
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

# Jetons visuels du prototype validé (guide_preview.html / guide.css)
_SAND = HexColor("#FAF7F2")
_INK = HexColor("#1E2A32")
_SEA = HexColor("#0E5A73")
_MUTED = HexColor("#6B7A84")
_LINE = HexColor("#E7E0D4")

_SIZES = {"a5": A5, "a4": A4}

# Textes du poster localisés (M-26 : menu FR/EN/ES). La « mention wifi » figure
# dans la phrase d'accueil (arrivée, wifi, urgences…). Repli français.
_TEXT: dict[str, dict[str, str]] = {
    "fr": {
        "eyebrow": "VOTRE GUIDE DE SÉJOUR",
        "welcome": "Scannez ce QR code pour ouvrir votre guide de séjour : "
                   "arrivée, wifi, urgences, commerces et bonnes adresses du quartier.",
        "title": "QR code du guide",
    },
    "en": {
        "eyebrow": "YOUR STAY GUIDE",
        "welcome": "Scan this QR code to open your stay guide: check-in, wifi, "
                   "emergencies, shops and the best spots nearby.",
        "title": "Guide QR code",
    },
    "es": {
        "eyebrow": "TU GUÍA DE ESTANCIA",
        "welcome": "Escanea este código QR para abrir tu guía de estancia: "
                   "llegada, wifi, urgencias, comercios y los mejores sitios del barrio.",
        "title": "Código QR de la guía",
    },
}


def _spaced(text: str) -> str:
    """« VOTRE GUIDE » → « V O T R E   G U I D E » (surtitre aéré, façon capitales
    espacées) tout en restant localisable (les accents sont préservés)."""
    return "   ".join(" ".join(word) for word in text.split(" "))


def _wrap(c: canvas.Canvas, text: str, font: str, size: float,
          max_width: float) -> list[str]:
    """Découpe `text` en lignes tenant dans `max_width` (points)."""
    lines: list[str] = []
    for para in text.split("\n"):
        words = para.split(" ")
        cur = ""
        for w in words:
            trial = (cur + " " + w).strip()
            if c.stringWidth(trial, font, size) <= max_width or not cur:
                cur = trial
            else:
                lines.append(cur)
                cur = w
        lines.append(cur)
    return lines


def build_guide_poster(*, property_name: str, guide_url: str,
                       city: str | None = None, size: str = "a5",
                       lang: str = "fr") -> bytes:
    """PDF de l'affiche QR (octets). `size` ∈ {'a5','a4'} ; `lang` ∈ {'fr','en','es'}
    localise entièrement le surtitre, le mot d'accueil et la mention wifi (M-26)."""
    txt = _TEXT.get(lang, _TEXT["fr"])
    page = _SIZES.get(size.lower(), A5)
    pw, ph = page
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=page)
    c.setTitle(f"{property_name} — {txt['title']}")

    # Fond sable + cadre fin (l'affiche reste élégante même en N&B).
    c.setFillColor(_SAND)
    c.rect(0, 0, pw, ph, fill=1, stroke=0)
    c.setStrokeColor(_LINE)
    c.setLineWidth(1)
    m = 12 * mm
    c.rect(m, m, pw - 2 * m, ph - 2 * m, fill=0, stroke=1)

    cx = pw / 2
    inner = pw - 2 * (m + 8 * mm)

    # Surtitre (localisé, capitales espacées)
    y = ph - m - 20 * mm
    c.setFillColor(_SEA)
    c.setFont("Helvetica-Bold", 10)
    c.drawCentredString(cx, y, _spaced(txt["eyebrow"]))

    # Nom du logement (titre — Times fait office de serif façon Fraunces)
    y -= 16 * mm
    title_size = 30 if size.lower() == "a4" else 24
    c.setFillColor(_INK)
    for line in _wrap(c, property_name, "Times-Bold", title_size, inner):
        c.setFont("Times-Bold", title_size)
        c.drawCentredString(cx, y, line)
        y -= (title_size + 4)
    if city:
        c.setFont("Helvetica", 12)
        c.setFillColor(_MUTED)
        c.drawCentredString(cx, y - 2 * mm, city)
        y -= 8 * mm

    # QR code du lien du guide, sur pastille blanche pour le contraste/scan
    qr_side = (72 if size.lower() == "a4" else 58) * mm
    pad = 6 * mm
    qy = y - 12 * mm - qr_side
    c.setFillColor(HexColor("#FFFFFF"))
    c.setStrokeColor(_LINE)
    c.roundRect(cx - qr_side / 2 - pad, qy - pad,
                qr_side + 2 * pad, qr_side + 2 * pad, 8, fill=1, stroke=1)
    widget = qr.QrCodeWidget(guide_url, barLevel="M")
    b = widget.getBounds()
    bw, bh = b[2] - b[0], b[3] - b[1]
    d = Drawing(qr_side, qr_side,
                transform=[qr_side / bw, 0, 0, qr_side / bh, 0, 0])
    d.add(widget)
    renderPDF.draw(d, c, cx - qr_side / 2, qy)

    # Mot d'accueil localisé (une seule langue, choisie par le propriétaire)
    ty = qy - 14 * mm
    c.setFillColor(_INK)
    for line in _wrap(c, txt["welcome"], "Helvetica", 12, inner):
        c.setFont("Helvetica", 12)
        c.drawCentredString(cx, ty, line)
        ty -= 16

    # Pied de page (identité)
    c.setFillColor(_SEA)
    c.setFont("Helvetica-Bold", 9)
    c.drawCentredString(cx, m + 8 * mm, "CasaGuide")

    c.showPage()
    c.save()
    return buf.getvalue()
