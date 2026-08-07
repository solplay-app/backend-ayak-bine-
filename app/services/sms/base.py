"""
Interface commune à tous les fournisseurs SMS (Orange SMS API, Twilio, ...).
Permet de changer de fournisseur via `SMS_PROVIDER` sans toucher au reste
du code (otp_service.py, routes auth).
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class SmsSendError(Exception):
    """
    Échec d'envoi du SMS (erreur réseau, timeout, ou refus du fournisseur).
    Volontairement une seule exception générique côté appelant : le détail
    du fournisseur (Twilio/Orange) reste encapsulé dans le provider, mais
    est conservé dans `.detail` pour les logs.
    """

    def __init__(self, message: str, *, detail: str | None = None):
        super().__init__(message)
        self.detail = detail


class SmsProvider(ABC):
    """Contrat minimal : envoyer un message texte à un numéro E.164."""

    @abstractmethod
    async def send(self, phone_number: str, message: str) -> None:
        """
        Envoie `message` à `phone_number` (format E.164, ex: +2250700000000).
        Lève `SmsSendError` en cas d'échec définitif (après retries internes
        éventuels). Ne retourne rien en cas de succès.
        """
        raise NotImplementedError

    async def close(self) -> None:
        """Libère les ressources (connexions HTTP) si nécessaire. No-op par défaut."""
        return None


def otp_message(code: str) -> str:
    """Gabarit unique du SMS OTP, partagé par tous les providers."""
    return f"Ayak'bine : votre code de vérification est {code}. Valable 5 minutes. Ne le partagez avec personne."
