"""WebUI 配置管理路由（超级管理员专用）"""

import asyncio
import re
import shutil
import tempfile
from pathlib import Path

import yaml
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.config import (
    BASIC_CONFIG_KEYS,
    DYNAMIC_CONFIG_RANGES,
    get_dynamic_config,
    get_label_config,
    get_strategy_config,
    invalidate_dynamic_config_cache,
    reload_label_config,
    reload_strategy_config,
)
from backend.models.database import AppConfig
from backend.services.label_service import label_service
from backend.webui.deps import (
    get_csrf_serializer,
    get_db,
    get_templates,
    get_user_preferences,
    render_template,
    require_csrf,
    require_super_admin,
    toast_redirect,
)
from backend.webui.helpers.admin_log import log_admin_action
from backend.webui.i18n import detect_language

router = APIRouter(prefix="/config", tags=["WebUI Config"])
templates = get_templates()

STRATEGIES_PATH = Path("config/strategies.yaml")
LABELS_PATH = Path("config/labels.yaml")

STRATEGY_KEYS = ["quick", "standard", "deep", "large"]

# 标签验证规则（匹配 GitHub 标签命名规范）
_LABEL_NAME_RE = re.compile(r"^[a-zA-Z0-9.\-_/ ]+$")
_LABEL_COLOR_RE = re.compile(r"^[0-9a-fA-F]{6}$")
_MAX_LABEL_NAME_LEN = 100

# 按配置文件路径 keyed 的异步锁，防止并发读-改-写竞态
_config_locks: dict[str, asyncio.Lock] = {}


def _validate_label(name: str, color: str):
    """验证标签名称和颜色格式，不合法时抛出 ValueError"""
    if len(name) > _MAX_LABEL_NAME_LEN:
        raise ValueError(
            f"标签名称过长（最多 {_MAX_LABEL_NAME_LEN} 字符）: {name[:20]}..."
        )
    if not _LABEL_NAME_RE.match(name):
        raise ValueError(f"标签名称包含非法字符: {name}")
    if not _LABEL_COLOR_RE.match(color):
        raise ValueError(f"颜色值格式错误（需 6 位十六进制）: {color}")


def _get_config_lock(path: str) -> asyncio.Lock:
    """获取指定配置文件的异步锁（单例，防止并发 TOCTOU）"""
    lock = _config_locks.setdefault(path, asyncio.Lock())
    if len(_config_locks) > 100:
        # 只清理未被占用的锁，保留活跃锁
        cleaned = {k: v for k, v in _config_locks.items() if v.locked()}
        if path not in cleaned:
            cleaned[path] = lock
        _config_locks.clear()
        _config_locks.update(cleaned)
    return lock


def _parse_positive_int_config(raw: object) -> int:
    """解析正整数配置值"""
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid integer config value") from exc
    if value < 1:
        raise ValueError("integer config value must be positive")
    return value


def _atomic_yaml_write(path: Path, full_config: dict):
    """原子写入 YAML 配置文件

    先写临时文件，再 rename，确保不会因写入中途出错而损坏配置。
    """
    yaml_str = yaml.dump(
        full_config, allow_unicode=True, default_flow_style=False, sort_keys=False
    )
    # round-trip 验证
    parsed = yaml.safe_load(yaml_str)
    if parsed is None:
        raise ValueError("YAML 序列化验证失败")

    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent), suffix=".tmp", prefix=f"{path.stem}_"
    )
    try:
        with open(fd, "w", encoding="utf-8") as f:
            f.write(yaml_str)
        shutil.move(tmp_path, str(path))
    except Exception:
        Path(tmp_path).unlink(missing_ok=True)
        raise


def _load_yaml(path: Path) -> dict:
    """加载 YAML 文件"""
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# ========== GET: 策略配置页 ==========


@router.get("/strategies")
async def strategies_page(
    request: Request,
    user: dict = Depends(require_super_admin),
    user_prefs: dict = Depends(get_user_preferences),
):
    """渲染审查策略配置页"""
    config_data = get_strategy_config().config
    tab = request.query_params.get("tab", "strategies")
    pr_dependency_graph = dict(config_data.get("pr_dependency_graph", {}))
    pr_dependency_graph["mode"] = await get_dynamic_config("pr_dependency_graph_mode")

    return render_template(
        "config_strategies.html",
        request,
        user_prefs=user_prefs,
        current_user=user,
        csrf_token=get_csrf_serializer().dumps({}),
        active_page="config_strategies",
        strategies=config_data.get("strategies", {}),
        file_filters=config_data.get("file_filters", {}),
        context_enhancement=config_data.get("context_enhancement", {}),
        review_policy=config_data.get("review_policy", {}),
        pr_dependency_graph=pr_dependency_graph,
        issue_analysis=config_data.get("issue_analysis", {}),
        active_tab=tab,
    )


# ========== POST: 保存策略配置 ==========


@router.post("/strategies/save")
async def save_strategies_section(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
    section: str = Form(...),
):
    """保存策略配置的某个 section"""
    try:
        form = await request.form()
        lock = _get_config_lock(str(STRATEGIES_PATH))
        async with lock:
            config = _load_yaml(STRATEGIES_PATH)

            if section == "strategies":
                # 收集 4 个策略的 conditions 和 prompt
                strategies = {}
                for key in STRATEGY_KEYS:
                    name = form.get(f"strategy_{key}_name", key)
                    try:
                        max_files = int(form.get(f"strategy_{key}_max_files", 999999))
                        max_lines = int(form.get(f"strategy_{key}_max_lines", 99999999))
                    except (ValueError, TypeError) as e:
                        raise ValueError(f"[{key}] 数值格式错误: {e}")
                    if not 1 <= max_files <= 100000:
                        raise ValueError(
                            f"[{key}] max_files 须在 1-100000 之间: {max_files}"
                        )
                    if not 1 <= max_lines <= 10000000:
                        raise ValueError(
                            f"[{key}] max_lines 须在 1-10000000 之间: {max_lines}"
                        )
                    prompt = form.get(f"strategy_{key}_prompt", "")
                    strategies[key] = {
                        "name": name,
                        "conditions": {"max_files": max_files, "max_lines": max_lines},
                        "prompt": prompt,
                    }
                config["strategies"] = strategies

            elif section == "file_filters":
                skip_ext_raw = form.get("skip_extensions", "")
                skip_paths_raw = form.get("skip_paths", "")
                code_ext_raw = form.get("code_extensions", "")
                config["file_filters"] = {
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
                config["context_enhancement"] = {
                    "enable_project_structure": form.get("enable_project_structure")
                    is not None,
                    "max_structure_files": int(form.get("max_structure_files", 500)),
                    "enable_ai_tools": form.get("enable_ai_tools") is not None,
                    "max_tool_iterations": int(form.get("max_tool_iterations", 20)),
                    "max_file_size": int(form.get("max_file_size", 200000)),
                    "max_files_for_deep_strategy": int(
                        form.get("max_files_for_deep_strategy", 10)
                    ),
                    "max_file_lines": int(float(form.get("max_file_lines", 500))),
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
                    },
                    "git_tools": {
                        "default_branch_count": int(
                            float(form.get("gt_default_branch_count", 20))
                        ),
                        "default_commit_count": int(
                            float(form.get("gt_default_commit_count", 10))
                        ),
                    },
                }

            elif section == "review_policy":
                config["review_policy"] = {
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
                    "repo_overrides": config.get("review_policy", {}).get(
                        "repo_overrides", {}
                    ),
                    "enable_idempotency_check": form.get("enable_idempotency_check")
                    is not None,
                    "review_templates": {
                        "approve": form.get("template_approve", ""),
                        "request_changes": form.get("template_request_changes", ""),
                        "comment": form.get("template_comment", ""),
                    },
                }

            elif section == "depgraph":
                depgraph_mode = form.get("pr_dependency_graph_mode", "ai")
                if depgraph_mode not in {"ai", "static"}:
                    depgraph_mode = "ai"
                # mode 是动态配置项，仅写入数据库；YAML 只保存 prompt 模板，避免双写不一致。
                config["pr_dependency_graph"] = {
                    "system_prompt": form.get("depgraph_system_prompt", ""),
                    "user_template": form.get("depgraph_user_template", ""),
                }
                existing = await db.execute(
                    select(AppConfig).where(
                        AppConfig.key_name == "pr_dependency_graph_mode"
                    )
                )
                app_config = existing.scalar_one_or_none()
                if app_config:
                    app_config.key_value = depgraph_mode
                else:
                    db.add(
                        AppConfig(
                            key_name="pr_dependency_graph_mode",
                            key_value=depgraph_mode,
                            description="PR dependency graph generation mode",
                        )
                    )

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

                config["issue_analysis"] = {
                    "categories": categories,
                    "priority_rules": priority_rules,
                    "issue_reference_keywords": ref_keywords,
                    "max_linked_issues_in_prompt": max_linked,
                    "system_prompt": form.get("issue_system_prompt", ""),
                    "comment_template": form.get("issue_comment_template", ""),
                    "comment_template_en": form.get("issue_comment_template_en", ""),
                }
            else:
                raise HTTPException(status_code=400, detail=f"未知 section: {section}")

            if section == "depgraph":
                await db.commit()
                invalidate_dynamic_config_cache(["pr_dependency_graph_mode"])
            _atomic_yaml_write(STRATEGIES_PATH, config)
            reload_strategy_config()
            logger.info(f"策略配置 [{section}] 已更新, by={user['sub']}")
            await log_admin_action(
                db, user["user_id"], "config_save", "strategy", section
            )

    except (ValueError, yaml.YAMLError) as e:
        logger.error(f"配置验证失败: {e}")
        return toast_redirect(
            f"/config/strategies?tab={section}",
            "toast.config_validation_failed",
            "error",
            lang=detect_language(),
            error=str(e),
        )
    except PermissionError as e:
        logger.error(f"文件权限不足: {e}")
        return toast_redirect(
            f"/config/strategies?tab={section}",
            "toast.file_permission_denied",
            "error",
            lang=detect_language(),
        )
    except Exception as e:
        logger.error(f"策略配置保存异常: {e}", exc_info=True)
        return toast_redirect(
            f"/config/strategies?tab={section}",
            "toast.save_failed",
            "error",
            lang=detect_language(),
        )

    return toast_redirect(
        f"/config/strategies?tab={section}",
        "toast.strategy_saved",
        lang=detect_language(),
        section=section,
    )


# ========== GET: 标签配置页 ==========


@router.get("/labels")
async def labels_page(
    request: Request,
    user: dict = Depends(require_super_admin),
    user_prefs: dict = Depends(get_user_preferences),
):
    """渲染标签配置页"""
    label_config = get_label_config()
    return render_template(
        "config_labels.html",
        request,
        user_prefs=user_prefs,
        current_user=user,
        csrf_token=get_csrf_serializer().dumps({}),
        active_page="config_labels",
        labels=label_config.get_labels(),
        recommendation=label_config.get_recommendation_settings(),
        conflict_rules=label_config.get_conflict_rules(),
    )


# ========== POST: 保存标签定义 ==========


@router.post("/labels/save-labels")
async def save_labels_definitions(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
):
    """保存标签定义（全量覆盖）"""
    try:
        form = await request.form()
        lock = _get_config_lock(str(LABELS_PATH))
        async with lock:
            config = _load_yaml(LABELS_PATH)

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
                            raise ValueError(f"标签颜色不能为空: {name}")
                        desc = str(form.get(f"label_desc_{idx}", "")).strip()
                        _validate_label(name, color)
                        labels[name] = {"color": color, "description": desc}

            config["labels"] = labels
            _atomic_yaml_write(LABELS_PATH, config)
            reload_label_config()
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
        logger.warning(f"标签验证失败: {e}")
        return toast_redirect(
            "/config/labels",
            "toast.label_validation_failed",
            "error",
            lang=detect_language(),
            error=str(e),
        )
    except Exception as e:
        logger.error(f"标签定义保存失败: {e}")
        return toast_redirect(
            "/config/labels", "toast.save_failed", "error", lang=detect_language()
        )

    return toast_redirect(
        "/config/labels",
        "toast.labels_saved",
        lang=detect_language(),
        count=len(labels),
    )


# ========== POST: 保存推荐设置 ==========


@router.post("/labels/save-settings")
async def save_recommendation_settings(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
):
    """保存标签推荐设置"""
    try:
        form = await request.form()
        lock = _get_config_lock(str(LABELS_PATH))
        async with lock:
            config = _load_yaml(LABELS_PATH)

            confidence_threshold = float(form.get("confidence_threshold", 0.7))
            if not 0.0 <= confidence_threshold <= 1.0:
                raise ValueError(
                    f"置信度阈值必须在 0.0-1.0 之间: {confidence_threshold}"
                )

            config["recommendation"] = {
                "enabled": form.get("rec_enabled") is not None,
                "confidence_threshold": confidence_threshold,
                "auto_create": form.get("auto_create") is not None,
            }

            _atomic_yaml_write(LABELS_PATH, config)
            reload_label_config()
            logger.info(f"标签推荐设置已更新, by={user['sub']}")
            await log_admin_action(db, user["user_id"], "config_save", "recommendation")

    except (ValueError, yaml.YAMLError) as e:
        logger.error(f"推荐设置验证失败: {e}")
        return toast_redirect(
            "/config/labels",
            "toast.label_settings_validation_failed",
            "error",
            lang=detect_language(),
            error=str(e),
        )
    except Exception as e:
        logger.error(f"标签推荐设置保存失败: {e}", exc_info=True)
        return toast_redirect(
            "/config/labels", "toast.save_failed", "error", lang=detect_language()
        )

    return toast_redirect(
        "/config/labels", "toast.label_settings_saved", lang=detect_language()
    )


# ========== POST: 保存标签冲突规则 ==========


@router.post("/labels/save-conflict-rules")
async def save_conflict_rules(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
):
    """保存标签冲突规则"""
    try:
        form = await request.form()
        lock = _get_config_lock(str(LABELS_PATH))
        async with lock:
            config = _load_yaml(LABELS_PATH)

            # 收集冲突规则行
            conflict_rules: dict[str, list] = {}
            for key in form:
                if key.startswith("conflict_source_"):
                    idx = key[len("conflict_source_") :]
                    source = str(form[key]).strip()
                    blocked_raw = str(form.get(f"conflict_blocked_{idx}", "")).strip()
                    if source and blocked_raw:
                        # 仅按逗号分隔，保留标签内部空格（如 "good first issue"）
                        blocked = [
                            b.strip() for b in blocked_raw.split(",") if b.strip()
                        ]
                        if blocked:
                            _validate_label_name(source)
                            for b in blocked:
                                _validate_label_name(b)
                            conflict_rules[source] = blocked

            config["conflict_rules"] = conflict_rules
            _atomic_yaml_write(LABELS_PATH, config)
            reload_label_config()
            label_service.reload_labels()
            logger.info(
                f"标签冲突规则已更新 ({len(conflict_rules)} 条), by={user['sub']}"
            )
            await log_admin_action(
                db,
                user["user_id"],
                "config_save",
                "conflict_rules",
                None,
                {"rule_count": len(conflict_rules)},
            )

    except ValueError as e:
        logger.warning(f"冲突规则验证失败: {e}")
        return toast_redirect(
            "/config/labels",
            "toast.conflict_rules_validation_failed",
            "error",
            lang=detect_language(),
            error=str(e),
        )
    except Exception as e:
        logger.error(f"冲突规则保存失败: {e}", exc_info=True)
        return toast_redirect(
            "/config/labels", "toast.save_failed", "error", lang=detect_language()
        )

    return toast_redirect(
        "/config/labels",
        "toast.conflict_rules_saved",
        lang=detect_language(),
        count=len(conflict_rules),
    )


def _validate_label_name(name: str):
    """验证标签名称格式（仅名称，不含颜色）"""
    if len(name) > _MAX_LABEL_NAME_LEN:
        raise ValueError(
            f"标签名称过长（最多 {_MAX_LABEL_NAME_LEN} 字符）: {name[:20]}..."
        )
    if not _LABEL_NAME_RE.match(name):
        raise ValueError(f"标签名称包含非法字符: {name}")


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


# ========== GET: 全局配置页 ==========


@router.get("/general")
async def general_config_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    user_prefs: dict = Depends(get_user_preferences),
):
    """全局配置页面（含动态配置分组）"""
    # 读取所有 AppConfig 记录
    result = await db.execute(select(AppConfig).order_by(AppConfig.id))
    configs = result.scalars().all()
    config_map = {c.key_name: c.key_value for c in configs}

    # 构建动态配置分组数据
    from backend.core.config import (
        DYNAMIC_CONFIG_GROUPS,
        DYNAMIC_CONFIG_LABELS,
        DYNAMIC_CONFIG_RANGES,
        DYNAMIC_CONFIG_SELECT_OPTIONS,
        DYNAMIC_CONFIG_SENSITIVE_KEYS,
        get_dynamic_config_input_type,
        get_settings,
        mask_sensitive_value,
    )

    settings = get_settings()
    from backend.webui.i18n import detect_language as _detect_language
    from backend.webui.i18n import i18n as _i18n

    lang = _detect_language(user_prefs)
    dynamic_groups = []
    for group_id, group_data in DYNAMIC_CONFIG_GROUPS.items():
        items = []
        for key in group_data["keys"]:
            value = config_map.get(key, str(getattr(settings, key, "")))
            default_val = str(getattr(settings, key, ""))
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

    # 基础配置项（非动态配置）
    basic_configs = [c for c in configs if c.key_name in BASIC_CONFIG_KEYS]

    from backend.webui.routes.auth import APP_VERSION

    return render_template(
        "config_general.html",
        request,
        user_prefs=user_prefs,
        current_user=user,
        csrf_token=get_csrf_serializer().dumps({}),
        active_page="config_general",
        configs=basic_configs,
        dynamic_groups=dynamic_groups,
        app_version=APP_VERSION,
    )


# ========== POST: 保存全局配置 ==========


@router.post("/general/save")
async def save_general_config(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
):
    """保存全局配置"""
    try:
        form = await request.form()
        changed = {}

        # max_concurrent_reviews
        raw = form.get("max_concurrent_reviews")
        if raw is not None:
            val = _parse_positive_int_config(raw)
            result = await db.execute(
                select(AppConfig).where(AppConfig.key_name == "max_concurrent_reviews")
            )
            cfg = result.scalar_one_or_none()
            if cfg is None:
                cfg = AppConfig(
                    key_name="max_concurrent_reviews",
                    key_value=str(val),
                    description="最大并发审查数量",
                )
                db.add(cfg)
                changed["max_concurrent_reviews"] = {"old": "(无)", "new": str(val)}
            elif cfg.key_value != str(val):
                changed["max_concurrent_reviews"] = {
                    "old": cfg.key_value,
                    "new": str(val),
                }
                cfg.key_value = str(val)

        # review_timeout_seconds
        raw = form.get("review_timeout_seconds")
        if raw is not None:
            val = _parse_positive_int_config(raw)
            result = await db.execute(
                select(AppConfig).where(AppConfig.key_name == "review_timeout_seconds")
            )
            cfg = result.scalar_one_or_none()
            if cfg is None:
                cfg = AppConfig(
                    key_name="review_timeout_seconds",
                    key_value=str(val),
                    description="审查任务整体超时时间（秒）",
                )
                db.add(cfg)
                changed["review_timeout_seconds"] = {"old": "(无)", "new": str(val)}
            elif cfg.key_value != str(val):
                changed["review_timeout_seconds"] = {
                    "old": cfg.key_value,
                    "new": str(val),
                }
                cfg.key_value = str(val)

        # enable_auto_review (checkbox: "true" if checked, absent if unchecked)
        raw = form.get("enable_auto_review")
        val = "true" if raw == "true" else "false"
        result = await db.execute(
            select(AppConfig).where(AppConfig.key_name == "enable_auto_review")
        )
        cfg = result.scalar_one_or_none()
        if cfg is None:
            cfg = AppConfig(
                key_name="enable_auto_review",
                key_value=val,
                description="是否启用 Webhook 自动审查",
            )
            db.add(cfg)
            changed["enable_auto_review"] = {"old": "(无)", "new": val}
        elif cfg.key_value != val:
            changed["enable_auto_review"] = {"old": cfg.key_value, "new": val}
            cfg.key_value = val

        # enable_check_runs (checkbox: "true" if checked, absent if unchecked)
        raw = form.get("enable_check_runs")
        val = "true" if raw == "true" else "false"
        result = await db.execute(
            select(AppConfig).where(AppConfig.key_name == "enable_check_runs")
        )
        cfg = result.scalar_one_or_none()
        if cfg is None:
            cfg = AppConfig(
                key_name="enable_check_runs",
                key_value=val,
                description="是否启用 GitHub Check Runs 审查进度可视化",
            )
            db.add(cfg)
            changed["enable_check_runs"] = {"old": "(无)", "new": val}
        elif cfg.key_value != val:
            changed["enable_check_runs"] = {"old": cfg.key_value, "new": val}
            cfg.key_value = val

        # enable_analysis_check (checkbox: "true" if checked, absent if unchecked)
        raw = form.get("enable_analysis_check")
        val = "true" if raw == "true" else "false"
        result = await db.execute(
            select(AppConfig).where(AppConfig.key_name == "enable_analysis_check")
        )
        cfg = result.scalar_one_or_none()
        if cfg is None:
            cfg = AppConfig(
                key_name="enable_analysis_check",
                key_value=val,
                description="是否启用副 Analysis Check（AI 运行时指标），仅工具模式下出现",
            )
            db.add(cfg)
            changed["enable_analysis_check"] = {"old": "(无)", "new": val}
        elif cfg.key_value != val:
            changed["enable_analysis_check"] = {"old": cfg.key_value, "new": val}
            cfg.key_value = val

        # enable_findings_check (checkbox)
        raw = form.get("enable_findings_check")
        val = "true" if raw == "true" else "false"
        result = await db.execute(
            select(AppConfig).where(AppConfig.key_name == "enable_findings_check")
        )
        cfg = result.scalar_one_or_none()
        if cfg is None:
            cfg = AppConfig(
                key_name="enable_findings_check",
                key_value=val,
                description="是否启用副 Findings Check（发现统计），仅有可发布 findings 时出现",
            )
            db.add(cfg)
            changed["enable_findings_check"] = {"old": "(无)", "new": val}
        elif cfg.key_value != val:
            changed["enable_findings_check"] = {"old": cfg.key_value, "new": val}
            cfg.key_value = val

        # analysis_min_interval_sec (number, seconds)
        raw = form.get("analysis_min_interval_sec")
        if raw is not None:
            val = _parse_positive_int_config(raw)
            result = await db.execute(
                select(AppConfig).where(
                    AppConfig.key_name == "analysis_min_interval_sec"
                )
            )
            cfg = result.scalar_one_or_none()
            if cfg is None:
                cfg = AppConfig(
                    key_name="analysis_min_interval_sec",
                    key_value=str(val),
                    description="Analysis Check 快照写入 GitHub 的最小间隔（秒）",
                )
                db.add(cfg)
                changed["analysis_min_interval_sec"] = {
                    "old": "(无)",
                    "new": str(val),
                }
            elif cfg.key_value != str(val):
                changed["analysis_min_interval_sec"] = {
                    "old": cfg.key_value,
                    "new": str(val),
                }
                cfg.key_value = str(val)

        # ========== Web 搜索配置 ==========
        web_search_keys = [
            "web_search_enabled",
            "web_search_provider",
            "web_search_api_key",
            "web_search_max_results",
            "web_search_max_content_length",
            "web_search_timeout",
        ]
        for key in web_search_keys:
            raw = form.get(key)
            if raw is None:
                continue
            val = raw.strip()
            # 验证
            if key == "web_search_enabled":
                val = "true" if val == "true" else "false"
            elif key in {
                "web_search_max_results",
                "web_search_max_content_length",
                "web_search_timeout",
            }:
                try:
                    val_i = int(val)
                except ValueError:
                    return toast_redirect(
                        "/config/general",
                        "toast.numeric_required",
                        "error",
                        lang=detect_language(),
                        field_key=key,
                    )

                min_v, max_v = DYNAMIC_CONFIG_RANGES[key]
                if not (min_v <= val_i <= max_v):
                    return toast_redirect(
                        "/config/general",
                        "toast.value_range",
                        "error",
                        lang=detect_language(),
                        field_key=key,
                        min_v=min_v,
                        max_v=max_v,
                    )
                val = str(val_i)
            elif key == "web_search_provider":
                if val not in ("duckduckgo", "tavily"):
                    return toast_redirect(
                        "/config/general",
                        "toast.unsupported_search_provider",
                        "error",
                        lang=detect_language(),
                    )
            # API key 无需特殊验证

            result = await db.execute(
                select(AppConfig).where(AppConfig.key_name == key)
            )
            cfg = result.scalar_one_or_none()
            if cfg and cfg.key_value != val:
                # API key 脱敏记录
                if key == "web_search_api_key" and val:
                    old_val = cfg.key_value
                    log_old = f"***{old_val[-4:]}" if len(old_val) > 4 else "***"
                    log_new = f"***{val[-4:]}" if len(val) > 4 else "***"
                    changed[key] = {"old": log_old, "new": log_new, "raw_new": val}
                else:
                    changed[key] = {"old": cfg.key_value, "new": val, "raw_new": val}
                cfg.key_value = val

        # ========== 动态配置保存 ==========
        from backend.core.config import (
            DYNAMIC_CONFIG_GROUPS,
            DYNAMIC_CONFIG_SELECT_OPTIONS,
            DYNAMIC_CONFIG_SENSITIVE_KEYS,
        )
        from backend.core.config import (
            mask_sensitive_value as _mask,
        )

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
                    from backend.core.config import _get_field_type

                    if _get_field_type(key) is bool:
                        raw = "false"
                    else:
                        continue

                val = str(raw).strip()

                # 验证
                if key in DYNAMIC_CONFIG_RANGES:
                    min_v, max_v = DYNAMIC_CONFIG_RANGES[key]
                    try:
                        num_val = float(val)
                    except ValueError:
                        return toast_redirect(
                            "/config/general",
                            "toast.numeric_required",
                            "error",
                            lang=detect_language(),
                            field_key=key,
                        )
                    if not (min_v <= num_val <= max_v):
                        return toast_redirect(
                            "/config/general",
                            "toast.value_range",
                            "error",
                            lang=detect_language(),
                            field_key=key,
                            min_v=min_v,
                            max_v=max_v,
                        )

                if key in DYNAMIC_CONFIG_SELECT_OPTIONS:
                    valid_values = [
                        opt["value"] for opt in DYNAMIC_CONFIG_SELECT_OPTIONS[key]
                    ]
                    if val not in valid_values:
                        return toast_redirect(
                            "/config/general",
                            "toast.value_invalid",
                            "error",
                            lang=detect_language(),
                            field_key=key,
                        )

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
            return toast_redirect(
                "/config/general",
                "toast.config_saved_restart",
                lang=detect_language(),
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
        return toast_redirect(
            "/config/general", "toast.config_saved_live", lang=detect_language()
        )

    except ValueError:
        return toast_redirect(
            "/config/general",
            "toast.invalid_param",
            "error",
            lang=detect_language(),
        )
    except Exception as e:
        logger.error(f"全局配置保存失败: {e}", exc_info=True)
        return toast_redirect(
            "/config/general",
            "toast.save_failed",
            "error",
            lang=detect_language(),
        )
