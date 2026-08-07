# Déployer le backend Ayak'bine sur Render

## 0. Ce que ce dossier contient maintenant
- `Dockerfile` — construit une image Python pour ton API.
- `start.sh` — lance l'app sur le port fourni par Render (`$PORT`), et peut
  créer les tables au premier démarrage (`RUN_INIT_DB=true`).
- `render.yaml` — un "Blueprint" Render qui crée automatiquement le service
  web + la base Postgres.
- `app/config.py` — corrigé pour accepter le format d'URL Postgres que Render
  fournit par défaut.

## 1. Mettre le code sur GitHub
Render déploie depuis un repo Git. Si ce n'est pas déjà fait :
```bash
git init
git add .
git commit -m "Prêt pour déploiement Render"
git branch -M main
git remote add origin https://github.com/<toi>/ayakbine-backend.git
git push -u origin main
```

## 2. Créer les services sur Render
1. Va sur [render.com](https://render.com) → **New** → **Blueprint**.
2. Connecte ton repo GitHub `ayakbine-backend`.
3. Render détecte `render.yaml` et propose de créer :
   - Le service web `ayakbine-backend` (Docker)
   - La base `ayakbine-postgres` (Postgres managé)
4. Clique **Apply**. Render construit l'image et lance le déploiement
   (première fois : 3-5 min).

## 3. Ajouter Redis (obligatoire, pas géré par le Blueprint)
Render ne propose pas Redis managé gratuit dans les Blueprints. Deux options :
- **Le plus simple : Upstash** (Redis serverless, free tier généreux) —
  crée une base sur [upstash.com](https://upstash.com), copie l'URL
  `rediss://...` fournie.
- Ou un service Redis payant directement sur Render (**New** → **Redis**).

Ensuite, dans Render → ton service `ayakbine-backend` → **Environment** :
ajoute `REDIS_URL` avec l'URL copiée.

## 4. Compléter les variables d'environnement
Dans **Environment**, remplis les variables marquées `sync: false` dans
`render.yaml` (elles ne peuvent pas être générées automatiquement) :
- `JEKO_BASE_URL`, `JEKO_API_KEY`, `JEKO_MERCHANT_ID`, `JEKO_WEBHOOK_SECRET`
- `REDIS_URL` (étape 3)
- `PUBLIC_BASE_URL` : une fois le premier déploiement terminé, Render te
  donne une URL du type `https://ayakbine-backend.onrender.com` — mets-la ici.
  Redéploie ensuite (Manual Deploy) pour que la variable soit prise en compte.
- Si tu veux du SMS réel plus tard : passe `SMS_PROVIDER=orange` (ou `twilio`)
  et remplis les clés correspondantes.

## 5. Créer les tables de la base au premier lancement
1. Dans Environment, mets `RUN_INIT_DB` à `true`.
2. Fais un **Manual Deploy** (ou attends le redeploy auto après avoir changé
   la variable).
3. Vérifie dans les logs que tu vois `✅ Tables créées avec succès.`
4. Remets `RUN_INIT_DB` à `false` et redéploie, pour ne pas relancer la
   création de tables à chaque déploiement futur.

## 6. Vérifier que ça tourne
```bash
curl https://ayakbine-backend.onrender.com/health
# -> {"status":"ok"}
```

## 7. Mettre à jour le mobile
Dans l'app Flutter, pointe vers ta nouvelle URL :
```bash
flutter run --dart-define=API_BASE_URL=https://ayakbine-backend.onrender.com
```

## À savoir sur le plan gratuit Render
- Le service web gratuit **se met en veille après 15 min d'inactivité** et
  met quelques secondes à se réveiller au prochain appel (peut faire "rater"
  un webhook JEKO si l'app dort à ce moment précis).
- Pour un usage réel avec paiements (JEKO), il vaut mieux passer sur le plan
  payant le plus bas (~7$/mois) qui reste actif en permanence — sinon les
  webhooks de confirmation de transaction peuvent être manqués ou retardés.
- La base Postgres gratuite Render expire après 90 jours (limite du free
  tier) : pour un vrai produit en prod, prends le plan payant dès le départ.
