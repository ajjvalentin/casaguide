"""Fenêtre « Envoyer le guide » — génération des liens à la demande (V2-23c, volet 3).

Deux endpoints **propriétaire authentifiés** (via `OwnedProperty`) qui génèrent le
token du lien au **premier usage** puis le réutilisent (idempotent) :

    POST /api/properties/{id}/showcase-link                → lien VITRINE  `/v/{token}`
    POST /api/properties/{id}/bookings/{bid}/stay-link     → lien SÉJOUR   `/b/{token}`

Le token n'est **jamais** créé ni renvoyé par une route publique : c'est ici, côté
propriétaire, que la fenêtre d'envoi le fabrique. Le lien **maison** (`/g/{guide_
token}`, QR imprimé) n'a pas d'endpoint — le front le construit depuis le
`guide_token` déjà connu (le lien maison SURVIT tel quel, il n'est pas généré).

Les liens de séjour et de vitrine n'ont **pas** de slug décoratif (`_real_token`
ne s'applique qu'à `/g/`) : ils sont *envoyés, jamais retapés*.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status

from .. import repo
from ..config import settings
from ..deps import Conn, CurrentOwner, OwnedProperty
from ..schemas import ShareLinkOut

router = APIRouter(prefix="/api/properties/{property_id}", tags=["partage"])


def _public_base(request: Request) -> str:
    """Origine publique des liens (même règle que le poster/les emails, M-07/V2-08) :
    `CASAGUIDE_PUBLIC_BASE_URL` sinon l'origine de la requête."""
    return (settings.public_base_url or str(request.base_url)).rstrip("/")


@router.post("/showcase-link", response_model=ShareLinkOut)
def showcase_link(conn: Conn, owner: CurrentOwner, prop: OwnedProperty,
                  request: Request):
    """Lien vitrine du logement (`/v/{showcase_token}`), généré au premier usage
    puis réutilisé. Ne montre jamais un secret réel (valeurs d'exemple) — c'est
    l'outil de vente (annonce/prospect/démo)."""
    token = repo.ensure_showcase_token(conn, str(owner["id"]), str(prop["id"]))
    if not token:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Logement introuvable")
    return ShareLinkOut(token=token, url=f"{_public_base(request)}/v/{token}")


@router.post("/bookings/{booking_id}/stay-link", response_model=ShareLinkOut)
def stay_link(booking_id: str, conn: Conn, prop: OwnedProperty, request: Request):
    """Lien de séjour du locataire (`/b/{stay_token}`), généré au premier usage
    puis réutilisé (idempotent, atomique côté base — deux clics ne créent qu'un
    token). 404 si le séjour n'appartient pas au logement (garde-fou multi-tenant).
    Le `guide_token` éternel ne voyage jamais dans ce lien (résolution côté serveur)."""
    token = repo.ensure_stay_token(conn, str(prop["id"]), booking_id)
    if not token:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Séjour introuvable")
    return ShareLinkOut(token=token, url=f"{_public_base(request)}/b/{token}")
