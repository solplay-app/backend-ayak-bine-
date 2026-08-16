"""Configuration chargée depuis .env (pydantic-settings v2)."""
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore")

    DATABASE_URL: str

    JWT_SECRET_KEY: str
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = 43200  # 30 jours

    ADMIN_BOOTSTRAP_SECRET: str

    WEBHOOK_SECRET: str
    SMS_LISTENER_TOKEN: str

    WAVE_MERCHANT_PHONE: str = "+221770000000"
    WAVE_MERCHANT_NAME: str = "Ayak'bine Wave"
    ORANGE_MERCHANT_PHONE: str = "+221780000000"
    ORANGE_MERCHANT_NAME: str = "Ayak'bine Orange"

    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000

    KYC_MAX_IMAGE_BASE64_CHARS: int = 6_000_000

    # Notifications push (FCM HTTP v1). Si l'une des deux est absente, le
    # push reste désactivé silencieusement (l'app continue de fonctionner
    # avec le polling seul — voir app/push_service.py).
    FIREBASE_PROJECT_ID: str = ""
    FIREBASE_SERVICE_ACCOUNT_JSON: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
