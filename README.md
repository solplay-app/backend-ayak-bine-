# Backend Wallet — Intégration JEKO Africa

Backend FastAPI (async) pour la plateforme multi-services financiers :
Dépôt (Pay-In), Retrait (Pay-Out), et réception des webhooks JEKO de façon
idempotente et sécurisée (signature HMAC).

## Structure

```
app/
  config.py              # Settings (pydantic-settings, lit .env)
  database.py             # Engine async SQLAlchemy + client Redis
  models/models.py        # Users, Wallets, Transactions (SQLAlchemy 2.0)
  schemas/schemas.py       # Schémas Pydantic v2 (requêtes/réponses/webhook)
  core/security.py        # JWT, hashing PIN (bcrypt), get_current_user
  services/
    jeko_client.py        # Client HTTPX async vers l'API JEKO (Pay-In/Pay-Out)
    wallet_service.py      # Locking Redis + PostgreSQL, débit/crédit atomique
    webhook_service.py     # Traitement idempotent des webhooks JEKO
    otp_service.py         # Génération/vérification OTP (Redis), délègue l'envoi à un SmsProvider
    sms/
      base.py               # Interface SmsProvider + SmsSendError
      console_provider.py   # Dev : logge le code au lieu de l'envoyer (SMS_PROVIDER=console)
      twilio_provider.py    # Envoi réel via l'API Twilio (SMS_PROVIDER=twilio)
      orange_provider.py    # Envoi réel via Orange SMS API (SMS_PROVIDER=orange)
      factory.py             # get_sms_provider() : sélectionne le provider actif (config)
  utils/hmac_verify.py     # Vérification signature HMAC-SHA256
  api/v1/
    wallet.py              # POST /api/v1/wallet/deposit, /withdraw
    webhooks.py            # POST /api/v1/webhooks/jeko
  main.py                  # Assemblage FastAPI
```

## Installation

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # puis renseigner les vraies valeurs
```

## Démarrage rapide (dev/démo, avec Docker)

```bash
# 1. Lancer PostgreSQL + Redis en local
docker compose up -d

# 2. Installer les dépendances Python
pip install -r requirements.txt

# 3. Créer les tables (pratique pour démarrer vite ; Alembic recommandé en prod)
python -m scripts.init_db

# 4. Lancer le serveur
uvicorn app.main:app --reload --port 8000
```

Documentation interactive : http://localhost:8000/docs — pratique pour
tester `POST /api/v1/auth/request-otp` puis `POST /api/v1/auth/verify-otp`
directement depuis le navigateur (bouton "Try it out").

Créer les tables (le SQL du CDC section 4 peut être exécuté tel quel, ou via
Alembic pour un vrai projet — non inclus ici par souci de concision).

## Lancer le serveur

```bash
uvicorn app.main:app --reload --port 8000
```

Documentation interactive : http://localhost:8000/docs

## Points clés d'implémentation

### 1. Idempotence des transactions
- `internal_reference` est **UNIQUE** en base : toute double-soumission (double
  clic côté app Android) échoue au niveau `create_pending_transaction` avec une
  IntegrityError, empêchant la création d'un doublon.
- Le **webhook** JEKO est dédupliqué à deux niveaux :
  1. Verrou Redis court-terme sur `jeko_transaction_id` (anti-rejeu concurrent).
  2. Vérification en base : si la transaction est déjà dans un état terminal
     (`SUCCESS`/`FAILED`/`CANCELLED`), le webhook est acquitté (200 OK) mais
     ignoré — JEKO ne le retentera pas indéfiniment, et aucun double crédit/débit
     n'est possible.

### 2. Sécurité des webhooks (HMAC)
La signature est vérifiée sur le **corps brut** de la requête (`request.body()`)
AVANT tout parsing JSON, via `hmac.compare_digest` (résistant aux attaques par
timing). Un webhook mal signé est rejeté en 401 sans jamais toucher la base.

### 3. Gestion du solde (race conditions)
Pour un retrait :
1. Verrou distribué Redis (`SETNX wallet:lock:{user_id}`) — empêche deux
   requêtes de retrait simultanées pour le même utilisateur.
2. Verrou pessimiste PostgreSQL (`SELECT ... FOR UPDATE`) — défense en
   profondeur si le verrou Redis venait à sauter (crash, TTL expiré).
3. Débit **optimiste** à l'initiation, recrédité automatiquement si JEKO
   renvoie `FAILED` via le webhook.

### 4. Client JEKO (HTTPX)
- Retry automatique (backoff exponentiel, 3 tentatives) uniquement sur les
  erreurs réseau/5xx (`JekoNetworkError`) — jamais sur les erreurs métier 4xx
  (`JekoAPIError`), pour ne pas relancer une opération refusée par JEKO
  (solde insuffisant côté JEKO, numéro invalide, etc.).
- Timeout configurable (`JEKO_TIMEOUT_SECONDS`).

### 5. Envoi réel des SMS OTP
`otp_service.generate_and_send_otp` délègue l'envoi à un `SmsProvider`
(interface dans `services/sms/base.py`), injecté par dépendance FastAPI
dans `POST /api/v1/auth/request-otp` — voir `services/sms/factory.py`.

Sélection via `SMS_PROVIDER` dans `.env` :
- `console` (défaut) : logge le code, aucun SMS réel — pratique en local.
- `orange` : Orange SMS API (OAuth2 client_credentials, token mis en cache
  en mémoire). Recommandé en priorité pour la Côte d'Ivoire/UEMOA vu la
  couverture Orange. Nécessite `ORANGE_CLIENT_ID`, `ORANGE_CLIENT_SECRET`,
  `ORANGE_SENDER_ADDRESS` (créer une app sur developer.orange.com).
- `twilio` : API Twilio en HTTPX direct (pas de SDK). Nécessite
  `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`.

Comportement en cas d'échec d'envoi : le code n'est **pas** persisté et le
cooldown de renvoi n'est **pas** posé (l'utilisateur peut retenter tout de
suite), l'API répond `502` avec un message explicite.

## Ce qui reste à faire pour la production
- Migrations Alembic (le schéma SQL du CDC est prêt à être versionné).
- Un job de réconciliation périodique (Celery/APScheduler) qui interroge
  `GET /transactions/{jeko_reference}` pour les transactions restées `PENDING`
  trop longtemps (webhook jamais reçu).
- Module Factures/Assurances (`/api/v1/services/pay-bill`) suit exactement le
  même pattern que `deposit`/`withdraw`.
- Vérifier le chemin exact de l'endpoint Orange SMS API sur le portail Orange
  Developer au moment de la souscription (peut varier légèrement selon
  l'espace pays choisi).
