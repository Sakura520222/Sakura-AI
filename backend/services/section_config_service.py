"""统一配置节读写服务
Unified section config read/write service.

统一配置存储写路径（docs/plans/2026-08-16-unified-config-store.md §3.3）：

    WebUI 表单 POST / REST API 写端点
      → SectionConfigService.save_section()
          1. 结构校验（按节注册的 validator；只校验类型/格式，不设数值上限）
          2. 每节一把 asyncio.Lock 内事务 upsert app_config 单键（值为 JSON 文本）
          3. update_section_store 同步进程内存 + 按节归属 reload facade 单例
          4. 返回变更日志（prompt 类节仅记录长度与 sha256 摘要，避免审计膨胀）

读取链：load_section 返回「内置默认 ← DB 覆盖」深度合并结果；
reset_section 删除 DB 键并清除 store 覆盖，回退内置默认。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections import OrderedDict
from copy import deepcopy
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.config import (
    get_dynamic_config,
    reload_label_config,
    reload_strategy_config,
)
from backend.core.config_sections import (
    _PRUNED,
    SECTION_REGISTRY,
    _prune_default_equal_leaves,
    clear_section_store,
    deep_merge,
    get_section_config,
    get_section_defaults,
    update_section_store,
)
from backend.models.database import AppConfig

# 变更日志类型：叶子路径 → {"old": ..., "new": ...}
ChangeLog = dict[str, dict[str, Any]]

# 标签验证规则（匹配 GitHub 标签命名规范；等价迁移自 WebUI 路由层）
_LABEL_NAME_RE = re.compile(r"^[a-zA-Z0-9.\-_/ ]+$")
_LABEL_COLOR_RE = re.compile(r"^[0-9a-fA-F]{6}$")
# GitHub 标签 API 的平台规范上限（等价迁移，非本项目自设限制）
_MAX_LABEL_NAME_LEN = 100

# PR 依赖图生成模式
_DEPGRAPH_MODES = frozenset({"ai", "static"})

# 大文本（prompt 类）节：变更日志只记长度 + sha256 前 8 位摘要，不落全文。
# 集合即设计文档划定的五个含模板的节。
_PROMPT_HEAVY_SECTIONS = frozenset(
    {
        "strategy.strategies",
        "strategy.review_policy",
        "strategy.issue_analysis",
        "strategy.pr_summary",
        "strategy.pr_dependency_graph",
    }
)

# 模板占位符（形如 {strategy_name} 的命名占位符）
_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


# ============================================================================
# 校验辅助 / validation helpers
# ============================================================================


def _validate_bool(value: Any, label: str) -> None:
    """校验布尔字段（存在时调用方保证键存在）。"""
    if not isinstance(value, bool):
        raise ValueError(f"{label} 必须是布尔值: {value!r}")


def _validate_int(value: Any, label: str) -> None:
    """校验整数字段（bool 不是合法整数；不设数值上下限）。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} 必须是整数: {value!r}")


def _validate_str(value: Any, label: str, *, allow_empty: bool = True) -> None:
    """校验字符串字段。"""
    if not isinstance(value, str):
        raise ValueError(f"{label} 必须是字符串: {value!r}")
    if not allow_empty and not value.strip():
        raise ValueError(f"{label} 不能为空")


def _validate_str_list(value: Any, label: str) -> None:
    """校验列表类节：元素必须是字符串。"""
    if not isinstance(value, list):
        raise ValueError(f"{label} 必须是列表: {value!r}")
    for idx, item in enumerate(value):
        if not isinstance(item, str):
            raise ValueError(f"{label}[{idx}] 必须是字符串: {item!r}")


def _validate_label_name(name: str) -> None:
    """验证标签名称格式（GitHub 标签命名规范）。"""
    if len(name) > _MAX_LABEL_NAME_LEN:
        raise ValueError(
            f"标签名称过长（最多 {_MAX_LABEL_NAME_LEN} 字符）: {name[:20]}..."
        )
    if not _LABEL_NAME_RE.match(name):
        raise ValueError(f"标签名称包含非法字符: {name}")


def _iter_string_leaves(node: Any, path: str = ""):
    """递归产出 dict 树中字符串叶子的 (路径, 值)；列表元素不参与模板校验。"""
    if isinstance(node, dict):
        for key, value in node.items():
            child_path = f"{path}.{key}" if path else str(key)
            yield from _iter_string_leaves(value, child_path)
    elif isinstance(node, str):
        yield path, node


def _extract_placeholders(text: str) -> frozenset[str]:
    """提取模板中的命名占位符集合。"""
    return frozenset(_PLACEHOLDER_RE.findall(text))


def _validate_template_placeholders(section_key: str, effective: dict) -> None:
    """校验模板占位符不丢失既有集合。

    占位符基准集合从当前内置默认模板中程序化提取（同路径字符串对比），
    不手工维护占位符清单；默认中不含占位符的字符串天然不设约束。
    """
    defaults = get_section_defaults(section_key)
    default_leaves = dict(_iter_string_leaves(defaults))
    for path, text in _iter_string_leaves(effective):
        default_text = default_leaves.get(path)
        if default_text is None:
            continue
        missing = _extract_placeholders(default_text) - _extract_placeholders(text)
        if missing:
            raise ValueError(
                f"[{section_key}] 模板 {path} 丢失必需占位符: {sorted(missing)}"
            )


# ============================================================================
# 按节注册的校验器 / per-section validators
# 输入为该节的有效值（内置默认 ← 覆盖深度合并后的结果）。
# ============================================================================


def _validate_strategy_strategies(data: dict) -> None:
    """策略分级：每策略 conditions 数值为正整数、prompt/name 非空字符串。"""
    if not isinstance(data, dict) or not data:
        raise ValueError("策略定义不能为空")
    for tier, spec in data.items():
        if not isinstance(spec, dict):
            raise ValueError(f"[{tier}] 策略定义必须是对象")
        _validate_str(spec.get("name"), f"[{tier}] name", allow_empty=False)
        _validate_str(spec.get("prompt"), f"[{tier}] prompt", allow_empty=False)
        conditions = spec.get("conditions")
        if not isinstance(conditions, dict):
            raise ValueError(f"[{tier}] conditions 必须是对象")
        for field in ("max_files", "max_lines"):
            value = conditions.get(field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"[{tier}] {field} 必须是不小于 1 的整数: {value!r}")


def _validate_file_filters(data: dict) -> None:
    """文件过滤：三个列表元素为字符串。"""
    for field in ("skip_extensions", "skip_paths", "code_extensions"):
        if field in data:
            _validate_str_list(data[field], f"file_filters.{field}")


# context_enhancement 中 WebUI 表单可编辑的字段（等价迁移旧表单校验：
# 类型检查；不设数值上下限）。sakura_memory / ci_failure_injection 等
# 运行时开关不由表单编辑，交由运行时容错，不做结构强校验。
_CE_BOOL_FIELDS = ("enable_project_structure", "enable_ai_tools")
_CE_INT_FIELDS = (
    "max_structure_files",
    "max_file_size",
    "max_files_for_deep_strategy",
    "max_file_lines",
    "default_context_lines",
    "max_context_lines",
)


def _validate_context_enhancement(data: dict) -> None:
    """上下文增强：布尔/数值字段类型校验（存在时）。"""
    for field in _CE_BOOL_FIELDS:
        if field in data:
            _validate_bool(data[field], f"context_enhancement.{field}")
    for field in _CE_INT_FIELDS:
        if field in data:
            _validate_int(data[field], f"context_enhancement.{field}")
    sif = data.get("search_in_files")
    if sif is not None:
        if not isinstance(sif, dict):
            raise ValueError("context_enhancement.search_in_files 必须是对象")
        for field in ("use_search_api", "skip_binary"):
            if field in sif:
                _validate_bool(sif[field], f"search_in_files.{field}")
        for field in (
            "default_context_lines",
            "default_max_results",
            "max_files_to_search",
        ):
            if field in sif:
                _validate_int(sif[field], f"search_in_files.{field}")
    git_tools = data.get("git_tools")
    if git_tools is not None:
        if not isinstance(git_tools, dict):
            raise ValueError("context_enhancement.git_tools 必须是对象")
        for field in ("default_branch_count", "default_commit_count"):
            if field in git_tools:
                _validate_int(git_tools[field], f"git_tools.{field}")


def _validate_review_policy(data: dict) -> None:
    """审查批准策略：布尔/整数/模板字段类型校验（存在时）。"""
    for field in ("enabled", "block_on_critical", "enable_idempotency_check"):
        if field in data:
            _validate_bool(data[field], f"review_policy.{field}")
    for field in ("approve_threshold", "block_threshold", "max_major_issues"):
        if field in data:
            _validate_int(data[field], f"review_policy.{field}")
    if "ignored_patterns" in data:
        _validate_str_list(data["ignored_patterns"], "review_policy.ignored_patterns")
    overrides = data.get("repo_overrides")
    if overrides is not None and not isinstance(overrides, dict):
        raise ValueError("review_policy.repo_overrides 必须是对象")
    for templates_key in ("review_templates", "review_templates_en"):
        templates = data.get(templates_key)
        if templates is None:
            continue
        if not isinstance(templates, dict):
            raise ValueError(f"review_policy.{templates_key} 必须是对象")
        for name, template in templates.items():
            _validate_str(template, f"review_policy.{templates_key}.{name}")


def _validate_issue_analysis(data: dict) -> None:
    """Issue 分析：分类/优先级规则/关键词列表结构校验。"""
    categories = data.get("categories")
    if not isinstance(categories, list) or not categories:
        raise ValueError("issue_analysis.categories 至少需要定义一个 Issue 分类")
    for idx, category in enumerate(categories):
        if not isinstance(category, dict):
            raise ValueError(f"issue_analysis.categories[{idx}] 必须是对象")
        _validate_str(
            category.get("name"), f"categories[{idx}].name", allow_empty=False
        )
        _validate_str(category.get("description"), f"categories[{idx}].description")
        _validate_str_list(category.get("keywords", []), f"categories[{idx}].keywords")
    priority_rules = data.get("priority_rules")
    if priority_rules is not None:
        if not isinstance(priority_rules, dict):
            raise ValueError("issue_analysis.priority_rules 必须是对象")
        for level, rule in priority_rules.items():
            if not isinstance(rule, dict):
                raise ValueError(f"issue_analysis.priority_rules.{level} 必须是对象")
            _validate_str_list(
                rule.get("keywords", []), f"priority_rules.{level}.keywords"
            )
    if "issue_reference_keywords" in data:
        _validate_str_list(
            data["issue_reference_keywords"], "issue_analysis.issue_reference_keywords"
        )
    if "max_linked_issues_in_prompt" in data:
        _validate_int(
            data["max_linked_issues_in_prompt"],
            "issue_analysis.max_linked_issues_in_prompt",
        )
    for field in ("system_prompt", "comment_template", "comment_template_en"):
        if field in data:
            _validate_str(data[field], f"issue_analysis.{field}")


def _validate_pr_summary(data: dict) -> None:
    """PR 摘要：模板字段为字符串（占位符由统一校验保护）。"""
    for field in ("system_prompt", "user_template"):
        if field in data:
            _validate_str(data[field], f"pr_summary.{field}")


def _validate_pr_dependency_graph(data: dict) -> None:
    """PR 依赖图：mode 合法值 + 模板字段为字符串。"""
    if "mode" in data and data["mode"] not in _DEPGRAPH_MODES:
        raise ValueError(
            f"pr_dependency_graph.mode 必须是 {'/'.join(sorted(_DEPGRAPH_MODES))}: "
            f"{data['mode']!r}"
        )
    for field in ("system_prompt", "user_template"):
        if field in data:
            _validate_str(data[field], f"pr_dependency_graph.{field}")


def _validate_scan(data: dict) -> None:
    """仓库扫描：system_prompt 为字符串。"""
    if "system_prompt" in data:
        _validate_str(data["system_prompt"], "scan.system_prompt")


def _validate_label_definitions(data: dict) -> None:
    """标签定义：颜色为 6 位十六进制、description 为字符串。"""
    if not isinstance(data, dict):
        raise ValueError("标签定义必须是对象")
    for name, spec in data.items():
        _validate_label_name(name)
        if not isinstance(spec, dict):
            raise ValueError(f"标签 [{name}] 定义必须是对象")
        color = spec.get("color")
        if not isinstance(color, str):
            raise ValueError(f"标签 [{name}] 颜色必须是字符串: {color!r}")
        color = color.strip().lstrip("#")
        if not _LABEL_COLOR_RE.match(color):
            raise ValueError(f"标签 [{name}] 颜色格式错误（需 6 位十六进制）: {color}")
        if not isinstance(spec.get("description"), str):
            raise ValueError(f"标签 [{name}] description 必须是字符串")


def _validate_label_recommendation(data: dict) -> None:
    """标签推荐：confidence_threshold 为 0-1 浮点、enabled/auto_create 为布尔。"""
    for field in ("enabled", "auto_create"):
        if field in data:
            _validate_bool(data[field], f"recommendation.{field}")
    if "confidence_threshold" in data:
        value = data["confidence_threshold"]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(
                f"recommendation.confidence_threshold 必须是数值: {value!r}"
            )
        if not 0.0 <= value <= 1.0:
            raise ValueError(
                f"recommendation.confidence_threshold 必须在 0.0-1.0 之间: {value!r}"
            )


def _validate_label_conflict_rules(data: dict) -> None:
    """标签冲突规则：key 为合法标签名、value 为字符串列表。"""
    if not isinstance(data, dict):
        raise ValueError("冲突规则必须是对象")
    for source, blocked in data.items():
        _validate_label_name(source)
        _validate_str_list(blocked, f"conflict_rules.{source}")
        for item in blocked:
            _validate_label_name(item)


# 校验器注册表：app_config 节键 → 该节的结构校验函数
SECTION_VALIDATORS: OrderedDict[str, Any] = OrderedDict(
    [
        ("strategy.strategies", _validate_strategy_strategies),
        ("strategy.file_filters", _validate_file_filters),
        ("strategy.context_enhancement", _validate_context_enhancement),
        ("strategy.review_policy", _validate_review_policy),
        ("strategy.issue_analysis", _validate_issue_analysis),
        ("strategy.pr_summary", _validate_pr_summary),
        ("strategy.pr_dependency_graph", _validate_pr_dependency_graph),
        ("strategy.scan", _validate_scan),
        ("label.definitions", _validate_label_definitions),
        ("label.recommendation", _validate_label_recommendation),
        ("label.conflict_rules", _validate_label_conflict_rules),
    ]
)


# ============================================================================
# 变更日志 / change log
# ============================================================================

_MISSING = object()


def _sha256_prefix(text: str) -> str:
    """sha256 前 8 位摘要（大文本审计用）。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def _serialize_for_audit(value: Any, *, digest: bool) -> Any:
    """序列化叶子值用于变更日志；digest 模式下字符串只记长度与摘要。"""
    if digest and isinstance(value, str):
        return f"(len={len(value)}, sha256={_sha256_prefix(value)})"
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return json.dumps(value, ensure_ascii=False)


def _leaf_diff(old: Any, new: Any, *, digest: bool, path: str = "") -> ChangeLog:
    """递归比较两棵树的叶子差异，产出变更日志。"""
    if isinstance(old, dict) and isinstance(new, dict):
        changes: ChangeLog = {}
        for key in sorted(set(old) | set(new), key=str):
            child_path = f"{path}.{key}" if path else str(key)
            changes.update(
                _leaf_diff(
                    old.get(key, _MISSING),
                    new.get(key, _MISSING),
                    digest=digest,
                    path=child_path,
                )
            )
        return changes
    if old is _MISSING:
        return {path: {"old": "(无)", "new": _serialize_for_audit(new, digest=digest)}}
    if new is _MISSING:
        return {
            path: {"old": _serialize_for_audit(old, digest=digest), "new": "(已移除)"}
        }
    if old != new:
        return {
            path: {
                "old": _serialize_for_audit(old, digest=digest),
                "new": _serialize_for_audit(new, digest=digest),
            }
        }
    return {}


# ============================================================================
# 服务 / service
# ============================================================================


class SectionConfigService:
    """统一配置节读写服务（单例）。"""

    def __init__(self) -> None:
        # 每节一把锁，防止同一节的并发读-改-写竞态
        self._locks: dict[str, asyncio.Lock] = {}

    # ---------- 读取 ----------

    async def load_section(self, db: AsyncSession, section_key: str) -> dict:
        """读取某节有效配置：内置默认 ← DB 覆盖深度合并。

        DB 键缺失或值非法（非 JSON / 非 dict）时回退内置默认。
        """
        spec = SECTION_REGISTRY.get(section_key)
        if spec is None:
            raise KeyError(f"未注册的配置节: {section_key}")
        override = await self._load_override(db, section_key)
        return deep_merge(spec["defaults"], override or {})

    async def resolve_depgraph_mode(self) -> str:
        """读取 PR 依赖图生成模式（ai/static）。

        优先读节配置 strategy.pr_dependency_graph 的 mode 字段；
        未配置时回退旧动态配置键 pr_dependency_graph_mode（兼容历史部署，
        其 Settings 默认为 "static"）。
        """
        mode = get_section_config("strategy.pr_dependency_graph").get("mode")
        if isinstance(mode, str) and mode.strip().lower() in _DEPGRAPH_MODES:
            return mode.strip().lower()
        legacy = await get_dynamic_config("pr_dependency_graph_mode")
        legacy_mode = str(legacy or "static").strip().lower()
        return legacy_mode if legacy_mode in _DEPGRAPH_MODES else "static"

    # ---------- 写入 ----------

    async def save_section(
        self,
        db: AsyncSession,
        section_key: str,
        data: dict,
        *,
        mode: str = "replace",
    ) -> dict:
        """校验并保存某节覆盖，返回变更日志。

        Args:
            db: 数据库会话（service 内部完成 commit/rollback）。
            section_key: 注册表中的节键（如 "strategy.strategies"）。
            data: 节数据。
            mode: "replace" 整节替换覆盖（全量表单）；"patch" 与既有 DB 覆盖
                深度合并（REST PATCH 合并语义，保留未提交叶子）。

        Returns:
            {"section": 键, "changed": 是否落库, "changes": 变更日志}。

        校验对象是有效值（默认 ← 覆盖合并），保证落库后组合总是合法；
        与内置默认完全等价的保存会移除 DB 覆盖（保持「无覆盖=用默认」语义）。
        """
        spec = SECTION_REGISTRY.get(section_key)
        if spec is None:
            raise KeyError(f"未注册的配置节: {section_key}")
        if not isinstance(data, dict):
            raise ValueError(f"配置节 [{section_key}] 数据必须是 JSON 对象")
        if mode not in ("replace", "patch"):
            raise ValueError(f"未知的保存模式: {mode}")

        async with self._get_lock(section_key):
            old_override = await self._load_override(db, section_key)
            old_effective = deep_merge(spec["defaults"], old_override or {})
            if mode == "patch":
                new_override = deep_merge(old_override or {}, data)
            else:
                new_override = deepcopy(data)
            new_effective = deep_merge(spec["defaults"], new_override)

            validator = SECTION_VALIDATORS[section_key]
            validator(new_effective)
            _validate_template_placeholders(section_key, new_effective)

            # 只从覆盖树中裁掉与当前内置默认相等的叶子；非默认叶子、
            # 未知键以及 patch 模式合并保留下来的旧覆盖必须继续持久化。
            pruned_override = _prune_default_equal_leaves(
                spec["defaults"], new_effective
            )

            changes = _leaf_diff(
                old_effective,
                new_effective,
                digest=section_key in _PROMPT_HEAVY_SECTIONS,
            )

            if pruned_override is _PRUNED:
                # 与内置默认无差异：移除 DB 覆盖回退默认，避免物化默认值
                existed = await self._delete_row(db, section_key)
                if existed:
                    clear_section_store(section_key)
                    self._reload_for_target(spec["target"])
                return {
                    "section": section_key,
                    "changed": existed,
                    "changes": changes,
                }

            new_override = pruned_override
            new_json = json.dumps(new_override, ensure_ascii=False, sort_keys=True)
            old_json = json.dumps(
                old_override or {}, ensure_ascii=False, sort_keys=True
            )
            if new_json == old_json:
                return {"section": section_key, "changed": False, "changes": {}}

            await self._upsert_row(db, section_key, new_json)
            update_section_store(section_key, new_override)
            self._reload_for_target(spec["target"])
            return {"section": section_key, "changed": True, "changes": changes}

    async def reset_section(self, db: AsyncSession, section_key: str) -> dict:
        """重置某节：删除 DB 键 + 清除 store 覆盖，回退内置默认。"""
        spec = SECTION_REGISTRY.get(section_key)
        if spec is None:
            raise KeyError(f"未注册的配置节: {section_key}")
        async with self._get_lock(section_key):
            existed = await self._delete_row(db, section_key)
            if existed:
                clear_section_store(section_key)
                self._reload_for_target(spec["target"])
        return {"section": section_key, "existed": existed}

    # ---------- 审计 ----------

    @staticmethod
    def build_audit_log(result: dict) -> dict:
        """构建审计日志（save_section 结果中的变更日志直接透传）。"""
        return result.get("changes") or {}

    # ---------- 内部 ----------

    def _get_lock(self, section_key: str) -> asyncio.Lock:
        """获取某节的异步锁（注册表键有限，无需清理）。"""
        lock = self._locks.get(section_key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[section_key] = lock
        return lock

    @staticmethod
    def _reload_for_target(target: str) -> None:
        """按节归属刷新 facade 单例（lru_cache 清除）。"""
        if target == "strategy":
            reload_strategy_config()
        elif target == "label":
            reload_label_config()

    @staticmethod
    async def _load_override(db: AsyncSession, section_key: str) -> dict | None:
        """读取某节的 DB 覆盖值；键缺失/值非法时返回 None。"""
        result = await db.execute(
            select(AppConfig).where(AppConfig.key_name == section_key)
        )
        cfg = result.scalar_one_or_none()
        if cfg is None or cfg.key_value is None:
            return None
        try:
            data = json.loads(str(cfg.key_value))
        except TypeError, ValueError:
            logger.warning(f"配置节 [{section_key}] DB 值 JSON 解析失败，按无覆盖处理")
            return None
        if not isinstance(data, dict):
            logger.warning(f"配置节 [{section_key}] DB 值不是 JSON 对象，按无覆盖处理")
            return None
        return data

    @staticmethod
    async def _upsert_row(db: AsyncSession, section_key: str, value_json: str) -> None:
        """事务内 upsert 单个 app_config 键（值为 JSON 文本）。"""
        try:
            result = await db.execute(
                select(AppConfig).where(AppConfig.key_name == section_key)
            )
            cfg = result.scalar_one_or_none()
            if cfg is None:
                db.add(
                    AppConfig(
                        key_name=section_key,
                        key_value=value_json,
                        description=section_key,
                    )
                )
            else:
                cfg.key_value = value_json
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    @staticmethod
    async def _delete_row(db: AsyncSession, section_key: str) -> bool:
        """事务内删除节键；键不存在返回 False。"""
        result = await db.execute(
            select(AppConfig).where(AppConfig.key_name == section_key)
        )
        cfg = result.scalar_one_or_none()
        if cfg is None:
            return False
        try:
            await db.delete(cfg)
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        return True


section_config_service = SectionConfigService()
