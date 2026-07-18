"""Image de marque pour les liens de partage (Open Graph, M-25).

Repli quand le logement n'a **aucune** photo : une vignette 1200×630 sobre
(fond sable, nom du logement, identité CasaGuide) que les scrapers WhatsApp /
iMessage / e-mail peuvent afficher à la place de l'URL technique. Générée avec
Pillow (déjà dépendance, M-12) — aucun secret, aucun appel externe.
"""
from __future__ import annotations

import io

from PIL import Image, ImageDraw, ImageFont

# Jetons visuels du prototype validé (guide.css / poster.py)
_SAND = (250, 247, 242)
_INK = (30, 42, 50)
_SEA = (14, 90, 115)
_MUTED = (107, 122, 132)
_LINE = (231, 224, 212)

_W, _H = 1200, 630

# Polices TrueType candidates (Linux prod : DejaVu ; macOS : Helvetica/Arial).
_FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/Library/Fonts/Arial.ttf",
]


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """Police à la taille voulue : TrueType si disponible, sinon police par
    défaut redimensionnable (Pillow ≥ 10.1)."""
    paths = _FONT_PATHS
    if bold:
        paths = [p for p in _FONT_PATHS if "Bold" in p] + _FONT_PATHS
    for p in paths:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # très vieux Pillow : police bitmap non redimensionnable
        return ImageFont.load_default()


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont,
          max_width: int) -> list[str]:
    lines: list[str] = []
    for para in text.split("\n"):
        cur = ""
        for word in para.split(" "):
            trial = (cur + " " + word).strip()
            if draw.textlength(trial, font=font) <= max_width or not cur:
                cur = trial
            else:
                lines.append(cur)
                cur = word
        lines.append(cur)
    return lines


def build_og_image(property_name: str, subtitle: str = "",
                   eyebrow: str = "VOTRE GUIDE DE SÉJOUR") -> bytes:
    """Vignette PNG 1200×630 de marque (octets)."""
    img = Image.new("RGB", (_W, _H), _SAND)
    d = ImageDraw.Draw(img)

    # Cadre fin
    d.rectangle([24, 24, _W - 25, _H - 25], outline=_LINE, width=2)

    inner = _W - 220
    cx = _W // 2

    # Surtitre
    f_eye = _font(26, bold=True)
    ew = d.textlength(eyebrow, font=f_eye)
    d.text((cx - ew / 2, 118), eyebrow, font=f_eye, fill=_SEA)

    # Nom du logement (titre, taille adaptée au nombre de lignes)
    name = (property_name or "CasaGuide").strip()
    size = 84
    f_title = _font(size, bold=True)
    lines = _wrap(d, name, f_title, inner)
    while len(lines) > 3 and size > 44:
        size -= 8
        f_title = _font(size, bold=True)
        lines = _wrap(d, name, f_title, inner)
    line_h = size + 14
    total_h = line_h * len(lines)
    y = (_H - total_h) // 2
    for ln in lines:
        w = d.textlength(ln, font=f_title)
        d.text((cx - w / 2, y), ln, font=f_title, fill=_INK)
        y += line_h

    # Sous-titre (ville) éventuel
    if subtitle:
        f_sub = _font(34)
        sw = d.textlength(subtitle, font=f_sub)
        d.text((cx - sw / 2, y + 6), subtitle, font=f_sub, fill=_MUTED)

    # Filet + signature
    d.line([(cx - 60, _H - 108), (cx + 60, _H - 108)], fill=_LINE, width=2)
    f_sig = _font(30, bold=True)
    sig = "CasaGuide"
    sgw = d.textlength(sig, font=f_sig)
    d.text((cx - sgw / 2, _H - 88), sig, font=f_sig, fill=_SEA)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
