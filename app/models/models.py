"""
Modèle de données Ayak'bine v2 — portefeuille, recharge, transfert et retrait.
"""
import enum
import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Enum, ForeignKey, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class TransactionType(str, enum.Enum):
    DEPOSIT = "DEPOSIT"
    TRANSFER = "TRANSFER"
    WITHDRAWAL = "WITHDRAWAL"
    BILL_PAYMENT = "BILL_PAYMENT"
    INSURANCE = "INSURANCE"


class TransactionStatus(str, enum.Enum):
    PENDING = "PENDING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    FAILED_PAYOUT = "FAILED_PAYOUT"
    CANCELLED = "CANCELLED"


class MobileOperator(str, enum.Enum):
    WAVE = "WAVE"
    ORANGE = "ORANGE"
    MTN = "MTN"
    MOOV = "MOOV"


class KycStatus(str, enum.Enum):
    PENDING = "PENDING"
    VERIFIED = "VERIFIED"
    REJECTED = "REJECTED"


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    full_name: Mapped[str] = mapped_column(String(100), nullable=False)
    phone_number: Mapped[str] = mapped_column(String(20), unique=True, nullable=False, index=True)
    pin_code_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    kyc_status: Mapped[KycStatus] = mapped_column(Enum(KycStatus, name="kyc_status"), default=KycStatus.PENDING)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    wallet: Mapped["Wallet"] = relationship(back_populates="user", uselist=False)
    transactions: Mapped[list["Transaction"]] = relationship(back_populates="user")


class Wallet(Base):
    __tablename__ = "wallets"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    balance: Mapped[float] = mapped_column(Numeric(15, 2), default=0)
    currency: Mapped[str] = mapped_column(String(5), default="XOF")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (CheckConstraint("balance >= 0", name="ck_wallet_balance_positive"),)

    user: Mapped["User"] = relationship(back_populates="wallet")


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    internal_reference: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    type: Mapped[TransactionType] = mapped_column(Enum(TransactionType, name="transaction_type"))

    source_operator: Mapped[MobileOperator | None] = mapped_column(Enum(MobileOperator, name="mobile_operator"), nullable=True)
    destination_operator: Mapped[MobileOperator | None] = mapped_column(Enum(MobileOperator, name="mobile_operator"), nullable=True)

    amount: Mapped[float] = mapped_column(Numeric(15, 2), nullable=False)
    fee: Mapped[float] = mapped_column(Numeric(15, 2), default=0)
    total_collected: Mapped[float | None] = mapped_column(Numeric(15, 2), nullable=True)
    jeko_deposit_fee_rate: Mapped[float | None] = mapped_column(Numeric(6, 4), nullable=True)

    jeko_payin_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    jeko_payout_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    payin_status: Mapped[TransactionStatus] = mapped_column(Enum(TransactionStatus, name="transaction_status"), default=TransactionStatus.PENDING)
    payout_status: Mapped[TransactionStatus | None] = mapped_column(Enum(TransactionStatus, name="transaction_status"), nullable=True)
    status: Mapped[TransactionStatus] = mapped_column(Enum(TransactionStatus, name="transaction_status"), default=TransactionStatus.PENDING)

    recipient_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    recipient_phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped["User"] = relationship(back_populates="transactions")


class KycSubmission(Base):
    """
    Demande de vérification d'identité (KYC), validée manuellement par un
    admin via app/api/v1/admin.py (pas de prestataire tiers automatique
    pour l'instant).

    Table volontairement séparée de `users` : ça évite de devoir modifier
    la table `users` existante en production (pas d'Alembic/migration ici,
    seulement Base.metadata.create_all au démarrage — qui ne crée que les
    tables manquantes, jamais de nouvelles colonnes sur une table existante).
    Une nouvelle table, elle, est créée automatiquement sans aucune action
    manuelle nécessaire sur Render.

    `status` est une simple chaîne (pas un Enum Postgres natif) pour la même
    raison : ajouter une valeur à un ENUM Postgres existant nécessite aussi
    une commande SQL manuelle (ALTER TYPE ... ADD VALUE), qu'on veut éviter ici.
    """
    __tablename__ = "kyc_submissions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)

    # Images encodées en base64 (pas de stockage objet type S3 configuré sur
    # ce projet actuellement) : simple et suffisant pour le volume actuel,
    # mais à migrer vers un stockage dédié si le nombre de demandes grossit
    # beaucoup (une table Postgres n'est pas l'idéal pour de gros blobs).
    id_document_base64: Mapped[str] = mapped_column(Text, nullable=False)
    selfie_base64: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(String(20), default="UNDER_REVIEW")  # UNDER_REVIEW / VERIFIED / REJECTED
    rejection_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)

    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped["User"] = relationship()
