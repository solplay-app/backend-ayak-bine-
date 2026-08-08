"""
Client d'intégration vers la vraie API Partner JEKO Africa
(https://developer.jeko.africa).

Réécrit pour coller exactement à la documentation officielle — la version
précédente de ce fichier utilisait un schéma d'API inventé/incorrect
(mauvais en-têtes d'authentification, mauvais endpoints, mauvais format de
montant) qui n'aurait jamais fonctionné contre le vrai serveur JEKO.

Points clés de la vraie API :
  - Authentification par DEUX en-têtes : X-API-KEY et X-API-KEY-ID
    (PAS un Bearer token, PAS de X-Merchant-Id).
  - Toutes les opérations sont rattachées à un `storeId` (magasin JEKO).
  - Les montants sont en CENTIMES, sous forme d'entier (`amountCents`),
    jamais en chaîne décimale.
  - Pay-in (dépôt) : POST /partner_api/payment_requests, type "redirect",
    avec `forceProviderDirect: true` + `payerPhone` pour déclencher un
    encaissement direct sur le numéro fourni sans passer par une page de
    paiement hébergée (flux adapté à une app wallet qui connaît déjà le
    numéro et l'opérateur choisis par l'utilisateur).
  - Pay-out (retrait) : POST /partner_api/transfers, avec un bénéficiaire
    "inline" (name/paymentMethod/identifier) plutôt qu'un contact
    pré-enregistré, puisque le destinataire change à chaque retrait.
"""
from __future__ import annotations

import logging
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.config import get_settings

logger = logging.getLogger("jeko_client")

settings = get_settings()

# Le "paymentMethod" attendu par JEKO diffère légèrement entre les endpoints
# de paiement (payment_requests / soundbox) et ceux de transfert/contacts —
# voir la doc : payment_requests utilise "orange", transfers/contacts
# utilisent "orange_money". Les autres valeurs (wave/mtn/moov) sont identiques.
PAYMENT_METHOD_FOR_DEPOSIT = {
    "WAVE": "wave",
    "ORANGE": "orange",
    "MTN": "mtn",
    "MOOV": "moov",
}
PAYMENT_METHOD_FOR_TRANSFER = {
    "WAVE": "wave",
    "ORANGE": "orange_money",
    "MTN": "mtn",
    "MOOV": "moov",
}


def _amount_to_cents(amount: Decimal) -> int:
    """XOF n'a pas de sous-unité réelle, mais JEKO exprime quand même tous
    les montants en centimes (minimums documentés : 500 centimes = 5 XOF
    pour un transfert, 100 centimes pour une demande de paiement)."""
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


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
        "X-API-KEY": settings.jeko_api_key,
        "X-API-KEY-ID": settings.jeko_api_key_id,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


class JekoClient:
    """Client asynchrone réutilisable pour la Partner API JEKO Africa."""

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
    async def _request(self, method: str, path: str, *, json_body: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            response = await self._client.request(method, path, json=json_body)
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout, httpx.ConnectTimeout) as exc:
            logger.warning("Erreur réseau JEKO sur %s %s: %s", method, path, exc)
            raise JekoNetworkError(str(exc)) from exc

        if response.status_code >= 500:
            logger.warning("Erreur serveur JEKO %s sur %s %s", response.status_code, method, path)
            raise JekoNetworkError(f"HTTP {response.status_code}")

        data = response.json() if response.content else {}

        if response.status_code >= 400:
            # Erreur métier JEKO (ex: insufficient_balance, validation_error,
            # store_not_found, contact_not_found...) -> pas de retry.
            raise JekoAPIError(response.status_code, data)

        return data

    # ---------- Pay-in (dépôt) ----------

    async def create_deposit_payment_request(
        self,
        *,
        internal_reference: str,
        amount: Decimal,
        operator: str,
        phone_number: str,
    ) -> dict[str, Any]:
        """
        Crée une demande de paiement JEKO en mode `redirect` +
        `forceProviderDirect`, ce qui déclenche directement une notification
        de paiement sur l'app mobile money du numéro fourni, sans page de
        paiement intermédiaire à afficher côté client.

        Réponse notable : {"id": "...", "status": "pending", ...}
        Le statut évolue ensuite vers "success"/"error", suivi via webhook
        ou via `get_payment_request_status`.
        """
        payment_method = PAYMENT_METHOD_FOR_DEPOSIT.get(operator.upper(), operator.lower())
        body = {
            "storeId": settings.jeko_store_id,
            "amountCents": _amount_to_cents(amount),
            "currency": "XOF",
            "reference": internal_reference,
            "paymentDetails": {
                "type": "redirect",
                "data": {
                    "paymentMethod": payment_method,
                    "forceProviderDirect": True,
                    "payerPhone": phone_number,
                    # Requis par le schéma même en mode direct ; le client
                    # mobile ne les utilise jamais dans ce flux.
                    "successUrl": f"{settings.public_base_url}/api/v1/webhooks/jeko/redirect-success",
                    "errorUrl": f"{settings.public_base_url}/api/v1/webhooks/jeko/redirect-error",
                },
            },
        }
        logger.info("Création payment_request JEKO (dépôt) ref=%s montant=%s", internal_reference, amount)
        return await self._request("POST", "/partner_api/payment_requests", json_body=body)

    async def get_payment_request_status(self, jeko_payment_request_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/partner_api/payment_requests/{jeko_payment_request_id}")

    # ---------- Pay-out (retrait) ----------

    async def create_withdrawal_transfer(
        self,
        *,
        internal_reference: str,
        amount: Decimal,
        operator: str,
        phone_number: str,
        beneficiary_name: str,
    ) -> dict[str, Any]:
        """
        Crée un transfert JEKO vers un bénéficiaire "inline" (sans contact
        pré-enregistré), adapté à un retrait wallet où le numéro
        destinataire change à chaque opération. JEKO crée en interne un
        contact masqué automatiquement.

        Réponse notable : {"id": "wth_...", "status": "pending", "fees": {...}, ...}
        """
        payment_method = PAYMENT_METHOD_FOR_TRANSFER.get(operator.upper(), operator.lower())
        body = {
            "storeId": settings.jeko_store_id,
            "name": beneficiary_name,
            "paymentMethod": payment_method,
            "identifier": {"reference": phone_number},
            "amountCents": _amount_to_cents(amount),
            "currency": "XOF",
            "description": "Retrait Wallet applicatif",
            "reference": internal_reference,
        }
        logger.info("Création transfer JEKO (retrait) ref=%s montant=%s", internal_reference, amount)
        return await self._request("POST", "/partner_api/transfers", json_body=body)

    # ---------- Magasin ----------

    async def get_store_balance(self) -> dict[str, Any]:
        """Solde disponible du magasin JEKO configuré (`jeko_store_id`)."""
        return await self._request("GET", f"/partner_api/stores/{settings.jeko_store_id}/balance")


_jeko_client: JekoClient | None = None


def get_jeko_client() -> JekoClient:
    """Singleton FastAPI dependency-friendly."""
    global _jeko_client
    if _jeko_client is None:
        _jeko_client = JekoClient()
    return _jeko_client
