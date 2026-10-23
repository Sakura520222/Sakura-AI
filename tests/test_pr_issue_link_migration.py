"""Legacy link migration keeps evidence without loading history into the client."""

import logging

import pytest
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.dialects import mysql, postgresql
from sqlalchemy.dialects.mysql.mariadb import MariaDBDialect
from sqlalchemy.exc import IntegrityError, OperationalError

from backend.models.database import PRIssueLink, _ensure_pr_issue_link_unique_index

LOGGER = logging.getLogger(__name__)
KEYS = ("repo_name", "pr_id", "issue_number", "link_type")
INSERT = text(
    "INSERT INTO pr_issue_links "
    "(id, repo_name, pr_id, issue_number, link_type, reference_text, inference_reason) "
    "VALUES (:id, :repo, :pr, :issue, :type, :reference, :reason)"
)


class AsyncConnection:
    def __init__(self, connection):
        self.connection = connection

    async def run_sync(self, function):
        return function(self.connection)


@pytest.fixture
def engine():
    database = create_engine("sqlite:///:memory:")
    try:
        yield database
    finally:
        database.dispose()


def create_legacy_table(connection, collation="BINARY"):
    assert collation in {"BINARY", "NOCASE", "RTRIM"}
    connection.execute(
        text(
            "CREATE TABLE pr_issue_links ("
            "id INTEGER PRIMARY KEY, "
            f"repo_name TEXT COLLATE {collation} NOT NULL, "
            "pr_id INTEGER NOT NULL, issue_number INTEGER NOT NULL, "
            f"link_type TEXT COLLATE {collation} NOT NULL, "
            "reference_text TEXT, inference_reason TEXT)"
        )
    )


def row(identifier, *, repo="o/r", pr=7, issue=11, link_type="semantic"):
    return {
        "id": identifier,
        "repo": repo,
        "pr": pr,
        "issue": issue,
        "type": link_type,
        "reference": f"reference-{identifier}",
        "reason": f"evidence-{identifier}",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("groups", [3, 2048])
async def test_history_stays_in_database_with_constant_migration_roundtrips(
    engine, groups
):
    """A Python scan or per-duplicate DELETE would grow with this history."""
    with engine.begin() as connection:
        create_legacy_table(connection)
        connection.execute(
            INSERT,
            [
                row(group * 5 + revision + 1, repo=f"o/repo-{group}")
                for group in range(groups)
                for revision in range(4)
            ]
            + [
                row(group * 5 + 5, repo=f"o/repo-{group}", link_type="explicit")
                for group in range(groups)
            ],
        )
        statements = []
        history_results = []

        def capture(_conn, cursor, statement, _params, _context, _many):
            statements.append(statement)
            # Inspector reads only schema metadata. A SELECT from the history
            # table creates a client result cursor even if the caller streams it.
            if cursor.description and statement.lstrip().upper().startswith("SELECT"):
                if "FROM pr_issue_links" in statement:
                    history_results.append(statement)

        event.listen(connection, "after_cursor_execute", capture)
        try:
            assert await _ensure_pr_issue_link_unique_index(
                AsyncConnection(connection), LOGGER
            )
        finally:
            event.remove(connection, "after_cursor_execute", capture)

        assert history_results == [], "migration returned link history to Python"
        deletes = [
            sql for sql in statements if sql.lstrip().upper().startswith("DELETE")
        ]
        assert len(deletes) == 1, "duplicate cleanup must not issue one DELETE per row"
        # Schema inspection and index installation add a fixed number of queries.
        assert len(statements) <= 20
        kept = connection.execute(
            text(
                "SELECT id, reference_text, inference_reason FROM pr_issue_links ORDER BY id"
            )
        ).all()
        assert kept == [
            (identifier, f"reference-{identifier}", f"evidence-{identifier}")
            for group in range(groups)
            for identifier in (group * 5 + 4, group * 5 + 5)
        ]


@pytest.mark.asyncio
async def test_latest_id_preserves_all_four_key_boundaries_and_evidence(engine):
    with engine.begin() as connection:
        create_legacy_table(connection)
        connection.execute(
            INSERT,
            [
                row(1),
                row(2, repo="x/r"),
                row(3, pr=8),
                row(4, issue=12),
                row(5, link_type="explicit"),
                row(6, repo="O/R"),
                row(7, link_type="Semantic"),
                row(9),
                row(8),  # Insertion order does not define the newest evidence.
            ],
        )
        assert await _ensure_pr_issue_link_unique_index(
            AsyncConnection(connection), LOGGER
        )
        kept = connection.execute(
            text(
                "SELECT id, reference_text, inference_reason FROM pr_issue_links ORDER BY id"
            )
        ).all()
        assert kept == [
            (identifier, f"reference-{identifier}", f"evidence-{identifier}")
            for identifier in [2, 3, 4, 5, 6, 7, 9]
        ]
        with pytest.raises(IntegrityError):
            connection.execute(INSERT, row(10))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("collation", "equivalent_repo", "equivalent_type"),
    [("NOCASE", "O/R", "SEMANTIC"), ("RTRIM", "o/r ", "semantic ")],
)
async def test_database_collation_defines_duplicate_keys(
    engine, collation, equivalent_repo, equivalent_type
):
    """Python equality would miss duplicates which the new DB index rejects."""
    with engine.begin() as connection:
        create_legacy_table(connection, collation)
        connection.execute(
            INSERT,
            [
                row(1),
                row(2, repo=equivalent_repo, link_type=equivalent_type),
                row(3, issue=12),
                row(4, link_type="explicit"),
            ],
        )
        assert await _ensure_pr_issue_link_unique_index(
            AsyncConnection(connection), LOGGER
        )
        assert connection.execute(
            text("SELECT id, repo_name, link_type FROM pr_issue_links ORDER BY id")
        ).all() == [
            (2, equivalent_repo, equivalent_type),
            (3, "o/r", "semantic"),
            (4, "o/r", "explicit"),
        ]
        with pytest.raises(IntegrityError):
            connection.execute(INSERT, row(5))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "definition",
    [
        "CREATE INDEX uq_pr_issue_link_key ON pr_issue_links(repo_name)",
        "CREATE UNIQUE INDEX uq_pr_issue_link_key ON pr_issue_links(id)",
    ],
)
async def test_conflicting_index_fails_before_cleanup(engine, definition):
    with engine.begin() as connection:
        create_legacy_table(connection)
        connection.execute(INSERT, [row(1), row(2)])
        connection.execute(text(definition))
        with pytest.raises(RuntimeError, match="not unique on the required key"):
            await _ensure_pr_issue_link_unique_index(
                AsyncConnection(connection), LOGGER
            )
        assert connection.execute(
            text("SELECT id FROM pr_issue_links ORDER BY id")
        ).all() == [
            (1,),
            (2,),
        ]


@pytest.mark.asyncio
async def test_missing_empty_and_already_unique_schemas_are_idempotent(engine):
    indexes_before = set(PRIssueLink.__table__.indexes)
    with engine.begin() as connection:
        adapter = AsyncConnection(connection)
        assert not await _ensure_pr_issue_link_unique_index(adapter, LOGGER)
        create_legacy_table(connection)
        assert await _ensure_pr_issue_link_unique_index(adapter, LOGGER)
        assert not await _ensure_pr_issue_link_unique_index(adapter, LOGGER)
        assert [
            (index["name"], tuple(index["column_names"]), bool(index["unique"]))
            for index in inspect(connection).get_indexes("pr_issue_links")
        ] == [("uq_pr_issue_link_key", KEYS, True)]
        connection.execute(text("DROP INDEX uq_pr_issue_link_key"))
        connection.execute(
            text(
                "CREATE UNIQUE INDEX other_name ON pr_issue_links"
                "(repo_name, pr_id, issue_number, link_type)"
            )
        )
        assert not await _ensure_pr_issue_link_unique_index(adapter, LOGGER)
    assert PRIssueLink.__table__.indexes == indexes_before


@pytest.mark.asyncio
async def test_cleanup_database_failure_propagates_without_installing_index(engine):
    with engine.begin() as connection:
        create_legacy_table(connection)
        connection.execute(INSERT, [row(1), row(2)])
        connection.execute(
            text(
                "CREATE TRIGGER deny_cleanup BEFORE DELETE ON pr_issue_links "
                "BEGIN SELECT RAISE(ABORT, 'cleanup unavailable'); END"
            )
        )
        with pytest.raises(IntegrityError, match="cleanup unavailable"):
            await _ensure_pr_issue_link_unique_index(
                AsyncConnection(connection), LOGGER
            )
        assert connection.scalar(text("SELECT COUNT(*) FROM pr_issue_links")) == 2
        assert inspect(connection).get_indexes("pr_issue_links") == []


@pytest.mark.asyncio
async def test_index_database_failure_propagates_and_outer_transaction_restores_rows(
    engine,
):
    indexes_before = set(PRIssueLink.__table__.indexes)
    with engine.begin() as connection:
        create_legacy_table(connection)
        connection.execute(INSERT, [row(1), row(2)])
        # SQLite index names are database-wide. This conflict is outside the
        # target table's inspector results and fails only after cleanup ran.
        connection.execute(text("CREATE TABLE another_table (id INTEGER PRIMARY KEY)"))
        connection.execute(
            text("CREATE INDEX uq_pr_issue_link_key ON another_table(id)")
        )
    with pytest.raises(OperationalError, match="already exists"):
        with engine.begin() as connection:
            await _ensure_pr_issue_link_unique_index(
                AsyncConnection(connection), LOGGER
            )
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT id FROM pr_issue_links ORDER BY id")
        ).all() == [
            (1,),
            (2,),
        ]
        assert inspect(connection).get_indexes("pr_issue_links") == []
    assert PRIssueLink.__table__.indexes == indexes_before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "dialect",
    [mysql.dialect(), MariaDBDialect(), postgresql.dialect()],
    ids=["mysql", "mariadb", "postgresql"],
)
async def test_delete_compiles_with_grouped_derived_table_materialization_barrier(
    engine, dialect
):
    """Removing the derived GROUP BY can cause MySQL target-table error 1093."""
    deletes = []

    def capture(_conn, statement, _multiparams, _params, _options):
        if getattr(statement, "is_delete", False):
            deletes.append(statement)

    with engine.begin() as connection:
        create_legacy_table(connection)
        connection.execute(INSERT, [row(1), row(2)])
        event.listen(connection, "before_execute", capture)
        try:
            assert await _ensure_pr_issue_link_unique_index(
                AsyncConnection(connection), LOGGER
            )
        finally:
            event.remove(connection, "before_execute", capture)
    assert len(deletes) == 1
    compiled = " ".join(str(deletes[0].compile(dialect=dialect)).split())
    assert compiled == (
        "DELETE FROM pr_issue_links WHERE (pr_issue_links.id NOT IN "
        "(SELECT latest_pr_issue_links.id FROM "
        "(SELECT max(pr_issue_links.id) AS id FROM pr_issue_links "
        "GROUP BY pr_issue_links.repo_name, pr_issue_links.pr_id, "
        "pr_issue_links.issue_number, pr_issue_links.link_type) AS latest_pr_issue_links))"
    )
