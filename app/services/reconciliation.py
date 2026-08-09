"""
Logique de réconciliation d'UNE transaction PENDING, partagée entre :
  - scripts/reconcile_pending_transactions.py (usage en ligne de commande,
    nécessite un accès Shell — indisponible sur le plan gratuit Render)
  - app/api/v1/admin.py (route HTTP protégée par un secret, utilisable
    simplement en collant une URL dans le navigateur — voir ce fichier
    pour le contournement "pas de Shell sur le plan gratuit").

Interroge JEKO directement (source de vérité) pour connaître le vrai statut
d'une étape pay-in/pay-out restée PENDING chez nous, puis rejoue la même
logique métier que le webhook réel (app/services/webhook_service.py).

IDEMPOTENT : chaque étape n'est traitée que si elle est encore PENDING en
base — si le vrai webhook JEKO finit par arriver en parallèle, aucun risque
de double versement / double recrédit (voir les checks `TERMINAL_LEG_STATUSES`
dans webhook_service.py, inchangés).
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import Transaction, TransactionStatus, TransactionType
from app.services.jeko_client import JekoAPIError, JekoClient, JekoNetworkError
from app.services.webhook_service import _handle_payin_webhook, _handle_payout_webhook  # noqa: SLF001


def map_jeko_status(raw_status: str | None) -> TransactionStatus | None:
    """None = toujours en attente côté JEKO (ou statut non reconnu), on ne touche à rien."""
    if raw_status is None:
        return None
    value = raw_status.lower()
    if value in ("success", "completed", "successful"):
        return TransactionStatus.SUCCESS
    if value in ("error", "failed", "cancelled", "canceled", "rejected"):
        return TransactionStatus.FAILED
    return None


class TransactionNotFoundForReconciliation(Exception):
    pass


async def reconcile_transaction_by_reference(
    db: AsyncSession,
    jeko: JekoClient,
    internal_reference: str,
    *,
    apply: bool,
) -> dict:
    """
    Réconcilie UNE transaction identifiée par sa référence interne.
    Retourne un dict décrivant précisément ce qui a été constaté/fait,
    directement affichable en JSON par la route admin.
    """
    result = await db.execute(select(Transaction).where(Transaction.internal_reference == internal_reference))
    transaction = result.scalar_one_or_none()
    if transaction is None:
        raise TransactionNotFoundForReconciliation(internal_reference)

    report: dict = {
        "internal_reference": internal_reference,
        "mode": "APPLY" if apply else "DRY_RUN",
        "status_before": transaction.status.value,
        "payin_status_before": transaction.payin_status.value if transaction.payin_status else None,
        "payout_status_before": transaction.payout_status.value if transaction.payout_status else None,
        "actions": [],
    }

    # --- Étape pay-in (uniquement pertinent pour un TRANSFER) ---
    if transaction.type == TransactionType.TRANSFER and transaction.payin_status == TransactionStatus.PENDING:
        if not transaction.jeko_payin_id:
            report["actions"].append("payin: ignoré (jamais initié côté JEKO, aucun jeko_payin_id)")
        else:
            try:
                jeko_data = await jeko.get_payment_request_status(transaction.jeko_payin_id)
            except (JekoAPIError, JekoNetworkError) as exc:
                report["actions"].append(f"payin: échec de vérification auprès de JEKO ({exc})")
                report["status_after"] = transaction.status.value
                return report

            new_status = map_jeko_status(jeko_data.get("status"))
            if new_status is None:
                report["actions"].append(f"payin: toujours PENDING côté JEKO (statut brut: {jeko_data.get('status')})")
            else:
                report["actions"].append(f"payin: réellement {new_status.value} côté JEKO")
                if apply:
                    await _handle_payin_webhook(
                        db, transaction, new_status, jeko_event_id="admin-reconcile", jeko=jeko
                    )
                    await db.commit()
                    report["actions"].append(f"payin: traité -> nouveau statut global = {transaction.status.value}")
                report["status_after"] = transaction.status.value
                return report  # pay-out à réconcilier dans un appel suivant si besoin

    # --- Étape pay-out (TRANSFER une fois le pay-in confirmé, ou WITHDRAWAL) ---
    if transaction.payout_status == TransactionStatus.PENDING:
        if not transaction.jeko_payout_id:
            report["actions"].append("payout: ignoré (jamais initié côté JEKO, aucun jeko_payout_id)")
        else:
            try:
                jeko_data = await jeko.get_transfer_status(transaction.jeko_payout_id)
            except (JekoAPIError, JekoNetworkError) as exc:
                report["actions"].append(f"payout: échec de vérification auprès de JEKO ({exc})")
                report["status_after"] = transaction.status.value
                return report

            new_status = map_jeko_status(jeko_data.get("status"))
            if new_status is None:
                report["actions"].append(f"payout: toujours PENDING côté JEKO (statut brut: {jeko_data.get('status')})")
            else:
                report["actions"].append(f"payout: réellement {new_status.value} côté JEKO")
                if apply:
                    await _handle_payout_webhook(db, transaction, new_status)
                    await db.commit()
                    report["actions"].append(f"payout: traité -> nouveau statut global = {transaction.status.value}")
    else:
        if not report["actions"]:
            report["actions"].append("rien à faire : aucune étape n'est PENDING pour cette transaction")

    report["status_after"] = transaction.status.value
    return report
