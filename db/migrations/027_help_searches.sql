-- Migration 027 — Journal des recherches d'aide (V2-31, volet 3a)
--
-- La recherche d'aide à couverture garantie (docs/aide.md) journalise CHAQUE
-- recherche du propriétaire. Le taux de zéro-résultat est la métrique de santé de
-- l'index : une requête réelle qui ne trouve rien désigne un trou de l'index (une
-- question du terrain non couverte, un mot du métier absent des mots-clés).
--
-- Rien de plus personnel que le strict nécessaire : l'auteur (owner_id), le texte
-- tapé (query), le nombre de résultats rendus (results_count), l'instant. Pas d'UI
-- de consultation dans ce volet — `psql` suffit à André :
--
--   SELECT query, results_count, searched_at FROM help_searches
--   WHERE results_count = 0 ORDER BY searched_at DESC;
--
-- L'écriture est BEST-EFFORT côté API (un échec de journal ne casse jamais la
-- recherche, qui est purement front). Idempotente (IF NOT EXISTS), sûre au rejeu,
-- aucun backfill (une table de journal naît vide — il n'y a pas d'historique à
-- reconstituer). ON DELETE CASCADE : le journal d'un compte supprimé disparaît
-- avec lui.

CREATE TABLE IF NOT EXISTS help_searches (
    id            BIGSERIAL PRIMARY KEY,
    owner_id      UUID NOT NULL REFERENCES owners(id) ON DELETE CASCADE,
    query         TEXT NOT NULL,
    results_count INT  NOT NULL,
    searched_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Index sur (results_count, searched_at) : la requête d'exploitation la plus
-- fréquente est « les dernières recherches infructueuses » (results_count = 0).
CREATE INDEX IF NOT EXISTS idx_help_searches_results
    ON help_searches(results_count, searched_at DESC);
