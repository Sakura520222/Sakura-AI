"""Inspect/replay durable verified payment evidence; never issue another payment/refund."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.core.config import get_settings
from backend.models import database
from backend.services.payment_event_service import PaymentEventService


async def run(args):
    database.init_async_db(get_settings().database_url)
    async with database.async_session() as session:
        service = PaymentEventService(session)
        if args.apply:
            record = await service.resolve(
                args.event_id,
                operator_id=args.actor_id,
                evidence=args.evidence,
                order_id=args.order_id,
                checkout_amount_cents=args.checkout_amount_cents,
                checkout_currency=args.checkout_currency,
                refund_reference_id=args.refund_reference_id,
                refund_amount_cents=args.refund_amount_cents,
                refund_currency=args.refund_currency,
            )
            await session.commit()
            print(
                json.dumps(
                    {
                        "event_id": record.id,
                        "status": record.status,
                        "pending_reason": record.pending_reason,
                        "order_id": record.order_id,
                    }
                )
            )
        else:
            rows = await service.list_pending(args.limit, offset=args.offset)
            print(
                json.dumps(
                    {
                        "dry_run": True,
                        "events": [
                            {
                                "id": row.id,
                                "provider": row.provider,
                                "event_key": row.event_key,
                                "status": row.status,
                                "pending_reason": row.pending_reason,
                                "order_id": row.order_id,
                                "evidence": row.evidence,
                            }
                            for row in rows
                        ],
                    },
                    ensure_ascii=False,
                )
            )
            await session.rollback()
    await database.async_engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--event-id", type=int)
    parser.add_argument("--actor-id", type=int)
    parser.add_argument("--evidence")
    parser.add_argument("--order-id", type=int)
    parser.add_argument("--checkout-amount-cents", type=int)
    parser.add_argument("--checkout-currency")
    parser.add_argument("--refund-reference-id")
    parser.add_argument("--refund-amount-cents", type=int)
    parser.add_argument("--refund-currency")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--offset", type=int, default=0)
    args = parser.parse_args()
    if args.apply and not all((args.event_id, args.actor_id, args.evidence)):
        parser.error("--apply requires --event-id --actor-id and reviewed --evidence")
    if not 1 <= args.limit <= 1000 or args.offset < 0:
        parser.error("Invalid pagination")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
