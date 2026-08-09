"""
Règles de frais — Ayak'bine v2 (transfert inter-opérateurs Mobile Money).

Deux frais bien distincts, à ne jamais confondre :
  1. PLATFORM_FEE_RATE (8%) : notre marge, prélevée sur le montant net
     souhaité par le client, affichée en direct dans l'app avant confirmation.
  2. JEKO_DEPOSIT_FEE_RATE : commission que JEKO déduit AUTOMATIQUEMENT et
     silencieusement de son côté au moment de la collecte (pay-in), selon
     l'opérateur SOURCE utilisé (voir CGU JEKO). Jamais affichée au client —
     utile uniquement pour calculer EXACTEMENT le montant à recréditer sur
     le wallet interne si le pay-out échoue après un pay-in réussi : le
     montant réellement disponible côté JEKO est déjà net de cette
     commission (elle n'est prélevée qu'une fois, à la collecte réussie,
     jamais côté pay-out puisque celui-ci n'a alors jamais abouti).
"""
from decimal import ROUND_HALF_UP, Decimal

from app.models.models import MobileOperator

PLATFORM_FEE_RATE = Decimal("0.08")  # 8%, tout compris (marge Ayak'bine + frais JEKO)

MIN_TRANSFER_AMOUNT = Decimal("250")
MAX_TRANSFER_AMOUNT = Decimal("30000")

# Commission JEKO à la collecte (pay-in), selon l'opérateur SOURCE — d'après
# les CGU officielles JEKO (grille tarifaire pay-in).
JEKO_DEPOSIT_FEE_RATE: dict[MobileOperator, Decimal] = {
    MobileOperator.WAVE: Decimal("0.015"),
    MobileOperator.ORANGE: Decimal("0.01"),
    MobileOperator.MTN: Decimal("0.01"),
    MobileOperator.MOOV: Decimal("0.01"),
}

# Commission JEKO au versement (pay-out), selon l'opérateur DESTINATION.
# Non utilisée dans les calculs de recréditation wallet (elle ne s'applique
# que si le pay-out a réellement réussi) — conservée pour reporting/analytics.
JEKO_PAYOUT_FEE_RATE: dict[MobileOperator, Decimal] = {
    MobileOperator.WAVE: Decimal("0.015"),
    MobileOperator.ORANGE: Decimal("0.01"),
    MobileOperator.MTN: Decimal("0.01"),
    MobileOperator.MOOV: Decimal("0.01"),
}


def compute_platform_fee(net_amount: Decimal) -> Decimal:
    """Frais plateforme (8%) sur le montant net voulu par le client, arrondi au XOF entier."""
    return (net_amount * PLATFORM_FEE_RATE).quantize(Decimal("1"), rounding=ROUND_HALF_UP)


def compute_total_to_collect(net_amount: Decimal) -> Decimal:
    """Montant total à faire payer au client (montant net + frais plateforme)."""
    return net_amount + compute_platform_fee(net_amount)


def compute_wallet_credit_on_payout_failure(total_collected: Decimal, source_operator: MobileOperator) -> Decimal:
    """
    Montant à créditer sur le wallet interne quand le pay-in a réussi mais
    que le pay-out a ensuite échoué : le total collecté, moins la commission
    JEKO de dépôt (déjà déduite silencieusement par JEKO côté pay-in, donc
    réellement absente du montant qu'Ayak'bine a effectivement reçu).
    """
    deposit_fee_rate = JEKO_DEPOSIT_FEE_RATE[source_operator]
    jeko_fee = (total_collected * deposit_fee_rate).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return total_collected - jeko_fee
