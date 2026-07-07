"""normalize_database_url 的单元测试。

锁定数据库连接串规范化行为：旧版 aiomysql 驱动自动转为 asyncmy，
裸 mysql:// / postgresql:// 自动补齐异步驱动前缀。
"""

import pytest

from backend.models.database import normalize_database_url


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # 向后兼容：aiomysql 自动转 asyncmy（项目已迁移到 asyncmy 驱动）
        (
            "mysql+aiomysql://user:pass@localhost:3306/sakura_ai",
            "mysql+asyncmy://user:pass@localhost:3306/sakura_ai",
        ),
        # 已是 asyncmy，保持不变
        (
            "mysql+asyncmy://user:pass@localhost:3306/sakura_ai",
            "mysql+asyncmy://user:pass@localhost:3306/sakura_ai",
        ),
        # 裸 mysql:// 补齐异步驱动
        (
            "mysql://user:pass@localhost:3306/sakura_ai",
            "mysql+asyncmy://user:pass@localhost:3306/sakura_ai",
        ),
        # 裸 postgresql:// 补齐异步驱动
        (
            "postgresql://user:pass@localhost:5432/sakura_ai",
            "postgresql+asyncpg://user:pass@localhost:5432/sakura_ai",
        ),
        # 已是 postgresql+asyncpg，保持不变
        (
            "postgresql+asyncpg://user:pass@localhost:5432/sakura_ai",
            "postgresql+asyncpg://user:pass@localhost:5432/sakura_ai",
        ),
    ],
)
def test_normalize_database_url(raw, expected):
    assert normalize_database_url(raw) == expected
