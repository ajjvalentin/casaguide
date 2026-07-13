"""Médias par section : téléversement, liste, service, légende, ordre (M-12).

Le propriétaire illustre chaque section (télécommandes, boîte à clés, plan des
poubelles, façade…). Stockage local abstrait (prêt pour S3, voir `storage.py`),
clés non devinables, validation stricte du type réel et du poids, ré-encodage
des images (strip EXIF). Isolation multi-tenant : tout passe par `OwnedProperty`.
"""
from __future__ import annotations

from fastapi import (APIRouter, File, Form, HTTPException, Query, Response,
                     UploadFile, status)

from .. import media_files, repo, storage
from ..config import settings
from ..deps import Conn, OwnedProperty
from ..schemas import MediaCaptionIn, MediaOut, MediaReorderIn

router = APIRouter(prefix="/api/properties/{property_id}/media", tags=["media"])


def _media_out(row: dict, property_id: str) -> MediaOut:
    return MediaOut(
        id=row["id"],
        section_code=row.get("section_code"),
        kind=row["kind"],
        caption=row.get("caption"),
        sort_order=row["sort_order"],
        url=f"/api/properties/{property_id}/media/{row['id']}/file",
        created_at=row["created_at"],
    )


@router.get("", response_model=list[MediaOut])
def list_media(conn: Conn, prop: OwnedProperty,
               section_code: str | None = Query(default=None)):
    """Médias du logement, éventuellement filtrés sur une section (`?section_code=`)."""
    pid = str(prop["id"])
    if section_code is not None:
        section_id = repo.get_section_id(conn, pid, section_code)
        if section_id is None:  # section jamais instanciée -> aucun média
            return []
        rows = repo.list_media(conn, pid, section_id=section_id, all_sections=False)
    else:
        rows = repo.list_media(conn, pid)
    return [_media_out(r, pid) for r in rows]


@router.post("", response_model=MediaOut, status_code=status.HTTP_201_CREATED)
async def upload_media(
    conn: Conn, prop: OwnedProperty,
    file: UploadFile = File(...),
    section_code: str | None = Form(default=None),
    caption: str | None = Form(default=None),
):
    """Téléverse un média (multipart) et le rattache à une section (ou au logement).

    Validation : type réel jpeg/png/webp/pdf (magic bytes), 10 Mo max. Les images
    sont ré-encodées (EXIF retiré, réduites à 2000 px)."""
    pid = str(prop["id"])

    # Lecture bornée : un octet de plus que la limite suffit à la détecter sans
    # charger un fichier démesuré en mémoire.
    data = await file.read(settings.max_upload_bytes + 1)
    if len(data) > settings.max_upload_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"Fichier trop volumineux (maximum {settings.max_upload_bytes // (1024 * 1024)} Mo)")
    if not data:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="Fichier vide")

    mime = media_files.sniff(data)
    if mime not in media_files.ALLOWED:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Format non pris en charge (acceptés : JPEG, PNG, WebP, PDF)")
    kind, ext = media_files.ALLOWED[mime]
    if kind == "photo":
        data = media_files.process_image(data, mime)

    # Rattachement à une section : la créer si besoin pour disposer d'un section_id.
    section_id: str | None = None
    if section_code:
        if not repo.section_template_exists(conn, section_code):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail=f"Section inconnue : {section_code}")
        section_id = repo.ensure_section(conn, pid, section_code)

    key = storage.new_key(pid, ext)
    store = storage.get_storage()
    store.write(key, data)
    try:
        row = repo.create_media(conn, pid, section_id, kind, key, caption)
    except Exception:  # écriture DB échouée : ne pas laisser de fichier orphelin
        store.delete(key)
        raise
    row = dict(row)
    row["section_code"] = section_code
    return _media_out(row, pid)


# `/reorder` (POST) est distinct de `/{media_id}` : aucune ambiguïté de route.
@router.post("/reorder", response_model=list[MediaOut])
def reorder_media(payload: MediaReorderIn, conn: Conn, prop: OwnedProperty):
    """Applique un nouvel ordre d'affichage aux médias du logement."""
    pid = str(prop["id"])
    repo.reorder_media(conn, pid, [str(i) for i in payload.ids])
    return [_media_out(r, pid) for r in repo.list_media(conn, pid)]


@router.get("/{media_id}/file")
def serve_media(media_id: str, conn: Conn, prop: OwnedProperty):
    """Sert le fichier au propriétaire authentifié (aperçu dans l'éditeur)."""
    row = repo.get_media(conn, str(prop["id"]), media_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Média introuvable")
    try:
        data = storage.get_storage().read(row["storage_key"])
    except FileNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Fichier introuvable")
    return Response(content=data,
                    media_type=media_files.content_type_for_key(row["storage_key"]),
                    headers={"Cache-Control": "private, max-age=3600"})


@router.patch("/{media_id}", response_model=MediaOut)
def update_media(media_id: str, payload: MediaCaptionIn, conn: Conn,
                 prop: OwnedProperty):
    """Modifie la légende d'un média."""
    pid = str(prop["id"])
    if not repo.update_media_caption(conn, pid, media_id, payload.caption):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Média introuvable")
    return _media_out(repo.get_media_full(conn, pid, media_id), pid)


@router.delete("/{media_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_media(media_id: str, conn: Conn, prop: OwnedProperty):
    row = repo.delete_media(conn, str(prop["id"]), media_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Média introuvable")
    storage.get_storage().delete(row["storage_key"])
    return None
