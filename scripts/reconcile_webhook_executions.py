"""Review uncertain webhook handoffs; read-only unless --apply is specified.

Never sends GitHub writes, AI requests, payments, refunds or queue messages.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.core.config import get_settings
from backend.models import database
from backend.services.webhook_execution_reconciliation import (
    list_pending_receipts,
    reconcile_receipt,
)


async def run(args):
    database.init_async_db(get_settings().database_url)
    try:
        async with database.async_session() as db:
            if args.apply:
                receipt = await reconcile_receipt(
                    db,
                    args.receipt_id,
                    actor_id=args.actor_id,
                    resolution=args.resolution,
                    evidence=args.evidence,
                    reason=args.reason,
                )
                await db.commit()
                report = {
                    "receipt_id": receipt.receipt_id,
                    "status": receipt.status,
                    "response": receipt.response,
                }
            else:
                report = {
                    "dry_run": True,
                    "receipts": await list_pending_receipts(
                        db, limit=args.limit, offset=args.offset
                    ),
                }
                await db.rollback()
            print(json.dumps(report, ensure_ascii=False))
    finally:
        await database.async_engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--receipt-id")
    parser.add_argument("--actor-id", type=int)
    parser.add_argument(
        "--resolution", choices=("terminal", "accepted", "cancelled_unstarted")
    )
    parser.add_argument("--evidence")
    parser.add_argument("--reason")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--offset", type=int, default=0)
    args = parser.parse_args()
    if args.apply and not all(
        (args.receipt_id, args.actor_id, args.resolution, args.evidence, args.reason)
    ):
        parser.error(
            "--apply requires --receipt-id --actor-id --resolution --evidence --reason"
        )
    if not 1 <= args.limit <= 1000 or args.offset < 0:
        parser.error("Invalid pagination")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
