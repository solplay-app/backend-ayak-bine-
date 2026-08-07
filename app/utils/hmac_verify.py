"""
Vérification de signature HMAC des webhooks entrants JEKO.
JEKO signe le corps brut de la requête avec HMAC-SHA256 en utilisant le
Webhook Secret partagé, et transmet la signature dans l'en-tête
`X-Jeko-Signature` (format hexdigest).
"""
import hashlib
import hmac

from app.config import get_settings

settings = get_settings()


def compute_signature(raw_body: bytes, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()


def verify_jeko_signature(raw_body: bytes, received_signature: str | None) -> bool:
    """
    Compare en temps constant la signature reçue avec celle recalculée
    à partir du corps brut de la requête. Retourne False si absente/invalide.
    """
    if not received_signature:
        return False

    expected_signature = compute_signature(raw_body, settings.jeko_webhook_secret)

    # hmac.compare_digest protège contre les attaques par timing
    return hmac.compare_digest(expected_signature, received_signature)
