"""WebUI 配置管理路由（超级管理员专用）"""

import asyncio
import re
import shutil
import tempfile
from pathlib import Path

import yaml
from fastapi import APIRouter, Request, Depends, Form, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.database import AppConfig
from loguru import logger

from backend.core.config import (
    get_strategy_config,
    reload_strategy_config,
    get_label_config,
    reload_label_config,
)
from backend.services.label_service import label_service
from backend.webui.deps import (
    require_super_admin,
    get_db,
    get_templates,
    get_csrf_serializer,
    require_csrf,
    get_user_preferences,
    toast_redirect,
)
from backend.webui.helpers.admin_log import log_admin_action

# 基础配置项（非动态配置），用于 WebUI 配置页面分组展示及 Settings 即时更新
_BASIC_CONFIG_KEYS = frozenset(
    {
        "max_concurrent_reviews",
        "review_timeout_seconds",
        "enable_auto_review",
        "issue_auto_create_labels",
        "issue_auto_assign",
        "issue_max_tool_iterations",
        "web_search_enabled",
        "web_search_provider",
        "web_search_api_key",
        "web_search_max_results",
        "web_search_max_content_length",
        "web_search_timeout",
    }
)

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

    return templates.TemplateResponse(
        "config_strategies.html",
        {
            "request": request,
            "current_user": user,
            "csrf_token": get_csrf_serializer().dumps({}),
            "active_page": "config_strategies",
            "user_prefs": user_prefs,
            "strategies": config_data.get("strategies", {}),
            "file_filters": config_data.get("file_filters", {}),
            "batch": config_data.get("batch", {}),
            "context_enhancement": config_data.get("context_enhancement", {}),
            "review_policy": config_data.get("review_policy", {}),
            "active_tab": tab,
        },
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

            elif section == "batch":
                config["batch"] = {
                    "max_files_per_batch": int(form.get("max_files_per_batch", 10)),
                    "max_lines_per_batch": int(form.get("max_lines_per_batch", 2000)),
                }

            elif section == "context_enhancement":
                config["context_enhancement"] = {
                    "enable_project_structure": form.get("enable_project_structure")
                    is not None,
                    "max_structure_files": int(form.get("max_structure_files", 500)),
                    "enable_ai_tools": form.get("enable_ai_tools") is not None,
                    "max_tool_iterations": int(form.get("max_tool_iterations", 20)),
                    "max_file_size": int(form.get("max_file_size", 200000)),
                    "enable_ai_tools_in_batch": form.get("enable_ai_tools_in_batch")
                    is not None,
                    "max_file_lines": int(float(form.get("max_file_lines", 500))),
                    "default_context_lines": int(
                        float(form.get("default_context_lines", 20))
                    ),
                    "max_context_lines": int(float(form.get("max_context_lines", 200))),
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
            else:
                raise HTTPException(status_code=400, detail=f"未知 section: {section}")

            _atomic_yaml_write(STRATEGIES_PATH, config)
            reload_strategy_config()
            logger.info(f"策略配置 [{section}] 已更新, by={user['sub']}")
            await log_admin_action(
                db, user["user_id"], "config_save", "strategy", section
            )

    except (ValueError, yaml.YAMLError) as e:
        logger.error(f"配置验证失败: {e}")
        return toast_redirect(
            f"/webui/config/strategies?tab={section}", f"配置验证失败: {e}", "error"
        )
    except PermissionError as e:
        logger.error(f"文件权限不足: {e}")
        return toast_redirect(
            f"/webui/config/strategies?tab={section}", "文件权限不足", "error"
        )
    except Exception as e:
        logger.error(f"策略配置保存异常: {e}", exc_info=True)
        return toast_redirect(
            f"/webui/config/strategies?tab={section}", "保存失败，请稍后重试", "error"
        )

    return toast_redirect(
        f"/webui/config/strategies?tab={section}",
        f"策略配置 [{section}] 已保存并即时生效",
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
    return templates.TemplateResponse(
        "config_labels.html",
        {
            "request": request,
            "current_user": user,
            "csrf_token": get_csrf_serializer().dumps({}),
            "active_page": "config_labels",
            "user_prefs": user_prefs,
            "labels": label_config.get_labels(),
            "recommendation": label_config.get_recommendation_settings(),
        },
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
        return toast_redirect("/webui/config/labels", f"标签验证失败: {e}", "error")
    except Exception as e:
        logger.error(f"标签定义保存失败: {e}")
        return toast_redirect("/webui/config/labels", "保存失败，请稍后重试", "error")

    return toast_redirect("/webui/config/labels", f"标签定义已更新（{len(labels)} 个）")


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
        return toast_redirect("/webui/config/labels", f"推荐设置验证失败: {e}", "error")
    except Exception as e:
        logger.error(f"标签推荐设置保存失败: {e}", exc_info=True)
        return toast_redirect("/webui/config/labels", "保存失败，请稍后重试", "error")

    return toast_redirect("/webui/config/labels", "标签推荐设置已更新")


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
        DYNAMIC_CONFIG_SENSITIVE_KEYS,
        DYNAMIC_CONFIG_SELECT_OPTIONS,
        DYNAMIC_CONFIG_RANGES,
        get_dynamic_config_input_type,
        get_settings,
        mask_sensitive_value,
    )

    settings = get_settings()
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

            items.append(
                {
                    "key": key,
                    "label": DYNAMIC_CONFIG_LABELS.get(key, key),
                    "description": group_data.get("descriptions", {}).get(key, ""),
                    "input_type": input_type,
                    "value": display_value,
                    "default": mask_sensitive_value(default_val)
                    if (is_sensitive and default_val)
                    else default_val,
                    "sensitive": is_sensitive,
                    "select_options": DYNAMIC_CONFIG_SELECT_OPTIONS.get(key, []),
                    "min_val": DYNAMIC_CONFIG_RANGES.get(key, (None, None))[0],
                    "max_val": DYNAMIC_CONFIG_RANGES.get(key, (None, None))[1],
                }
            )
        dynamic_groups.append(
            {
                "id": group_id,
                "label": group_data["label"],
                "icon": group_data.get("icon", ""),
                "fields": items,
            }
        )

    # 基础配置项（非动态配置）
    basic_configs = [c for c in configs if c.key_name in _BASIC_CONFIG_KEYS]

    from backend.webui.routes.auth import APP_VERSION

    return templates.TemplateResponse(
        "config_general.html",
        {
            "request": request,
            "current_user": user,
            "csrf_token": get_csrf_serializer().dumps({}),
            "active_page": "config_general",
            "user_prefs": user_prefs,
            "configs": basic_configs,
            "dynamic_groups": dynamic_groups,
            "app_version": APP_VERSION,
        },
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
            val = int(raw)
            if not 1 <= val <= 100:
                return toast_redirect(
                    "/webui/config/general", "参数验证失败，请检查输入值", "error"
                )
            result = await db.execute(
                select(AppConfig).where(AppConfig.key_name == "max_concurrent_reviews")
            )
            cfg = result.scalar_one_or_none()
            if cfg and cfg.key_value != str(val):
                changed["max_concurrent_reviews"] = {
                    "old": cfg.key_value,
                    "new": str(val),
                }
                cfg.key_value = str(val)

        # review_timeout_seconds
        raw = form.get("review_timeout_seconds")
        if raw is not None:
            val = int(raw)
            if not 10 <= val <= 3600:
                return toast_redirect(
                    "/webui/config/general", "参数验证失败，请检查输入值", "error"
                )
            result = await db.execute(
                select(AppConfig).where(AppConfig.key_name == "review_timeout_seconds")
            )
            cfg = result.scalar_one_or_none()
            if cfg and cfg.key_value != str(val):
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
        if cfg and cfg.key_value != val:
            changed["enable_auto_review"] = {"old": cfg.key_value, "new": val}
            cfg.key_value = val

        # issue_auto_create_labels (checkbox)
        raw = form.get("issue_auto_create_labels")
        val = "true" if raw == "true" else "false"
        result = await db.execute(
            select(AppConfig).where(AppConfig.key_name == "issue_auto_create_labels")
        )
        cfg = result.scalar_one_or_none()
        if cfg and cfg.key_value != val:
            changed["issue_auto_create_labels"] = {"old": cfg.key_value, "new": val}
            cfg.key_value = val

        # issue_auto_assign (checkbox)
        raw = form.get("issue_auto_assign")
        val = "true" if raw == "true" else "false"
        result = await db.execute(
            select(AppConfig).where(AppConfig.key_name == "issue_auto_assign")
        )
        cfg = result.scalar_one_or_none()
        if cfg and cfg.key_value != val:
            changed["issue_auto_assign"] = {"old": cfg.key_value, "new": val}
            cfg.key_value = val

        # issue_max_tool_iterations
        raw = form.get("issue_max_tool_iterations")
        if raw is not None:
            try:
                val = int(raw)
            except ValueError:
                return toast_redirect(
                    "/webui/config/general",
                    "AI 工具调用迭代次数必须是有效整数",
                    "error",
                )
            if not 1 <= val <= 150:
                return toast_redirect(
                    "/webui/config/general",
                    "AI 工具调用迭代次数须在 1-150 之间",
                    "error",
                )
            result = await db.execute(
                select(AppConfig).where(
                    AppConfig.key_name == "issue_max_tool_iterations"
                )
            )
            cfg = result.scalar_one_or_none()
            if cfg and cfg.key_value != str(val):
                changed["issue_max_tool_iterations"] = {
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
            elif key == "web_search_max_results":
                val_i = int(val)
                if not 1 <= val_i <= 10:
                    return toast_redirect(
                        "/webui/config/general",
                        "Web 搜索最大结果数须在 1-10 之间",
                        "error",
                    )
                val = str(val_i)
            elif key == "web_search_max_content_length":
                val_i = int(val)
                if not 100 <= val_i <= 5000:
                    return toast_redirect(
                        "/webui/config/general",
                        "结果截断长度须在 100-5000 之间",
                        "error",
                    )
                val = str(val_i)
            elif key == "web_search_timeout":
                val_i = int(val)
                if not 5 <= val_i <= 60:
                    return toast_redirect(
                        "/webui/config/general", "搜索超时须在 5-60 秒之间", "error"
                    )
                val = str(val_i)
            elif key == "web_search_provider":
                if val not in ("duckduckgo", "tavily"):
                    return toast_redirect(
                        "/webui/config/general", "不支持的搜索提供商", "error"
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
            DYNAMIC_CONFIG_SENSITIVE_KEYS,
            DYNAMIC_CONFIG_RANGES,
            DYNAMIC_CONFIG_SELECT_OPTIONS,
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
                            "/webui/config/general",
                            f"{key} 必须是有效数值",
                            "error",
                        )
                    if not (min_v <= num_val <= max_v):
                        return toast_redirect(
                            "/webui/config/general",
                            f"{key} 值须在 {min_v}-{max_v} 之间",
                            "error",
                        )

                if key in DYNAMIC_CONFIG_SELECT_OPTIONS:
                    valid_values = [
                        opt["value"] for opt in DYNAMIC_CONFIG_SELECT_OPTIONS[key]
                    ]
                    if val not in valid_values:
                        return toast_redirect(
                            "/webui/config/general",
                            f"{key} 值无效",
                            "error",
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
                "/webui/config/general", "全局配置已保存（部分配置需重启后生效）"
            )

        await db.commit()

        # 清除动态配置缓存 + 同步 Settings 单例
        from backend.core.config import (
            invalidate_dynamic_config_cache,
            update_settings_field,
            get_all_dynamic_config_keys,
        )

        all_dynamic_keys = get_all_dynamic_config_keys()
        invalidate_dynamic_config_cache(all_dynamic_keys)

        # 即时更新 Settings 单例，无需重启
        for key, change in changed.items():
            if key in all_dynamic_keys or key in _BASIC_CONFIG_KEYS:
                update_settings_field(key, change.get("raw_new", change["new"]))

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
        return toast_redirect("/webui/config/general", "全局配置已保存并即时生效")

    except ValueError:
        return toast_redirect(
            "/webui/config/general", "参数验证失败，请检查输入值", "error"
        )
    except Exception as e:
        logger.error(f"全局配置保存失败: {e}", exc_info=True)
        return toast_redirect("/webui/config/general", "保存失败，请稍后重试", "error")
