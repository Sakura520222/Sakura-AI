"""Small synchronous SQLite adapter for legacy async configuration unit tests.

Unlike the old FIFO mocks it executes predicates, upserts and transactions.
Production async/parallel sessions are exercised by test_config_center.py.
"""

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from backend.models.database import (
    AppConfig,
    Base,
    ConfigRevision,
    UserConfig,
    WebUIConfig,
)


class SQLiteConfigSession:
    def __init__(self, rows=None):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(
            self.engine, tables=[AppConfig.__table__, ConfigRevision.__table__, UserConfig.__table__, WebUIConfig.__table__]
        )
        self.session = Session(self.engine, expire_on_commit=False)
        for row in rows or []:
            self.session.add(
                row
                if isinstance(row, AppConfig)
                else AppConfig(key_name=row.key_name, key_value=row.key_value)
            )
        self.session.commit()
        self.added = []
        self.committed = False
        self.rolled_back = False
        self.commits = 0
        self.queries = 0
        self.deleted = []

    @property
    def rows(self):
        return list(self.session.scalars(select(AppConfig)))

    def get_bind(self):
        return self.engine

    async def execute(self, statement):
        self.queries += 1
        return self.session.execute(statement)

    def add(self, row):
        self.added.append(row)
        self.session.add(row)

    async def delete(self, row):
        self.deleted.append(row)
        self.session.delete(row)

    async def commit(self):
        self.session.commit()
        self.committed = True
        self.commits += 1

    async def rollback(self):
        self.session.rollback()
        self.rolled_back = True
        self.added = []
