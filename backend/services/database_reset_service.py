"""彻底清空应用数据库并重新进入 Setup 模式。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from loguru import logger
from sqlalchemy import inspect
from sqlalchemy.engine import Connection, Inspector
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from backend.core.bootstrap import read_connection_config, write_connection_config
from backend.models.database import normalize_database_url

DATABASE_RESET_CONFIRMATION = "RESET SAKURA AI"
_SUPPORTED_DATABASE_PREFIXES = (
    "mysql+asyncmy://",
    "postgresql+asyncpg://",
)
BeforeDatabaseDrop = Callable[[], Awaitable[None]]


@dataclass(frozen=True)
class DatabaseObjectInventory:
    """目标默认 schema 中需要删除的对象。"""

    tables: tuple[str, ...] = ()
    views: tuple[str, ...] = ()
    materialized_views: tuple[str, ...] = ()
    sequences: tuple[str, ...] = ()

    @property
    def total(self) -> int:
        return (
            len(self.tables)
            + len(self.views)
            + len(self.materialized_views)
            + len(self.sequences)
        )


@dataclass(frozen=True)
class DatabaseResetResult:
    """数据库重置结果，仅包含非敏感计数。"""

    tables_dropped: int
    views_dropped: int
    materialized_views_dropped: int
    sequences_dropped: int

    @property
    def total_dropped(self) -> int:
        return (
            self.tables_dropped
            + self.views_dropped
            + self.materialized_views_dropped
            + self.sequences_dropped
        )


class DatabaseResetError(RuntimeError):
    """数据库重置失败。

    ``setup_state_reset`` 表示 connection.json 已被切换到未完成状态。此时即使
    DDL 失败，也必须重启并停留在 Setup 模式，不能继续运行旧应用。
    """

    def __init__(self, *, setup_state_reset: bool = False) -> None:
        super().__init__("database reset failed")
        self.setup_state_reset = setup_state_reset


def _optional_names(
    inspector: Inspector,
    getter_name: str,
) -> tuple[str, ...]:
    getter: Callable[[], list[str]] | None = getattr(inspector, getter_name, None)
    if getter is None:
        return ()
    try:
        return tuple(sorted(getter()))
    except NotImplementedError:
        return ()


def _collect_database_objects(connection: Connection) -> DatabaseObjectInventory:
    """盘点当前连接默认 schema 中的全部可持久化对象。"""

    inspector = inspect(connection)
    return DatabaseObjectInventory(
        tables=tuple(sorted(inspector.get_table_names())),
        views=tuple(sorted(inspector.get_view_names())),
        materialized_views=_optional_names(
            inspector,
            "get_materialized_view_names",
        ),
        sequences=_optional_names(inspector, "get_sequence_names"),
    )


def _drop_statement(
    connection: Connection,
    object_type: str,
    name: str,
    *,
    cascade: bool,
) -> None:
    quoted_name = connection.dialect.identifier_preparer.quote_identifier(name)
    cascade_sql = " CASCADE" if cascade else ""
    connection.exec_driver_sql(
        f"DROP {object_type} IF EXISTS {quoted_name}{cascade_sql}"
    )


def _drop_database_objects(
    connection: Connection,
    inventory: DatabaseObjectInventory,
) -> DatabaseResetResult:
    """删除盘点到的全部对象，并用全新 Inspector 验证无残留。"""

    dialect_name = connection.dialect.name
    is_mysql = dialect_name in {"mysql", "mariadb"}
    use_cascade = dialect_name == "postgresql"

    if is_mysql:
        # MySQL 无法为所有循环外键稳定计算删除顺序；关闭当前会话的外键检查，
        # 并在 finally 中恢复，防止连接意外回到连接池后继续处于关闭状态。
        connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 0")

    try:
        for name in inventory.materialized_views:
            _drop_statement(
                connection,
                "MATERIALIZED VIEW",
                name,
                cascade=use_cascade,
            )
        for name in inventory.views:
            _drop_statement(connection, "VIEW", name, cascade=use_cascade)
        for name in inventory.tables:
            _drop_statement(connection, "TABLE", name, cascade=use_cascade)
        for name in inventory.sequences:
            _drop_statement(connection, "SEQUENCE", name, cascade=use_cascade)
    finally:
        if is_mysql:
            connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 1")

    remaining = _collect_database_objects(connection)
    if remaining.total:
        raise RuntimeError(
            "database reset verification found remaining objects: "
            f"tables={len(remaining.tables)}, views={len(remaining.views)}, "
            f"materialized_views={len(remaining.materialized_views)}, "
            f"sequences={len(remaining.sequences)}"
        )

    return DatabaseResetResult(
        tables_dropped=len(inventory.tables),
        views_dropped=len(inventory.views),
        materialized_views_dropped=len(inventory.materialized_views),
        sequences_dropped=len(inventory.sequences),
    )


class DatabaseResetService:
    """执行不可恢复的数据库全量重置。"""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    async def reset(
        self,
        *,
        before_drop: BeforeDatabaseDrop | None = None,
    ) -> DatabaseResetResult:
        """清空默认 schema，保留连接地址并将 Setup 标记重置为 false。"""

        async with self._lock:
            connection_config = read_connection_config()
            database_url = str(connection_config.get("database_url") or "").strip()
            if not database_url or connection_config.get("setup_completed") is not True:
                raise DatabaseResetError()

            normalized_url = normalize_database_url(database_url)
            if not normalized_url.startswith(_SUPPORTED_DATABASE_PREFIXES):
                raise DatabaseResetError()

            engine = None
            setup_state_reset = False
            try:
                engine = create_async_engine(normalized_url, poolclass=NullPool)
                async with engine.begin() as connection:
                    # 连接和盘点必须先成功；之后再切换 Setup 状态，避免纯连接故障
                    # 把一个仍然完整的部署误置为未完成。
                    inventory = await connection.run_sync(_collect_database_objects)

                    # DDL 在 MySQL 中可能隐式提交。先持久化未完成状态可保证进程在
                    # 任何部分删除或崩溃后都只会进入 Setup，而不会按正常模式启动。
                    write_connection_config(database_url, setup_completed=False)
                    setup_state_reset = True

                    # connection.json 已切换到 Setup 模式，新请求会被中间件拦截。
                    # 在真正删除表之前，等待仍可能访问数据库的后台任务退出。
                    if before_drop is not None:
                        await before_drop()

                    result = await connection.run_sync(
                        _drop_database_objects,
                        inventory,
                    )

                logger.info(
                    "数据库已被超级管理员彻底清空: tables={}, views={}, "
                    "materialized_views={}, sequences={}",
                    result.tables_dropped,
                    result.views_dropped,
                    result.materialized_views_dropped,
                    result.sequences_dropped,
                )
                return result
            except DatabaseResetError:
                raise
            except Exception as exc:
                # 不记录异常文本，避免驱动错误将含密码的 database_url 写入日志。
                logger.error(
                    "数据库彻底重置失败: error_type={}, setup_state_reset={}",
                    type(exc).__name__,
                    setup_state_reset,
                )
                raise DatabaseResetError(setup_state_reset=setup_state_reset) from exc
            finally:
                if engine is not None:
                    try:
                        await engine.dispose()
                    except Exception as exc:
                        # 数据库对象和 Setup 状态已经有自己的结果，连接池清理失败
                        # 不能覆盖该结果或阻止后续 SIGTERM 重启。
                        logger.error(
                            "数据库重置连接池释放失败: error_type={}",
                            type(exc).__name__,
                        )


database_reset_service = DatabaseResetService()
