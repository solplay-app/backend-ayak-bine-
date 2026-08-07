"""
Provider de développement : n'envoie aucun SMS réel, se contente de logger.
Utilisé quand SMS_PROVIDER=console (valeur par défaut hors production),
ce qui reproduit le comportement d'origine du projet.
"""
from __future__ import annotations

import logging

from .base import SmsProvider

logger = logging.getLogger("sms.console")


class ConsoleSmsProvider(SmsProvider):
    async def send(self, phone_number: str, message: str) -> None:
        logger.info("[SMS SIMULÉ] -> %s : %s", phone_number, message)
