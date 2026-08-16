-- Corrige les lignes kyc_submissions dont le statut vaut 'VERIFIED', une
-- valeur qui n'a jamais existé dans l'enum applicatif KycStatus
-- (PENDING, APPROVED, REJECTED) et qui fait planter SQLAlchemy avec
-- "LookupError: 'VERIFIED' is not among the defined enum values"
-- dès qu'un utilisateur ou un admin déclenche une requête qui lit cette
-- colonne (dashboard admin -> onglet Utilisateurs / KYC).

DO $$
BEGIN
    -- N'agit que si le type Postgres kyc_status contient bien le label
    -- 'VERIFIED' (sinon la donnée ne peut physiquement pas être présente).
    IF EXISTS (
        SELECT 1
        FROM pg_enum e
        JOIN pg_type t ON t.oid = e.enumtypid
        WHERE t.typname = 'kyc_status' AND e.enumlabel = 'VERIFIED'
    ) THEN
        UPDATE kyc_submissions
        SET status = 'APPROVED'
        WHERE status::text = 'VERIFIED';
    END IF;
END $$;
