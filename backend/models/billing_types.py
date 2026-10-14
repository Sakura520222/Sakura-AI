"""Exact provider-defined pricing identities on every supported SQL dialect."""

from sqlalchemy import String
from sqlalchemy.dialects.mysql import VARCHAR
from sqlalchemy.types import TypeDecorator


def price_identity_collation(dialect) -> str | None:
    if dialect.name not in {"mysql", "mariadb"}:
        return None
    if dialect.name == "mariadb" or getattr(dialect, "is_mariadb", False):
        return "utf8mb4_nopad_bin"
    # Unlike utf8mb4_bin, NO PAD also distinguishes trailing spaces in opaque IDs.
    return "utf8mb4_0900_bin"


class PricingIdentityString(TypeDecorator):
    impl = String
    cache_ok = True

    def __init__(self, length: int):
        self.length = length
        super().__init__(length=length)

    def load_dialect_impl(self, dialect):
        collation = price_identity_collation(dialect)
        if collation is not None:
            return dialect.type_descriptor(
                VARCHAR(self.length, charset="utf8mb4", collation=collation)
            )
        return dialect.type_descriptor(String(self.length))
