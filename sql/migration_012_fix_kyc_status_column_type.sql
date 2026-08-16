-- Migration 012 — Corrige le type de la colonne kyc_submissions.status
--
-- Historique : sur cette base, la table kyc_submissions a été créée AVANT
-- que la migration 002 ne définisse le type enum Postgres `kyc_status`
-- (ex: via scripts/init_db.py avec une version antérieure des modèles où
-- `status` était un simple VARCHAR). Comme la migration 002 utilise
-- `CREATE TABLE IF NOT EXISTS`, elle n'a jamais touché la colonne
-- existante : elle est restée en `character varying` au lieu du type
-- `kyc_status`.
--
-- Conséquence concrète : toute requête SQLAlchemy qui filtre sur `status`
-- (ex: dashboard admin -> /api/v1/admin/kyc/pending) échoue avec :
--   "operator does not exist: character varying = kyc_status"
-- car SQLAlchemy envoie le paramètre casté en kyc_status alors que la
-- colonne, elle, est restée du texte brut.
--
-- Idempotente : ne fait rien si la colonne est déjà du bon type (relance
-- sans danger à chaque déploiement).

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_name = 'kyc_submissions'
          AND column_name = 'status'
          AND data_type <> 'USER-DEFINED'   -- 'USER-DEFINED' = déjà un enum
    ) THEN
        -- Normalise les valeurs héritées qui ne correspondent à aucun
        -- label de l'enum applicatif (ex: 'VERIFIED', voir migration 011)
        -- AVANT de forcer le cast, sinon le cast échoue.
        UPDATE kyc_submissions SET status = 'APPROVED' WHERE status = 'VERIFIED';
        UPDATE kyc_submissions SET status = 'PENDING'
         WHERE status NOT IN ('PENDING', 'APPROVED', 'REJECTED');

        ALTER TABLE kyc_submissions
            ALTER COLUMN status DROP DEFAULT,
            ALTER COLUMN status TYPE kyc_status USING status::kyc_status,
            ALTER COLUMN status SET DEFAULT 'PENDING';
    END IF;
END
$$;
