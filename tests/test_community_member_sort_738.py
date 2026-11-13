"""Member sort is applied before database pagination."""

import pytest
from sqlalchemy import select

from backend.models.star_aid_models import StarAidMember
from backend.services import star_aid_service


@pytest.mark.asyncio
async def test_member_name_sort_is_database_scoped():
    class Result:
        def scalar_one(self):
            return 0

        def scalars(self):
            return self

        def all(self):
            return []

    class Session:
        def __init__(self):
            self.statements = []

        async def execute(self, statement):
            self.statements.append(statement)
            return Result()

    session = Session()
    await star_aid_service.get_admin_member_page(
        session, member_sort="name", member_order="asc"
    )
    actual = str(session.statements[-1]).split("ORDER BY")[1]
    expected = str(
        select(StarAidMember).order_by(
            StarAidMember.github_username.asc(), StarAidMember.id
        )
    ).split("ORDER BY")[1]
    assert actual.split("LIMIT")[0].strip() == expected.strip()
