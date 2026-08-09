#!/bin/sh
set -e

# ⚠️ RUN_RESET_DB=true supprime TOUTES les tables puis les recrée avec le
# schéma actuel. Usage ponctuel uniquement (ex: passage à un schéma
# incompatible). Ne JAMAIS laisser cette variable à true une fois de
# vraies données en base.
if [ "$RUN_RESET_DB" = "true" ]; then
  echo "RUN_RESET_DB=true -> suppression puis recréation des tables..."
  python -m scripts.reset_db
fi

# Au premier déploiement (ou après un reset de la base), mets la variable
# d'environnement RUN_INIT_DB=true dans Render pour créer les tables
# automatiquement au démarrage. Remets-la à false (ou supprime-la) une fois
# les tables créées, pour éviter de le relancer inutilement à chaque redeploy.
if [ "$RUN_INIT_DB" = "true" ]; then
  echo "RUN_INIT_DB=true -> création des tables..."
  python -m scripts.init_db
fi

exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
