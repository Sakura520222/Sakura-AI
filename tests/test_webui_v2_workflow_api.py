"""Keep API workflows equivalent to the filters and detail data in WebUI."""

from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.api.v1 import issues, reviews
from backend.api.v1.deps import require_api_auth
from backend.models.database import IssueAnalysis, PRReview
from backend.webui.deps import get_db


@pytest.mark.asyncio
async def test_review_list_count_and_export_share_repo_date_and_permission_filters():
    engine = create_async_engine("sqlite+aiosqlite://")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(PRReview.__table__.create)
        async with sessions() as db:
            for identifier, owner, author, day in (
                (1, "team", "Alice", 2),
                (2, "team", "alice", 4),
                (3, "other", "alice", 2),
                (4, "team", "bob", 2),
            ):
                db.add(
                    PRReview(
                        id=identifier,
                        pr_id=identifier,
                        pr_number=identifier,
                        repo_owner=owner,
                        repo_name="api",
                        author=author,
                        title=f"Review {identifier}",
                        branch="main",
                        strategy="standard",
                        status="completed",
                        decision="approve",
                        created_at=datetime(2026, 10, day, tzinfo=UTC),
                    )
                )
            await db.commit()

        app = FastAPI()
        app.include_router(reviews.router, prefix="/api/v1")
        app.dependency_overrides[require_api_auth] = lambda: {
            "user_id": 1,
            "sub": "alice",
            "role": "user",
        }

        async def request_db():
            async with sessions() as db:
                yield db

        app.dependency_overrides[get_db] = request_db
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://testserver"
        ) as client:
            params = {
                "repo": "team/api",
                "date_from": "2026-10-01",
                "date_to": "2026-10-03",
                "status": "completed",
                "decision": "approve",
            }
            response = await client.get("/api/v1/reviews", params=params)
            assert response.status_code == 200
            data = response.json()["data"]
            assert data["total"] == 1
            assert [item["id"] for item in data["items"]] == [1]
            exported = await client.get("/api/v1/reviews/export", params=params)
            assert exported.status_code == 200
            assert "Review 1" in exported.text
            assert all(
                f"Review {identifier}" not in exported.text for identifier in (2, 3, 4)
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_issue_detail_preserves_usage_version_and_persisted_lifecycle():
    engine = create_async_engine("sqlite+aiosqlite://")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(IssueAnalysis.__table__.create)
        async with sessions() as db:
            db.add(
                IssueAnalysis(
                    id=1,
                    issue_number=638,
                    repo_name="ai",
                    repo_owner="sakura",
                    author="alice",
                    analysis_version=3,
                    issue_state="closed",
                    prompt_tokens=123,
                    completion_tokens=45,
                    estimated_cost=6,
                )
            )
            await db.commit()
        app = FastAPI()
        app.include_router(issues.router, prefix="/api/v1")
        app.dependency_overrides[require_api_auth] = lambda: {
            "user_id": 1,
            "sub": "alice",
            "role": "user",
        }

        async def request_db():
            async with sessions() as db:
                yield db

        app.dependency_overrides[get_db] = request_db
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://testserver"
        ) as client:
            response = await client.get("/api/v1/issues/1")
            assert response.status_code == 200
            data = response.json()["data"]
            assert data["issue_state"] == "closed"
            assert data["analysis_version"] == 3
            assert data["prompt_tokens"] == 123
            assert data["completion_tokens"] == 45
            assert data["estimated_cost"] == 6
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_issue_repository_filter_distinguishes_same_name_and_keeps_acl():
    engine = create_async_engine("sqlite+aiosqlite://")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(IssueAnalysis.__table__.create)
        async with sessions() as db:
            for identifier, owner, author in (
                (1, "team", "Alice"),
                (2, "other", "alice"),
                (3, "team", "bob"),
            ):
                db.add(
                    IssueAnalysis(
                        id=identifier,
                        issue_number=identifier,
                        repo_owner=owner,
                        repo_name="api",
                        author=author,
                    )
                )
            await db.commit()
        app = FastAPI()
        app.include_router(issues.router, prefix="/api/v1")
        app.dependency_overrides[require_api_auth] = lambda: {
            "user_id": 1,
            "sub": "alice",
            "role": "user",
        }

        async def request_db():
            async with sessions() as db:
                yield db

        app.dependency_overrides[get_db] = request_db
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://testserver"
        ) as client:
            filtered = await client.get(
                "/api/v1/issues", params={"repo_name": "api", "repo_owner": "team"}
            )
            assert filtered.status_code == 200
            assert filtered.json()["data"]["total"] == 1
            assert [row["id"] for row in filtered.json()["data"]["items"]] == [1]
            legacy = await client.get("/api/v1/issues", params={"repo_name": "api"})
            assert legacy.status_code == 200
            assert legacy.json()["data"]["total"] == 2
    finally:
        await engine.dispose()
