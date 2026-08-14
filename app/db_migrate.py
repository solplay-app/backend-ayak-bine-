"""Applique automatiquement les fichiers sql/migration_*.sql manquants,
au démarrage du serveur — aucune intervention manuelle (psql/Shell) requise.

Fonctionnement :
  - Une table `schema_migrations` retient le nom de chaque fichier déjà
    exécuté avec succès.
  - Au démarrage, chaque fichier sql/migration_NNN_*.sql est exécuté dans
    l'ordre, UNIQUEMENT s'il n'est pas déjà dans `schema_migrations`.
  - Cas particulier : si la base existe déjà (table `users` présente) mais
    que `schema_migrations` est vide (première mise à jour de ce système),
    la toute première migration (le schéma de base) est marquée comme déjà
    appliquée sans être rejouée, pour ne pas tenter de recréer des tables
    existantes.
"""
from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Engine

logger = logging.getLogger("ayakbine.migrations")

SQL_DIR = Path(__file__).resolve().parent.parent / "sql"


def _migration_files() -> list[Path]:
    return sorted(SQL_DIR.glob("migration_*.sql"), key=lambda p: p.name)


def _ensure_tracking_table(conn) -> None:
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            name        VARCHAR(255) PRIMARY KEY,
            applied_at  TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        )
    """))


def _table_exists(conn, table_name: str) -> bool:
    result = conn.execute(
        text("SELECT 1 FROM information_schema.tables WHERE table_name = :t"),
        {"t": table_name},
    ).first()
    return result is not None


def _already_applied(conn) -> set[str]:
    rows = conn.execute(text("SELECT name FROM schema_migrations")).fetchall()
    return {r[0] for r in rows}


def run_migrations(engine: Engine) -> None:
    files = _migration_files()
    if not files:
        logger.info("Aucun fichier de migration trouvé dans %s", SQL_DIR)
        return

    with engine.begin() as conn:
        _ensure_tracking_table(conn)
        applied = _already_applied(conn)

        # Base pré-existante jamais suivie par schema_migrations : on
        # marque la 1re migration (schéma de base) comme déjà faite pour
        # éviter de tenter de recréer les tables existantes.
        first = files[0]
        if first.name not in applied and _table_exists(conn, "users"):
            logger.warning(
                "Table 'users' déjà présente et %s non suivie — "
                "marquée comme déjà appliquée sans être rejouée.",
                first.name,
            )
            conn.execute(
                text("INSERT INTO schema_migrations (name) VALUES (:n) ON CONFLICT DO NOTHING"),
                {"n": first.name},
            )
            applied.add(first.name)

    for path in files:
        if path.name in applied:
            logger.info("Migration déjà appliquée, ignorée : %s", path.name)
            continue

        logger.info("Application de la migration : %s", path.name)
        sql_content = path.read_text(encoding="utf-8")
        with engine.begin() as conn:
            conn.execute(text(sql_content))
            conn.execute(
                text("INSERT INTO schema_migrations (name) VALUES (:n) ON CONFLICT DO NOTHING"),
                {"n": path.name},
            )
        logger.info("Migration appliquée avec succès : %s", path.name)
