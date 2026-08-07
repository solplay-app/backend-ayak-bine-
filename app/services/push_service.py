"""
Service de notification push via Firebase Cloud Messaging (HTTP v1 API).
Utilisé pour prévenir l'utilisateur de la confirmation/échec d'une transaction
même si l'application Android est fermée (le polling côté client ne suffit pas).

Pré-requis production :
  - Un compte de service Firebase (JSON) avec le scope
    "https://www.googleapis.com/auth/firebase.messaging"
  - Génération d'un access token OAuth2 (google-auth) à partir de ce compte
    de service, non détaillée ici par souci de concision (voir doc Firebase).
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import get_settings

logger = logging.getLogger("push_service")
settings = get_settings()

FCM_ENDPOINT_TEMPLATE = "https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"


class PushService:
    def __init__(self, project_id: str, access_token_provider) -> None:
        """
        `access_token_provider` : callable async retournant un access token OAuth2
        valide (à rafraîchir en interne, ex: via google-auth `Credentials.refresh`).
        Injecté pour ne pas coupler ce service à une implémentation OAuth précise.
        """
        self._project_id = project_id
        self._get_token = access_token_provider

    async def send(self, fcm_token: str, title: str, body: str, data: dict[str, Any] | None = None) -> None:
        access_token = await self._get_token()
        url = FCM_ENDPOINT_TEMPLATE.format(project_id=self._project_id)
        payload = {
            "message": {
                "token": fcm_token,
                "notification": {"title": title, "body": body},
                "data": {k: str(v) for k, v in (data or {}).items()},
                "android": {"priority": "high"},
            }
        }
        headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}

        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(url, json=payload, headers=headers)
            if response.status_code >= 400:
                # Un token FCM invalide/désinstallé ne doit jamais faire échouer
                # le traitement métier (webhook) : on logge et on continue.
                logger.warning("Échec envoi push FCM (%s): %s", response.status_code, response.text)


_push_service: PushService | None = None


def get_push_service() -> PushService | None:
    """Retourne None si le push n'est pas configuré (mode dégradé silencieux)."""
    return _push_service


def configure_push_service(project_id: str, access_token_provider) -> None:
    global _push_service
    _push_service = PushService(project_id, access_token_provider)
