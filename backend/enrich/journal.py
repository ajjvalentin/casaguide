"""Journal daté des modifications et retour arrière (V2-92, migration 043).

Le journal (`content_changes`) est écrit par le DÉCLENCHEUR `trg_pois_journal`, jamais
par le code : ce module ne fait que DÉCLARER l'auteur de la transaction courante et
ANNULER une modification.

Déclaration de l'auteur — trois variables de TRANSACTION (`set_config(…, true)`),
jamais de session (une valeur de session fuirait d'une requête à la suivante sur une
connexion réutilisée) : `holaguia.actor`, `holaguia.batch_id`, `holaguia.job_id`.
Sans déclaration, le déclencheur écrit `system`.

- `declare(conn, actor, …)` : LA primitive — pose les trois variables pour la
  transaction en cours.
- `acting(conn, actor)` : gestionnaire de contexte — déclare le temps d'un bloc puis
  restaure l'auteur précédent (le juge à l'intérieur d'une exécution du pipeline
  redevient `system` en sortie).
- `bind(conn, actor, …)` : liaison DURABLE pour une connexion qui valide plusieurs
  fois (le pipeline : ~30 `commit` par exécution). Une variable de transaction meurt
  au `commit` ; `JournalConnection` la REPOSE en tête de chaque nouvelle transaction
  (premier `execute` sur une connexion au repos, ou `transaction()` ouverte au repos).
  Repose PARESSEUSE et non « juste après le commit » : reposer tout de suite
  ouvrirait une transaction, et les `with conn.transaction()` du pipeline, conçus
  comme SAVEPOINT, changeraient de sens là où ils ouvrent aujourd'hui une vraie
  transaction (et la connexion resterait « idle in transaction » pendant les appels
  réseau).

Retour arrière : `revert_change` / `revert_batch` — seules les modifications d'auteur
`system` ou `judge` s'annulent ; annulation SÛRE (rien n'est restauré si la valeur a
changé depuis ou si le propriétaire a repris la main sur le champ) ; chaque
annulation écrit sa propre ligne `revert` (via le déclencheur, `holaguia.reverts_id`).
"""
from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg import pq

ACTORS = ("system", "judge", "owner", "support")
REVERTIBLE_ACTORS = ("system", "judge")

_SET_SQL = ("SELECT set_config('holaguia.actor', %s, true), "
            "set_config('holaguia.batch_id', %s, true), "
            "set_config('holaguia.job_id', %s, true)")
_GET_SQL = ("SELECT current_setting('holaguia.actor', true) AS actor, "
            "current_setting('holaguia.batch_id', true) AS batch_id, "
            "current_setting('holaguia.job_id', true) AS job_id")


def _check_actor(actor: str) -> None:
    if actor not in ACTORS:
        raise ValueError(f"Auteur inconnu : {actor!r} (attendu : {ACTORS})")


def _s(v: Any) -> str:
    return "" if v is None else str(v)


class JournalConnection(psycopg.Connection):
    """Connexion qui REPOSE l'auteur lié (`bind`) en tête de chaque transaction.
    Sans liaison, strictement identique à `psycopg.Connection`. `db.connect()` la
    renvoie partout : l'API (une connexion par requête, jamais liée) n'en voit rien."""

    _journal_ctx: tuple[str, str, str] | None = None

    def _journal_repose(self, *, force: bool = False) -> None:
        if self._journal_ctx is None:
            return
        if force or self.info.transaction_status == pq.TransactionStatus.IDLE:
            psycopg.Connection.execute(self, _SET_SQL, self._journal_ctx)

    def execute(self, *args, **kwargs):  # type: ignore[override]
        self._journal_repose()
        return super().execute(*args, **kwargs)

    @contextmanager
    def transaction(self, *args, **kwargs) -> Iterator[psycopg.Transaction]:  # type: ignore[override]
        idle = self.info.transaction_status == pq.TransactionStatus.IDLE
        with super().transaction(*args, **kwargs) as tx:
            if idle:   # vraie transaction (BEGIN) : reposer DEDANS
                self._journal_repose(force=True)
            yield tx


def declare(conn, actor: str, *, batch_id: str | None = None,
            job_id: str | None = None) -> None:
    """Pose auteur / lot / job pour la TRANSACTION COURANTE (portée locale)."""
    _check_actor(actor)
    conn.execute(_SET_SQL, (actor, _s(batch_id), _s(job_id)))


@contextmanager
def acting(conn, actor: str, *, batch_id: str | None = None,
           job_id: str | None = None) -> Iterator[None]:
    """Déclare `actor` le temps du bloc, puis restaure l'auteur précédent de la
    transaction (lot/job hérités si non fournis). Ne valide rien."""
    prev = _one(conn, _GET_SQL)
    declare(conn, actor,
            batch_id=batch_id if batch_id is not None else (prev["batch_id"] or None),
            job_id=job_id if job_id is not None else (prev["job_id"] or None))
    try:
        yield
    finally:
        if conn.info.transaction_status == pq.TransactionStatus.INTRANS:
            conn.execute(_SET_SQL, (_s(prev["actor"]), _s(prev["batch_id"]),
                                    _s(prev["job_id"])))


def bind(conn, actor: str, *, batch_id: str | None = None,
         job_id: str | None = None) -> None:
    """Lie DURABLEMENT auteur/lot/job à une `JournalConnection` : reposés en tête
    de chaque transaction, donc à travers les `commit` (pipeline)."""
    _check_actor(actor)
    if not isinstance(conn, JournalConnection):
        raise TypeError("bind() exige une JournalConnection (enrich.db.connect())")
    conn._journal_ctx = (actor, _s(batch_id), _s(job_id))
    conn._journal_repose(force=conn.info.transaction_status
                         == pq.TransactionStatus.INTRANS)


def unbind(conn) -> None:
    if isinstance(conn, JournalConnection):
        conn._journal_ctx = None


# ── Retour arrière ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RevertResult:
    """Issue d'une annulation. `outcome` : 'reverted' | 'conflict' | 'refused'.
    `reason` nomme le conflit / refus (None si annulée)."""
    change_id: int
    outcome: str
    reason: str | None = None
    revert_id: int | None = None
    entity_id: str | None = None
    field: str | None = None
    action: str | None = None


def _one(conn, sql: str, params=()) -> dict | None:
    """Une ligne en dict, quelle que soit la fabrique de lignes de la connexion."""
    cur = conn.execute(sql, params)
    r = cur.fetchone()
    if r is None or isinstance(r, dict):
        return r
    return {d.name: v for d, v in zip(cur.description, r)}


def revert_change(conn, change_id: int, *, actor: str,
                  batch_id: str | None = None) -> RevertResult:
    """Annule UNE modification du journal. N'écrit que si l'annulation est sûre ;
    sinon renvoie un conflit nommé sans rien toucher. Ne valide pas (l'appelant
    commit) ; l'écriture se fait dans un SAVEPOINT."""
    _check_actor(actor)
    ch = _one(conn,
              """SELECT id, entity, entity_id, action, field, old_value, new_value, actor
                 FROM content_changes WHERE id = %s FOR UPDATE""", (change_id,))
    if ch is None:
        return RevertResult(change_id, "refused", "not_found")
    base = {"entity_id": str(ch["entity_id"]), "field": ch["field"],
            "action": ch["action"]}

    def res(outcome, reason=None, revert_id=None):
        return RevertResult(change_id, outcome, reason, revert_id, **base)

    if ch["action"] == "revert":
        return res("refused", "is_revert")
    if ch["actor"] not in REVERTIBLE_ACTORS:
        return res("refused", f"actor_{ch['actor']}")
    if ch["entity"] != "poi":
        return res("refused", "unsupported_entity")
    if ch["action"] == "create":
        return res("refused", "unsupported_action")
    if _one(conn, "SELECT 1 AS x FROM content_changes WHERE reverts_id = %s",
            (change_id,)):
        return res("refused", "already_reverted")

    cur = _one(conn, "SELECT journal_poi_snapshot(p) AS snap, p.human_fields "
                     "FROM pois p WHERE p.id = %s FOR UPDATE", (ch["entity_id"],))
    old, new = ch["old_value"], ch["new_value"]
    if ch["action"] == "delete":
        if cur is not None:
            return res("conflict", "poi_exists")
    else:
        if cur is None:
            return res("conflict", "poi_missing")
        snap = cur["snap"]
        if ch["action"] == "status":
            if (snap.get("status"), snap.get("status_by")) != (new.get("status"),
                                                               new.get("status_by")):
                return res("conflict", "value_changed")
        else:
            if ch["field"] in (cur["human_fields"] or []):
                return res("conflict", "human_locked")
            if snap.get(ch["field"]) != new:
                return res("conflict", "value_changed")

    # SAVEPOINT + auteur restauré en sortie : l'appelant retrouve son contexte.
    with conn.transaction(), acting(conn, actor, batch_id=batch_id):
        conn.execute("SELECT set_config('holaguia.reverts_id', %s, true)",
                     (str(change_id),))
        if ch["action"] == "delete":
            snap = dict(old)
            g = snap.pop("geom")
            snap["geom"] = f"SRID=4326;POINT({g['lon']} {g['lat']})"
            conn.execute(
                "INSERT INTO pois SELECT (jsonb_populate_record(NULL::pois, %s::jsonb)).*",
                (json.dumps(snap),))
        elif ch["action"] == "status":
            conn.execute(
                "UPDATE pois SET status = %s, status_by = %s, status_at = %s::timestamptz "
                "WHERE id = %s",
                (old.get("status"), old.get("status_by"), old.get("status_at"),
                 ch["entity_id"]))
        elif ch["field"] == "geom":
            conn.execute(
                "UPDATE pois SET geom = ST_SetSRID(ST_MakePoint(%s, %s), 4326) WHERE id = %s",
                (old["lon"], old["lat"], ch["entity_id"]))
        else:
            f = ch["field"]
            conn.execute(
                f"UPDATE pois SET {f} = (jsonb_populate_record(NULL::pois, "
                f"jsonb_build_object(%s::text, %s::jsonb))).{f} WHERE id = %s",
                (f, json.dumps(old), ch["entity_id"]))
        conn.execute("SELECT set_config('holaguia.reverts_id', '', true)")
        rid = _one(conn, "SELECT id FROM content_changes WHERE reverts_id = %s",
                   (change_id,))
        if rid is None:   # le déclencheur DOIT tracer toute annulation
            raise RuntimeError(f"annulation de {change_id} non journalisée")
    return res("reverted", revert_id=rid["id"])


def revert_batch(conn, batch_id: str, *, actor: str) -> list[RevertResult]:
    """Annule tout un lot, du plus récent au plus ancien, SANS s'arrêter au premier
    conflit. Les annulations forment elles-mêmes un nouveau lot. Renvoie le détail."""
    _check_actor(actor)
    cur = conn.execute(
        "SELECT id FROM content_changes WHERE batch_id = %s AND action <> 'revert' "
        "ORDER BY id DESC", (batch_id,))
    ids = [r["id"] if isinstance(r, dict) else r[0] for r in cur.fetchall()]
    revert_lot = str(uuid.uuid4())
    return [revert_change(conn, i, actor=actor, batch_id=revert_lot) for i in ids]
