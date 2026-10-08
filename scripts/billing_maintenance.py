"""Audit wallets, release abandoned reservations and resolve reviewed Usage.

Read-only by default. This CLI never sends an AI request, payment or refund.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from backend.core.config import get_settings
from backend.models import database
from backend.models.billing_models import BillingWallet
from backend.models.telegram_models import TelegramUser
from backend.services.billing_reconciliation_service import resolve_billing_call
from backend.services.billing_service import BillingError, BillingService


async def run(args):
    database.init_async_db(get_settings().database_url)
    try:
        async with database.async_session() as session:
            if args.apply:
                actor = await session.get(TelegramUser, args.actor_id)
                if actor is None or not actor.is_active or actor.role != "super_admin":
                    raise BillingError("Apply requires an active super-admin actor")
            service = BillingService(session)
            report = []
            if args.command == "audit":
                query = select(BillingWallet.user_id).where(
                    BillingWallet.user_id > args.after_id
                )
                if args.user_id:
                    query = query.where(BillingWallet.user_id == args.user_id)
                users = (
                    (
                        await session.execute(
                            query.order_by(BillingWallet.user_id).limit(args.batch_size)
                        )
                    )
                    .scalars()
                    .all()
                )
                report = [await service.reconcile_wallet(user_id) for user_id in users]
            elif args.command == "recover":
                report = await service.recover_operations(
                    limit=args.batch_size, dry_run=not args.apply
                )
            else:
                entries = json.loads(Path(args.manifest).read_text())
                if not isinstance(entries, list):
                    raise ValueError("Resolution manifest must be a JSON list")
                for entry in entries[: args.batch_size]:
                    try:
                        async with session.begin_nested():
                            event = await resolve_billing_call(
                                session, actor_id=args.actor_id, **entry
                            )
                            report.append(
                                {
                                    "call_id": event.call_id,
                                    "event_key": event.event_key,
                                    "status": "resolved",
                                }
                            )
                        if args.apply:
                            await session.commit()
                        else:
                            await session.rollback()
                    except Exception as exc:
                        await session.rollback()
                        report.append(
                            {
                                "call_id": entry.get("call_id"),
                                "status": "failed",
                                "error_type": type(exc).__name__,
                            }
                        )
            if args.apply and args.command != "audit":
                await session.commit()
            else:
                await session.rollback()
            print(
                json.dumps(
                    {
                        "command": args.command,
                        "dry_run": not args.apply,
                        "results": report,
                    },
                    ensure_ascii=False,
                )
            )
            if any(
                row.get("status") == "failed" or row.get("consistent") is False
                for row in report
            ):
                raise SystemExit(1)
    finally:
        await database.async_engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("audit", "recover", "resolve-calls"))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--actor-id", type=int)
    parser.add_argument("--user-id", type=int)
    parser.add_argument("--after-id", type=int, default=0)
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--manifest")
    args = parser.parse_args()
    if args.batch_size < 1 or args.batch_size > 1000:
        parser.error("batch-size must be between 1 and 1000")
    if args.apply and not args.actor_id:
        parser.error("--apply requires --actor-id")
    if args.command == "resolve-calls" and (not args.manifest or not args.actor_id):
        parser.error("resolve-calls requires reviewed --manifest and --actor-id")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
