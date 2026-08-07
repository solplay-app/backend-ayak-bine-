"""
Client d'intégration vers l'API JEKO Africa (Partner API).
Gère :
  - Pay-In (encaissement / dépôt Mobile Money -> Wallet)
  - Pay-Out (décaissement / retrait Wallet -> Mobile Money)
Retry automatique sur erreurs réseau/5xx via tenacity (jamais sur 4xx métier).
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.config import get_settings

logger = logging.getLogger("jeko_client")

settings = get_settings()


class JekoAPIError(Exception):
    """Erreur métier renvoyée par JEKO (4xx) - ne doit PAS être retryée."""

    def __init__(self, status_code: int, payload: dict[str, Any]):
        self.status_code = status_code
        self.payload = payload
        super().__init__(f"JEKO API error {status_code}: {payload}")


class JekoNetworkError(Exception):
    """Erreur réseau / timeout / 5xx - éligible au retry."""


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.jeko_api_key}",
        "X-Merchant-Id": settings.jeko_merchant_id,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


class JekoClient:
    """Client asynchrone réutilisable pour l'API JEKO Africa."""

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=settings.jeko_base_url,
            headers=_headers(),
            timeout=settings.jeko_timeout_seconds,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "JekoClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
        retry=retry_if_exception_type(JekoNetworkError),
    )
    async def _post(self, path: str, json_body: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._client.post(path, json=json_body)
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout, httpx.ConnectTimeout) as exc:
            logger.warning("Erreur réseau JEKO sur %s: %s", path, exc)
            raise JekoNetworkError(str(exc)) from exc

        if response.status_code >= 500:
            logger.warning("Erreur serveur JEKO %s sur %s", response.status_code, path)
            raise JekoNetworkError(f"HTTP {response.status_code}")

        data = response.json() if response.content else {}

        if response.status_code >= 400:
            # Erreur métier (solde JEKO insuffisant, numéro invalide, etc.) -> pas de retry
            raise JekoAPIError(response.status_code, data)

        return data

    async def initiate_payin(
        self,
        *,
        internal_reference: str,
        amount: Decimal,
        operator: str,
        phone_number: str,
    ) -> dict[str, Any]:
        """
        Déclenche un encaissement (Cash-In / Dépôt) côté JEKO.
        L'utilisateur va généralement recevoir un push USSD/App sur son téléphone
        pour confirmer le paiement. La confirmation finale arrive via webhook.
        """
        body = {
            "merchant_reference": internal_reference,
            "amount": str(amount),
            "currency": "XOF",
            "operator": operator,
            "customer_phone": phone_number,
            "notify_url": f"{settings.public_base_url}/api/v1/webhooks/jeko",
            "description": "Dépôt Wallet applicatif",
        }
        logger.info("Initiation Pay-In JEKO ref=%s montant=%s", internal_reference, amount)
        return await self._post("/payments", body)

    async def initiate_payout(
        self,
        *,
        internal_reference: str,
        amount: Decimal,
        operator: str,
        phone_number: str,
    ) -> dict[str, Any]:
        """
        Déclenche un décaissement (Cash-Out / Retrait) côté JEKO :
        transfert du Wallet applicatif vers le Mobile Money du client.
        """
        body = {
            "merchant_reference": internal_reference,
            "amount": str(amount),
            "currency": "XOF",
            "operator": operator,
            "recipient_phone": phone_number,
            "notify_url": f"{settings.public_base_url}/api/v1/webhooks/jeko",
            "description": "Retrait Wallet applicatif",
        }
        logger.info("Initiation Pay-Out JEKO ref=%s montant=%s", internal_reference, amount)
        return await self._post("/disbursements", body)

    async def get_transaction_status(self, jeko_reference: str) -> dict[str, Any]:
        """Vérification active (polling / réconciliation) d'une transaction JEKO."""
        try:
            response = await self._client.get(f"/transactions/{jeko_reference}")
        except (httpx.ConnectError, httpx.ReadTimeout) as exc:
            raise JekoNetworkError(str(exc)) from exc
        if response.status_code >= 400:
            raise JekoAPIError(response.status_code, response.json())
        return response.json()


_jeko_client: JekoClient | None = None


def get_jeko_client() -> JekoClient:
    """Singleton FastAPI dependency-friendly."""
    global _jeko_client
    if _jeko_client is None:
        _jeko_client = JekoClient()
    return _jeko_client
