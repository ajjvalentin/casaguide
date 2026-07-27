# Facturation Stripe (V2-05b) — runbook

Paiement des offres **solo** / **pro** par Stripe : Checkout hébergé, webhooks
comme **seule source de vérité**, portail client. Tout se développe et se valide
en **mode Test** ; la bascule live se fait par simple **échange de clés** + une
**re-synchronisation** des produits.

> Rappels d'architecture (cf. `CLAUDE.md`) : le prix vit **en base**
> (`plans.price_month_cts`) et est *poussé* vers Stripe (jamais l'inverse). Le
> webhook est la seule autorité sur `subscriptions.status/plan_id/current_period_end`.
> Un downgrade ne supprime **jamais** de données (retour à `free` = lecture seule
> des logements/langues excédentaires, invariant V2-05a).

---

## 1. Configuration (mode Test)

### Clés API

Dashboard Stripe → **Développeurs → Clés API** (mode Test) → copier la clé
secrète `sk_test_…`. Dans `backend/.env` (jamais committé) :

```bash
CASAGUIDE_STRIPE_SECRET_KEY=sk_test_...........
CASAGUIDE_STRIPE_WEBHOOK_SECRET=            # renseigné à l'étape 2
```

Sans `CASAGUIDE_STRIPE_SECRET_KEY`, toute la facturation reste en **mode dégradé
propre** : `/api/billing/*` répond **503**, le webhook **503**, le reste de l'app
est intact (un avertissement est journalisé au démarrage). C'est le même régime
que le mailer (V2-08).

### Synchroniser les produits/prix vers Stripe

Crée/retrouve un Product et un Price mensuel EUR par plan payant, et écrit
`plans.stripe_price_id`. **Idempotent** (relançable sans doublon) :

```bash
cd backend
export CASAGUIDE_STRIPE_SECRET_KEY=sk_test_...
export CASAGUIDE_DB=postgresql:///casaguide
python ../ops/stripe_sync_products.py
```

À lancer **avant** tout Checkout (sinon `POST /api/billing/checkout` renvoie 503
« offre pas encore disponible au paiement »). Un **changement de prix** = mettre
à jour `db/seed.sql` (`price_month_cts`), rejouer le seed, puis **relancer ce
script** : un nouveau Price est créé, l'ancien archivé (les abonnements en cours
dessus restent valides).

---

## 2. Webhooks en local (Stripe CLI)

Installer la [Stripe CLI](https://stripe.com/docs/stripe-cli), puis :

```bash
stripe login
stripe listen --forward-to localhost:8000/api/stripe/webhook
```

La commande affiche un secret `whsec_…` : le coller dans `backend/.env` sous
`CASAGUIDE_STRIPE_WEBHOOK_SECRET`, puis **redémarrer uvicorn**. Laisser
`stripe listen` tourner pendant les tests : il relaie les événements réels de
votre compte Test vers le serveur local (et affiche chaque événement + sa réponse).

Déclencher un événement de test manuellement (optionnel) :

```bash
stripe trigger checkout.session.completed
```

---

## 3. Parcours de validation de bout en bout (mode Test)

Avec uvicorn lancé (clés Test + `stripe listen` actifs) :

1. **S'inscrire** ou se connecter au back-office. Aller sur **« Mon abonnement »**.
2. Cliquer **« Passer en Solo »** → redirection vers le Checkout Stripe hébergé.
3. Payer avec la carte de test **`4242 4242 4242 4242`**, date future quelconque,
   CVC quelconque, code postal quelconque.
4. Retour sur `#/abonnement?checkout=success` → bandeau **« Paiement en cours de
   confirmation »**. Dans la console `stripe listen`, on voit arriver
   `checkout.session.completed` puis `customer.subscription.created` → **200**.
5. **Actualiser** la page : l'offre affichée passe à **Solo**, les jauges
   (logements/enrichissements/langues) reflètent le plan Solo, et le bouton
   **« Gérer mon abonnement »** apparaît.
6. Cliquer **« Gérer mon abonnement »** → **portail client** Stripe : y **annuler**
   l'abonnement (immédiatement, ou en fin de période). Stripe envoie
   `customer.subscription.deleted` → l'offre **repasse à Gratuit**, `status=active`.
7. **Vérifier l'invariant downgrade** : les logements créés pendant l'abonnement
   payant **existent toujours** (lecture seule si au-delà du quota gratuit,
   jamais supprimés).

Cartes de test utiles : `4242…` (succès), `4000 0000 0000 9995` (paiement refusé
→ `invoice.payment_failed` → statut `past_due`, accès conservé le temps des
relances). Voir la [doc cartes de test Stripe](https://stripe.com/docs/testing).

---

## 3 bis. Validation de la grille V2-18b (add-on, staff) — à dérouler avec André

Grille V2-18b : **Pro 24,00 € / 6 logements** + **add-on 3,00 €/mois par logement
supplémentaire** (item d'abonnement Stripe à quantité) ; guide équipe `/s/`
**exclusif Pro**. Prérequis : produits **re-synchronisés** (`ops/stripe_sync_products.py`
→ nouveau Price Pro 24 € **et** Price add-on `pro_addon`), `stripe listen` actif.

> **Invariant clé** : le **webhook** est la seule autorité de `addon_qty` (l'UI
> demande, Stripe dispose, le webhook écrit). Une baisse d'add-on ne supprime
> **jamais** de logement — l'excédent passe en lecture seule (402 à la création).

1. **Essai → Pro.** Compte jetable → essai 21 j. « Mon abonnement » → **« Passer
   en Pro »** → Checkout `4242` → webhook → offre **Pro**, jauge logements
   **`X / 6`**.
2. **Add-on +2.** Dans « Mon abonnement », section **« Logements supplémentaires »**
   → **`+`** deux fois → l'impact affiche **« passera à 30,00 € / mois (8
   logements) »** → **Confirmer**. `stripe listen` montre
   `customer.subscription.updated` (2 items). **Actualiser** : `addon_qty=2`,
   jauge **`X / 8`**. Vérifier la **facture/portail** Stripe à **30 €/mois**.
3. **Baisse à 0.** Ramener le stepper à **0** → Confirmer → webhook → retour
   **24 €/mois**, jauge **`X / 6`**. Si plus de 6 logements existaient, la
   création est refusée (**402**) mais **aucun n'est supprimé** (les vérifier
   présents).
4. **Gating staff.** Compte **Solo** (ou Pro annulé → free non grand-périsé) :
   ouvrir `/s/{staff_token}` → page sobre **« Cahier de l'équipe indisponible…
   offre Pro »** (jamais d'erreur brute) ; éditer une section staff au back-office
   → encart **« Réservé à l'offre Pro »**. Compte **Pro** ou **fondateur
   grand-périsé** : `/s/` **accessible** normalement.
5. **M-30.** Enrichir un logement réel : vérifier que **stations-service** et
   **bornes de recharge** remontent dans les POI (chapitre Transports).

### Changement d'offre : upgrade immédiat / downgrade programmé (V2-18e)

> **Politique** (invariant 12) : **upgrade** (offre plus chère) = **immédiat**
> avec prorata ; **downgrade** (offre moins chère) = **à l'échéance**
> (`current_period_end`), **annulable** tant qu'il n'a pas pris effet. L'endpoint
> **ne modifie jamais l'abonnement en base** — le **webhook** (schedule +
> transition) est seule autorité. Prérequis : les six événements
> `subscription_schedule.*` **activés** sur l'endpoint webhook.

1. **Upgrade Solo → Pro (immédiat).** Compte **Solo actif** → « Mon abonnement »
   → **« Passer en Pro »** → le dialogue annonce **« démarre immédiatement…
   prorata jusqu'au <date> »** → Confirmer. `stripe listen` montre
   `customer.subscription.updated` (price Pro). **Actualiser** : offre **Pro**
   tout de suite ; facture de prorata visible au portail.
2. **Downgrade Pro → Solo (programmé).** Compte **Pro actif** → **« Passer en
   Solo »** → le dialogue annonce **« reste active jusqu'au <date>… Solo prendra
   le relais »** → Confirmer. `stripe listen` montre `subscription_schedule.
   created/updated`. **Actualiser** : l'offre **reste Pro**, un **bandeau
   « Changement programmé : Solo à partir du <date> »** apparaît, la carte Solo
   affiche **« Programmée »**. Rien n'est facturé, l'accès Pro est conservé.
3. **Annulation du programmé.** Sur le bandeau → **« Annuler le changement
   programmé »** → confirmer. `stripe listen` montre `subscription_schedule.
   released`. **Actualiser** : le bandeau disparaît, toujours **Pro**.
4. **Prise d'effet à l'échéance** (avancer l'horloge de test Stripe, ou attendre) :
   à `current_period_end`, `customer.subscription.updated` bascule sur Solo →
   offre **Solo**, add-ons retirés, bandeau effacé. Si plus de 3 logements
   existaient, l'excédent passe en **lecture seule** (402), **aucun supprimé**.

---

## 4. Déploiement en production (toujours en mode Test au début)

Sur le serveur (`ssh` → `/opt/casaguide`), après `git pull` :

```bash
sudo -u casaguide /opt/casaguide/deploy.sh    # applique la migration 008 (idempotente)
```

Puis, **à la main** dans `/opt/casaguide/backend/.env` (fichier `600`, hors dépôt) :

```bash
CASAGUIDE_STRIPE_SECRET_KEY=sk_test_...        # clé Test pour commencer
CASAGUIDE_STRIPE_WEBHOOK_SECRET=whsec_...       # secret de l'endpoint prod (ci-dessous)
```

### Créer l'endpoint webhook de production

Dashboard Stripe (mode Test) → **Développeurs → Webhooks → Ajouter un endpoint** :

- **URL** : `https://holaguia.com/api/stripe/webhook`
- **Événements** : `checkout.session.completed`,
  `customer.subscription.created`, `customer.subscription.updated`,
  `customer.subscription.deleted`, `invoice.payment_failed`,
  `subscription_schedule.created`, `subscription_schedule.updated`,
  `subscription_schedule.released`, `subscription_schedule.canceled`,
  `subscription_schedule.completed`, `subscription_schedule.aborted`.
  > Les six événements `subscription_schedule.*` (V2-18e) alimentent le **bandeau
  > de changement d'offre programmé** (downgrade à l'échéance) et son effacement.
  > Sans eux, le downgrade fonctionne côté Stripe mais le bandeau back-office
  > n'apparaît jamais. `stripe listen` les transmet déjà en local (aucune option).

Copier le **secret de signature** de l'endpoint (`whsec_…`) dans `.env`
(`CASAGUIDE_STRIPE_WEBHOOK_SECRET`), puis redémarrer le service :

```bash
sudo systemctl restart casaguide
```

### Synchroniser les produits sur le serveur

```bash
cd /opt/casaguide/backend
sudo -u casaguide CASAGUIDE_STRIPE_SECRET_KEY=sk_test_... \
    /opt/casaguide/.venv/bin/python /opt/casaguide/ops/stripe_sync_products.py
```

> **V2-18b — re-sync OBLIGATOIRE.** Après un déploiement portant la nouvelle
> grille (migration 011 + seed), ce script est **indispensable** : il crée le
> **nouveau Price Pro à 24 €** (l'ancien 29 € est archivé, non supprimé) et le
> **Price de l'add-on** « logement supplémentaire » (metadata `plan_id=pro_addon`),
> et écrit `plans.stripe_price_id` / `plans.addon_stripe_price_id`. Sans lui,
> `/api/billing/checkout` et `/api/billing/addons` restent en **503** (offre non
> synchronisée). Les abonnés Test existants restent sur l'ancien Price 29 €
> (sans conséquence en mode Test ; en Live ce cas n'existera pas encore).

Retester le parcours `4242` **en production** (toujours en mode Test), puis la
grille V2-18b (voir §3 bis).

---

## 5. Bascule en mode Live (plus tard, après décision d'André)

La bascule ne change **aucun code** — uniquement des clés :

1. Récupérer les clés **Live** dans le Dashboard (`sk_live_…`).
2. Créer l'endpoint webhook **Live** (même URL, mêmes événements) → nouveau
   `whsec_…` Live.
3. Remplacer les deux valeurs dans `/opt/casaguide/backend/.env` et redémarrer.
4. **Re-synchroniser** les produits en Live (les Products/Prices Test et Live
   sont séparés) :
   `CASAGUIDE_STRIPE_SECRET_KEY=sk_live_... python ops/stripe_sync_products.py`.
5. Retester un vrai paiement (petite somme) puis rembourser.

> **En attente d'André** avant le mode Live : montants définitifs, éventuelle
> facturation annuelle (s'ajoutera comme de simples Prices Stripe
> supplémentaires — l'architecture le permet sans refonte).
