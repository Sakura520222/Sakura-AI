"""Versioned Agent JSON API sharing the established admission transactions."""

import json
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.api.v1.deps import require_api_auth, require_api_super_admin
from backend.api.v1.private_routes import PrivateAPIRoute
from backend.api.v1.responses import success_response
from backend.models.agent_team_models import AgentTeamIteration, AgentTeamTask
from backend.webui.deps import get_db, paginate, require_csrf_header
from backend.webui.routes import agent_team as agent

router = APIRouter(prefix="/agent", tags=["Agent"], route_class=PrivateAPIRoute)


class AgentTask(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    summary: str | None = None
    repo_full_name: str
    repo_owner: str
    repo_name: str
    source_type: str
    source_id: int | None = None
    source_issue_number: int | None = None
    status: str
    priority: str | None = None
    candidate_score: int
    current_phase: str | None = None
    failed_phase: str | None = None
    failed_role: str | None = None
    branch_name: str | None = None
    base_branch: str | None = None
    pr_number: int | None = None
    pr_url: str | None = None
    iteration_count: int
    max_iterations: int
    resume_count: int
    execution_round: int
    started_by: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    estimated_cost: int | None = None
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    last_checkpoint_at: datetime | None = None
    rate_limit_reset_at: datetime | None = None


class AgentPatchFile(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    file_path: str
    change_type: str | None = None
    additions: int | None = None
    deletions: int | None = None
    diff_summary: str | None = None
    risk_level: str | None = None


class AgentIteration(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    iteration_number: int
    fullstack_plan: str | None = None
    fullstack_result: str | None = None
    professional_review: str | None = None
    review_passed: int
    test_command: str | None = None
    test_output: str | None = None
    test_passed: int
    diff_summary: str | None = None
    decision: str | None = None
    created_at: datetime
    completed_at: datetime | None = None
    patch_files: list[AgentPatchFile]


class AgentFeedback(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source: str
    author: str | None = None
    content: str
    resolved: int
    created_at: datetime


class AgentTaskDetail(AgentTask):
    iterations: list[AgentIteration]
    feedback: list[AgentFeedback]


class AgentTaskPage(BaseModel):
    items: list[AgentTask]
    total: int
    page: int
    total_pages: int
    per_page: int


class AgentTaskListResponse(BaseModel):
    success: Literal[True] = True
    data: AgentTaskPage


class AgentTaskResponse(BaseModel):
    success: Literal[True] = True
    data: AgentTaskDetail


class TaskEdits(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    title: str = ""
    summary: str = ""
    priority: str = ""
    candidate_score: str = ""
    status: str = ""
    branch_name: str = ""
    base_branch: str = ""


class CreateIssueTask(TaskEdits):
    issue_ref: str = Field(min_length=1)


class PreviewIssueTask(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    issue_ref: str = Field(min_length=1)
    draft: TaskEdits | None = None


class CreateCandidateTask(TaskEdits):
    source_type: str
    source_id: int
    ai_filter_requirement: str = ""


class AgentPrompt(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    content: str = Field(min_length=1)


class CandidateFilter(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    ai_filter_requirement: str = Field("", max_length=1200)


@router.post("/candidates")
async def candidates(
    body: CandidateFilter,
    user: dict = Depends(require_api_super_admin),
    db: AsyncSession = Depends(get_db),
    csrf: str = Depends(require_csrf_header),
):
    return await agent.preview_candidates(
        db=db,
        user=user,
        csrf_token=csrf,
        ai_filter_requirement=body.ai_filter_requirement,
    )


@router.get("/tasks", response_model=AgentTaskListResponse)
async def list_tasks(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    status: str = "",
    source_type: str = "",
    q: str = "",
    sort: Literal["newest", "updated", "score"] = "newest",
    user: dict = Depends(require_api_auth),
    db: AsyncSession = Depends(get_db),
):
    filters = []
    scope = agent._build_task_owner_filter(user)
    if scope is not None:
        filters.append(scope)
    if status and status != "all":
        filters.append(
            AgentTeamTask.status.in_(agent.AGENT_TEAM_ACTIVE_STATUSES)
            if status == "active"
            else AgentTeamTask.status == status
        )
    if source_type and source_type != "all":
        filters.append(AgentTeamTask.source_type == source_type)
    if q.strip():
        keyword = (
            "%"
            + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            + "%"
        )
        filters.append(
            or_(
                *(
                    column.ilike(keyword, escape="\\")
                    for column in (
                        AgentTeamTask.title,
                        AgentTeamTask.repo_full_name,
                        AgentTeamTask.summary,
                        AgentTeamTask.branch_name,
                    )
                )
            )
        )
    column = {
        "newest": AgentTeamTask.created_at,
        "updated": AgentTeamTask.updated_at,
        "score": AgentTeamTask.candidate_score,
    }[sort]
    query = (
        select(AgentTeamTask)
        .where(*filters)
        .order_by(desc(column), desc(AgentTeamTask.id))
    )
    count = select(func.count(AgentTeamTask.id)).where(*filters)
    rows, total, total_pages, page = await paginate(db, query, count, page, per_page)
    return AgentTaskListResponse(
        data=AgentTaskPage(
            items=[AgentTask.model_validate(row) for row in rows],
            total=total,
            total_pages=total_pages,
            page=page,
            per_page=per_page,
        )
    )


@router.get("/stats")
async def stats(
    user: dict = Depends(require_api_auth), db: AsyncSession = Depends(get_db)
):
    return success_response(data=await agent._load_stats(db, user=user))


@router.get("/tasks/{task_id}", response_model=AgentTaskResponse)
async def get_task(
    task_id: int,
    user: dict = Depends(require_api_auth),
    db: AsyncSession = Depends(get_db),
):
    query = select(AgentTeamTask).where(AgentTeamTask.id == task_id)
    scope = agent._build_task_owner_filter(user)
    if scope is not None:
        query = query.where(scope)
    task = (
        await db.execute(
            query.options(
                selectinload(AgentTeamTask.iterations).selectinload(
                    AgentTeamIteration.patch_files
                ),
                selectinload(AgentTeamTask.feedback),
            )
        )
    ).scalar_one_or_none()
    if task is None:
        raise HTTPException(404, "Task not found")
    data = AgentTaskDetail.model_validate(task)
    data.iterations.sort(key=lambda item: item.iteration_number)
    data.feedback.sort(key=lambda item: item.created_at, reverse=True)
    for iteration in data.iterations:
        iteration.patch_files.sort(key=lambda item: item.file_path)
    return AgentTaskResponse(data=data)


@router.post("/tasks/preview-from-issue")
async def preview_issue_task(
    body: PreviewIssueTask,
    user: dict = Depends(require_api_auth),
    db: AsyncSession = Depends(get_db),
    csrf: str = Depends(require_csrf_header),
):
    return await agent.preview_task_from_issue(
        db=db,
        user=user,
        csrf_token=csrf,
        issue_ref=body.issue_ref,
        draft_json=json.dumps(body.draft.model_dump(exclude_unset=True))
        if body.draft
        else "",
    )


@router.post("/tasks")
async def create_issue_task(
    body: CreateIssueTask,
    background_tasks: BackgroundTasks,
    user: dict = Depends(require_api_auth),
    db: AsyncSession = Depends(get_db),
    csrf: str = Depends(require_csrf_header),
):
    # Keep quota, billing admission, execution rounds, event outbox and dispatch
    # under the established transaction. Source identity comes from issue_ref.
    return await agent.create_task_from_issue(
        background_tasks=background_tasks,
        db=db,
        user=user,
        csrf_token=csrf,
        **body.model_dump(),
        source_type="",
        source_id="",
        source_issue_number="",
        repo_full_name="",
        repo_owner="",
        repo_name="",
    )


@router.post("/tasks/from-candidate")
async def create_candidate_task(
    body: CreateCandidateTask,
    background_tasks: BackgroundTasks,
    user: dict = Depends(require_api_super_admin),
    db: AsyncSession = Depends(get_db),
    csrf: str = Depends(require_csrf_header),
):
    return await agent.create_task_from_candidate(
        background_tasks=background_tasks,
        db=db,
        user=user,
        csrf_token=csrf,
        **body.model_dump(),
        edited_source_type="",
        edited_source_id="",
        source_issue_number="",
        repo_full_name="",
        repo_owner="",
        repo_name="",
    )


@router.post("/tasks/{task_id}/retry")
async def retry_task(
    task_id: int,
    background_tasks: BackgroundTasks,
    user: dict = Depends(require_api_auth),
    db: AsyncSession = Depends(get_db),
    csrf: str = Depends(require_csrf_header),
):
    return await agent.retry_task(
        task_id, background_tasks, db=db, user=user, csrf_token=csrf
    )


@router.post("/tasks/{task_id}/resume")
async def resume_task(
    task_id: int,
    background_tasks: BackgroundTasks,
    user: dict = Depends(require_api_auth),
    db: AsyncSession = Depends(get_db),
    csrf: str = Depends(require_csrf_header),
):
    return await agent.resume_task(
        task_id, background_tasks, db=db, user=user, csrf_token=csrf
    )


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(
    task_id: int,
    user: dict = Depends(require_api_auth),
    db: AsyncSession = Depends(get_db),
    csrf: str = Depends(require_csrf_header),
):
    return await agent.cancel_task(task_id, db=db, user=user, csrf_token=csrf)


@router.delete("/tasks/{task_id}")
async def delete_task(
    task_id: int,
    user: dict = Depends(require_api_auth),
    db: AsyncSession = Depends(get_db),
    csrf: str = Depends(require_csrf_header),
):
    return await agent.delete_task(task_id, db=db, user=user, csrf_token=csrf)


@router.get("/tasks/{task_id}/stream-data")
async def stream_data(
    task_id: int,
    after_id: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    user: dict = Depends(require_api_auth),
    db: AsyncSession = Depends(get_db),
):
    return await agent.task_stream_data(
        task_id, after_id=after_id, limit=limit, user=user, db=db
    )


@router.get("/tasks/{task_id}/prompts")
async def prompts(
    task_id: int,
    user: dict = Depends(require_api_auth),
    db: AsyncSession = Depends(get_db),
):
    return await agent.list_user_prompts(task_id, user=user, db=db)


@router.post("/tasks/{task_id}/prompts", dependencies=[Depends(require_csrf_header)])
async def submit_prompt(
    task_id: int,
    body: AgentPrompt,
    user: dict = Depends(require_api_auth),
    db: AsyncSession = Depends(get_db),
):
    return await agent.submit_user_prompt(
        task_id, content=body.content, user=user, db=db
    )


@router.get("/repos/{owner}/{name}/branches")
async def branches(owner: str, name: str, user: dict = Depends(require_api_auth)):
    return await agent.list_repo_branches(owner, name, user=user)
