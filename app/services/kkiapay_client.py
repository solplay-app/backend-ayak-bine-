"""
Client Kkiapay — utilisé UNIQUEMENT pour la Recharge wallet (DEPOSIT),
temporairement, en attendant le déblocage du compte JEKO (documents KYC
en cours de validation).

⚠️ Important, à ne pas oublier en réintégrant JEKO plus tard :
Kkiapay ne propose PAS d'API de versement instantané vers un destinataire
arbitraire (leur mécanisme de "payout" ne fait que reverser VOS propres
fonds collectés vers VOTRE compte marchand, à intervalle régulier ou par
seuil — pas un virement à la demande vers un numéro choisi par le client).
Transférer et Retirer restent donc sur JEKO, qui lui sait le faire.

Modèle d'intégration différent de JEKO : chez JEKO, c'est le BACKEND qui
initie le paiement. Chez Kkiapay, c'est le WIDGET côté app (SDK Flutter
officiel) qui gère la collecte ; le backend se contente de créer la
transaction PENDING, puis de vérifier server-side le résultat une fois le
widget fermé (voir POST /api/v1/wallet/deposit/confirm), pour éviter
qu'un client malveillant ne falsifie un succès depuis l'app.

Le SDK officiel `kkiapay` (pip) est synchrone (utilise `requests`), on
l'exécute donc dans un thread pour ne pas bloquer la boucle asyncio de
FastAPI.
"""
from __future__ import annotations

import asyncio
import hmac

from kkiapay import Kkiapay as _KkiapaySDK

from app.config import get_settings


class KkiapayNotConfigured(Exception):
    pass


def _get_client() -> _KkiapaySDK:
    settings = get_settings()
    if not (settings.kkiapay_public_key and settings.kkiapay_private_key and settings.kkiapay_secret):
        raise KkiapayNotConfigured(
            "KKIAPAY_PUBLIC_KEY / KKIAPAY_PRIVATE_KEY / KKIAPAY_SECRET manquants "
            "dans les variables d'environnement Render."
        )
    return _KkiapaySDK(
        settings.kkiapay_public_key,
        settings.kkiapay_private_key,
        settings.kkiapay_secret,
        settings.kkiapay_sandbox,
    )


async def verify_transaction(kkiapay_transaction_id: str) -> dict:
    """
    Vérifie côté serveur le VRAI statut d'une transaction Kkiapay (jamais se
    fier au seul callback du widget côté app, qui peut être falsifié).
    Retourne le dict brut Kkiapay, ex: {"status": "SUCCESS", "type": "DEBIT", ...}
    """
    client = _get_client()
    return await asyncio.to_thread(client.verify_transaction, kkiapay_transaction_id)


def is_success(kkiapay_data: dict) -> bool:
    return str(kkiapay_data.get("status", "")).upper() == "SUCCESS"


def is_definitively_failed(kkiapay_data: dict) -> bool:
    return str(kkiapay_data.get("status", "")).upper() in ("FAILED", "CANCELLED", "CANCELED", "ERROR")


def verify_webhook_secret(received_secret: str | None) -> bool:
    """
    Kkiapay signe ses webhooks via l'en-tête `x-kkiapay-secret`, qui doit
    correspondre exactement au HASH SECRET défini sur le dashboard Kkiapay,
    dans Developers > API Keys > Webhook (PAS le "secret" des clés API
    utilisé pour le SDK — ce sont deux valeurs différentes).
    Pas un HMAC à calculer, une simple égalité — voir leur documentation
    webhook officielle.
    """
    settings = get_settings()
    if not settings.kkiapay_webhook_secret or not received_secret:
        return False
    return hmac.compare_digest(received_secret, settings.kkiapay_webhook_secret)
