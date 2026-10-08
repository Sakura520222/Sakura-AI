"""Dry-run/audited repair of historical quota inflation, after schema migration.

Usage: uv run python scripts/migrate_legacy_billing.py --manifest reviewed.json
       uv run python scripts/migrate_legacy_billing.py --manifest reviewed.json --apply --actor-id 1
The database URL is read from deployment configuration and is never printed.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.core.config import get_settings
from backend.models import database
from backend.services.legacy_billing_migration_service import (
    LegacyBillingMigrationService,
)


async def run(args):
    database.init_async_db(get_settings().database_url)
    input_file = args.redeem_snapshots or args.source_conversions or args.manifest
    manifest = json.loads(Path(input_file).read_text()) if input_file else []
    if not isinstance(manifest, list):
        raise ValueError("Manifest must be a list of reviewed source entries")
    after_id = args.after_id
    results = []
    async with database.async_session() as session:
        service = LegacyBillingMigrationService(session)
        if not manifest:
            print(
                json.dumps(
                    await service.audit(after_id=after_id, batch_size=args.batch_size),
                    ensure_ascii=False,
                )
            )
            await session.rollback()
            return
        cursor_key = (
            "code_id"
            if args.redeem_snapshots
            else "entitlement_id"
            if args.source_conversions
            else "order_id"
        )
        for entry in sorted(manifest, key=lambda value: value[cursor_key]):
            if entry[cursor_key] <= after_id:
                continue
            try:
                async with session.begin_nested():
                    if args.redeem_snapshots:
                        result = await service.prepare_code_snapshot(
                            entry["code_id"],
                            entry["snapshot"],
                            actor_id=args.actor_id or 0,
                            evidence=entry["evidence"],
                            dry_run=not args.apply,
                        )
                    elif args.source_conversions:
                        result = await service.convert_source(
                            entry["entitlement_id"],
                            entry["conversion_credits"],
                            actor_id=args.actor_id or 0,
                            evidence=entry["evidence"],
                            dry_run=not args.apply,
                        )
                    else:
                        result = await service.repair(
                            entry, actor_id=args.actor_id or 0, dry_run=not args.apply
                        )
                if args.apply:
                    await session.commit()
                else:
                    await session.rollback()
                results.append(result)
            except Exception as exc:
                await session.rollback()
                results.append(
                    {
                        cursor_key: entry.get(cursor_key),
                        "status": "failed",
                        "error_type": type(exc).__name__,
                        "message": str(exc)
                        if type(exc).__name__ == "PaymentError"
                        else "Database repair failed; inspect local server logs",
                    }
                )
            if len(results) >= args.batch_size:
                break
    print(
        json.dumps(
            {
                "dry_run": not args.apply,
                "results": results,
                "next_after_id": results[-1].get(
                    cursor_key, results[-1].get("order_id")
                )
                if results
                else after_id,
            },
            ensure_ascii=False,
        )
    )
    await database.async_engine.dispose()
    if any(result["status"] == "failed" for result in results):
        raise SystemExit(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group()
    inputs.add_argument(
        "--redeem-snapshots",
        help="Reviewed historical redeem code purchased snapshots JSON",
    )
    inputs.add_argument(
        "--source-conversions",
        help="Reviewed source-specific exact Credits conversion JSON",
    )
    inputs.add_argument(
        "--manifest",
        help="Reviewed JSON source evidence; omission produces an audit-only report",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply reviewed repairs; default is dry-run",
    )
    parser.add_argument("--actor-id", type=int, help="Existing authorized operator ID")
    parser.add_argument("--after-id", type=int, default=0)
    parser.add_argument("--batch-size", type=int, default=100)
    args = parser.parse_args()
    if args.apply and (
        not (args.manifest or args.source_conversions or args.redeem_snapshots)
        or not args.actor_id
    ):
        parser.error(
            "--apply requires reviewed --manifest or --source-conversions, and --actor-id"
        )
    if not 1 <= args.batch_size <= 1000 or args.after_id < 0:
        parser.error("Invalid batch size or after-id")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
