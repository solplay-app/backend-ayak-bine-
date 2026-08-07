"""
Provider SMS Orange (Orange SMS API — orange.com), pertinent en priorité
pour la Côte d'Ivoire / zone UEMOA vu la couverture réseau Orange.

Auth : OAuth2 client_credentials (Basic Auth client_id:client_secret sur
/oauth/v3/token), token mis en cache en mémoire jusqu'à expiration.
Envoi : POST /smsmessaging/v1/outbound/{senderAddress}/requests

Doc : https://developer.orange.com/apis/sms-ci/getting-started
(le chemin exact peut varier légèrement selon le pays/l'espace développeur
Orange choisi à la souscription — à vérifier sur le portail Orange Developer
lors de la mise en production).
"""
from __future__ import annotations

import logging
import time
from urllib.parse import quote

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.config import get_settings

from .base import SmsProvider, SmsSendError

logger = logging.getLogger("sms.orange")

settings = get_settings()

_TOKEN_URL = "https://api.orange.com/oauth/v3/token"
_TOKEN_SAFETY_MARGIN_SECONDS = 30  # renouvelle un peu avant l'expiration réelle


class _OrangeNetworkError(Exception):
    """Erreur réseau/5xx Orange — éligible au retry (interne, non exposée)."""


class OrangeSmsProvider(SmsProvider):
    def __init__(self) -> None:
        if not (
            settings.orange_client_id
            and settings.orange_client_secret
            and settings.orange_sender_address
        ):
            raise RuntimeError(
                "ORANGE_CLIENT_ID, ORANGE_CLIENT_SECRET et ORANGE_SENDER_ADDRESS sont requis "
                "quand SMS_PROVIDER=orange."
            )
        self._client = httpx.AsyncClient(base_url="https://api.orange.com", timeout=settings.sms_timeout_seconds)
        self._sender_address = settings.orange_sender_address  # ex: "tel:+2250000000"
        self._sender_name = settings.orange_sender_name or "Ayak'bine"
        self._access_token: str | None = None
        self._token_expires_at: float = 0.0

    async def close(self) -> None:
        await self._client.aclose()

    async def _get_access_token(self) -> str:
        if self._access_token and time.monotonic() < self._token_expires_at:
            return self._access_token

        try:
            response = await self._client.post(
                _TOKEN_URL,
                data={"grant_type": "client_credentials"},
                auth=(settings.orange_client_id, settings.orange_client_secret),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout) as exc:
            raise _OrangeNetworkError(f"Échec récupération token OAuth Orange: {exc}") from exc

        if response.status_code >= 400:
            payload = response.json() if response.content else {}
            raise SmsSendError(
                "Authentification au service SMS impossible.",
                detail=f"Orange OAuth {response.status_code}: {payload}",
            )

        data = response.json()
        self._access_token = data["access_token"]
        self._token_expires_at = time.monotonic() + int(data.get("expires_in", 3600)) - _TOKEN_SAFETY_MARGIN_SECONDS
        return self._access_token

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
        retry=retry_if_exception_type(_OrangeNetworkError),
    )
    async def _send_with_retry(self, phone_number: str, message: str) -> None:
        token = await self._get_access_token()

        # Orange SMS API attend le préfixe "tel:" pour les numéros MSISDN.
        recipient = phone_number if phone_number.startswith("tel:") else f"tel:{phone_number}"
        encoded_sender = quote(self._sender_address, safe="")

        body = {
            "outboundSMSMessageRequest": {
                "address": [recipient],
                "senderAddress": self._sender_address,
                "senderName": self._sender_name,
                "outboundSMSTextMessage": {"message": message},
            }
        }

        try:
            response = await self._client.post(
                f"/smsmessaging/v1/outbound/{encoded_sender}/requests",
                json=body,
                headers={"Authorization": f"Bearer {token}"},
            )
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout, httpx.ConnectTimeout) as exc:
            logger.warning("Erreur réseau Orange SMS vers %s: %s", phone_number, exc)
            raise _OrangeNetworkError(str(exc)) from exc

        if response.status_code >= 500:
            logger.warning("Erreur serveur Orange SMS %s pour %s", response.status_code, phone_number)
            raise _OrangeNetworkError(f"HTTP {response.status_code}")

        if response.status_code >= 400:
            payload = response.json() if response.content else {}
            # Un 401 signifie probablement un token expiré côté cache local malgré la marge :
            # on force son renouvellement pour la prochaine tentative.
            if response.status_code == 401:
                self._access_token = None
            logger.error("Orange SMS a refusé l'envoi vers %s: %s", phone_number, payload)
            raise SmsSendError(
                "Échec de l'envoi du SMS.",
                detail=f"Orange {response.status_code}: {payload}",
            )

        logger.info("SMS Orange envoyé à %s", phone_number)

    async def send(self, phone_number: str, message: str) -> None:
        try:
            await self._send_with_retry(phone_number, message)
        except _OrangeNetworkError as exc:
            raise SmsSendError("Service SMS momentanément indisponible.", detail=str(exc)) from exc
