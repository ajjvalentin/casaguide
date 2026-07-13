"""Validation et normalisation des médias téléversés (M-12).

Sécurité du téléversement (§8) :
  * le **type réel** est déterminé par les octets d'en-tête (magic bytes), pas
    par le nom de fichier ni l'entête Content-Type déclaré par le client ;
  * seuls jpeg / png / webp / pdf sont acceptés ;
  * les images sont ré-encodées via Pillow (si disponible), ce qui **retire les
    métadonnées EXIF** (dont la géolocalisation) et applique l'orientation, avec
    une réduction à 2000 px de côté maximum pour limiter le poids.

La taille maximale (10 Mo par défaut) est vérifiée en amont, à la lecture du
flux (voir le routeur), pour ne jamais charger un fichier démesuré en mémoire.
"""
from __future__ import annotations

import io
import logging

log = logging.getLogger("casaguide.api")

# mime réel -> (kind stocké, extension de fichier)
ALLOWED: dict[str, tuple[str, str]] = {
    "image/jpeg": ("photo", "jpg"),
    "image/png": ("photo", "png"),
    "image/webp": ("photo", "webp"),
    "application/pdf": ("pdf", "pdf"),
}

# extension -> mime, pour servir le fichier avec le bon Content-Type
_MIME_BY_EXT = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "png": "image/png", "webp": "image/webp", "pdf": "application/pdf",
}

_SAVE_FORMAT = {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP"}
_MAX_DIM = 2000  # px : côté le plus long après réduction


def sniff(data: bytes) -> str | None:
    """Type MIME réel d'après les octets d'en-tête, ou None si non reconnu."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:5] == b"%PDF-":
        return "application/pdf"
    return None


def content_type_for_key(key: str) -> str:
    """Content-Type de service déduit de l'extension de la clé de stockage."""
    ext = key.rsplit(".", 1)[-1].lower() if "." in key else ""
    return _MIME_BY_EXT.get(ext, "application/octet-stream")


def process_image(data: bytes, mime: str) -> bytes:
    """Ré-encode une image en retirant l'EXIF et en la réduisant si besoin.

    Sans effet (retourne les octets d'origine) si Pillow est absent ou si le
    décodage échoue — le fichier reste alors celui validé par `sniff()`."""
    try:
        from PIL import Image, ImageOps
    except ModuleNotFoundError:  # pragma: no cover - dépend de l'installation
        return data
    try:
        with Image.open(io.BytesIO(data)) as im:
            im = ImageOps.exif_transpose(im)  # applique l'orientation, retire l'EXIF
            if max(im.size) > _MAX_DIM:
                im.thumbnail((_MAX_DIM, _MAX_DIM))
            fmt = _SAVE_FORMAT[mime]
            params: dict = {}
            if fmt == "JPEG":
                im = im.convert("RGB")
                params = {"quality": 85, "optimize": True}
            elif fmt == "WEBP":
                params = {"quality": 85, "method": 4}
            out = io.BytesIO()
            im.save(out, format=fmt, **params)
            return out.getvalue()
    except Exception:  # décodage impossible : conserver l'original validé
        log.warning("Ré-encodage image impossible ; original conservé", exc_info=True)
        return data
