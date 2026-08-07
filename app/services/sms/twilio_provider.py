"""
Provider SMS Twilio, via l'API REST directement en HTTPX (pas de SDK Twilio,
pour rester cohérent avec le reste du projet — voir jeko_client.py — et
éviter une dépendance supplémentaire).

Doc API : https://www.twilio.com/docs/sms/api/message-resource
"""
from __future__ import annotations

import logging

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.config import get_settings

from .base import SmsProvider, SmsSendError

logger = logging.getLogger("sms.twilio")

settings = get_settings()


class _TwilioNetworkError(Exception):
    """Erreur réseau/5xx Twilio — éligible au retry (interne, non exposée)."""


class TwilioSmsProvider(SmsProvider):
    def __init__(self) -> None:
        if not (settings.twilio_account_sid and settings.twilio_auth_token and settings.twilio_from_number):
            raise RuntimeError(
                "TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN et TWILIO_FROM_NUMBER sont requis "
                "quand SMS_PROVIDER=twilio."
            )
        self._from_number = settings.twilio_from_number
        self._client = httpx.AsyncClient(
            base_url=f"https://api.twilio.com/2010-04-01/Accounts/{settings.twilio_account_sid}",
            auth=(settings.twilio_account_sid, settings.twilio_auth_token),
            timeout=settings.sms_timeout_seconds,
        )

    async def close(self) -> None:
        await self._client.aclose()

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
        retry=retry_if_exception_type(_TwilioNetworkError),
    )
    async def _send_with_retry(self, phone_number: str, message: str) -> None:
        try:
            response = await self._client.post(
                "/Messages.json",
                data={"To": phone_number, "From": self._from_number, "Body": message},
            )
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout, httpx.ConnectTimeout) as exc:
            logger.warning("Erreur réseau Twilio vers %s: %s", phone_number, exc)
            raise _TwilioNetworkError(str(exc)) from exc

        if response.status_code >= 500:
            logger.warning("Erreur serveur Twilio %s pour %s", response.status_code, phone_number)
            raise _TwilioNetworkError(f"HTTP {response.status_code}")

        if response.status_code >= 400:
            # Erreur métier Twilio (numéro invalide, solde compte épuisé, etc.) : pas de retry.
            payload = response.json() if response.content else {}
            logger.error("Twilio a refusé l'envoi vers %s: %s", phone_number, payload)
            raise SmsSendError(
                "Échec de l'envoi du SMS.",
                detail=f"Twilio {response.status_code}: {payload}",
            )

        logger.info("SMS Twilio envoyé à %s", phone_number)

    async def send(self, phone_number: str, message: str) -> None:
        try:
            await self._send_with_retry(phone_number, message)
        except _TwilioNetworkError as exc:
            raise SmsSendError("Service SMS momentanément indisponible.", detail=str(exc)) from exc
