#!/usr/bin/env python3
"""Export fixed MySQL immutable guards for an authorized DBA; never connects."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.models.billing_schema import IMMUTABLE_TABLES, mysql_guard_ddl


def export_sql():
    header = (
        "-- Billing 2.0 immutable guards for MySQL 8.0.29+ / 8.4.\n"
        "-- Review and run as an authorized database administrator in the Sakura database.\n"
        "-- The installer account becomes DEFINER and must remain valid.\n"
        "-- Runtime accounts need table TRIGGER privileges for metadata verification.\n"
        "-- Existing guards are verified by application startup; conflicting definitions fail closed.\n"
        "-- This file changes no balances, grants no privileges and changes no global settings.\n\n"
    )
    return (
        header
        + "\n\n".join(
            mysql_guard_ddl(table, action, if_not_exists=True) + ";"
            for table in IMMUTABLE_TABLES
            for action in ("UPDATE", "DELETE")
        )
        + "\n"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, help="Write SQL to this file; default stdout"
    )
    args = parser.parse_args()
    sql = export_sql()
    if args.output:
        args.output.write_text(sql, encoding="utf-8")
    else:
        print(sql, end="")


if __name__ == "__main__":
    main()
