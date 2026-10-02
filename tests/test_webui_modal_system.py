"""统一 Modal / Dialog / Confirm 弹窗体系回归测试（Issue #625）。

覆盖四层契约：
  1. 基础设施：base.html 中 Sakura 全局 API、scroll lock、统一动画与
     prefers-reduced-motion 支持。
  2. ConfirmDialog：components/confirm_dialog.html 的 aria / dismiss 守卫 /
     variant 语义 / focus 管理。
  3. Modal 宏：components/modal.html modal_shell 的尺寸 / persistent /
     声明式挂载点，以及业务页面迁移后不再手写 fixed inset-0 结构。
  4. 原生 confirm 灭绝：生产模板中不允许出现 confirm() / window.confirm() /
     onclick|onsubmit 内联 confirm。
"""

from __future__ import annotations

import json
import re
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

from backend.webui.deps import get_templates

TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "backend" / "webui" / "templates"
BASE_HTML = (TEMPLATES_DIR / "base.html").read_text(encoding="utf-8")
CONFIRM_DIALOG = (TEMPLATES_DIR / "components" / "confirm_dialog.html").read_text(
    encoding="utf-8"
)
MODAL_COMPONENT = (TEMPLATES_DIR / "components" / "modal.html").read_text(
    encoding="utf-8"
)

# 生产模板 = templates 目录下所有 HTML（本仓库 WebUI 模板无构建产物目录）。
PRODUCTION_TEMPLATES = sorted(
    str(path.relative_to(TEMPLATES_DIR))
    for path in TEMPLATES_DIR.rglob("*.html")
)


def _run_modal_javascript(
    assertions: str, *, close_handler: str = "", extra_scripts: tuple[str, ...] = ()
) -> None:
    """Execute the actual template scripts with a minimal DOM event harness."""
    scripts = re.findall(r"<script>(.*?)</script>", BASE_HTML, re.DOTALL)
    payload = {
        "scripts": [
            next(script for script in scripts if "var dialogStack" in script),
            next(script for script in scripts if "Alpine.data('sakuraModal'" in script),
            *extra_scripts,
        ],
        "closeHandler": close_handler,
    }
    harness = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const payload = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const listeners = new Map();
const components = {};
global.window = globalThis;
global.document = {
    activeElement: null,
    body: {classList: {add() {}, remove() {}}},
    addEventListener(name, callback) {
        if (!listeners.has(name)) listeners.set(name, new Set());
        listeners.get(name).add(callback);
    },
    removeEventListener(name, callback) { listeners.get(name)?.delete(callback); },
};
global.HTMLElement = class {
    children = [];
    isConnected = true;
    display = 'block';
    visibility = 'visible';
    focus() { document.activeElement = this; }
    contains(node) { return node === this || this.children.includes(node); }
    querySelectorAll() { return this.children.filter(node => !node.disabled); }
    getClientRects() { return this.display === 'none' ? [] : [{}]; }
};
global.getComputedStyle = node => node;
global.requestAnimationFrame = callback => callback();
global.Alpine = {data(name, factory) { components[name] = factory; }};
for (const script of payload.scripts) vm.runInThisContext(script);
for (const callback of listeners.get('alpine:init')) callback();
function pressTab(shiftKey) {
    const event = {key: 'Tab', shiftKey, defaultPrevented: false,
        preventDefault() { this.defaultPrevented = true; }};
    for (const callback of listeners.get('keydown')) callback(event);
    return event;
}
"""
    result = subprocess.run(
        ["node", "-e", harness + assertions],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(("loading", "persistent"), [(True, False), (False, True), (True, True)])
def test_modal_header_close_respects_dismiss_guard(loading, persistent) -> None:
    template = get_templates().env.from_string(
        '{% from "components/modal.html" import modal_shell %}'
        "{% call modal_shell(id='guarded-modal', title='Guarded') %}body{% endcall %}"
    )
    buttons = []

    class ButtonParser(HTMLParser):
        def handle_starttag(self, tag, attrs):
            if tag == "button":
                buttons.append(dict(attrs))

    ButtonParser().feed(template.render())
    _run_modal_javascript(
        f"""
const modal = components.sakuraModal({{id: 'guarded-modal'}});
modal.$root = new HTMLElement();
modal.init();
modal.show();
modal.setLoading({json.dumps(loading)});
modal.persistent = {json.dumps(persistent)};
const clickClose = new Function('with (this) {{ ' + payload.closeHandler + '; }}');
clickClose.call(modal);
assert.equal(modal.open, true, 'header close bypassed the dismiss guard');
modal.setLoading(false);
modal.persistent = false;
clickClose.call(modal);
assert.equal(modal.open, false, 'idle modal must remain dismissible');
modal.show();
modal.persistent = true;
Sakura.closeModal(modal.id);
assert.equal(modal.open, false, 'programmatic close must remain available');
""",
        close_handler=buttons[0]["@click"],
    )


@pytest.mark.parametrize("controls", ["empty", "hidden", "disabled"])
def test_empty_dialog_traps_tab_in_both_directions(controls) -> None:
    _run_modal_javascript(
        f"""
const trigger = new HTMLElement();
trigger.focus();
const underneath = new HTMLElement();
underneath.children = [new HTMLElement()];
Sakura.openDialog(underneath);
const dialog = new HTMLElement();
const control = new HTMLElement();
control.display = {json.dumps('none' if controls == 'hidden' else 'block')};
control.disabled = {json.dumps(controls == 'disabled')};
dialog.children = {"[]" if controls == "empty" else "[control]"};
Sakura.openDialog(dialog, {{focusTarget: () => dialog}});
for (const start of [dialog, trigger]) {{
    for (const backwards of [false, true]) {{
        start.focus();
        assert.equal(pressTab(backwards).defaultPrevented, true, 'Tab must be intercepted');
        assert.equal(document.activeElement, dialog, 'focus must stay in the top dialog');
    }}
}}
control.display = 'block';
control.disabled = false;
const lastControl = new HTMLElement();
dialog.children = [control, lastControl];
for (const backwards of [false, true]) {{
    dialog.focus();
    assert.equal(pressTab(backwards).defaultPrevented, true);
    assert.equal(document.activeElement, backwards ? lastControl : control,
        'focus must enter newly enabled controls from the dialog root');
}}
lastControl.focus();
assert.equal(pressTab(false).defaultPrevented, true);
assert.equal(document.activeElement, control, 'terminal button must be reachable');
assert.equal(pressTab(true).defaultPrevented, true);
assert.equal(document.activeElement, lastControl);
Sakura.closeDialog(dialog);
assert.equal(document.activeElement, underneath.children[0]);
Sakura.closeDialog(underneath);
assert.equal(document.activeElement, trigger);
"""
    )


@pytest.mark.parametrize("failure", ["server", "network", "invalid-json"])
def test_failed_database_reset_releases_modal_loading_guard(failure) -> None:
    source = (TEMPLATES_DIR / "system_config.html").read_text(encoding="utf-8")
    script = next(
        script
        for script in re.findall(r"<script>(.*?)</script>", source, re.DOTALL)
        if "function systemConfig()" in script
    )
    rendered = get_templates().env.from_string(script).render(
        _=lambda key: key,
        csrf_token="test",
        database_reset_confirmation="RESET TEST DATABASE",
        lang="en",
    )
    _run_modal_javascript(
        f"""
(async () => {{
    const modal = components.sakuraModal({{id: 'database-reset-modal'}});
    modal.$root = new HTMLElement();
    modal.init();
    modal.show();
    document.querySelector = () => modal.$root;
    Alpine.$data = () => modal;
    const settings = systemConfig();
    settings.databaseResetConfirmation = settings.databaseResetConfirmationPhrase;
    const failure = {json.dumps(failure)};
    global.fetch = async url => {{
        if (url === '/health') return {{ok: true, json: async () => ({{}})}};
        assert.equal(url, '/system-config/reset-database');
        assert.equal(modal.loading, true);
        if (failure === 'network') throw new Error('Network unavailable');
        return {{json: async () => {{
            if (failure === 'invalid-json') throw new SyntaxError('Invalid JSON');
            return {{message: 'Simulated reset failure'}};
        }}}};
    }};
    await settings.resetDatabase();
    assert.equal(settings.resetting, false);
    assert.ok(settings.databaseResetError, 'failure feedback must be retained');
    assert.equal(modal.open, true, 'failure feedback must stay visible');
    assert.equal(modal.loading, false, 'failed reset must release loading guard');
    modal.dismiss();
    assert.equal(modal.open, false, 'header/ESC/backdrop may close after failure');
    modal.show();
    modal.dismiss();
    assert.equal(modal.open, false, 'reopened modal must remain dismissible');
}})();
""",
        extra_scripts=(rendered,),
    )


# ============ 1. 基础设施：base.html ============


def test_base_defines_sakura_global_api() -> None:
    """base.html 必须暴露 Sakura.confirm / openModal / closeModal 公共 API。"""
    assert "window.Sakura" in BASE_HTML
    for api in (
        "confirm: function",
        "openModal: function",
        "closeModal: function",
        "isModalOpen: function",
        "lockScroll: lockScroll",
        "unlockScroll: unlockScroll",
    ):
        assert api in BASE_HTML, f"missing Sakura API entry: {api}"


def test_base_confirm_dialog_uses_fail_closed_guard() -> None:
    """Alpine 未就绪时 Sakura.confirm 必须解析为 false（操作被阻止）。"""
    assert "return Promise.resolve(false);" in BASE_HTML


def test_base_scroll_lock_is_reference_counted() -> None:
    """scroll lock 采用引用计数，支持弹窗叠加且不误删他人锁。"""
    assert "scrollLockCount += 1" in BASE_HTML
    assert "scrollLockCount -= 1" in BASE_HTML
    assert "if (scrollLockCount === 1)" in BASE_HTML
    assert "if (scrollLockCount === 0)" in BASE_HTML
    assert "body.sakura-scroll-locked { overflow: hidden; }" in BASE_HTML


def test_base_registers_unified_modal_component() -> None:
    """sakuraModal 组件提供统一的 show/hide/dismiss/setLoading 契约。"""
    assert "Alpine.data('sakuraModal'" in BASE_HTML
    assert "Alpine.data('sakuraConfirmDialog'" in BASE_HTML
    for method in ("dismiss()", "setLoading(value)", "registerModal", "unregisterModal"):
        assert method in BASE_HTML


def test_base_dismiss_guard_blocks_loading_and_persistent() -> None:
    """loading / persistent 状态下 ESC 与 backdrop 不得关闭弹窗（行为端在
    base.html 的 Alpine 组件中，组件模板只负责 dismiss 事件绑定）。"""
    assert "if (this.loading || this.persistent) return;" in BASE_HTML
    assert "canDismiss: () => !this.loading && !this.persistent" in BASE_HTML
    assert '@click="dismiss()"' in CONFIRM_DIALOG


def test_confirm_dialog_allows_explicit_cancel_while_persistent() -> None:
    """persistent 仅阻止 ESC / 遮罩误关，不得禁用显式取消按钮。"""
    assert re.search(
        r"cancel\(\)\s*\{\s*(?://[^\n]*\s*)?if \(this\.loading\) return;\s*this\.close\(false\);",
        BASE_HTML,
    )
    assert "this.loading || this.persistent" not in BASE_HTML.split(
        "cancel() {", 1
    )[1].split("dismiss()", 1)[0]


def test_repeated_confirm_requests_do_not_leave_old_promise_pending() -> None:
    """重复 ask 时先 fail-closed 旧 Promise，再挂载新请求。"""
    assert "if (this.active && this._resolver)" in BASE_HTML
    assert "this._resolver(false);" in BASE_HTML


def test_data_confirm_replays_submit_lifecycle_without_cancel_loading() -> None:
    """确认通过后重新触发 submit；确认取消时不能把原提交按钮锁死。"""
    assert "form.requestSubmit()" in BASE_HTML
    assert "if (e.defaultPrevented) return;" in BASE_HTML


def test_base_unified_modal_visual_and_reduced_motion() -> None:
    """统一视觉层：rounded-2xl / z-[110] / 淡入 + prefers-reduced-motion。"""
    assert ".sakura-modal-shell" in BASE_HTML
    assert "sakura-modal-fade-in" in BASE_HTML
    assert "sakura-modal-panel-in" not in BASE_HTML
    assert "translateY(8px)" not in BASE_HTML
    reduced_motion = re.search(
        r"@media \(prefers-reduced-motion: reduce\) \{[^}]*sakura-modal-shell",
        BASE_HTML,
    )
    assert reduced_motion, "prefers-reduced-motion must disable modal animations"


def test_base_includes_confirm_dialog_component() -> None:
    assert '{% include "components/confirm_dialog.html" %}' in BASE_HTML


def test_global_dialog_stack_is_shared_by_all_modal_kinds() -> None:
    """页面 Modal、ConfirmDialog 与特殊 Modal 共用焦点 / 滚动 / ESC 栈。"""
    assert "var dialogStack = [];" in BASE_HTML
    assert "openDialog: openDialog" in BASE_HTML
    assert "closeDialog: closeDialog" in BASE_HTML
    assert "window.Sakura.openDialog(this.$root" in BASE_HTML
    assert "window.Sakura.closeDialog(this.$root" in BASE_HTML
    assert "event.stopImmediatePropagation();" in BASE_HTML
    assert "visibleFocusables(element)" in BASE_HTML


def test_data_confirm_handler_supports_variant_and_uses_public_api() -> None:
    """data-confirm 表单拦截走 Sakura.confirm 且支持 data-confirm-variant。"""
    assert "form.dataset.confirmVariant" in BASE_HTML
    assert "window.Sakura.confirm({" in BASE_HTML


# ============ 2. ConfirmDialog 组件 ============


def test_confirm_dialog_has_complete_aria_attributes() -> None:
    assert 'role="dialog"' in CONFIRM_DIALOG
    assert 'aria-modal="true"' in CONFIRM_DIALOG
    assert 'aria-labelledby="sakura-confirm-dialog-title"' in CONFIRM_DIALOG
    assert 'aria-describedby="sakura-confirm-dialog-message"' in CONFIRM_DIALOG


def test_confirm_dialog_declares_public_api_hook() -> None:
    """data-sakura-confirm-dialog 是 Sakura.confirm 查找实例的锚点。"""
    assert "data-sakura-confirm-dialog" in CONFIRM_DIALOG
    assert 'x-data="sakuraConfirmDialog()"' in CONFIRM_DIALOG


def test_confirm_dialog_supports_semantic_variants() -> None:
    """default / warning / danger 三种语义色。"""
    for variant in ("'danger'", "'warning'"):
        assert variant in CONFIRM_DIALOG
    assert "bg-red-600" in CONFIRM_DIALOG
    assert "bg-amber-600" in CONFIRM_DIALOG
    assert "bg-sakura-600" in CONFIRM_DIALOG


def test_confirm_dialog_backdrop_click_uses_dismiss_guard() -> None:
    """backdrop 走 dismiss()；ESC 由全局 dialog 栈统一守卫。"""
    assert '@click="dismiss()"' in CONFIRM_DIALOG
    assert "canDismiss: () => !this.loading && !this.persistent" in BASE_HTML


def test_confirm_dialog_focus_targets_are_marked() -> None:
    """focusConfirm 依赖 data-sakura-confirm-button 定位确认按钮。"""
    assert CONFIRM_DIALOG.count("data-sakura-confirm-button") == 2
    assert "querySelectorAll('button[data-sakura-confirm-button]')" in BASE_HTML


def test_confirm_dialog_uses_unified_visual_language() -> None:
    assert "rounded-2xl" in CONFIRM_DIALOG
    assert "shadow-2xl" in CONFIRM_DIALOG
    assert "sakura-modal-shell" in CONFIRM_DIALOG
    assert "bg-gray-950/80 backdrop-blur-sm" in CONFIRM_DIALOG
    assert "dark:" in CONFIRM_DIALOG  # Dark Mode


def test_all_unified_dialog_backdrops_match_restart_overlay() -> None:
    """所有统一 Dialog 遮罩都使用全屏 gray-950/80 + blur。"""
    cases = {
        "components/modal.html": 1,
        "components/confirm_dialog.html": 1,
        "agent_team.html": 2,
        "version_manager.html": 1,
    }
    for name, expected_count in cases.items():
        source = (TEMPLATES_DIR / name).read_text(encoding="utf-8")
        assert source.count("bg-gray-950/80 backdrop-blur-sm") == expected_count
        assert "bg-gray-950/50 backdrop-blur-sm" not in source


def test_page_content_does_not_trap_modal_below_nav_and_sidebar() -> None:
    """main 不能创建 z-index 层叠上下文，否则页面内 z-[110] Modal 会被压住。"""
    assert 'relative z-10">' not in BASE_HTML
    assert 'min-h-[calc(100vh-4rem)] relative">' in BASE_HTML


# ============ 3. Modal 宏 ============


@pytest.mark.parametrize(
    ("size", "expected_class"),
    [
        ("sm", "max-w-md"),
        ("md", "max-w-lg"),
        ("lg", "max-w-3xl"),
        ("xl", "max-w-5xl"),
        ("full", "max-w-[calc(100vw-2rem)]"),
    ],
)
def test_modal_shell_supports_all_sizes(size: str, expected_class: str) -> None:
    """统一尺寸 sm / md / lg / xl / full 均映射到确定宽度。"""
    templates = get_templates()
    source = (
        '{% from "components/modal.html" import modal_shell %}'
        "{% call modal_shell(id='t-modal', title='T', size='" + size + "') %}"
        "body{% endcall %}"
    )
    html = templates.env.from_string(source).render()
    assert 'data-sakura-modal="t-modal"' in html
    assert expected_class in html


def test_modal_shell_renders_aria_and_dismiss_contract() -> None:
    templates = get_templates()
    template = templates.env.from_string(
        '{% from "components/modal.html" import modal_shell %}'
        "{% call modal_shell(id='aria-modal', title='Hello', "
        "description='Desc', persistent=true, close_sr='Close') %}"
        "content{% endcall %}"
    )
    html = template.render()
    assert 'role="dialog"' in html
    assert 'aria-modal="true"' in html
    assert 'aria-labelledby="aria-modal-title"' in html
    assert 'aria-describedby="aria-modal-description"' in html
    assert "persistent: true" in html
    assert 'aria-label="Close"' in html


def test_modal_shell_renders_one_valid_alpine_x_data_attribute() -> None:
    """Alpine 参数必须留在一个 HTML 属性内，防止页面加载时弹窗自动显示。

    ``id | tojson`` 输出双引号；如果外层 ``x-data`` 也使用双引号，浏览器会把
    JSON 字符串截断成多个无效属性，导致 ``x-show="open"`` 不生效。
    """
    templates = get_templates()
    template = templates.env.from_string(
        '{% from "components/modal.html" import modal_shell %}'
        "{% call modal_shell(id='alpine-modal', title='Hello') %}"
        "content{% endcall %}"
    )
    html = template.render()
    modal_attrs = None

    class ModalParser(HTMLParser):
        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            nonlocal modal_attrs
            values = dict(attrs)
            if values.get("data-sakura-modal") == "alpine-modal":
                modal_attrs = values

    parser = ModalParser()
    parser.feed(html)
    assert modal_attrs is not None
    assert modal_attrs["x-data"] == "sakuraModal({ id: \"alpine-modal\", persistent: false })"
    assert modal_attrs["x-show"] == "open"


def test_migration_pages_use_modal_shell_instead_of_hand_written_modals() -> None:
    """Phase 3 迁移页面：不再包含手写 hidden fixed inset-0 Modal 结构。"""
    migrated = [
        "billing/admin_codes.html",
        "billing/admin_plans.html",
        "components/add_user_modal.html",
        "system_config.html",
        "agent_skills.html",
    ]
    for name in migrated:
        source = (TEMPLATES_DIR / name).read_text(encoding="utf-8")
        assert "hidden fixed inset-0" not in source, f"{name} still hand-writes modal"
        assert (
            'from "components/modal.html" import modal_shell' in source
            or "{% from \"components/modal.html\" import modal_shell %}" in source
        ), f"{name} must import modal_shell"
        assert "<dialog" not in source, f"{name} still uses native <dialog>"


def test_agent_skills_install_modal_is_registered() -> None:
    """原生 <dialog> 迁移后通过 Sakura.openModal 打开。"""
    source = (TEMPLATES_DIR / "agent_skills.html").read_text(encoding="utf-8")
    assert "Sakura.openModal('agent-skill-install')" in source
    assert "showModal" not in source


# ============ 4. 原生 confirm 灭绝 ============


def test_no_native_confirm_calls_in_production_templates() -> None:
    """生产模板禁止 confirm() / window.confirm() / 内联 return confirm。

    豁免：sakuraConfirmDialog / branchDialog 组件自身的 confirm() 方法
    （这是统一弹窗基础设施的实现，不是浏览器原生 confirm）。
    """
    offenders = []
    for name in PRODUCTION_TEMPLATES:
        source = (TEMPLATES_DIR / name).read_text(encoding="utf-8")
        for line_no, line in enumerate(source.splitlines(), start=1):
            stripped = line.strip()
            if "Sakura.confirm" in line or "sakuraConfirmDialog" in line:
                continue
            if re.search(r"(?<![\w.$])confirm\(", line) and "_confirm" not in line:
                # 组件方法定义/调用（confirm() { ... } 或 @click="confirm()"），
                # 不是浏览器原生 confirm 调用。
                if re.search(r"confirm\(\)\s*\{", line):
                    continue
                if "branchDialog" in line or '@click="confirm()"' in line:
                    continue
                offenders.append(f"{name}:{line_no}: {stripped[:120]}")
            if "window.confirm" in line:
                offenders.append(f"{name}:{line_no}: {stripped[:120]}")
    assert not offenders, "native confirm() found:\n" + "\n".join(offenders)


def test_no_inline_confirm_handlers_in_production_templates() -> None:
    """禁止 onclick=\"return confirm(...)\" / onsubmit=\"return confirm(...)\"。"""
    offenders = []
    for name in PRODUCTION_TEMPLATES:
        source = (TEMPLATES_DIR / name).read_text(encoding="utf-8")
        for pattern in ("onclick=\"return confirm", "onsubmit=\"return confirm"):
            if pattern in source:
                offenders.append(f"{name}: {pattern}")
    assert not offenders, "inline confirm handler found:\n" + "\n".join(offenders)


def test_declarative_data_confirm_forms_use_variant_metadata() -> None:
    """迁移为 data-confirm 的代表性表单必须同时声明 variant 语义。"""
    cases = [
        (
            "security_user_detail.html",
            'data-confirm="{{ _(\'security.confirm_reset_all_mfa\') }}"',
            "danger",
        ),
        (
            "config_backup.html",
            'data-confirm="{{ _(\'config.backup_import_confirm\') }}"',
            "danger",
        ),
        (
            "billing/index.html",
            'data-confirm="{{ _(\'billing.confirm_delete_order\') }}"',
            "danger",
        ),
        (
            "components/agent_skills_list_fragment.html",
            'data-confirm="{{ _(\'agent_skills.confirm_delete\') }}"',
            "danger",
        ),
    ]
    for name, marker, variant in cases:
        source = (TEMPLATES_DIR / name).read_text(encoding="utf-8")
        assert marker in source, f"{name} missing declarative data-confirm"
        assert f'data-confirm-variant="{variant}"' in source


def test_agent_team_uses_public_confirm_api_without_fallback() -> None:
    """agent_team 删除 window.confirm fallback，统一走 Sakura.confirm。"""
    source = (TEMPLATES_DIR / "agent_team.html").read_text(encoding="utf-8")
    assert "window.confirm" not in source
    assert "Sakura.confirm(" in source
    assert "[x-data=\"confirmDialog()\"]" not in source


def test_agent_team_drawers_are_separated_from_modals() -> None:
    """Phase 4：Drawer / Sheet 标注为 Drawer 层级（z-[100]/z-[105]），
    Modal（issue 预览 / branch 选择）统一为 z-[110] sakura-modal-shell。"""
    source = (TEMPLATES_DIR / "agent_team.html").read_text(encoding="utf-8")
    assert "agent-drawer-shell" in source
    assert "z-[105]" in source  # 任务详情 Drawer
    assert "z-[100]" in source  # Secondary Panel Drawer
    # issuePreview Modal + branchDialog Modal（不含 base.html 提供的 ConfirmDialog）
    assert source.count("sakura-modal-shell") == 2
    assert "z-[120]" not in source and "z-[125]" not in source


def test_agent_team_custom_modals_use_shared_dialog_behavior() -> None:
    """Issue 预览 / base branch Modal 不因特殊业务而绕过焦点与滚动契约。"""
    source = (TEMPLATES_DIR / "agent_team.html").read_text(encoding="utf-8")
    assert source.count("window.Sakura.openDialog(") == 2
    assert source.count("window.Sakura.closeDialog(") == 3
    assert 'x-ref="issuePreviewDialog"' in source
    assert "canDismiss: () => !this.issuePreview.creating" in source
    assert "canDismiss: () => !this.loading" in source
    assert "cancel() { if (this.loading) return;" in source


def test_version_manager_progress_modal_uses_unified_visual_layer() -> None:
    """Host updater 进度弹窗对齐统一视觉层（z-[110] / sakura-modal-shell），
    但保留其特殊关闭策略：仅终态错误允许关闭。"""
    source = (TEMPLATES_DIR / "version_manager.html").read_text(encoding="utf-8")
    assert 'id="update-progress-modal"' in source
    progress_block = source[source.index('id="update-progress-modal"') :]
    progress_block = progress_block[: progress_block.index(">")]
    assert "z-[110]" in progress_block
    assert "sakura-modal-shell" in progress_block
    assert "progressTerminal" in source
    assert "window.Sakura?.openDialog(updateProgressModal" in source
    assert "window.Sakura?.closeDialog(updateProgressModal" in source
    assert "document.body.classList.add('overflow-hidden')" not in source
