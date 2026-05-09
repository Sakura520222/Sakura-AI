"""Agent 专家团队候选任务筛选服务"""

import json
from dataclasses import dataclass
from typing import Iterable

from sqlalchemy import and_, desc, not_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.config import get_dynamic_config
from backend.models.agent_team_models import AgentTeamSourceType, AgentTeamTask, AgentTeamTaskStatus
from backend.models.database import IssueAnalysis, IssueAnalysisStatus
from backend.models.scan_models import RepoScan, ScanFinding

_PRIORITY_SCORE = {"critical": 100, "high": 80, "medium": 50, "low": 20}
_PRIORITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
_SEVERITY_SCORE = {"critical": 100, "major": 75, "minor": 35, "suggestion": 15}


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


class AgentTeamCandidateService:
    """从 Issue 分析和仓库扫描发现中筛选候选任务。"""

    async def collect_candidates(self, db: AsyncSession, limit: int = 20) -> list[AgentCandidate]:
        """收集候选任务，当前仅供 super_admin 手动触发。"""
        allowlist = await self._load_repo_allowlist()
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
            AgentTeamTask.source_type == AgentTeamSourceType.ISSUE_ANALYSIS.value
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

    async def _collect_scan_candidates(
        self, db: AsyncSession, allowlist: set[str], limit: int
    ) -> list[AgentCandidate]:
        existing_subquery = select(AgentTeamTask.source_id).where(
            AgentTeamTask.source_type == AgentTeamSourceType.SCAN_FINDING.value
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
