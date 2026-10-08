"""Review old TRON invoice identities; never guess an ambiguous payer."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.core.config import get_settings
from backend.models import database
from backend.services.payment_service import PaymentService


async def run(args):
    database.init_async_db(get_settings().database_url)
    async with database.async_session() as session:
        report = await PaymentService(session).audit_tron_invoices(
            operator_id=args.actor_id, dry_run=not args.apply
        )
        if args.apply:
            await session.commit()
        else:
            await session.rollback()
        print(json.dumps(report, ensure_ascii=False))
    await database.async_engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--actor-id", type=int)
    args = parser.parse_args()
    if args.apply and not args.actor_id:
        parser.error("--apply requires --actor-id")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
