"""
Notifications push via Firebase Cloud Messaging (HTTP v1), déclenchées à
chaque fois qu'un client est crédité (dépôt confirmé, recharge admin,
transfert interne reçu) — pour une notification en temps réel même si
l'app est fermée. Le polling déjà en place côté app reste un filet de
sécurité si le push échoue ou n'est pas configuré.

Configuration requise (voir app/config.py, variables d'env) :
  - FIREBASE_PROJECT_ID
  - FIREBASE_SERVICE_ACCOUNT_JSON (contenu JSON complet du compte de service)

Best-effort : ne lève jamais d'exception. Un échec d'envoi push ne doit
jamais faire échouer une opération métier (crédit de wallet, transfert...).
"""
from __future__ import annotations

import logging
import uuid
from functools import lru_cache
from typing import Any

import httpx
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.service_account import Credentials
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import DeviceToken

logger = logging.getLogger("push_service")

FCM_ENDPOINT_TEMPLATE = "https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"
FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"

# Codes FCM indiquant un token définitivement invalide (app désinstallée,
# token expiré...) : à supprimer de la base pour ne plus jamais réessayer.
_INVALID_TOKEN_FCM_CODES = {"UNREGISTERED", "INVALID_ARGUMENT"}


@lru_cache
def _get_credentials() -> Credentials | None:
    import json

    settings = get_settings()
    if not settings.FIREBASE_PROJECT_ID or not settings.FIREBASE_SERVICE_ACCOUNT_JSON:
        return None
    try:
        info = json.loads(settings.FIREBASE_SERVICE_ACCOUNT_JSON)
        return Credentials.from_service_account_info(info, scopes=[FCM_SCOPE])
    except Exception:  # noqa: BLE001
        logger.exception("FIREBASE_SERVICE_ACCOUNT_JSON invalide — push désactivé.")
        return None


def _access_token(creds: Credentials) -> str:
    if not creds.valid:
        creds.refresh(GoogleAuthRequest())
    return creds.token


def _send_to_token(project_id: str, access_token: str, fcm_token: str, title: str, body: str, data: dict[str, Any]) -> bool:
    """Retourne False si le token est invalide et doit être supprimé."""
    url = FCM_ENDPOINT_TEMPLATE.format(project_id=project_id)
    payload = {
        "message": {
            "token": fcm_token,
            "notification": {"title": title, "body": body},
            "data": {k: str(v) for k, v in data.items()},
            "android": {"priority": "high"},
        }
    }
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    try:
        response = httpx.post(url, json=payload, headers=headers, timeout=10)
    except httpx.HTTPError:
        logger.exception("Erreur réseau lors de l'envoi push FCM.")
        return True  # erreur transitoire : on garde le token

    if response.status_code < 400:
        return True

    content_type = response.headers.get("content-type", "")
    error_body = response.json() if content_type.startswith("application/json") else {}
    fcm_error_code = error_body.get("error", {}).get("status", "")
    logger.warning("Échec envoi push FCM (%s, %s): %s", response.status_code, fcm_error_code, response.text)
    return fcm_error_code not in _INVALID_TOKEN_FCM_CODES


def notify_user(
    db: Session,
    user_id: uuid.UUID | str,
    *,
    title: str,
    body: str,
    data: dict[str, Any] | None = None,
) -> None:
    """
    Envoie la notification à TOUS les appareils enregistrés de cet
    utilisateur. Supprime automatiquement les tokens invalides.
    Best-effort — n'interrompt jamais l'appelant en cas d'échec.
    """
    creds = _get_credentials()
    if creds is None:
        return

    try:
        access_token = _access_token(creds)
    except Exception:  # noqa: BLE001
        logger.exception("Impossible de rafraîchir le token OAuth2 FCM — push ignoré.")
        return

    settings = get_settings()
    tokens = db.execute(select(DeviceToken).where(DeviceToken.user_id == user_id)).scalars().all()
    if not tokens:
        return

    payload_data = data or {}
    any_change = False
    for device_token in tokens:
        try:
            still_valid = _send_to_token(settings.FIREBASE_PROJECT_ID, access_token, device_token.token, title, body, payload_data)
            if not still_valid:
                db.delete(device_token)
                any_change = True
        except Exception:  # noqa: BLE001
            logger.exception("Erreur inattendue lors de l'envoi push à un appareil.")
    if any_change:
        db.commit()
