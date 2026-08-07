"""
Script de test ISOLÉ : envoie un vrai SMS via Twilio pour vérifier que les
identifiants et le numéro vérifié fonctionnent, SANS avoir besoin de monter
tout le backend (PostgreSQL, Redis, etc.).

Fonctionne dans les deux cas :
  - En local : valeurs lues depuis .env
  - Dans GitHub Codespaces : valeurs lues depuis les Codespaces Secrets
    (variables d'environnement déjà injectées automatiquement, pas besoin
    de fichier .env)

Utilisation :
    pip install httpx python-dotenv
    python test_sms.py +2250779321619

Le numéro donné en argument DOIT être un numéro "vérifié" dans la Console
Twilio (Phone Numbers > Manage > Verified Caller IDs) tant que le compte
est en essai gratuit.
"""
import os
import sys

import httpx
from dotenv import dotenv_values

# Priorité aux vraies variables d'environnement (cas Codespaces Secrets),
# puis on complète avec .env s'il existe (cas local).
_dotenv_values = dotenv_values(".env")


def _get(key: str) -> str | None:
    return os.environ.get(key) or _dotenv_values.get(key)


ACCOUNT_SID = _get("TWILIO_ACCOUNT_SID")
AUTH_TOKEN = _get("TWILIO_AUTH_TOKEN")
FROM_NUMBER = _get("TWILIO_FROM_NUMBER")

if not (ACCOUNT_SID and AUTH_TOKEN and FROM_NUMBER):
    print("ERREUR : TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_FROM_NUMBER manquants dans .env")
    sys.exit(1)

if len(sys.argv) < 2:
    print("Usage : python test_sms.py +2250779321619")
    sys.exit(1)

to_number = sys.argv[1]
code = "123456"
message = f"Ayak'bine : votre code de vérification est {code}. Valable 5 minutes."

print(f"Envoi d'un SMS de test à {to_number} depuis {FROM_NUMBER}...")

response = httpx.post(
    f"https://api.twilio.com/2010-04-01/Accounts/{ACCOUNT_SID}/Messages.json",
    data={"To": to_number, "From": FROM_NUMBER, "Body": message},
    auth=(ACCOUNT_SID, AUTH_TOKEN),
    timeout=15,
)

print(f"Statut HTTP : {response.status_code}")
print(response.json())

if response.status_code < 300:
    print("\n✅ SMS envoyé avec succès ! Regarde ton téléphone.")
else:
    print("\n❌ Échec de l'envoi. Vérifie le message d'erreur ci-dessus.")
