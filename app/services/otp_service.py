"""
Service OTP (One-Time Password) pour l'authentification par numéro de
téléphone. Stockage éphémère dans Redis (pas de table SQL nécessaire).
L'envoi réel du SMS est délégué à un SmsProvider (voir services/sms/),
choisi via SMS_PROVIDER (console/twilio/orange) — voir services/sms/factory.py.
"""
import logging
import random

from redis.asyncio import Redis

from app.services.sms import SmsProvider, SmsSendError, otp_message

logger = logging.getLogger("otp_service")

OTP_TTL_SECONDS = 300  # 5 minutes
OTP_MAX_ATTEMPTS = 5
RESEND_COOLDOWN_SECONDS = 60


class OtpRateLimited(Exception):
    pass


class OtpInvalid(Exception):
    pass


class OtpDeliveryFailed(Exception):
    """Le code a été généré mais le SMS n'a pas pu être envoyé."""


def _otp_key(phone_number: str) -> str:
    return f"otp:code:{phone_number}"


def _cooldown_key(phone_number: str) -> str:
    return f"otp:cooldown:{phone_number}"


def _attempts_key(phone_number: str) -> str:
    return f"otp:attempts:{phone_number}"


async def generate_and_send_otp(redis: Redis, phone_number: str, sms: SmsProvider) -> None:
    if await redis.get(_cooldown_key(phone_number)):
        raise OtpRateLimited("Veuillez patienter avant de redemander un code.")

    code = f"{random.randint(0, 999999):06d}"

    # On envoie AVANT de persister le code / poser le cooldown : si l'envoi
    # échoue, l'utilisateur peut retenter immédiatement (pas de cooldown
    # "gaspillé" sur un SMS jamais reçu).
    try:
        await sms.send(phone_number, otp_message(code))
    except SmsSendError as exc:
        logger.error("Échec d'envoi de l'OTP à %s: %s", phone_number, exc.detail or exc)
        raise OtpDeliveryFailed("Impossible d'envoyer le code par SMS pour le moment.") from exc

    await redis.set(_otp_key(phone_number), code, ex=OTP_TTL_SECONDS)
    await redis.set(_cooldown_key(phone_number), "1", ex=RESEND_COOLDOWN_SECONDS)
    await redis.delete(_attempts_key(phone_number))

    logger.info("OTP envoyé à %s (provider=%s)", phone_number, type(sms).__name__)


async def verify_otp(redis: Redis, phone_number: str, submitted_code: str) -> bool:
    attempts_key = _attempts_key(phone_number)
    attempts = int(await redis.get(attempts_key) or 0)
    if attempts >= OTP_MAX_ATTEMPTS:
        raise OtpInvalid("Trop de tentatives. Redemandez un nouveau code.")

    stored_code = await redis.get(_otp_key(phone_number))
    if stored_code is None:
        raise OtpInvalid("Code expiré. Redemandez un nouveau code.")

    if stored_code != submitted_code:
        await redis.incr(attempts_key)
        await redis.expire(attempts_key, OTP_TTL_SECONDS)
        raise OtpInvalid("Code incorrect.")

    await redis.delete(_otp_key(phone_number))
    await redis.delete(attempts_key)
    return True
