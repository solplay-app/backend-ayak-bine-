"""
Modèle de données Ayak'bine v2 — Transfert inter-opérateurs Mobile Money.

Changement de cap par rapport aux versions précédentes :
  - Plus de modèle "agent physique + fiches clients sans compte" : `User`
    redevient un CLIENT FINAL classique, propriétaire de son propre Wallet.
  - Plus de CASH_IN/CASH_OUT : remplacés par TRANSFER (mouvement en 2 étapes
    chaînées, pay-in depuis un opérateur source puis pay-out vers un
    opérateur destination) et WITHDRAWAL (retrait simple wallet -> Mobile
    Money, une seule étape pay-out, pour récupérer un solde déjà présent
    dans le wallet interne — notamment après un pay-out en échec).
"""
import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Numeric,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class TransactionType(str, enum.Enum):
    TRANSFER = "TRANSFER"  # Mouvement inter-opérateurs en 2 étapes (pay-in + pay-out chaînés)
    WITHDRAWAL = "WITHDRAWAL"  # Retrait simple : wallet interne -> Mobile Money (1 étape, pay-out seul)
    BILL_PAYMENT = "BILL_PAYMENT"
    INSURANCE = "INSURANCE"


class TransactionStatus(str, enum.Enum):
    PENDING = "PENDING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    # Cas spécifique au TRANSFER : le pay-in (collecte) a réussi mais le
    # pay-out (versement au destinataire) a échoué. Le montant collecté a
    # alors été recrédité sur le wallet interne du client (filet de
    # sécurité) — l'argent n'est jamais perdu, mais le transfert visé n'a
    # pas abouti et doit être distingué d'un SUCCESS ou d'un FAILED simple.
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
    """Client final de l'application — propriétaire de son propre Wallet."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    full_name: Mapped[str] = mapped_column(String(100), nullable=False)
    phone_number: Mapped[str] = mapped_column(String(20), unique=True, nullable=False, index=True)
    pin_code_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    kyc_status: Mapped[KycStatus] = mapped_column(
        Enum(KycStatus, name="kyc_status"), default=KycStatus.PENDING
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    wallet: Mapped["Wallet"] = relationship(back_populates="user", uselist=False)
    transactions: Mapped[list["Transaction"]] = relationship(back_populates="user")


class Wallet(Base):
    """
    Solde interne RÉELLEMENT UTILISABLE par le client (pas juste des
    commissions) : sert de filet de sécurité (crédité automatiquement si un
    transfert échoue après collecte réussie) et peut être retiré vers le
    Mobile Money du client à tout moment via WITHDRAWAL.
    """

    __tablename__ = "wallets"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    balance: Mapped[float] = mapped_column(Numeric(15, 2), default=0)
    currency: Mapped[str] = mapped_column(String(5), default="XOF")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (CheckConstraint("balance >= 0", name="ck_wallet_balance_positive"),)

    user: Mapped["User"] = relationship(back_populates="wallet")


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # Référence interne "de base" pour cette intention de transfert. Les
    # appels JEKO utilisent des références DÉRIVÉES ({internal_reference}-IN
    # et {internal_reference}-OUT) pour garantir leur unicité même si JEKO
    # partage le même espace de noms entre payment_requests et transfers
    # (non confirmé explicitement par leur doc — on ne prend pas le risque).
    internal_reference: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    type: Mapped[TransactionType] = mapped_column(Enum(TransactionType, name="transaction_type"))

    # Pour TRANSFER : opérateur d'où part l'argent (toujours celui du compte
    # connecté). Pour WITHDRAWAL : non utilisé (l'argent part du wallet interne).
    source_operator: Mapped[MobileOperator | None] = mapped_column(
        Enum(MobileOperator, name="mobile_operator"), nullable=True
    )
    # Pour TRANSFER : opérateur du destinataire. Pour WITHDRAWAL : opérateur
    # vers lequel le solde wallet est retiré.
    destination_operator: Mapped[MobileOperator | None] = mapped_column(
        Enum(MobileOperator, name="mobile_operator"), nullable=True
    )

    # Montant NET garanti au destinataire (TRANSFER) ou montant retiré (WITHDRAWAL).
    amount: Mapped[float] = mapped_column(Numeric(15, 2), nullable=False)
    # Frais plateforme Ayak'bine (8%), en XOF — 0 pour un WITHDRAWAL (pas de frais au retrait).
    fee: Mapped[float] = mapped_column(Numeric(15, 2), default=0)
    # Montant total réellement collecté auprès du client (amount + fee) —
    # uniquement rempli pour TRANSFER, au moment où le pay-in est initié.
    total_collected: Mapped[float | None] = mapped_column(Numeric(15, 2), nullable=True)
    # Snapshot du taux de commission JEKO pay-in (selon source_operator) au
    # moment de la création — nécessaire pour calculer EXACTEMENT le
    # recrédit wallet en cas d'échec du pay-out, même si la grille JEKO
    # change plus tard.
    jeko_deposit_fee_rate: Mapped[float | None] = mapped_column(Numeric(6, 4), nullable=True)

    # Identifiants JEKO des deux étapes (remplis progressivement : jeko_payin_id
    # dès la création, jeko_payout_id seulement une fois le pay-in confirmé).
    jeko_payin_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    jeko_payout_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    payin_status: Mapped[TransactionStatus] = mapped_column(
        Enum(TransactionStatus, name="transaction_status"), default=TransactionStatus.PENDING
    )
    payout_status: Mapped[TransactionStatus | None] = mapped_column(
        Enum(TransactionStatus, name="transaction_status"), nullable=True
    )

    # Statut global dérivé (voir webhook_service.py) : reflète l'état
    # d'ensemble pour affichage simple côté app (historique, notifications).
    status: Mapped[TransactionStatus] = mapped_column(
        Enum(TransactionStatus, name="transaction_status"), default=TransactionStatus.PENDING
    )

    recipient_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    recipient_phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped["User"] = relationship(back_populates="transactions")
