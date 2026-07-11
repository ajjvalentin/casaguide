"""API FastAPI de CasaGuide (§12 étape 1 du CdC).

Trois surfaces :
  * back-office propriétaire (authentifié par JWT) : comptes, logements,
    déclenchement de l'enrichissement, validation des POI suggérés ;
  * endpoint public du guide voyageur (`GET /g/{guide_token}`) qui sert des
    données entièrement pré-calculées en base (aucun appel externe, §invariant 4)
    et n'expose jamais les données sensibles (§invariant 5) ;
  * multi-tenant : chaque requête sur les données d'un logement filtre par
    propriétaire (`owner_id`).
"""
