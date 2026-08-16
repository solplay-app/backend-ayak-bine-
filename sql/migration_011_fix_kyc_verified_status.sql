-- =====================================================================
-- Migration 011 — device_tokens existait déjà (ancien projet JEKO, avec
-- une structure différente), donc le "CREATE TABLE IF NOT EXISTS" de
-- migration_004 n'a rien fait : la colonne `token` n'a jamais été créée.
-- Résultat : toute notification push échouait silencieusement
-- ("column device_tokens.token does not exist"), avalée par le design
-- best-effort de notify_user() — invisible sauf dans les logs serveur.
--
-- Idempotente et non-destructive : ADD COLUMN IF NOT EXISTS uniquement,
-- aucune donnée existante supprimée ou modifiée.
-- =====================================================================

CREATE TABLE IF NOT EXISTS device_tokens (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4()
);

ALTER TABLE device_tokens ADD COLUMN IF NOT EXISTS user_id     UUID;
ALTER TABLE device_tokens ADD COLUMN IF NOT EXISTS token       VARCHAR(255);
ALTER TABLE device_tokens ADD COLUMN IF NOT EXISTS platform    VARCHAR(20) NOT NULL DEFAULT 'android';
ALTER TABLE device_tokens ADD COLUMN IF NOT EXISTS created_at  TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP;

-- Contrainte de clé étrangère vers users, si absente.
DO $$
BEGIN
    ALTER TABLE device_tokens
        ADD CONSTRAINT device_tokens_user_id_fkey
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;
EXCEPTION
    WHEN duplicate_object THEN NULL;
END
$$;

-- Contrainte d'unicité sur token, si absente (un même token FCM ne doit
-- être rattaché qu'à un seul enregistrement).
DO $$
BEGIN
    ALTER TABLE device_tokens ADD CONSTRAINT device_tokens_token_key UNIQUE (token);
EXCEPTION
    WHEN duplicate_table THEN NULL;
    WHEN duplicate_object THEN NULL;
END
$$;
