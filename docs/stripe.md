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
  `customer.subscription.deleted`, `invoice.payment_failed`.

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

Retester le parcours `4242` **en production** (toujours en mode Test).

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
