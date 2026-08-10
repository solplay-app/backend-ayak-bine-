"""
Notifications push via Firebase Cloud Messaging (HTTP v1 API).

Prévient l'utilisateur de l'évolution de son transfert (réussi, échoué,
argent recrédité en wallet) même si l'application Android est fermée — le
polling de TransactionStatusScreen reste un mécanisme de secours si le push
n'arrive pas ou n'est pas configuré.

Configuration requise (voir app/config.py) :
  - firebase_project_id
  - firebase_service_account_json (contenu JSON complet du compte de service)

Si l'une des deux est absente, `get_push_service()` retourne None et tout
appelant doit se comporter en mode dégradé silencieux (ne jamais faire
échouer le traitement d'un webhook à cause d'un push manquant).
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import httpx
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.service_account import Credentials
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.device_token import DeviceToken

logger = logging.getLogger("push_service")
settings = get_settings()

FCM_ENDPOINT_TEMPLATE = "https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"
FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"

# Codes FCM indiquant un token définitivement invalide (app désinstallée,
# token expiré...) : à supprimer de la base pour ne plus jamais réessayer.
_INVALID_TOKEN_FCM_CODES = {"UNREGISTERED", "INVALID_ARGUMENT"}


class PushService:
    """Client FCM HTTP v1, avec rafraîchissement automatique du token OAuth2."""

    def __init__(self, project_id: str, credentials: Credentials) -> None:
        self._project_id = project_id
        self._credentials = credentials

    def _get_access_token(self) -> str:
        # `google-auth` gère lui-même l'expiration : ne rafraîchit que si
        # le token est absent ou expiré, sinon réutilise le token en cache.
        if not self._credentials.valid:
            self._credentials.refresh(GoogleAuthRequest())
        return self._credentials.token

    async def _send_to_token(
        self, fcm_token: str, title: str, body: str, data: dict[str, Any]
    ) -> bool:
        """Retourne False si le token est invalide et doit être supprimé."""
        url = FCM_ENDPOINT_TEMPLATE.format(project_id=self._project_id)
        payload = {
            "message": {
                "token": fcm_token,
                "notification": {"title": title, "body": body},
                "data": {k: str(v) for k, v in data.items()},
                "android": {"priority": "high"},
            }
        }
        headers = {
            "Authorization": f"Bearer {self._get_access_token()}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(url, json=payload, headers=headers)

        if response.status_code < 400:
            return True

        content_type = response.headers.get("content-type", "")
        error_body = response.json() if content_type.startswith("application/json") else {}
        fcm_error_code = error_body.get("error", {}).get("status", "")
        logger.warning("Échec envoi push FCM (%s, %s): %s", response.status_code, fcm_error_code, response.text)
        return fcm_error_code not in _INVALID_TOKEN_FCM_CODES

    async def notify_user(
        self,
        db: AsyncSession,
        user_id: uuid.UUID,
        *,
        title: str,
        body: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        """
        Envoie la notification à TOUS les appareils enregistrés de cet
        utilisateur (il peut en avoir plusieurs : ancien + nouveau téléphone,
        réinstallations...). Supprime automatiquement les tokens invalides.
        Ne lève jamais d'exception : un échec de push ne doit jamais faire
        échouer le traitement métier appelant (le webhook JEKO en premier lieu).
        """
        result = await db.execute(select(DeviceToken).where(DeviceToken.user_id == user_id))
        tokens = result.scalars().all()
        if not tokens:
            return

        payload_data = data or {}
        any_change = False
        for device_token in tokens:
            try:
                still_valid = await self._send_to_token(device_token.fcm_token, title, body, payload_data)
                if not still_valid:
                    await db.delete(device_token)
                    any_change = True
            except Exception:  # noqa: BLE001 - best-effort, ne jamais propager
                logger.exception("Erreur inattendue lors de l'envoi push à un token")
        if any_change:
            await db.flush()


_push_service: PushService | None = None


def get_push_service() -> PushService | None:
    """Retourne None si le push n'est pas configuré (mode dégradé silencieux)."""
    return _push_service


def configure_push_service() -> None:
    """
    À appeler une fois au démarrage de l'app (voir app/main.py). Ne lève
    jamais d'exception : si la config Firebase est absente ou invalide, le
    push reste simplement désactivé plutôt que de faire planter le serveur.
    """
    global _push_service

    if not settings.firebase_project_id or not settings.firebase_service_account_json:
        logger.warning(
            "Firebase non configuré (firebase_project_id / firebase_service_account_json manquants) — push désactivé."
        )
        return

    try:
        service_account_info = json.loads(settings.firebase_service_account_json)
        credentials = Credentials.from_service_account_info(service_account_info, scopes=[FCM_SCOPE])
        _push_service = PushService(settings.firebase_project_id, credentials)
        logger.info("Service push Firebase configuré (projet=%s).", settings.firebase_project_id)
    except Exception:  # noqa: BLE001
        logger.exception("Échec de configuration du service push Firebase — push désactivé.")
        _push_service = None
