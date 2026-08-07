#!/bin/sh
set -e

# Au premier déploiement (ou après un reset de la base), mets la variable
# d'environnement RUN_INIT_DB=true dans Render pour créer les tables
# automatiquement au démarrage. Remets-la à false (ou supprime-la) une fois
# les tables créées, pour éviter de le relancer inutilement à chaque redeploy.
if [ "$RUN_INIT_DB" = "true" ]; then
  echo "RUN_INIT_DB=true -> création des tables..."
  python -m scripts.init_db
fi

exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
