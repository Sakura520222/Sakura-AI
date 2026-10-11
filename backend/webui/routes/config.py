"""WebUI 配置管理路由（超级管理员专用）

全局配置页 /config（R3）：单页吃下平铺动态配置组与策略/标签节表单；
页面右上角统一保存按钮一次提交 /config/save-all（内部顺序复用既有
保存 handler），既有 POST 端点保留，旧 GET 页面统一 302 到 /config。
"""

import json
import math
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, Response
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import FormData

from backend.core.config import (
    BASIC_CONFIG_KEYS,
    get_dynamic_config_fresh,
    get_settings,
)
from backend.core.config_sections import get_sections_for_target
from backend.core.time_service import filename_timestamp
from backend.models.database import AppConfig, _settings_default_to_str
from backend.services.agent_team.network_policy import (
    AgentTeamNetworkPolicy,
    NetworkCapability,
    get_agent_team_network_policy_state,
    network_mode_for_policy,
)
from backend.services.agent_team.sandbox_client import (
    read_sandbox_capability_status,
    validate_execution_backend,
)
from backend.services.config_backup_service import (
    BACKUP_MAX_BYTES,
    ConfigBackupError,
    export_config_backup,
    parse_config_backup,
    refresh_imported_runtime_config,
    restore_config_backup,
    serialize_config_backup,
)
from backend.services.label_service import label_service
from backend.services.section_config_service import section_config_service
from backend.services.user_backup_service import (
    USER_BACKUP_MAX_BYTES,
    UserBackupError,
    export_user_backup,
    parse_user_backup,
    restore_user_backup,
    serialize_user_backup,
)
from backend.webui.config_feedback import config_issue, config_save_response
from backend.webui.deps import (
    get_csrf_serializer,
    get_db,
    get_templates,
    get_user_preferences,
    render_template,
    require_csrf,
    require_csrf_header,
    require_super_admin,
    toast_redirect,
)
from backend.webui.helpers.admin_log import log_admin_action
from backend.webui.i18n import detect_language, i18n

router = APIRouter(prefix="/config", tags=["WebUI Config"])
templates = get_templates()

STRATEGY_KEYS = ["quick", "standard", "deep", "large"]

# 策略配置页 section 名 → 统一配置节键（strategy.pr_dependency_graph 的
# 页面别名为 depgraph；mode 与模板统一走节配置体系，旧 DB 键
# pr_dependency_graph_mode 仅保持兼容读取）
_STRATEGY_SECTION_KEYS = {
    "strategies": "strategy.strategies",
    "file_filters": "strategy.file_filters",
    "context_enhancement": "strategy.context_enhancement",
    "review_policy": "strategy.review_policy",
    "issue_analysis": "strategy.issue_analysis",
    "depgraph": "strategy.pr_dependency_graph",
    "pr_summary": "strategy.pr_summary",
    "scan": "strategy.scan",
}

# 表单仅覆盖部分字段的 section（patch 模式保留未展示字段的自定义覆盖，
# 如 context_enhancement.sakura_memory、review_policy.repo_overrides）
_STRATEGY_PATCH_SECTIONS = frozenset({"context_enhancement", "review_policy"})


@router.get("/strategies")
async def strategies_page(
    user: dict = Depends(require_super_admin),
):
    return RedirectResponse(url="/config#section-strategy-strategies", status_code=302)


# ========== POST: 保存策略配置 ==========


@router.post("/strategies/save")
async def save_strategies_section(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
    section: str = Form(...),
    user_prefs: dict = Depends(get_user_preferences),
):
    """保存策略配置的某个 section（统一节配置存储）"""
    lang = _config_language(request, user_prefs)
    try:
        form = await request.form()
        numeric_errors = _section_number_issues(form, section, lang)
        if numeric_errors:
            return config_save_response(
                request,
                "/config",
                "toast.config_fields_invalid",
                lang=lang,
                errors=numeric_errors,
            )
        section_key = _STRATEGY_SECTION_KEYS.get(section)
        if section_key is None:
            raise HTTPException(status_code=400, detail=f"未知 section: {section}")

        if section == "strategies":
            # 收集 4 个策略的 conditions 和 prompt
            data = {}
            for key in STRATEGY_KEYS:
                name = form.get(f"strategy_{key}_name", key)
                try:
                    max_files = int(form.get(f"strategy_{key}_max_files", 999999))
                    max_lines = int(form.get(f"strategy_{key}_max_lines", 99999999))
                except (ValueError, TypeError) as e:
                    raise ValueError(f"[{key}] 数值格式错误: {e}")
                prompt = form.get(f"strategy_{key}_prompt", "")
                data[key] = {
                    "name": name,
                    "conditions": {"max_files": max_files, "max_lines": max_lines},
                    "prompt": prompt,
                }

        elif section == "file_filters":
            skip_ext_raw = form.get("skip_extensions", "")
            skip_paths_raw = form.get("skip_paths", "")
            code_ext_raw = form.get("code_extensions", "")
            data = {
                "skip_extensions": [
                    x.strip() for x in skip_ext_raw.splitlines() if x.strip()
                ],
                "skip_paths": [
                    x.strip() for x in skip_paths_raw.splitlines() if x.strip()
                ],
                "code_extensions": [
                    x.strip() for x in code_ext_raw.splitlines() if x.strip()
                ],
            }

        elif section == "context_enhancement":
            data = {
                "enable_project_structure": form.get("enable_project_structure")
                is not None,
                "max_structure_files": int(form.get("max_structure_files", 500)),
                "enable_ai_tools": form.get("enable_ai_tools") is not None,
                "max_file_size": int(form.get("max_file_size", 200000)),
                "max_files_for_deep_strategy": int(
                    form.get("max_files_for_deep_strategy", 10)
                ),
                "max_file_lines": int(float(form.get("max_file_lines", 500))),
                "max_file_output_chars": int(
                    float(form.get("max_file_output_chars", 50000))
                ),
                "default_context_lines": int(
                    float(form.get("default_context_lines", 20))
                ),
                "max_context_lines": int(float(form.get("max_context_lines", 200))),
                "search_in_files": {
                    "use_search_api": form.get("sif_use_search_api") is not None,
                    "skip_binary": form.get("sif_skip_binary") is not None,
                    "default_context_lines": int(
                        float(form.get("sif_default_context_lines", 3))
                    ),
                    "default_max_results": int(
                        float(form.get("sif_default_max_results", 20))
                    ),
                    "max_files_to_search": int(
                        float(form.get("sif_max_files_to_search", 100))
                    ),
                    "concurrency": int(float(form.get("sif_concurrency", 8))),
                    "max_file_bytes": int(
                        float(form.get("sif_max_file_bytes", 2_097_152))
                    ),
                    "max_total_scan_bytes": int(
                        float(form.get("sif_max_total_scan_bytes", 16_777_216))
                    ),
                    "max_matches_per_file": int(
                        float(form.get("sif_max_matches_per_file", 20))
                    ),
                    "max_output_chars": int(
                        float(form.get("sif_max_output_chars", 50_000))
                    ),
                },
                "git_tools": {
                    "default_branch_count": int(
                        float(form.get("gt_default_branch_count", 20))
                    ),
                    "default_commit_count": int(
                        float(form.get("gt_default_commit_count", 10))
                    ),
                },
                # sakura_memory 嵌套节旋钮（A8 合并后的单一事实源）：
                # patch 模式深度合并，表单未覆盖的子键（model 等）保留原值
                "sakura_memory": {
                    "enabled": form.get("sakura_enabled") is not None,
                    "consolidation": {
                        "interval": int(form.get("sakura_consolidation_interval", 5)),
                        "max_memory_chars": int(
                            form.get("sakura_max_memory_chars", 2000)
                        ),
                        "max_sakura_chars": int(
                            form.get("sakura_max_sakura_chars", 3000)
                        ),
                        "partial_commit": form.get("sakura_partial_commit") is not None,
                    },
                    "knowledge_extraction": {
                        "min_reflections": int(form.get("sakura_min_reflections", 15)),
                    },
                    "initialization": {
                        "auto_init": form.get("sakura_auto_init") is not None,
                    },
                    "directory_convention": {
                        "auto_create_subdirs": form.get("sakura_auto_create_subdirs")
                        is not None,
                    },
                },
            }

        elif section == "review_policy":
            # repo_overrides 不在表单中：patch 模式保留既有自定义覆盖
            data = {
                "enabled": form.get("rp_enabled") is not None,
                "approve_threshold": int(form.get("approve_threshold", 8)),
                "block_threshold": int(form.get("block_threshold", 4)),
                "block_on_critical": form.get("block_on_critical") is not None,
                "max_major_issues": int(form.get("max_major_issues", 1)),
                "ignored_patterns": [
                    x.strip()
                    for x in form.get("ignored_patterns", "").splitlines()
                    if x.strip()
                ],
                "enable_idempotency_check": form.get("enable_idempotency_check")
                is not None,
                "review_templates": {
                    "approve": form.get("template_approve", ""),
                    "request_changes": form.get("template_request_changes", ""),
                    "comment": form.get("template_comment", ""),
                },
            }

        elif section == "depgraph":
            depgraph_mode = form.get("pr_dependency_graph_mode", "static")
            if depgraph_mode not in {"ai", "static"}:
                depgraph_mode = "static"
            # mode 与模板统一存入节配置（单键单写，消除 YAML+DB 双写）
            data = {
                "mode": depgraph_mode,
                "system_prompt": form.get("depgraph_system_prompt", ""),
                "user_template": form.get("depgraph_user_template", ""),
            }

        elif section == "pr_summary":
            # PR 总结模板节（A10：此前注册了节存储但无渲染表单）
            data = {
                "system_prompt": form.get("pr_summary_system_prompt", ""),
                "user_template": form.get("pr_summary_user_template", ""),
            }

        elif section == "scan":
            # 仓库扫描提示词 focus 节（英文强化契约由代码注入，此处仅配置 focus）
            data = {
                "system_prompt": form.get("scan_system_prompt", ""),
            }

        elif section == "issue_analysis":
            # 解析分类定义
            cat_names = form.getlist("cat_name")
            cat_descs = form.getlist("cat_desc")
            cat_keywords_raw = form.getlist("cat_keywords")
            categories = []
            for name, desc, kw_raw in zip(cat_names, cat_descs, cat_keywords_raw):
                name = name.strip()
                if not name:
                    continue
                keywords = [k.strip() for k in kw_raw.split(",") if k.strip()]
                categories.append(
                    {
                        "name": name,
                        "description": desc.strip(),
                        "keywords": keywords,
                    }
                )
            if not categories:
                raise ValueError("至少需要定义一个 Issue 分类")

            # 解析优先级规则
            priority_rules = {}
            for pkey in ("critical", "high", "medium", "low"):
                kw_raw = form.get(f"priority_{pkey}", "")
                keywords = [k.strip() for k in kw_raw.split(",") if k.strip()]
                priority_rules[pkey] = {"keywords": keywords}

            # 解析关联关键词
            ref_kw_raw = form.get("issue_reference_keywords", "")
            ref_keywords = [k.strip() for k in ref_kw_raw.split(",") if k.strip()]

            raw_linked = form.get("max_linked_issues_in_prompt", "5")
            try:
                max_linked = int(raw_linked)
            except ValueError, TypeError:
                raise ValueError("关联 Issue 数量上限必须是有效整数")

            data = {
                "categories": categories,
                "priority_rules": priority_rules,
                "issue_reference_keywords": ref_keywords,
                "max_linked_issues_in_prompt": max_linked,
                "system_prompt": form.get("issue_system_prompt", ""),
                "comment_template": form.get("issue_comment_template", ""),
                "comment_template_en": form.get("issue_comment_template_en", ""),
            }
        else:  # pragma: no cover - _STRATEGY_SECTION_KEYS 已前置拦截
            raise HTTPException(status_code=400, detail=f"未知 section: {section}")

        result = await section_config_service.save_section(
            db,
            section_key,
            data,
            mode="patch" if section in _STRATEGY_PATCH_SECTIONS else "replace",
        )
        logger.info(f"策略配置 [{section}] 已更新, by={user['sub']}")
        await log_admin_action(
            db,
            user["user_id"],
            "config_save",
            "strategy",
            section,
            section_config_service.build_audit_log(result),
        )

    except HTTPException:
        raise
    except ValueError as e:
        logger.warning(
            "策略配置验证失败: section={} error_type={}", section, type(e).__name__
        )
        return config_save_response(
            request,
            f"/config?section={section}",
            "toast.config_fields_invalid",
            lang=lang,
            errors=[
                _section_issue(e, _STRATEGY_SECTION_KEYS.get(section, ""), form, lang)
            ],
        )
    except Exception as e:
        logger.error(f"策略配置保存异常: {e}", exc_info=True)
        return config_save_response(
            request,
            f"/config?section={section}",
            "toast.save_failed",
            lang=lang,
            errors=[config_issue("", "save_failed", "toast.save_failed", lang=lang)],
        )

    return config_save_response(
        request,
        f"/config?section={section}",
        "toast.strategy_saved",
        lang=lang,
        section=section,
    )


@router.get("/labels")
async def labels_page(
    user: dict = Depends(require_super_admin),
):
    return RedirectResponse(url="/config#section-label-definitions", status_code=302)


# ========== POST: 保存标签定义 ==========


@router.post("/labels/save-labels")
async def save_labels_definitions(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
    user_prefs: dict = Depends(get_user_preferences),
):
    """保存标签定义（全量覆盖，统一节配置存储）"""
    lang = _config_language(request, user_prefs)
    try:
        form = await request.form()

        # 收集所有标签行（不假设连续索引，因为 JS 删除行会产生间隔）
        labels = {}
        for key in form:
            if key.startswith("label_name_"):
                idx = key[len("label_name_") :]
                name = str(form[key]).strip()
                if name:
                    color = (
                        str(form.get(f"label_color_{idx}", "0366d6"))
                        .strip()
                        .lstrip("#")
                    )
                    if not color:
                        return config_save_response(
                            request,
                            "/config",
                            "toast.config_fields_invalid",
                            lang=lang,
                            errors=[
                                config_issue(
                                    f"label_color_{idx}",
                                    "label_color",
                                    "section_validation.label_color",
                                    lang=lang,
                                )
                            ],
                        )
                    desc = str(form.get(f"label_desc_{idx}", "")).strip()
                    labels[name] = {"color": color, "description": desc}

        await section_config_service.save_section(db, "label.definitions", labels)
        label_service.reload_labels()
        logger.info(f"标签定义已更新 ({len(labels)} 个), by={user['sub']}")
        await log_admin_action(
            db,
            user["user_id"],
            "config_save",
            "label",
            None,
            {"label_count": len(labels)},
        )

    except ValueError as e:
        logger.warning("标签验证失败: error_type={}", type(e).__name__)
        return config_save_response(
            request,
            "/config?section=labels",
            "toast.config_fields_invalid",
            lang=lang,
            errors=[_section_issue(e, "label.definitions", form, lang)],
        )
    except Exception as e:
        logger.error(f"标签定义保存失败: {e}")
        return config_save_response(
            request,
            "/config?section=labels",
            "toast.save_failed",
            lang=lang,
            errors=[config_issue("", "save_failed", "toast.save_failed", lang=lang)],
        )

    return config_save_response(
        request,
        "/config?section=labels",
        "toast.labels_saved",
        lang=lang,
        count=len(labels),
    )


# ========== POST: 保存推荐设置 ==========


@router.post("/labels/save-settings")
async def save_recommendation_settings(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
    user_prefs: dict = Depends(get_user_preferences),
):
    """保存标签推荐设置"""
    lang = _config_language(request, user_prefs)
    try:
        form = await request.form()
        numeric_errors = _section_number_issues(form, "recommendation", lang)
        if numeric_errors:
            return config_save_response(
                request,
                "/config",
                "toast.config_fields_invalid",
                lang=lang,
                errors=numeric_errors,
            )

        confidence_threshold = float(form.get("confidence_threshold", 0.7))
        if not 0.0 <= confidence_threshold <= 1.0:
            return config_save_response(
                request,
                "/config",
                "toast.config_fields_invalid",
                lang=lang,
                errors=[
                    config_issue(
                        "confidence_threshold",
                        "invalid_config_value",
                        "toast.value_range",
                        lang=lang,
                        params={"min_v": 0, "max_v": 1},
                    )
                ],
            )

        data = {
            "enabled": form.get("rec_enabled") is not None,
            "confidence_threshold": confidence_threshold,
            "auto_create": form.get("auto_create") is not None,
        }

        await section_config_service.save_section(db, "label.recommendation", data)
        logger.info(f"标签推荐设置已更新, by={user['sub']}")
        await log_admin_action(db, user["user_id"], "config_save", "recommendation")

    except ValueError as e:
        logger.warning("推荐设置验证失败: error_type={}", type(e).__name__)
        return config_save_response(
            request,
            "/config?section=labels",
            "toast.config_fields_invalid",
            lang=lang,
            errors=[_section_issue(e, "label.recommendation", form, lang)],
        )
    except Exception as e:
        logger.error(f"标签推荐设置保存失败: {e}", exc_info=True)
        return config_save_response(
            request,
            "/config?section=labels",
            "toast.save_failed",
            lang=lang,
            errors=[config_issue("", "save_failed", "toast.save_failed", lang=lang)],
        )

    return config_save_response(
        request, "/config?section=labels", "toast.label_settings_saved", lang=lang
    )


# ========== POST: 保存标签冲突规则 ==========


@router.post("/labels/save-conflict-rules")
async def save_conflict_rules(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
    user_prefs: dict = Depends(get_user_preferences),
):
    """保存标签冲突规则"""
    lang = _config_language(request, user_prefs)
    try:
        form = await request.form()

        # 收集冲突规则行
        conflict_rules: dict[str, list] = {}
        for key in form:
            if key.startswith("conflict_source_"):
                idx = key[len("conflict_source_") :]
                source = str(form[key]).strip()
                blocked_raw = str(form.get(f"conflict_blocked_{idx}", "")).strip()
                if source and blocked_raw:
                    # 仅按逗号分隔，保留标签内部空格（如 "good first issue"）
                    blocked = [b.strip() for b in blocked_raw.split(",") if b.strip()]
                    if blocked:
                        conflict_rules[source] = blocked

        await section_config_service.save_section(
            db, "label.conflict_rules", conflict_rules
        )
        label_service.reload_labels()
        logger.info(f"标签冲突规则已更新 ({len(conflict_rules)} 条), by={user['sub']}")
        await log_admin_action(
            db,
            user["user_id"],
            "config_save",
            "conflict_rules",
            None,
            {"rule_count": len(conflict_rules)},
        )

    except ValueError as e:
        logger.warning("冲突规则验证失败: error_type={}", type(e).__name__)
        return config_save_response(
            request,
            "/config?section=labels",
            "toast.config_fields_invalid",
            lang=lang,
            errors=[_section_issue(e, "label.conflict_rules", form, lang)],
        )
    except Exception as e:
        logger.error(f"冲突规则保存失败: {e}", exc_info=True)
        return config_save_response(
            request,
            "/config?section=labels",
            "toast.save_failed",
            lang=lang,
            errors=[config_issue("", "save_failed", "toast.save_failed", lang=lang)],
        )

    return config_save_response(
        request,
        "/config?section=labels",
        "toast.conflict_rules_saved",
        lang=lang,
        count=len(conflict_rules),
    )


# ========== POST: 统一保存全部配置 ==========


class _FormOnlyRequest:
    """save-all 复用既有保存 handler；handler 仅使用 request.form() 接口。"""

    def __init__(self, fields: FormData, *, language=None) -> None:
        self._fields = fields
        self.config_language = language
        self.config_ajax = True

    async def form(self) -> FormData:
        return self._fields


def _payload_to_form_data(fields: dict) -> FormData:
    """把 JSON 字段对象（值为字符串或字符串数组）转回表单语义的 FormData。"""
    pairs: list[tuple[str, str]] = []
    for key, value in fields.items():
        values = value if isinstance(value, list) else [value]
        pairs.extend(
            (str(key), item if isinstance(item, str) else str(item)) for item in values
        )
    return FormData(pairs)


class _DynamicConfigValidationError(ValueError):
    """动态配置预校验失败时携带既有 toast 上下文。"""

    def __init__(self, toast_key: str, **context: object) -> None:
        super().__init__(toast_key)
        self.toast_key = toast_key
        self.context = context


def _config_language(request, user_prefs=None):
    carried = getattr(request, "config_language", None)
    if carried in {"zh-CN", "en"}:
        return carried
    return (
        detect_language(user_prefs)
        if isinstance(user_prefs, dict)
        else detect_language()
    )


def _dynamic_issue(exc, lang):
    params = dict(exc.context)
    field = params.pop("field_key", "")
    return config_issue(
        field, "invalid_config_value", exc.toast_key, lang=lang, params=params
    )


def _billing_issues(exc, lang):
    errors = [config_issue(lang=lang, **issue) for issue in exc.issues]
    for error in errors:
        if error.get("details"):
            localized = []
            for route in error["details"]:
                kind = route.rsplit("/", 1)[-1]
                key = "billing.form." + kind
                label = i18n.t(key, lang=lang)
                localized.append(route + " — " + label if label != key else route)
            error["details"] = localized
    return errors


def _section_number_issues(form, section, lang):
    fields = set()
    if section == "strategies":
        fields = {
            f"strategy_{tier}_{name}"
            for tier in STRATEGY_KEYS
            for name in ("max_files", "max_lines")
        }
    elif section == "context_enhancement":
        from backend.services.section_config_service import _CE_INT_FIELDS

        fields = set(_CE_INT_FIELDS) | {
            "sif_default_context_lines",
            "sif_default_max_results",
            "sif_max_files_to_search",
            "sif_concurrency",
            "sif_max_file_bytes",
            "sif_max_total_scan_bytes",
            "sif_max_matches_per_file",
            "sif_max_output_chars",
            "gt_default_branch_count",
            "gt_default_commit_count",
            "sakura_consolidation_interval",
            "sakura_max_memory_chars",
            "sakura_max_sakura_chars",
            "sakura_min_reflections",
        }
    elif section == "review_policy":
        fields = {"approve_threshold", "block_threshold", "max_major_issues"}
    elif section == "issue_analysis":
        fields = {"max_linked_issues_in_prompt"}
    elif section == "recommendation":
        fields = {"confidence_threshold"}
    issues = []
    for field in sorted(fields):
        if field not in form:
            continue
        try:
            amount = Decimal(str(form[field]))
            if not amount.is_finite() or (
                field != "confidence_threshold" and amount != amount.to_integral_value()
            ):
                raise ValueError
        except InvalidOperation, ValueError, TypeError:
            issues.append(
                config_issue(
                    field, "invalid_number", "toast.numeric_required", lang=lang
                )
            )
    return issues


def _section_field(path, section, form):
    """Map service paths to existing controls, without reflecting arbitrary keys."""
    path = path.removeprefix(section.split(".")[-1] + ".")
    if section == "strategy.strategies":
        bits = path.split(".")
        if bits[0] in STRATEGY_KEYS:
            return f"strategy_{bits[0]}_{bits[-1]}"
    if section == "strategy.context_enhancement":
        if path.startswith("search_in_files."):
            return "sif_" + path.rsplit(".", 1)[-1]
        if path.startswith("git_tools."):
            return "gt_" + path.rsplit(".", 1)[-1]
    if section == "strategy.review_policy" and path.startswith("review_templates."):
        return "template_" + path.rsplit(".", 1)[-1]
    if section == "strategy.pr_dependency_graph":
        return "pr_dependency_graph_mode" if path == "mode" else "depgraph_" + path
    if section == "strategy.pr_summary":
        return "pr_summary_" + path
    if section == "strategy.scan":
        return "scan_" + path
    if section == "strategy.issue_analysis":
        if path.startswith("categories"):
            return "cat_name"
        if path in {"system_prompt", "comment_template", "comment_template_en"}:
            return "issue_" + path
    if section in {"label.definitions", "label.conflict_rules"}:
        import re

        from backend.core.config_sections import get_section_defaults

        definitions = section == "label.definitions"
        prefix = "label_name_" if definitions else "conflict_source_"
        submitted = {}
        for field in form:
            matched = re.fullmatch(re.escape(prefix) + r"([0-9]+)", field)
            if matched is None:
                continue
            row = matched[1]
            name = str(form[field]).strip()
            if not name:
                continue
            if not definitions:
                blocked = str(form.get(f"conflict_blocked_{row}", "")).strip()
                if not any(part.strip() for part in blocked.split(",")):
                    continue
            # Match the handlers' keyed map: the final duplicate owns the value,
            # while its insertion position remains unchanged for deep merging.
            submitted[name] = row

        effective_keys = list(
            dict.fromkeys([*get_section_defaults(section), *submitted])
        )
        indexed = re.fullmatch(r"\[([0-9]+)\](.*)", path)
        if indexed:
            index = int(indexed[1])
            if index >= len(effective_keys):
                return ""
            name, suffix = effective_keys[index], indexed[2]
        else:
            matched = next(
                (
                    (name, path[len(name) :])
                    for name in effective_keys
                    if path == name
                    or path.startswith(name + ".")
                    or (not definitions and path.startswith(name + "["))
                ),
                None,
            )
            if matched is None:
                return ""
            name, suffix = matched
        row = submitted.get(name)
        if row is None:
            return ""
        if definitions:
            control = (
                "label_color_"
                if suffix == ".color"
                else "label_desc_"
                if suffix == ".description"
                else "label_name_"
            )
        else:
            control = "conflict_source_" if suffix == ".name" else "conflict_blocked_"
        # Dynamic names remain local; only a known control and numeric row id
        # cross the response boundary, including custom and invalid label names.
        return control + row
    return path if path in form else ""


def _section_issue(exc, section, form, lang):
    from backend.services.section_config_service import SectionConfigValidationError

    if isinstance(exc, SectionConfigValidationError):
        field = _section_field(exc.path, section, form)
        return config_issue(
            field, exc.code, exc.translation_key, lang=lang, params=exc.params
        )
    return config_issue(
        "", "invalid_config", "section_validation.invalid_config", lang=lang
    )


def _validate_dynamic_config_value(
    key: str,
    raw: object,
    *,
    expected_type: type,
    ranges: dict[str, tuple[float, float | None]],
    select_options: dict[str, list[dict]],
) -> str:
    """按 Settings 的真实字段类型校验并标准化一个动态配置值。

    该 helper 只做纯校验，不访问数据库、缓存或运行时单例；调用方可以先
    对整张表单收集所有结果，再进入任何持久化/热加载路径。
    """
    value = "" if raw is None else str(raw).strip()

    if key in select_options:
        from backend.core.config import MONETARY_CURRENCY_CONFIG_KEYS
        from backend.services.payment.currency_units import normalize_currency

        if key in MONETARY_CURRENCY_CONFIG_KEYS:
            try:
                value = normalize_currency(value)
            except ValueError:
                raise _DynamicConfigValidationError(
                    "toast.value_invalid", field_key=key
                ) from None
        valid_values = [option["value"] for option in select_options[key]]
        if value not in valid_values:
            raise _DynamicConfigValidationError("toast.value_invalid", field_key=key)
        return value

    numeric_value: int | float | None = None
    if expected_type is bool:
        bool_values = {
            "true": "true",
            "false": "false",
            "1": "true",
            "0": "false",
            "yes": "true",
            "no": "false",
        }
        normalized_bool = bool_values.get(value.lower())
        if normalized_bool is None:
            raise _DynamicConfigValidationError("toast.value_invalid", field_key=key)
        return normalized_bool
    if expected_type is int:
        try:
            numeric_value = int(value)
        except ValueError, TypeError:
            raise _DynamicConfigValidationError(
                "toast.numeric_required", field_key=key
            ) from None
    elif expected_type is float:
        try:
            numeric_value = float(value)
        except ValueError, TypeError:
            raise _DynamicConfigValidationError(
                "toast.numeric_required", field_key=key
            ) from None
        if not math.isfinite(numeric_value):
            raise _DynamicConfigValidationError("toast.numeric_required", field_key=key)

    if key in ranges:
        min_value, max_value = ranges[key]
        if numeric_value is None:
            raise _DynamicConfigValidationError("toast.numeric_required", field_key=key)
        if numeric_value < min_value or (
            max_value is not None and numeric_value > max_value
        ):
            if max_value is None:
                raise _DynamicConfigValidationError(
                    "toast.value_min_required",
                    field_key=key,
                    min_v=min_value,
                )
            raise _DynamicConfigValidationError(
                "toast.value_range",
                field_key=key,
                min_v=min_value,
                max_v=max_value,
            )

    return value


@router.post("/save-all")
async def save_all_config(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf_header),
    user_prefs: dict = Depends(get_user_preferences),
):
    """统一保存页右上角唯一保存按钮：一次请求按页面顺序保存全部表单。

    每个逻辑表单转发给既有保存 handler（general/strategies/labels），
    从其 toast_redirect 的 Location 提取 _toast/_toast_type 判定成败，
    聚合为单条 toast 返回 JSON；部分失败时逐项透出结果供前端定位分区。
    """
    lang = _config_language(request, user_prefs)
    try:
        body = await request.json()
    except ValueError, TypeError:
        raise HTTPException(
            status_code=400, detail=i18n.t("toast.config_payload_invalid", lang=lang)
        ) from None
    items = body.get("requests") if isinstance(body, dict) else None
    if not isinstance(items, list):
        raise HTTPException(
            status_code=400, detail=i18n.t("toast.config_payload_invalid", lang=lang)
        )
    if any(
        not isinstance(item, dict)
        or not isinstance(item.get("fields") or {}, dict)
        or (item.get("anchor") is not None and not isinstance(item.get("anchor"), str))
        for item in items
    ):
        raise HTTPException(
            status_code=400, detail=i18n.t("toast.config_payload_invalid", lang=lang)
        )

    # 在函数内解析模块属性，保证可测试性（monkeypatch 生效）
    handlers = {
        "/config/general/save": save_general_config,
        "/config/strategies/save": save_strategies_section,
        "/config/labels/save-labels": save_labels_definitions,
        "/config/labels/save-settings": save_recommendation_settings,
        "/config/labels/save-conflict-rules": save_conflict_rules,
    }

    results: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            raise HTTPException(
                status_code=400,
                detail=i18n.t("toast.config_payload_invalid", lang=lang),
            )
        action = str(item.get("action", ""))
        anchor = item.get("anchor")
        handler = handlers.get(action)
        if handler is None:
            results.append(
                {
                    "action": action,
                    "anchor": anchor,
                    "ok": False,
                    "toast": i18n.t("toast.config_unknown_save_target", lang=lang),
                    "errors": [
                        config_issue(
                            "",
                            "unknown_save_target",
                            "toast.config_unknown_save_target",
                            lang=lang,
                            anchor=anchor,
                        )
                    ],
                }
            )
            continue

        form = _payload_to_form_data(item.get("fields") or {})
        kwargs = (
            {"section": form.get("section", "")}
            if action.endswith("/strategies/save")
            else {}
        )
        try:
            response = await handler(
                _FormOnlyRequest(form, language=lang),
                db=db,
                user=user,
                csrf_token=csrf_token,
                **kwargs,
            )
            if isinstance(response, JSONResponse):
                feedback = json.loads(response.body)
            else:
                query = dict(
                    parse_qsl(urlsplit(response.headers.get("location", "/")).query)
                )
                feedback = {
                    "ok": query.get("_toast_type", "success") != "error",
                    "toast": query.get("_toast", ""),
                    "errors": json.loads(query.get("_errors", "[]")),
                }
            results.append(
                {
                    "action": action,
                    "anchor": anchor,
                    "ok": bool(feedback.get("ok")) and response.status_code < 400,
                    "toast": feedback.get("toast", ""),
                    "errors": feedback.get("errors", []),
                }
            )
        except HTTPException as exc:
            results.append(
                {
                    "action": action,
                    "anchor": anchor,
                    "ok": False,
                    "toast": i18n.t(
                        "toast.config_save_http_failed",
                        lang=lang,
                        status=exc.status_code,
                    ),
                    "errors": [
                        config_issue(
                            "",
                            "save_request_failed",
                            "toast.config_save_http_failed",
                            lang=lang,
                            params={"status": exc.status_code},
                            anchor=anchor,
                        )
                    ],
                }
            )
        except Exception as exc:
            # 批量保存不因单项异常中断：记录失败并继续后续分区
            logger.error(f"save-all [{action}] 保存异常: {exc}", exc_info=True)
            results.append(
                {
                    "action": action,
                    "anchor": anchor,
                    "ok": False,
                    "toast": i18n.t("toast.save_failed", lang=lang),
                    "errors": [
                        config_issue(
                            "",
                            "save_failed",
                            "toast.save_failed",
                            lang=lang,
                            anchor=anchor,
                        )
                    ],
                }
            )

    errors = [issue for result in results for issue in result.get("errors", [])]
    failed = [result for result in results if not result["ok"]]
    if failed:
        message = i18n.t(
            "toast.save_all_partial",
            lang=lang,
            count=len(failed),
            error=(
                failed[0]["errors"][0]["message"]
                if failed[0].get("errors")
                else failed[0]["toast"]
            ),
        )
        return JSONResponse(
            {"ok": False, "toast": message, "results": results, "errors": errors}
        )
    message = i18n.t("toast.save_all_success", lang=lang, count=len(results))
    return JSONResponse(
        {"ok": True, "toast": message, "results": results, "errors": []}
    )


# ========== GET: AI 账号配置页 ==========


@router.get("/ai")
async def ai_config_page(
    request: Request,
    user: dict = Depends(require_super_admin),
    user_prefs: dict = Depends(get_user_preferences),
):
    """AI 提供商账号配置页（多厂商持久化、随时切换、故障转移链）."""
    from backend.webui.routes.auth import APP_VERSION

    return render_template(
        "config_ai.html",
        request,
        user_prefs=user_prefs,
        current_user=user,
        csrf_token=get_csrf_serializer().dumps({}),
        active_page="config_ai",
        app_version=APP_VERSION,
    )


# ========== 配置备份与恢复 ==========


@router.get("/backup")
async def config_backup_page(
    request: Request,
    user: dict = Depends(require_super_admin),
    user_prefs: dict = Depends(get_user_preferences),
):
    """配置备份页面。"""
    return render_template(
        "config_backup.html",
        request,
        user_prefs=user_prefs,
        current_user=user,
        csrf_token=get_csrf_serializer().dumps({}),
        active_page="config_backup",
    )


@router.post("/backup/export/users")
async def download_user_backup(
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
    user_prefs: dict = Depends(get_user_preferences),
):
    """下载全部用户及个人配置、两步验证和通行密钥的 JSON 备份。"""
    lang = _config_language(None, user_prefs)
    try:
        document = await export_user_backup(db)
        content = serialize_user_backup(document)
        counts = {
            "users": document.get("user_count", len(document.get("users", []))),
            "personal_configs": sum(
                len(item.get("personal_config", {}).get("dynamic_overrides", []))
                for item in document.get("users", [])
            ),
            "recovery_codes": sum(
                len(item.get("two_factor", {}).get("recovery_codes", []))
                for item in document.get("users", [])
            ),
            "passkeys": sum(
                len(item.get("passkeys", [])) for item in document.get("users", [])
            ),
        }
        await log_admin_action(
            db,
            user["user_id"],
            "user_export",
            "users",
            "all",
            {"scope": "users", "counts": counts},
        )

        timestamp = filename_timestamp()
        filename = f"sakura-ai-users-{timestamp}.json"
        logger.info(
            "用户信息备份已导出, by={}, counts={}",
            user["sub"],
            counts,
        )
        return Response(
            content=content,
            media_type="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "no-store, max-age=0",
                "Pragma": "no-cache",
                "X-Content-Type-Options": "nosniff",
            },
        )
    except UserBackupError as exc:
        return toast_redirect(
            "/config/backup",
            "toast.user_backup_export_failed",
            "error",
            lang=lang,
            reason=str(exc),
        )
    except Exception as exc:
        logger.error("用户信息备份导出失败: {}", exc, exc_info=True)
        return toast_redirect(
            "/config/backup",
            "toast.user_backup_export_failed",
            "error",
            lang=lang,
            reason="internal error",
        )


@router.post("/backup/export/{scope}")
async def download_config_backup(
    scope: str,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
    user_prefs: dict = Depends(get_user_preferences),
):
    """下载全局、AI、系统配置或完整的版本化 JSON 备份。"""
    lang = _config_language(None, user_prefs)
    try:
        document = await export_config_backup(db, scope)
        content = serialize_config_backup(document)
        counts = {
            section: data["count"] for section, data in document["sections"].items()
        }
        await log_admin_action(
            db,
            user["user_id"],
            "config_export",
            "config",
            scope,
            {"scope": scope, "counts": counts},
        )

        timestamp = filename_timestamp()
        filename = f"sakura-ai-config-{scope}-{timestamp}.json"
        logger.info(
            "配置备份已导出, by={}, scope={}, counts={}",
            user["sub"],
            scope,
            counts,
        )
        return Response(
            content=content,
            media_type="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "no-store, max-age=0",
                "Pragma": "no-cache",
                "X-Content-Type-Options": "nosniff",
            },
        )
    except ConfigBackupError as exc:
        return toast_redirect(
            "/config/backup",
            "toast.config_backup_export_failed",
            "error",
            lang=lang,
            reason=str(exc),
        )
    except Exception as exc:
        logger.error("配置备份导出失败: {}", exc, exc_info=True)
        return toast_redirect(
            "/config/backup",
            "toast.config_backup_export_failed",
            "error",
            lang=lang,
            reason="internal error",
        )


@router.post("/backup/import")
async def upload_config_backup(
    backup_file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
    user_prefs: dict = Depends(get_user_preferences),
):
    """校验并精确恢复备份中包含的配置分类。"""
    lang = _config_language(None, user_prefs)
    result = None
    try:
        content = await backup_file.read(BACKUP_MAX_BYTES + 1)
        sections = parse_config_backup(content)
        # A running deployment cannot atomically commit a new database URL to
        # both AppConfig and connection.json.  Keep the live restore scoped to
        # runtime-safe settings and direct database moves through Setup.
        result = await restore_config_backup(
            db,
            sections,
            allow_database_url=False,
        )
        runtime_refresh_ok = True
        try:
            refresh_imported_runtime_config(result)
        except Exception as exc:
            runtime_refresh_ok = False
            logger.error(
                "配置已导入，但运行时配置刷新失败，需重启应用: {}",
                exc,
                exc_info=True,
            )

        safe_filename = Path(backup_file.filename or "backup.json").name[:255]
        detail = {
            "filename": safe_filename,
            "sections": list(result.sections),
            "created": result.created,
            "updated": result.updated,
            "deleted": result.deleted,
            "unchanged": result.unchanged,
            "runtime_refresh_ok": runtime_refresh_ok,
            "requires_restart": result.requires_restart,
        }
        await log_admin_action(
            db,
            user["user_id"],
            "config_import",
            "config",
            ",".join(result.sections),
            detail,
        )
        logger.info(
            "配置备份已导入, by={}, sections={}, created={}, updated={}, deleted={}",
            user["sub"],
            result.sections,
            result.created,
            result.updated,
            result.deleted,
        )
        from backend.webui.i18n import i18n as _i18n

        section_names = ", ".join(
            _i18n.t(f"config.backup_{section}", lang=lang)
            for section in result.sections
        )
        return toast_redirect(
            "/config/backup",
            (
                "toast.config_backup_imported_restart"
                if not runtime_refresh_ok
                else (
                    "toast.config_backup_imported_restart_required"
                    if result.requires_restart
                    else "toast.config_backup_imported"
                )
            ),
            lang=lang,
            sections=section_names,
            created=result.created,
            updated=result.updated,
            deleted=result.deleted,
            unchanged=result.unchanged,
        )
    except ConfigBackupError as exc:
        return toast_redirect(
            "/config/backup",
            "toast.config_backup_invalid",
            "error",
            lang=lang,
            reason=str(exc),
        )
    except Exception as exc:
        logger.error("配置备份导入失败: {}", exc, exc_info=True)
        if result is not None:
            return toast_redirect(
                "/config/backup",
                "toast.config_backup_imported_restart",
                "error",
                lang=lang,
                sections=", ".join(result.sections),
                created=result.created,
                updated=result.updated,
                deleted=result.deleted,
                unchanged=result.unchanged,
            )
        await db.rollback()
        return toast_redirect(
            "/config/backup",
            "toast.config_backup_import_failed",
            "error",
            lang=lang,
        )
    finally:
        await backup_file.close()


@router.post("/backup/users/import")
async def upload_user_backup(
    backup_file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
    user_prefs: dict = Depends(get_user_preferences),
):
    """校验并合并全部用户及其受支持的安全信息。"""
    lang = _config_language(None, user_prefs)
    try:
        content = await backup_file.read(USER_BACKUP_MAX_BYTES + 1)
        document = parse_user_backup(content)
        result = await restore_user_backup(db, document)

        safe_filename = Path(backup_file.filename or "users.json").name[:255]
        detail = {
            "filename": safe_filename,
            "users_created": result.users_created,
            "users_updated": result.users_updated,
            "users_unchanged": result.users_unchanged,
            "user_configs_created": result.user_configs_created,
            "user_configs_updated": result.user_configs_updated,
            "user_configs_deleted": result.user_configs_deleted,
            "webui_configs_created": result.webui_configs_created,
            "webui_configs_updated": result.webui_configs_updated,
            "webui_configs_deleted": result.webui_configs_deleted,
            "recovery_codes_imported": result.recovery_codes_imported,
            "passkeys_created": result.passkeys_created,
            "passkeys_updated": result.passkeys_updated,
            "recovery_codes_portable": result.recovery_codes_portable,
        }
        await log_admin_action(
            db,
            user["user_id"],
            "user_import",
            "users",
            "all",
            detail,
        )
        from backend.webui.deps import invalidate_user_prefs_cache

        for user_id in result.affected_user_ids:
            invalidate_user_prefs_cache(user_id)

        logger.info(
            "用户信息备份已导入, by={}, users_created={}, users_updated={}, passkeys={}, recovery_codes_portable={}",
            user["sub"],
            result.users_created,
            result.users_updated,
            result.passkeys_imported,
            result.recovery_codes_portable,
        )
        message_key = (
            "toast.user_backup_imported"
            if result.recovery_codes_portable
            else "toast.user_backup_imported_warning"
        )
        return toast_redirect(
            "/config/backup",
            message_key,
            "success",
            lang=lang,
            users_created=result.users_created,
            users_updated=result.users_updated,
            configs_created=result.user_configs_created,
            configs_updated=result.user_configs_updated,
            passkeys=result.passkeys_imported,
            recovery_codes=result.recovery_codes_imported,
        )
    except UserBackupError as exc:
        return toast_redirect(
            "/config/backup",
            "toast.user_backup_invalid",
            "error",
            lang=lang,
            reason=str(exc),
        )
    except Exception as exc:
        logger.error("用户信息备份导入失败: {}", exc, exc_info=True)
        await db.rollback()
        return toast_redirect(
            "/config/backup",
            "toast.user_backup_import_failed",
            "error",
            lang=lang,
        )
    finally:
        await backup_file.close()


async def _build_dynamic_groups(db: AsyncSession, lang: str) -> list[dict]:
    """读取 AppConfig 并组装动态配置分组数据（平铺键卡片渲染上下文）。

    Build dynamic config group context shared by the unified config page
    (labels/descriptions resolve via i18n with DYNAMIC_* fallbacks).
    """
    from backend.core.config import (
        DYNAMIC_CONFIG_GROUPS,
        DYNAMIC_CONFIG_LABELS,
        DYNAMIC_CONFIG_RANGES,
        DYNAMIC_CONFIG_SELECT_OPTIONS,
        DYNAMIC_CONFIG_SENSITIVE_KEYS,
        MONETARY_CURRENCY_CONFIG_KEYS,
        _get_field_type,
        get_dynamic_config_input_type,
        get_settings,
        mask_sensitive_value,
    )
    from backend.webui.i18n import i18n as _i18n

    result = await db.execute(select(AppConfig).order_by(AppConfig.id))
    configs = result.scalars().all()
    config_map = {c.key_name: c.key_value for c in configs}

    settings = get_settings()
    dynamic_groups = []
    for group_id, group_data in DYNAMIC_CONFIG_GROUPS.items():
        items = []
        for key in group_data["keys"]:
            default_val = _settings_default_to_str(getattr(settings, key, ""))
            value = config_map.get(key, default_val)
            if key in MONETARY_CURRENCY_CONFIG_KEYS:
                # Render valid historical lowercase codes without rewriting data.
                value = value.upper()
            input_type = get_dynamic_config_input_type(key)
            is_sensitive = key in DYNAMIC_CONFIG_SENSITIVE_KEYS

            display_value = (
                mask_sensitive_value(value) if (is_sensitive and value) else value
            )

            # Translate select options via i18n
            raw_options = DYNAMIC_CONFIG_SELECT_OPTIONS.get(key, [])
            translated_options = []
            for opt in raw_options:
                opt_key = f"config.option.{key}_{opt['value']}"
                opt_label = _i18n.t(opt_key, lang=lang)
                # Fallback to original label if key not found
                translated_options.append(
                    {
                        "value": opt["value"],
                        "label": opt_label if opt_key != opt_label else opt["label"],
                    }
                )
            if key in MONETARY_CURRENCY_CONFIG_KEYS and value not in {
                option["value"] for option in translated_options
            }:
                translated_options.append(
                    {
                        "value": value,
                        "label": value
                        + " · "
                        + _i18n.t("billing.unsupported_currency", lang=lang),
                        "disabled": True,
                    }
                )

            items.append(
                {
                    "key": key,
                    "label": (
                        translated_label
                        if (
                            translated_label := _i18n.t(
                                f"config.label.{key}", lang=lang
                            )
                        )
                        != f"config.label.{key}"
                        else DYNAMIC_CONFIG_LABELS.get(key, key)
                    ),
                    "description": (
                        ""
                        if not group_data.get("descriptions", {}).get(key)
                        else (
                            translated
                            if (translated := _i18n.t(f"config.desc.{key}", lang=lang))
                            != f"config.desc.{key}"
                            else group_data["descriptions"][key]
                        )
                    ),
                    "input_type": input_type,
                    "step": "any" if _get_field_type(key) is float else "1",
                    "value": display_value,
                    "default": mask_sensitive_value(default_val)
                    if (is_sensitive and default_val)
                    else default_val,
                    "sensitive": is_sensitive,
                    "select_options": translated_options,
                    "min_val": DYNAMIC_CONFIG_RANGES.get(key, (None, None))[0],
                    "max_val": DYNAMIC_CONFIG_RANGES.get(key, (None, None))[1],
                }
            )
        dynamic_groups.append(
            {
                "id": group_id,
                "label": (
                    translated_group
                    if (
                        translated_group := _i18n.t(
                            f"config.group.{group_id}", lang=lang
                        )
                    )
                    != f"config.group.{group_id}"
                    else group_data["label"]
                ),
                "icon": group_data.get("icon", ""),
                "fields": items,
            }
        )
    return dynamic_groups


@router.get("")
async def unified_config_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    user_prefs: dict = Depends(get_user_preferences),
):
    """全局配置页：平铺动态配置组 + 策略/标签节表单单页呈现。"""
    lang = detect_language(user_prefs)
    dynamic_groups = await _build_dynamic_groups(db, lang)

    strategy_data = get_sections_for_target("strategy")
    pr_dependency_graph = dict(strategy_data.get("pr_dependency_graph", {}))
    pr_dependency_graph["mode"] = await section_config_service.resolve_depgraph_mode()
    label_data = get_sections_for_target("label")

    return render_template(
        "config_unified.html",
        request,
        user_prefs=user_prefs,
        current_user=user,
        csrf_token=get_csrf_serializer().dumps({}),
        active_page="config_unified",
        dynamic_groups=dynamic_groups,
        strategies=strategy_data.get("strategies", {}),
        file_filters=strategy_data.get("file_filters", {}),
        context_enhancement=strategy_data.get("context_enhancement", {}),
        review_policy=strategy_data.get("review_policy", {}),
        pr_dependency_graph=pr_dependency_graph,
        issue_analysis=strategy_data.get("issue_analysis", {}),
        pr_summary=strategy_data.get("pr_summary", {}),
        scan=strategy_data.get("scan", {}),
        labels=label_data.get("labels", {}),
        recommendation=label_data.get("recommendation", {}),
        conflict_rules=label_data.get("conflict_rules", {}),
    )


@router.get("/agent-network-status", response_class=JSONResponse)
async def agent_network_status(
    user: dict = Depends(require_super_admin),
):
    """Return a safe readiness/capability projection for the Agent policy UI.

    This endpoint deliberately omits the deployment-owned Docker network name.
    Policy and backend are read from the database on every request so a save in
    another Web worker is visible immediately.
    """

    del user
    try:
        policy_state = await get_agent_team_network_policy_state()
        backend_value = await get_dynamic_config_fresh("agent_team_execution_backend")
        backend = validate_execution_backend(
            str(backend_value or ""),
            deploy_mode=str(getattr(get_settings(), "sakura_deploy_mode", "unknown")),
        )
    except Exception as exc:
        logger.bind(error_type=type(exc).__name__).warning(
            "agent network status is unavailable"
        )
        return JSONResponse(
            {
                "backend": "unavailable",
                "backend_ready": False,
                "sandbox_ready": False,
                "egress_capability": "unavailable",
                "egress_available": False,
                "policy": "unavailable",
                "policy_revision": "unavailable",
                "full_access_risk": False,
                "local_host_network": False,
                "agent_network_mode": "unavailable",
                "dependency_network_mode": "unavailable",
                "dependency_egress_available": False,
            },
            status_code=503,
        )

    policy = policy_state.policy
    sandbox_ready = False
    local_host_network = backend == "local"
    # A local runner has no sandbox egress capability: its network boundary
    # is the host process itself.  Keep that distinction explicit so the UI
    # does not report a missing sandbox capability as a local readiness
    # failure.
    egress_capability = "not_applicable" if local_host_network else "unavailable"
    egress_available: bool | None = None if local_host_network else False
    agent_network_mode = "not_applicable"
    dependency_network_mode = "not_applicable"
    if backend == "sandbox":
        agent_network_mode = network_mode_for_policy(
            policy, capability=NetworkCapability.NONE
        )
        dependency_network_mode = network_mode_for_policy(policy, profile="dependency")
        sandbox_status = await read_sandbox_capability_status()
        sandbox_ready = bool(sandbox_status.get("available"))
        egress_capability = str(sandbox_status.get("egress_capability", "unavailable"))
        egress_available = egress_capability == "egress"

    if backend == "local":
        # Local execution is deliberately source-only and host-unrestricted;
        # it is ready only for the policy that explicitly permits that risk.
        backend_ready = policy is AgentTeamNetworkPolicy.FULL_ACCESS
    else:
        backend_ready = sandbox_ready and (
            policy is not AgentTeamNetworkPolicy.FULL_ACCESS or egress_available
        )
    return JSONResponse(
        {
            "backend": backend,
            "backend_ready": backend_ready,
            "sandbox_ready": sandbox_ready,
            "egress_capability": egress_capability,
            "egress_available": egress_available,
            "policy": policy.value,
            "policy_revision": policy_state.revision,
            "full_access_risk": policy is AgentTeamNetworkPolicy.FULL_ACCESS,
            "local_host_network": local_host_network,
            "agent_network_mode": agent_network_mode,
            "dependency_network_mode": dependency_network_mode,
            "dependency_egress_available": bool(
                sandbox_ready
                and egress_available
                and dependency_network_mode == "egress"
            ),
        }
    )


@router.get("/general")
async def general_config_page(
    user: dict = Depends(require_super_admin),
):
    return RedirectResponse(url="/config#section-basic", status_code=302)


# ========== POST: 保存全局配置 ==========


@router.post("/general/save")
async def save_general_config(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
    user_prefs: dict = Depends(get_user_preferences),
):
    """保存全局配置页的全部平铺键（通用逐键 upsert 循环）。

    review_basic / web_search 等原手写键段已注册进 DYNAMIC_CONFIG_GROUPS，
    与其余动态组共用同一循环；bool 未勾选时表单不提交，按 Settings
    字段类型回填 "false"（与既有 checkbox 语义一致）。
    """
    lang = _config_language(request, user_prefs)
    try:
        form = await request.form()

        # ========== 动态配置保存（覆盖全部 DYNAMIC 组键） ==========
        from backend.core.config import (
            DYNAMIC_CONFIG_GROUPS,
            DYNAMIC_CONFIG_RANGES,
            DYNAMIC_CONFIG_SELECT_OPTIONS,
            DYNAMIC_CONFIG_SENSITIVE_KEYS,
            _get_field_type,
        )
        from backend.core.config import (
            mask_sensitive_value as _mask,
        )

        # 先完成整张表单的纯校验与标准化。任何字段失败都在数据库查询、
        # ORM 对象修改、缓存失效和 Settings/信号量热加载之前返回。
        validated_values: list[tuple[str, str, bool]] = []
        errors = []
        for group_data in DYNAMIC_CONFIG_GROUPS.values():
            for key in group_data["keys"]:
                is_sensitive = key in DYNAMIC_CONFIG_SENSITIVE_KEYS

                # 敏感字段：检查 _changed 标记
                if is_sensitive:
                    changed_flag = form.get(f"{key}_changed")
                    if changed_flag != "true":
                        continue

                raw = form.get(key)
                if raw is None:
                    # boolean 字段未勾选时表单不提交
                    # 从 Settings 获取类型判断
                    if _get_field_type(key) is bool:
                        raw = "false"
                    else:
                        continue

                try:
                    val = _validate_dynamic_config_value(
                        key,
                        raw,
                        expected_type=_get_field_type(key),
                        ranges=DYNAMIC_CONFIG_RANGES,
                        select_options=DYNAMIC_CONFIG_SELECT_OPTIONS,
                    )
                except _DynamicConfigValidationError as exc:
                    errors.append(_dynamic_issue(exc, lang))
                    continue

                validated_values.append((key, val, is_sensitive))

        if errors:
            return config_save_response(
                request,
                "/config",
                "toast.config_fields_invalid",
                lang=lang,
                errors=errors,
            )
        changed = {}
        from backend.services.billing_configuration_service import (
            BillingConfigurationError,
            validate_billing_configuration,
        )
        from backend.services.billing_service import BillingError

        try:
            await validate_billing_configuration(
                db, {key: val for key, val, _ in validated_values}
            )
        except BillingConfigurationError as exc:
            errors.extend(_billing_issues(exc, lang))
        except BillingError, ValueError, TypeError:
            errors.append(
                config_issue(
                    "billing_enabled",
                    "invalid_billing_config",
                    "toast.value_invalid",
                    lang=lang,
                )
            )
        if errors:
            return config_save_response(
                request,
                "/config",
                "toast.config_fields_invalid",
                lang=lang,
                errors=errors,
            )
        for key, val, is_sensitive in validated_values:
            # 保存
            result = await db.execute(
                select(AppConfig).where(AppConfig.key_name == key)
            )
            cfg = result.scalar_one_or_none()
            if cfg is None:
                # 首次创建
                cfg = AppConfig(key_name=key, key_value=val, description=key)
                db.add(cfg)
                changed[key] = {
                    "old": "(无)",
                    "new": _mask(val) if is_sensitive else val,
                    "raw_new": val,
                }
            elif cfg.key_value != val:
                if is_sensitive:
                    changed[key] = {
                        "old": _mask(cfg.key_value),
                        "new": _mask(val),
                        "raw_new": val,
                    }
                else:
                    changed[key] = {
                        "old": cfg.key_value,
                        "new": val,
                        "raw_new": val,
                    }
                cfg.key_value = val

        if not changed:
            return config_save_response(
                request,
                "/config",
                "toast.config_saved_restart",
                lang=lang,
            )

        await db.commit()

        # 清除动态配置缓存 + 同步 Settings 单例
        from backend.core.config import (
            get_all_dynamic_config_keys,
            invalidate_dynamic_config_cache,
            update_settings_field,
        )

        all_dynamic_keys = get_all_dynamic_config_keys()
        invalidate_dynamic_config_cache(all_dynamic_keys)

        # 即时更新 Settings 单例，无需重启
        for key, change in changed.items():
            if key in all_dynamic_keys or key in BASIC_CONFIG_KEYS:
                update_settings_field(key, change.get("raw_new", change["new"]))

        # 即时重置信号量，使并发配置立即生效
        if "max_concurrent_issues" in changed:
            # 延迟导入避免 config ↔ worker 循环引用
            from backend.workers.issue_worker import reset_issue_semaphore

            reset_issue_semaphore()

        if "max_concurrent_reviews" in changed:
            # 延迟导入避免 config ↔ worker 循环引用
            from backend.workers.review_worker import reset_review_semaphore

            reset_review_semaphore()

        if "star_aid_scheduler_enabled" in changed:
            from backend.services.star_aid_scheduler import get_star_aid_scheduler

            star_sched = get_star_aid_scheduler()
            if star_sched is not None:
                star_sched.restart_if_needed()

        logger.info(f"全局配置已更新, by={user['sub']}, changed={list(changed.keys())}")
        # 构建脱敏日志副本（不包含 raw_new 明文，并对敏感键二次脱敏防御）
        log_changed = {}
        for k, v in changed.items():
            log_entry = {"old": v["old"], "new": v["new"]}
            if k in DYNAMIC_CONFIG_SENSITIVE_KEYS:
                log_entry["old"] = _mask(str(log_entry["old"]))
                log_entry["new"] = _mask(str(log_entry["new"]))
            log_changed[k] = log_entry
        await log_admin_action(
            db, user["user_id"], "config_save", "global", None, log_changed
        )
        return config_save_response(
            request, "/config", "toast.config_saved_live", lang=lang
        )

    except ValueError:
        return config_save_response(
            request,
            "/config",
            "toast.config_fields_invalid",
            lang=lang,
            errors=[
                config_issue(
                    "", "invalid_config_value", "toast.invalid_param", lang=lang
                )
            ],
        )
    except Exception as e:
        logger.error(f"全局配置保存失败: {e}", exc_info=True)
        await db.rollback()
        return config_save_response(
            request,
            "/config",
            "toast.save_failed",
            lang=lang,
            errors=[config_issue("", "save_failed", "toast.save_failed", lang=lang)],
        )
