"""
Point d'entrée unique pour obtenir le fournisseur SMS actif, choisi via
la variable d'environnement SMS_PROVIDER (console | twilio | orange).
Utilisé comme dépendance FastAPI (voir api/v1/auth.py).
"""
from __future__ import annotations

from app.config import get_settings

from .base import SmsProvider
from .console_provider import ConsoleSmsProvider
from .orange_provider import OrangeSmsProvider
from .twilio_provider import TwilioSmsProvider

settings = get_settings()

_provider: SmsProvider | None = None


def get_sms_provider() -> SmsProvider:
    """Singleton FastAPI dependency-friendly, comme get_jeko_client()."""
    global _provider
    if _provider is None:
        provider_name = settings.sms_provider.lower()
        if provider_name == "twilio":
            _provider = TwilioSmsProvider()
        elif provider_name == "orange":
            _provider = OrangeSmsProvider()
        elif provider_name == "console":
            _provider = ConsoleSmsProvider()
        else:
            raise RuntimeError(
                f"SMS_PROVIDER='{settings.sms_provider}' inconnu (valeurs valides : "
                "console, twilio, orange)."
            )
    return _provider


async def close_sms_provider() -> None:
    """À appeler au shutdown de l'app (voir main.py)."""
    if _provider is not None:
        await _provider.close()
