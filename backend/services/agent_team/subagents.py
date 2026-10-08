"""Durable, read-only child sessions owned by one parent Agent run.

Model text never supplies execution rights. The spawn ledger, immutable scope
snapshot and child session are committed together before scheduling any work.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import asdict, dataclass, replace
from typing import TYPE_CHECKING, Any
from weakref import WeakKeyDictionary, WeakValueDictionary

from sqlalchemy import select

from backend.models import database as db_module
from backend.models.agent_team_models import (
    AgentTeamSession,
    AgentTeamSubagent,
    AgentTeamToolCall,
)
from backend.services.agent_team.skill_scope import SkillRestriction

if TYPE_CHECKING:
    from backend.services.agent_team.conversation_checkpoint import (
        ConversationCheckpointService,
    )
    from backend.services.agent_team.tools.base import ToolContext

ACTIVE_STATES = frozenset({"queued", "running", "cancelling"})
TERMINAL_STATES = frozenset(
    {"completed", "cancelled", "blocked", "unrecoverable_error"}
)


@dataclass(frozen=True)
class SubagentRecord:
    session_id: int
    parent_session_id: int
    instruction: str
    skill_scopes: dict[str, SkillRestriction]
    iteration_number: int
    status: str
    result: dict[str, Any] | None

    def view(self) -> dict[str, Any]:
        return {
            "agent_id": self.session_id,
            "status": self.status,
            "result": self.result,
        }


class SubagentStore:
    """Task-scoped mapping/ledger transactions, with no in-memory recovery state."""

    def __init__(self, task_id: int):
        self.task_id = task_id

    async def _session(self, db, session_id: int, *, lock: bool = False):
        row = await db.get(AgentTeamSession, session_id, with_for_update=lock)
        if row is None or row.task_id != self.task_id:
            raise ValueError("Agent session is outside this task")
        return row

    @staticmethod
    def _record(mapping, row) -> SubagentRecord:
        try:
            data = json.loads(mapping.restriction_payload)
            if (
                not isinstance(data, dict)
                or type(data.get("version")) is not int
                or data["version"] != 1
                or data.get("read_only") is not True
                or not isinstance(data.get("skills"), dict)
            ):
                raise ValueError("Invalid delegation restriction")
            scopes = {
                slug: SkillRestriction.from_data(value)
                for slug, value in data["skills"].items()
            }
            payload = json.loads(row.result_payload) if row.result_payload else None
            if row.status in TERMINAL_STATES:
                if (
                    not isinstance(payload, dict)
                    or payload.get("success") is not (row.status == "completed")
                    or payload.get("outcome")
                    != ("success" if row.status == "completed" else row.status)
                ):
                    raise ValueError("Invalid child terminal result")
            elif row.status not in ACTIVE_STATES:
                raise ValueError("Invalid child session state")
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Invalid durable subagent state") from exc
        return SubagentRecord(
            row.id,
            mapping.parent_session_id,
            mapping.instruction,
            scopes,
            row.iteration_number,
            row.status,
            payload,
        )

    async def for_session(self, session_id: int) -> SubagentRecord | None:
        """Detect a restored child even when its caller supplies no child flag."""
        async with db_module.async_session() as db:
            row = await self._session(db, session_id)
            mapping = await db.get(AgentTeamSubagent, session_id)
            if mapping is None:
                if row.role_name == "subagent":
                    raise ValueError("Subagent runtime restriction is missing")
                return None
            await self._session(db, mapping.parent_session_id)
            if row.role_name != "subagent":
                raise ValueError("Subagent role is inconsistent")
            return self._record(mapping, row)

    async def get(self, session_id: int, parent_session_id: int) -> SubagentRecord:
        async with db_module.async_session() as db:
            await self._session(db, parent_session_id)
            row = await self._session(db, session_id)
            mapping = await db.get(AgentTeamSubagent, session_id)
            if (
                mapping is None
                or mapping.parent_session_id != parent_session_id
                or row.role_name != "subagent"
            ):
                raise ValueError("Subagent does not belong to this parent session")
            return self._record(mapping, row)

    async def list_children(self, parent_session_id: int) -> list[SubagentRecord]:
        async with db_module.async_session() as db:
            await self._session(db, parent_session_id)
            result = await db.execute(
                select(AgentTeamSubagent, AgentTeamSession)
                .join(
                    AgentTeamSession,
                    AgentTeamSubagent.session_id == AgentTeamSession.id,
                )
                .where(AgentTeamSubagent.parent_session_id == parent_session_id)
                .order_by(AgentTeamSubagent.session_id)
            )
            children = []
            for mapping, row in result.all():
                if row.task_id != self.task_id or row.role_name != "subagent":
                    raise ValueError("Subagent task/role is inconsistent")
                children.append(self._record(mapping, row))
            return children

    async def create(
        self,
        parent_session_id: int,
        tool_call_id: str,
        instruction: str,
        skill_scopes: dict[str, SkillRestriction],
    ) -> SubagentRecord:
        if (
            not isinstance(instruction, str)
            or not instruction.strip()
            or not tool_call_id
        ):
            raise ValueError(
                "A checkpointed spawn call and non-empty task are required"
            )
        async with db_module.async_session() as db:
            # Serialize identity allocation with parent checkpoint sequence changes.
            parent = await self._session(db, parent_session_id, lock=True)
            if parent.role_name == "subagent" or await db.get(
                AgentTeamSubagent, parent.id
            ):
                raise ValueError("Read-only children cannot delegate")
            result = await db.execute(
                select(AgentTeamToolCall).where(
                    AgentTeamToolCall.session_id == parent_session_id,
                    AgentTeamToolCall.tool_call_id == tool_call_id,
                )
            )
            call = result.scalar_one_or_none()
            if call is None or call.name != "spawn_agent" or call.status != "running":
                raise ValueError("Spawn has no admitted parent ledger call")
            try:
                args = json.loads(call.arguments_json)
            except (ValueError, TypeError) as exc:
                raise ValueError("Invalid spawn ledger arguments") from exc
            if (
                args != {"task": instruction}
                or call.arguments_hash
                != hashlib.sha256(call.arguments_json.encode()).hexdigest()
            ):
                raise ValueError("Spawn does not match its durable arguments")
            result = await db.execute(
                select(AgentTeamSubagent).where(
                    AgentTeamSubagent.spawn_tool_call_id == call.id
                )
            )
            existing = result.scalar_one_or_none()
            if existing is not None:
                child = await self._session(db, existing.session_id)
                if (
                    existing.parent_session_id != parent.id
                    or existing.instruction != instruction
                ):
                    raise ValueError("Spawn identity is inconsistent")
                return self._record(existing, child)
            child = AgentTeamSession(
                task_id=self.task_id,
                iteration_number=parent.iteration_number,
                role_name="subagent",
                status="queued",
            )
            db.add(child)
            await db.flush()
            mapping = AgentTeamSubagent(
                session_id=child.id,
                parent_session_id=parent.id,
                spawn_tool_call_id=call.id,
                instruction=instruction,
                restriction_payload=json.dumps(
                    {
                        "version": 1,
                        "read_only": True,
                        "skills": {
                            slug: scope.to_data()
                            for slug, scope in skill_scopes.items()
                        },
                    }
                ),
            )
            db.add(mapping)
            await db.commit()
            return self._record(mapping, child)

    async def set_active_status(
        self, session_id: int, parent_session_id: int, status: str
    ) -> SubagentRecord:
        if status not in {"running", "cancelling"}:
            raise ValueError("Invalid active subagent state")
        async with db_module.async_session() as db:
            await self._session(db, parent_session_id)
            row = await self._session(db, session_id, lock=True)
            mapping = await db.get(AgentTeamSubagent, session_id)
            if mapping is None or mapping.parent_session_id != parent_session_id:
                raise ValueError("Subagent does not belong to this parent session")
            if row.status in ACTIVE_STATES and row.status != "cancelling":
                row.status = status
                await db.commit()
            return self._record(mapping, row)


_managers: WeakKeyDictionary = WeakKeyDictionary()


async def drain_cleanup(awaitable) -> None:
    """Finish owned cleanup even if the caller is cancelled repeatedly."""
    operation = asyncio.ensure_future(awaitable)
    cancelled = False
    while not operation.done():
        try:
            await asyncio.shield(operation)
        except asyncio.CancelledError:
            cancelled = True
    await operation
    if cancelled:
        raise asyncio.CancelledError


class SubagentManager:
    """A fixed set of active slots consuming durable, unbounded queued work.

    The parent worker owns the task lifecycle. Like the workspace scheduler,
    this manager guards duplicate local execution on one event loop; it does
    not claim to be a distributed worker lease.
    """

    def __init__(
        self,
        checkpoint: ConversationCheckpointService,
        parent_session_id: int,
        context: ToolContext,
        *,
        concurrency: int,
    ):
        if type(concurrency) is not int or concurrency < 1:
            raise ValueError("Subagent concurrency must be a positive integer")
        self.checkpoint = checkpoint
        self.parent_session_id = parent_session_id
        self.context = context
        self.store = SubagentStore(checkpoint.task_id)
        self.concurrency = concurrency
        self._queue: asyncio.Queue[int] = asyncio.Queue()
        self._scheduled: set[int] = set()
        self._events: dict[int, asyncio.Event] = {}
        self._active: dict[int, asyncio.Task] = {}
        self._failures: dict[int, Exception] = {}
        self._workers: list[asyncio.Task] = []
        self._lock = asyncio.Lock()
        self._started = False
        self._closing = False
        self._owners = _managers.setdefault(
            asyncio.get_running_loop(), WeakValueDictionary()
        )
        self._key = (checkpoint.task_id, parent_session_id)
        if self._owners.get(self._key) is not None:
            raise ValueError("Parent session already has an active subagent manager")
        self._owners[self._key] = self

    def _enqueue(self, child: SubagentRecord) -> None:
        if (
            child.status in ACTIVE_STATES
            and child.session_id not in self._scheduled
            and child.session_id not in self._failures
        ):
            self._scheduled.add(child.session_id)
            self._events.setdefault(child.session_id, asyncio.Event())
            self._queue.put_nowait(child.session_id)

    async def start(self) -> None:
        async with self._lock:
            if self._closing:
                raise ValueError("Subagent manager is closing")
            if self._started:
                return
            for child in await self.store.list_children(self.parent_session_id):
                self._enqueue(child)
            self._workers = [
                asyncio.create_task(self._worker()) for _ in range(self.concurrency)
            ]
            self._started = True

    async def spawn(self, tool_call_id: str, task: str) -> dict[str, Any]:
        await self.start()
        async with self._lock:
            if self._closing:
                raise ValueError("Subagent manager is closing")
            child = await self.store.create(
                self.parent_session_id,
                tool_call_id,
                task,
                dict(self.context.active_skill_tools),
            )
            self._enqueue(child)
            return child.view()

    async def _worker(self) -> None:
        while True:
            child_id = await self._queue.get()
            try:
                async with self._lock:
                    stopping = self._closing or (
                        self.context.cancel_event is not None
                        and self.context.cancel_event.is_set()
                    )
                    child = await self.store.set_active_status(
                        child_id,
                        self.parent_session_id,
                        "cancelling" if stopping else "running",
                    )
                    if child.status in TERMINAL_STATES:
                        continue
                    operation = asyncio.create_task(self._run_child(child))
                    self._active[child_id] = operation
                try:
                    await operation
                except asyncio.CancelledError:
                    if asyncio.current_task().cancelling():
                        raise
                    # cancel() can win before the child coroutine enters its
                    # try/finally. The owning slot still persists its outcome
                    # and stays available for later, unlimited queued work.
                    await self._run_child(replace(child, status="cancelling"))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # A persistence failure must reach the waiting parent, never
                # leave an unobserved worker exception or an endless wait.
                self._failures[child_id] = exc
            finally:
                self._active.pop(child_id, None)
                self._scheduled.discard(child_id)
                self._events.setdefault(child_id, asyncio.Event()).set()
                self._queue.task_done()

    async def _run_child(self, child: SubagentRecord) -> None:
        from backend.services.agent_team.fullstack_expert import (
            FullStackExpertAgent,
            FullStackResult,
        )

        try:
            if child.status == "cancelling":
                result = FullStackResult(False, "子任务已取消", error="cancelled")
            else:
                messages = await self.checkpoint.load_messages(child.session_id)
                agent = FullStackExpertAgent(
                    self.context.workspace,
                    self.context.workspace_service,
                    checkpoint=self.checkpoint,
                    session_id=child.session_id,
                    initial_messages=messages or None,
                    execution_runner=self.context.execution_runner,
                )
                result = await agent.execute(
                    "Read-only investigation",
                    child.instruction,
                    skills_context={
                        "skills_index": dict(
                            self.context.extra.get("admin_skills_index", {})
                        )
                    },
                    iteration=child.iteration_number,
                    cancel_event=asyncio.Event(),
                )
        except asyncio.CancelledError:
            result = FullStackResult(False, "子任务已取消", error="cancelled")
        except Exception:
            # API/transport errors may contain provider credentials. Report a
            # stable classification; do not persist raw exception text.
            result = FullStackResult(
                False, "子任务执行失败", error="subagent_execution_failed"
            )
        payload = asdict(result)
        payload["outcome"] = result.outcome
        await self.checkpoint.finish_session(child.session_id, result.outcome, payload)

    async def wait(self, child_id: int) -> dict[str, Any]:
        child = await self.store.get(child_id, self.parent_session_id)
        if child_id in self._failures:
            raise RuntimeError(
                "Subagent checkpoint persistence failed"
            ) from self._failures[child_id]
        if child.status in ACTIVE_STATES:
            await self.start()
            self._enqueue(child)
            await self._events[child_id].wait()
        if child_id in self._failures:
            raise RuntimeError(
                "Subagent checkpoint persistence failed"
            ) from self._failures[child_id]
        return (await self.store.get(child_id, self.parent_session_id)).view()

    async def cancel(self, child_id: int) -> dict[str, Any]:
        # Ownership is checked before any cancellation side effect.
        child = await self.store.get(child_id, self.parent_session_id)
        await self._cancel_descendants(child_id)
        async with self._lock:
            child = await self.store.set_active_status(
                child_id, self.parent_session_id, "cancelling"
            )
            operation = self._active.get(child_id)
            if operation is not None and child.status in ACTIVE_STATES:
                operation.cancel()
            elif child.status in ACTIVE_STATES:
                await self._run_child(child)
                self._events.setdefault(child_id, asyncio.Event()).set()
        if operation is not None:
            await drain_cleanup(asyncio.gather(operation, return_exceptions=True))
            # The slot worker records persistence failures and signals waiters.
            await self._events[child_id].wait()
        if (
            await self.store.get(child_id, self.parent_session_id)
        ).status in TERMINAL_STATES:
            # A successful explicit cancellation reconciles a prior failed
            # checkpoint. Merely waiting never clears that failure or retries.
            self._failures.pop(child_id, None)
        return await self.wait(child_id)

    async def _cancel_descendants(self, parent_id: int) -> None:
        # Children cannot spawn today. Traverse durable descendants anyway, so
        # a future/legacy tree cannot outlive its parent on recovery/shutdown.
        for child in await self.store.list_children(parent_id):
            await self._cancel_descendants(child.session_id)
            if child.status in ACTIVE_STATES:
                child = await self.store.set_active_status(
                    child.session_id, parent_id, "cancelling"
                )
                await self._run_child(child)

    async def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        errors = []
        try:
            try:
                children = await self.store.list_children(self.parent_session_id)
                for child in children:
                    try:
                        await self.cancel(child.session_id)
                    except Exception as exc:
                        errors.append(exc)
            except Exception as exc:
                errors.append(exc)
        finally:
            # Always drain actual model/tool work even if the database is down.
            active = list(self._active.values())
            for task in active:
                task.cancel()
            await asyncio.gather(*active, return_exceptions=True)
            for worker in self._workers:
                worker.cancel()
            await asyncio.gather(*self._workers, return_exceptions=True)
            self._workers.clear()
            self._owners.pop(self._key, None)
        if errors:
            raise RuntimeError(
                "Subagent cleanup could not persist all child states"
            ) from errors[0]
