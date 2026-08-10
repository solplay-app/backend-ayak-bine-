from functools import lru_cache
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Base de données
    database_url: str
    redis_url: str = "redis://localhost:6379/0"

    @field_validator("database_url")
    @classmethod
    def _force_asyncpg_driver(cls, v: str) -> str:
        # Render (et d'autres hébergeurs) fournissent DATABASE_URL au format
        # "postgres://..." ou "postgresql://..." (driver sync par défaut).
        # SQLAlchemy async a besoin explicitement du driver asyncpg.
        if v.startswith("postgres://"):
            v = v.replace("postgres://", "postgresql+asyncpg://", 1)
        elif v.startswith("postgresql://") and "+asyncpg" not in v:
            v = v.replace("postgresql://", "postgresql+asyncpg://", 1)
        return v

    # Sécurité applicative
    jwt_secret_key: str
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60

    # JEKO Africa (voir https://developer.jeko.africa)
    # Authentification réelle JEKO : deux en-têtes distincts, PAS un Bearer token.
    jeko_base_url: str = "https://api.jeko.africa"
    jeko_api_key: str
    jeko_api_key_id: str
    jeko_store_id: str  # storeId JEKO — obligatoire sur presque tous les endpoints
    jeko_webhook_secret: str
    jeko_timeout_seconds: int = 15

    public_base_url: str

    # --- SMS (OTP) ---
    sms_provider: str = "console"  # console | twilio | orange
    sms_timeout_seconds: int = 10

    # Twilio
    twilio_account_sid: str | None = None
    twilio_auth_token: str | None = None
    twilio_from_number: str | None = None

    # Orange SMS API (prioritaire pour la Côte d'Ivoire / UEMOA)
    orange_client_id: str | None = None
    orange_client_secret: str | None = None
    orange_sender_address: str | None = None  # ex: "tel:+2250000000"
    orange_sender_name: str | None = None

    # --- Admin (réconciliation manuelle sans Shell, plan gratuit Render) ---
    # Doit être défini en variable d'environnement Render (onglet "Environment",
    # PAS besoin de Shell). Si absent, la route /admin/reconcile-transaction
    # refuse tout accès (403), donc pas de risque à laisser le code déployé.
    admin_reconcile_secret: str | None = None

    # --- Notifications push (Firebase Cloud Messaging) ---
    # `firebase_project_id` : l'ID du projet Firebase (ex: "ayak-bine"),
    # visible dans Firebase Console > Paramètres du projet > Général.
    # `firebase_service_account_json` : le CONTENU COMPLET (pas un chemin de
    # fichier) du fichier JSON de compte de service Firebase, collé tel quel
    # dans la variable d'environnement. Généré depuis Firebase Console >
    # Paramètres du projet > Comptes de service > "Générer une nouvelle clé
    # privée". Si l'un des deux est absent, les push sont simplement
    # désactivés (mode dégradé silencieux) : l'app continue de fonctionner
    # via le polling déjà en place côté écran de statut.
    firebase_project_id: str | None = None
    firebase_service_account_json: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
