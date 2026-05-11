"""Agent 专家团队候选任务筛选服务"""

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable

from openai import BadRequestError
from sqlalchemy import and_, desc, not_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.config import get_dynamic_config
from backend.models.agent_team_models import AgentTeamSourceType, AgentTeamTask, AgentTeamTaskStatus
from backend.models.database import IssueAnalysis, IssueAnalysisStatus
from backend.models.scan_models import RepoScan, ScanFinding
from backend.services.agent_team.ai_client import create_agent_team_client

_PRIORITY_SCORE = {"critical": 100, "high": 80, "medium": 50, "low": 20}
_PRIORITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
_SEVERITY_SCORE = {"critical": 100, "major": 75, "minor": 35, "suggestion": 15}
_VALID_PRIORITIES = {"critical", "high", "medium", "low"}
_AI_FILTER_MAX_ITEMS = 60
_AI_FILTER_TEXT_LIMIT = 900


@dataclass(frozen=True)
class AgentCandidate:
    """Agent 专家团队候选任务。"""

    source_type: str
    source_id: int
    source_issue_number: int | None
    repo_full_name: str
    repo_owner: str
    repo_name: str
    title: str
    summary: str
    priority: str
    candidate_score: int
    filter_reason: str = ""


class AgentTeamCandidateService:
    """从 Issue 分析和仓库扫描发现中筛选候选任务。"""

    async def collect_candidates(
        self,
        db: AsyncSession,
        limit: int = 20,
        ai_filter_requirement: str | None = None,
    ) -> list[AgentCandidate]:
        """收集候选任务，当前仅供 super_admin 手动触发。"""
        allowlist = await self._load_repo_allowlist()
        requirement = (ai_filter_requirement or "").strip()
        if requirement:
            return await self._collect_ai_filtered_issue_candidates(db, allowlist, limit, requirement)

        candidates: list[AgentCandidate] = []
        candidates.extend(await self._collect_issue_candidates(db, allowlist, limit))
        candidates.extend(await self._collect_scan_candidates(db, allowlist, limit))
        candidates.sort(key=lambda item: item.candidate_score, reverse=True)
        return candidates[:limit]

    async def create_task_from_candidate(
        self,
        db: AsyncSession,
        candidate: AgentCandidate,
        started_by: str,
        ai_config_snapshot: dict | None = None,
    ) -> AgentTeamTask:
        """将候选项转为 AgentTeamTask。"""
        task = AgentTeamTask(
            source_type=candidate.source_type,
            source_id=candidate.source_id,
            source_issue_number=candidate.source_issue_number,
            repo_full_name=candidate.repo_full_name,
            repo_owner=candidate.repo_owner,
            repo_name=candidate.repo_name,
            title=candidate.title,
            summary=candidate.summary,
            priority=candidate.priority,
            candidate_score=candidate.candidate_score,
            status=AgentTeamTaskStatus.QUEUED.value,
            started_by=started_by,
            ai_config_snapshot=json.dumps(ai_config_snapshot or {}, ensure_ascii=False),
        )
        db.add(task)
        await db.commit()
        await db.refresh(task)
        return task

    async def _collect_issue_candidates(
        self, db: AsyncSession, allowlist: set[str], limit: int
    ) -> list[AgentCandidate]:
        min_priority = str(await get_dynamic_config("agent_team_min_priority") or "high")
        allowed_priorities = self._allowed_priorities(min_priority)
        keywords = await self._load_feasibility_keywords()

        existing_subquery = select(AgentTeamTask.source_id).where(
            AgentTeamTask.source_type == AgentTeamSourceType.ISSUE_ANALYSIS.value,
            AgentTeamTask.status.notin_(
                [AgentTeamTaskStatus.FAILED.value, AgentTeamTaskStatus.CANCELLED.value, AgentTeamTaskStatus.ABANDONED.value]
            ),
        )
        stmt = (
            select(IssueAnalysis)
            .where(
                and_(
                    IssueAnalysis.status == IssueAnalysisStatus.COMPLETED.value,
                    IssueAnalysis.duplicate_of.is_(None),
                    not_(IssueAnalysis.id.in_(existing_subquery)),
                )
            )
            .order_by(desc(IssueAnalysis.completed_at), desc(IssueAnalysis.created_at))
            .limit(limit * 3)
        )
        result = await db.execute(stmt)
        candidates: list[AgentCandidate] = []
        for analysis in result.scalars().all():
            repo_full_name = f"{analysis.repo_owner}/{analysis.repo_name}"
            if allowlist and repo_full_name not in allowlist:
                continue
            priority = analysis.priority or "medium"
            feasibility = analysis.feasibility or ""
            priority_ok = priority in allowed_priorities
            feasibility_ok = any(keyword in feasibility for keyword in keywords)
            if not priority_ok and not feasibility_ok:
                continue
            score = _PRIORITY_SCORE.get(priority, 30) + (20 if feasibility_ok else 0)
            candidates.append(
                AgentCandidate(
                    source_type=AgentTeamSourceType.ISSUE_ANALYSIS.value,
                    source_id=analysis.id,
                    source_issue_number=int(analysis.issue_number),
                    repo_full_name=repo_full_name,
                    repo_owner=analysis.repo_owner,
                    repo_name=analysis.repo_name,
                    title=analysis.title or f"Issue #{analysis.issue_number}",
                    summary=analysis.summary or analysis.body or "",
                    priority=priority,
                    candidate_score=min(score, 100),
                )
            )
        return candidates

    async def _collect_ai_filtered_issue_candidates(
        self,
        db: AsyncSession,
        allowlist: set[str],
        limit: int,
        requirement: str,
    ) -> list[AgentCandidate]:
        """使用 Agent 专用 AI 从已分析 Issue 中按自然语言要求筛选候选。"""
        existing_subquery = select(AgentTeamTask.source_id).where(
            AgentTeamTask.source_type == AgentTeamSourceType.ISSUE_ANALYSIS.value,
            AgentTeamTask.status.notin_(
                [AgentTeamTaskStatus.FAILED.value, AgentTeamTaskStatus.CANCELLED.value, AgentTeamTaskStatus.ABANDONED.value]
            ),
        )
        stmt = (
            select(IssueAnalysis)
            .where(
                and_(
                    IssueAnalysis.status == IssueAnalysisStatus.COMPLETED.value,
                    IssueAnalysis.duplicate_of.is_(None),
                    not_(IssueAnalysis.id.in_(existing_subquery)),
                )
            )
            .order_by(desc(IssueAnalysis.completed_at), desc(IssueAnalysis.created_at))
            .limit(max(limit * 5, _AI_FILTER_MAX_ITEMS))
        )
        result = await db.execute(stmt)
        analyses = []
        for analysis in result.scalars().all():
            repo_full_name = f"{analysis.repo_owner}/{analysis.repo_name}"
            if allowlist and repo_full_name not in allowlist:
                continue
            analyses.append(analysis)

        if not analyses:
            return []

        ai_results = await self._filter_issue_candidates_with_ai(requirement, analyses[:_AI_FILTER_MAX_ITEMS])
        result_map = {item["source_id"]: item for item in ai_results if item.get("selected")}

        candidates: list[AgentCandidate] = []
        for analysis in analyses:
            ai_item = result_map.get(int(analysis.id))
            if not ai_item:
                continue
            priority = _normalize_priority(ai_item.get("priority"), analysis.priority or "medium")
            score = _normalize_score(ai_item.get("score"), _PRIORITY_SCORE.get(priority, 50))
            repo_full_name = f"{analysis.repo_owner}/{analysis.repo_name}"
            candidates.append(
                AgentCandidate(
                    source_type=AgentTeamSourceType.ISSUE_ANALYSIS.value,
                    source_id=analysis.id,
                    source_issue_number=int(analysis.issue_number),
                    repo_full_name=repo_full_name,
                    repo_owner=analysis.repo_owner,
                    repo_name=analysis.repo_name,
                    title=analysis.title or f"Issue #{analysis.issue_number}",
                    summary=analysis.summary or analysis.body or "",
                    priority=priority,
                    candidate_score=score,
                    filter_reason=str(ai_item.get("reason") or "")[:500],
                )
            )

        candidates.sort(key=lambda item: item.candidate_score, reverse=True)
        return candidates[:limit]

    async def _filter_issue_candidates_with_ai(self, requirement: str, analyses: list[IssueAnalysis]) -> list[dict[str, Any]]:
        """调用 AI 判断 Issue 是否满足自然语言筛选要求。"""
        client, config = await create_agent_team_client()
        model = _select_ai_filter_model(config.model, config.review_model, config.summary_model)
        issue_items = [_issue_analysis_to_filter_item(analysis) for analysis in analyses]
        messages = [
            {
                "role": "system",
                "content": (
                    "你是 Agent 专家团队的任务候选筛选器。请根据超级管理员的自然语言要求，"
                    "从给定 Issue 分析结果中选择适合由代码 Agent 自动处理的任务。"
                    "必须只返回 JSON 数组，不要输出 Markdown。每个元素包含："
                    "source_id(int), selected(bool), score(0-100 int), priority(critical/high/medium/low), reason(string)。"
                    "只返回 selected=true 的条目；如果没有符合要求的 Issue，返回 []。"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "requirement": requirement[:1200],
                        "issues": issue_items,
                    },
                    ensure_ascii=False,
                ),
            },
        ]
        try:
            response = await client.call_with_retry(
                messages=messages,
                model=model,
                temperature=0.1,
                max_tokens=min(config.max_tokens, 4096),
                timeout=config.timeout_seconds,
            )
        except BadRequestError as exc:
            if _is_model_not_found_error(exc):
                raise ValueError(
                    f"AI 筛选使用的模型不存在：{model}。请在 Agent 专家团队配置中检查全栈专家模型/专业审查模型，"
                    "或在使用主 AI 时检查全局模型名称。"
                ) from exc
            raise
        if not response.choices:
            return []
        content = response.choices[0].message.content or ""
        return _parse_ai_filter_response(content)

    async def _collect_scan_candidates(
        self, db: AsyncSession, allowlist: set[str], limit: int
    ) -> list[AgentCandidate]:
        existing_subquery = select(AgentTeamTask.source_id).where(
            AgentTeamTask.source_type == AgentTeamSourceType.SCAN_FINDING.value,
            AgentTeamTask.status.notin_(
                [AgentTeamTaskStatus.FAILED.value, AgentTeamTaskStatus.CANCELLED.value, AgentTeamTaskStatus.ABANDONED.value]
            ),
        )
        stmt = (
            select(ScanFinding, RepoScan)
            .join(RepoScan, RepoScan.id == ScanFinding.scan_id)
            .where(
                and_(
                    ScanFinding.severity.in_(["critical", "major"]),
                    not_(ScanFinding.id.in_(existing_subquery)),
                )
            )
            .order_by(desc(ScanFinding.confidence), desc(ScanFinding.created_at))
            .limit(limit * 3)
        )
        result = await db.execute(stmt)
        candidates: list[AgentCandidate] = []
        for finding, scan in result.all():
            if allowlist and scan.repo_name not in allowlist:
                continue
            repo_owner, repo_name = self._split_repo(scan.repo_name)
            score = _SEVERITY_SCORE.get(finding.severity, 20)
            if finding.confidence:
                score += min(int(finding.confidence), 100) // 5
            candidates.append(
                AgentCandidate(
                    source_type=AgentTeamSourceType.SCAN_FINDING.value,
                    source_id=finding.id,
                    source_issue_number=int(scan.report_issue_number) if scan.report_issue_number else None,
                    repo_full_name=scan.repo_name,
                    repo_owner=repo_owner,
                    repo_name=repo_name,
                    title=finding.title,
                    summary=finding.description or finding.suggestion or "",
                    priority="critical" if finding.severity == "critical" else "high",
                    candidate_score=min(score, 100),
                )
            )
        return candidates

    async def _load_repo_allowlist(self) -> set[str]:
        raw = str(await get_dynamic_config("agent_team_repo_allowlist") or "")
        return {item.strip() for item in raw.split(",") if item.strip()}

    async def _load_feasibility_keywords(self) -> list[str]:
        raw = str(await get_dynamic_config("agent_team_feasibility_keywords") or "")
        return [item.strip() for item in raw.split(",") if item.strip()]

    def _allowed_priorities(self, min_priority: str) -> set[str]:
        threshold = _PRIORITY_ORDER.get(min_priority, _PRIORITY_ORDER["high"])
        return {priority for priority, order in _PRIORITY_ORDER.items() if order <= threshold}

    def _split_repo(self, repo_full_name: str) -> tuple[str, str]:
        if "/" not in repo_full_name:
            return "", repo_full_name
        owner, name = repo_full_name.split("/", 1)
        return owner, name


def candidates_to_dicts(candidates: Iterable[AgentCandidate]) -> list[dict]:
    """将候选任务转为模板/JSON 友好的字典。"""
    return [candidate.__dict__.copy() for candidate in candidates]


def _issue_analysis_to_filter_item(analysis: IssueAnalysis) -> dict[str, Any]:
    """将 IssueAnalysis 压缩为 AI 筛选输入。"""
    return {
        "source_id": int(analysis.id),
        "issue_number": int(analysis.issue_number),
        "repo_full_name": f"{analysis.repo_owner}/{analysis.repo_name}",
        "title": analysis.title or "",
        "priority": analysis.priority or "medium",
        "category": analysis.category or "other",
        "summary": _truncate_text(analysis.summary or ""),
        "feasibility": _truncate_text(analysis.feasibility or ""),
        "body": _truncate_text(analysis.body or ""),
    }


def _parse_ai_filter_response(text: str) -> list[dict[str, Any]]:
    """解析 AI 筛选响应，返回规范化条目列表。"""
    raw = _extract_json_payload(text)
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return []
    if isinstance(data, dict):
        data = data.get("items") or data.get("candidates") or data.get("results") or []
    if not isinstance(data, list):
        return []

    items = []
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            source_id = int(item.get("source_id"))
        except (TypeError, ValueError):
            continue
        selected = item.get("selected", True)
        if isinstance(selected, str):
            selected = selected.strip().lower() not in {"false", "0", "no", "否"}
        normalized = {
            "source_id": source_id,
            "selected": bool(selected),
            "score": _normalize_score(item.get("score"), 50),
            "priority": _normalize_priority(item.get("priority"), "medium"),
            "reason": str(item.get("reason") or item.get("filter_reason") or "")[:500],
        }
        items.append(normalized)
    return items


def _extract_json_payload(text: str) -> str:
    """从纯文本或 Markdown 代码块中提取 JSON。"""
    value = (text or "").strip()
    value = re.sub(r"^```(?:json)?\s*", "", value)
    value = re.sub(r"\s*```$", "", value)
    if value.startswith("[") or value.startswith("{"):
        return value

    array_match = re.search(r"\[[\s\S]*\]", value)
    if array_match:
        return array_match.group(0)
    object_match = re.search(r"\{[\s\S]*\}", value)
    if object_match:
        return object_match.group(0)
    return value


def _normalize_score(value: Any, default: int) -> int:
    """规范化 AI 返回评分。"""
    try:
        score = int(float(value))
    except (TypeError, ValueError):
        score = default
    return max(0, min(score, 100))


def _normalize_priority(value: Any, default: str) -> str:
    """规范化优先级。"""
    priority = str(value or "").strip().lower()
    if priority in _VALID_PRIORITIES:
        return priority
    fallback = str(default or "medium").strip().lower()
    return fallback if fallback in _VALID_PRIORITIES else "medium"


def _select_ai_filter_model(model: str, review_model: str, summary_model: str) -> str:
    """选择 AI 筛选模型。

    筛选候选需要稳定遵循 JSON 输出，优先使用主执行模型，避免摘要模型配置为
    低成本/别名模型但实际供应商不支持时导致“模型不存在”。
    """
    return (model or review_model or summary_model or "").strip()


def _is_model_not_found_error(exc: BadRequestError) -> bool:
    """判断 BadRequestError 是否属于模型不存在/不可用。"""
    text = str(exc)
    return "模型不存在" in text or "model" in text.lower() and "not" in text.lower() and "exist" in text.lower()


def _truncate_text(value: str, limit: int = _AI_FILTER_TEXT_LIMIT) -> str:
    """限制发给 AI 的单字段长度，避免候选池请求过大。"""
    text = (value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "..."
