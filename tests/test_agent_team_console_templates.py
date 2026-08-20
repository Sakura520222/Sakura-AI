"""Structural contracts for the compact Implementation Agent console."""

from pathlib import Path

import yaml

from backend.webui.deps import get_templates

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ROOT = ROOT / "backend" / "webui" / "templates"
PAGE = TEMPLATE_ROOT / "agent_team.html"
LIST = TEMPLATE_ROOT / "components" / "agent_team_task_list_fragment.html"
DETAIL = TEMPLATE_ROOT / "components" / "agent_team_task_detail_fragment.html"
LIVE = TEMPLATE_ROOT / "components" / "agent_team_live_view_fragment.html"
WORKSPACES = TEMPLATE_ROOT / "components" / "agent_team_workspace_list_fragment.html"
WORKTREES = TEMPLATE_ROOT / "components" / "agent_team_worktree_list_fragment.html"
TRANSLATIONS = ROOT / "backend" / "webui" / "translations"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_agent_team_templates_compile_and_page_uses_one_console_surface():
    templates = get_templates()
    for name in (
        "agent_team.html",
        "components/agent_team_task_list_fragment.html",
        "components/agent_team_task_detail_fragment.html",
        "components/agent_team_live_view_fragment.html",
    ):
        templates.env.get_template(name)

    page = _read(PAGE)
    assert "agent-console-shell" in page
    assert "lg:grid-cols-[minmax(18rem,23rem)_minmax(0,1fr)]" in page
    assert "agent-task-detail-panel" in page
    assert "agent-task-live" in page
    assert "activeTab" not in page
    assert "bg-gradient" not in page
    assert "hero" not in page.lower()
    assert "<select x-model=\"selectedTaskId\"" not in page
    assert "components/agent_team_live_view_fragment.html" in page
    assert "loadTaskDetail(taskId); watchLive(taskId);" in page
    assert "mobileDetail = true" in page
    assert "clearSelectedTask()" in page
    assert "@click=\"refreshLiveView()\"" in _read(LIVE)
    assert page.count("agent-secondary-drawer-shell") == 1
    assert "top-16 z-40 h-[calc(100dvh-4rem)]" in page
    assert "secondaryPanel === 'candidates'" in page
    assert "secondaryPanel === 'create'" in page
    assert "secondaryPanel === 'workspaces'" in page
    assert "absolute right-0 top-0 flex h-full w-full max-w-2xl" not in page
    assert "submissionContext?.full_submission_preview" in page
    assert "systemMatch" not in page
    assert "const sections" not in page
    assert "draft_json" in page
    assert "preview.dirty" in page
    assert "max_iterations" not in page


def test_agent_console_relies_on_alpine_automatic_init_only():
    """Avoid duplicate document listeners that immediately undo a toggle."""
    page = _read(PAGE)

    assert 'x-data="agentTeamPage()"' in page
    assert "init() {" in page
    assert 'x-init="init()"' not in page
    assert "document.removeEventListener('click', this._workspaceClickHandler)" in page


def test_agent_console_has_no_redundant_page_identity_header():
    page = _read(PAGE)

    assert '<header class=' not in page
    assert "{{ _('agent_team.description') }}" not in page
    assert "agent-console-toolbar" in page


def test_task_list_is_compact_selectable_and_keyboard_actionable():
    template = _read(LIST)
    assert 'data-task-action="select"' in template
    assert 'data-task-action="cancel"' in template
    assert 'data-task-action="retry"' in template
    assert 'data-page="{{ page }}"' in template
    assert "agent-mono" in template
    assert 'role="list"' in template
    assert "task.iteration_count or 0 }}/{{" not in template
    assert "max_iterations" not in template
    assert "onclick=" not in template


def test_detail_keeps_long_context_collapsed_and_uses_single_agent_language():
    template = _read(DETAIL)
    assert "<details" in template
    assert "agent_team.agent_context" in template
    assert "agent_team.fullstack_plan" not in template
    assert "agent_team.fullstack_result" not in template
    assert "agent_team.professional_review" not in template
    assert "Fullstack Expert" not in template
    assert "Professional Reviewer" not in template
    assert "context.source_role" not in template
    assert "context.target_role" not in template
    assert "task.iteration_count or 0 }}/{{" not in template
    assert "max_iterations" not in template


def test_live_view_has_execution_rail_safe_markdown_and_stream_recovery_controls():
    template = _read(LIVE)
    assert "agent-rail" in template
    assert 'role="log"' in template
    assert 'aria-live="polite"' in template
    assert "<details" in template
    assert "DOMPurify.sanitize(marked.parse(content))" in template
    assert "_lastMsgId" in template
    assert "_userScrolledUp" in template
    assert "scrollToLatest()" in template
    assert "data.messages.length === 0" in template
    assert "new URLSearchParams({content: this.promptText})" in template
    assert "_sseHandlers" in template
    assert "visibilitychange" in template
    assert "destroy()" in template
    assert "## 管理员指导" not in template
    assert "Fullstack Expert" not in template
    assert "Professional Reviewer" not in template
    assert '<select x-model="selectedTaskId"' not in template


def test_console_fragments_do_not_embed_inline_handlers_or_remote_fonts():
    templates = "\n".join(_read(path) for path in (PAGE, LIST, DETAIL, LIVE, WORKSPACES, WORKTREES))
    assert "onclick=" not in templates
    assert "onerror=" not in templates
    assert "fonts.googleapis.com" not in templates
    assert "font-family: ui-monospace" in templates


def test_workspace_fragments_use_direct_accessible_actions_and_stable_controls():
    workspaces = _read(WORKSPACES)
    worktrees = _read(WORKTREES)
    assert 'data-agent-workspace-action="toggle"' in workspaces
    assert 'data-agent-workspace-action="delete-workspace"' in workspaces
    assert 'aria-expanded="false"' in workspaces
    assert 'aria-controls="agent-worktrees-{{ loop.index }}"' in workspaces
    assert 'id="agent-worktrees-{{ loop.index }}"' in workspaces
    assert 'data-agent-workspace-action="clean-orphans"' in worktrees
    assert 'data-agent-workspace-action="delete-worktree"' in worktrees
    assert "onclick=" not in workspaces
    assert "onclick=" not in worktrees
    page = _read(PAGE)
    assert "detail.dataset.loaded === 'true'" in page
    assert "button.setAttribute('aria-expanded'" in page


def test_agent_team_translations_are_in_parity_without_retired_iteration_labels():
    catalogs = {}
    for locale in ("en", "zh-CN"):
        with (TRANSLATIONS / f"{locale}.yaml").open(encoding="utf-8") as stream:
            catalogs[locale] = yaml.safe_load(stream)["agent_team"]

    assert set(catalogs["en"]) == set(catalogs["zh-CN"])
    for retired_key in (
        "max_iterations",
        "tab_live",
        "live_select_task",
        "live_fullstack_thinking",
        "live_reviewer_thinking",
        "live_iteration_fullstack",
        "live_iteration_reviewer",
    ):
        assert retired_key not in catalogs["en"]
        assert retired_key not in catalogs["zh-CN"]
