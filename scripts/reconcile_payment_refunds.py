"""Inspect quarantined payment refunds; apply only a provider-verified outcome."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from backend.core.config import get_settings
from backend.models import database
from backend.models.legacy_entitlement_models import PaymentRefundAttempt
from backend.services.payment_service import PaymentService


async def run(args):
    database.init_async_db(get_settings().database_url)
    async with database.async_session() as session:
        if args.apply:
            order = await PaymentService(session).reconcile_refund(
                args.attempt_id,
                operator_id=args.actor_id,
                upstream_status=args.outcome,
                evidence=args.evidence,
                provider_refund_id=args.provider_refund_id,
            )
            await session.commit()
            print(
                json.dumps(
                    {
                        "attempt_id": args.attempt_id,
                        "order_id": order.id,
                        "order_status": order.status,
                        "refunded_amount_cents": order.refunded_amount_cents,
                    }
                )
            )
        else:
            attempts = (
                (
                    await session.execute(
                        select(PaymentRefundAttempt)
                        .where(
                            PaymentRefundAttempt.status.in_(
                                ("pending", "unknown", "upstream_succeeded")
                            )
                        )
                        .order_by(PaymentRefundAttempt.id)
                        .limit(args.limit)
                    )
                )
                .scalars()
                .all()
            )
            print(
                json.dumps(
                    {
                        "dry_run": True,
                        "attempts": [
                            {
                                "id": row.id,
                                "order_id": row.order_id,
                                "status": row.status,
                                "amount_cents": row.amount_cents,
                                "currency": row.currency,
                                "credit_hold_units": row.credit_hold_units,
                                "provider_refund_id": row.provider_refund_id,
                            }
                            for row in attempts
                        ],
                    }
                )
            )
            await session.rollback()
    await database.async_engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--attempt-id", type=int)
    parser.add_argument("--actor-id", type=int)
    parser.add_argument("--outcome", choices=("refunded", "not_refunded"))
    parser.add_argument(
        "--evidence", help="Provider dashboard/request evidence; exclude credentials"
    )
    parser.add_argument("--provider-refund-id")
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    if args.apply and not all(
        (args.attempt_id, args.actor_id, args.outcome, args.evidence)
    ):
        parser.error("--apply requires --attempt-id --actor-id --outcome --evidence")
    if not 1 <= args.limit <= 1000:
        parser.error("Invalid limit")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
