from typing import AsyncGenerator, Generator

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings

settings = get_settings()


def _sync_url(url: str) -> str:
    """Convertit l'URL async (asyncpg) en URL sync (psycopg2) si besoin,
    pour que les migrations et get_sync_db utilisent un vrai driver
    synchrone plutot que de tenter (a tort) d'utiliser asyncpg en sync."""
    if url.startswith("postgresql+asyncpg://"):
        return url.replace("postgresql+asyncpg://", "postgresql+psycopg2://", 1)
    if url.startswith("postgres+asyncpg://"):
        return url.replace("postgres+asyncpg://", "postgresql+psycopg2://", 1)
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+psycopg2://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg2://", 1)
    return url


engine = create_async_engine(
    settings.DATABASE_URL,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
    # Timeout explicite : sans ca, asyncpg peut rester bloque indefiniment si
    # la base est injoignable/pas encore prete (ex: cold start Render), ce qui
    # empeche uvicorn de demarrer et de binder le port -> deploiement qui
    # echoue silencieusement apres le timeout de scan de port de Render.
    connect_args={"timeout": 10, "command_timeout": 30},
)
AsyncSessionLocal = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)


# Moteur synchrone (psycopg2) — utilise par le systeme de migrations
# automatiques au demarrage (db_migrate.py) et par les routes qui ont besoin
# d'une session synchrone (get_sync_db, ex: api_wallet.py).
_sync_engine = create_engine(
    _sync_url(settings.DATABASE_URL),
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
)
SyncSessionLocal = sessionmaker(bind=_sync_engine, expire_on_commit=False, class_=Session)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()


def get_sync_db() -> Generator[Session, None, None]:
    db = SyncSessionLocal()
    try:
        yield db
    finally:
        db.close()
