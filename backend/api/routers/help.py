"""Journal des recherches d'aide (V2-31, volet 3a).

La recherche d'aide vit **côté front** (index statique `frontend/js/help/`, aucune
dépendance serveur) : le back ne fait que **tracer** chaque recherche pour mesurer
la santé de l'index. Le taux de zéro-résultat (`results_count = 0`) désigne les
questions du terrain que l'index ne couvre pas encore.

    POST /api/help/searches   { query, results_count }   → 204

L'écriture est **best-effort** : un échec de journal (base indisponible, contrainte)
ne doit jamais casser la recherche, qui a déjà rendu son résultat côté navigateur.
On avale donc toute exception et on renvoie 204 dans tous les cas (le front
n'attend rien du corps). Réservé aux propriétaires authentifiés — aucune donnée
personnelle au-delà de l'auteur, du texte tapé et du compte de résultats.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Response, status

from .. import repo
from ..deps import Conn, CurrentOwner
from ..schemas import HelpSearchIn

log = logging.getLogger("casaguide.help")

router = APIRouter(prefix="/api/help", tags=["aide"])


@router.post("/searches", status_code=status.HTTP_204_NO_CONTENT)
def log_search(payload: HelpSearchIn, conn: Conn, owner: CurrentOwner) -> Response:
    """Journalise une recherche d'aide (best-effort). Renvoie 204 même en cas
    d'échec d'écriture — la recherche est purement front, jamais bloquée par son
    journal."""
    try:
        repo.record_help_search(conn, owner_id=str(owner["id"]),
                                query=payload.query.strip(),
                                results_count=payload.results_count)
        conn.commit()
    except Exception:  # noqa: BLE001 — journal best-effort, jamais bloquant
        log.warning("Journal de recherche d'aide non écrit.", exc_info=True)
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
    return Response(status_code=status.HTTP_204_NO_CONTENT)
