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


@pytest.mark.asyncio
async def test_review_api_exposes_repository_number_separately_from_global_id():
    from backend.models.database import ReviewComment

    engine = create_async_engine("sqlite+aiosqlite://")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(PRReview.__table__.create)
            await connection.run_sync(ReviewComment.__table__.create)
        async with sessions() as db:
            db.add(
                PRReview(
                    id=1,
                    pr_id=987654321,
                    pr_number=42,
                    repo_owner="alice",
                    repo_name="app",
                    strategy="standard",
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
            transport=ASGITransport(app=app), base_url="https://test"
        ) as client:
            listing = (await client.get("/api/v1/reviews")).json()["data"]["items"][0]
            detail = (await client.get("/api/v1/reviews/1")).json()["data"]
            for record in (listing, detail):
                assert record["pr_number"] == 42
                assert record["pr_id"] == 987654321
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_dashboard_logs_and_csv_export_expose_pr_number_and_version_check_reports_503():
    from backend.api.v1 import dashboard, logs, version
    from backend.api.v1.deps import require_api_super_admin
    from backend.models.database import ReviewComment
    from backend.webui.deps import require_csrf_header

    engine = create_async_engine("sqlite+aiosqlite://")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(PRReview.__table__.create)
            await connection.run_sync(ReviewComment.__table__.create)
        async with sessions() as db:
            db.add(
                PRReview(
                    id=1,
                    pr_id=987654321,
                    pr_number=42,
                    repo_owner="alice",
                    repo_name="app",
                    author="alice",
                    title="Fix settings UX",
                    strategy="standard",
                    status="completed",
                )
            )
            await db.commit()

        class FailingChecker:
            async def check_once(self):
                return {"check_error": "github_timeout", "last_checked": "2026-11-18T00:00:00Z"}

        app = FastAPI()
        app.state.update_checker = FailingChecker()
        app.include_router(dashboard.router, prefix="/api/v1")
        app.include_router(logs.router, prefix="/api/v1")
        app.include_router(reviews.router, prefix="/api/v1")
        app.include_router(version.router, prefix="/api/v1")
        app.dependency_overrides[require_api_auth] = lambda: {
            "user_id": 1,
            "sub": "alice",
            "role": "super_admin",
        }
        app.dependency_overrides[require_api_super_admin] = lambda: {
            "user_id": 1,
            "sub": "alice",
            "role": "super_admin",
        }
        app.dependency_overrides[require_csrf_header] = lambda: "csrf-ok"

        async def request_db():
            async with sessions() as db:
                yield db

        app.dependency_overrides[get_db] = request_db
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://test"
        ) as client:
            recent = (await client.get("/api/v1/dashboard/recent-reviews")).json()["data"][0]
            assert recent["pr_number"] == 42
            assert recent["pr_id"] == 987654321

            log_list = (await client.get("/api/v1/logs/reviews")).json()["data"]["items"][0]
            log_detail = (await client.get("/api/v1/logs/reviews/1")).json()["data"]
            assert log_list["pr_number"] == 42
            assert log_detail["pr_number"] == 42

            csv_resp = await client.get("/api/v1/reviews/export")
            assert csv_resp.status_code == 200
            lines = [line.strip() for line in csv_resp.text.lstrip("\ufeff").splitlines() if line.strip()]
            headers = lines[0].split(",")
            row = lines[1].split(",")
            assert "PR 编号" in headers
            assert row[headers.index("PR 编号")] == "42"
            assert row[headers.index("PR ID")] == "987654321"

            check_resp = await client.post("/api/v1/version/check")
            assert check_resp.status_code == 503
            assert check_resp.json()["error"] == "update_check_failed"
    finally:
        await engine.dispose()
