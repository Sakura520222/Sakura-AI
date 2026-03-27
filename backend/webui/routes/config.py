"""WebUI 配置管理路由（超级管理员专用）"""

import shutil
import tempfile
from pathlib import Path
from typing import Optional

import yaml
from fastapi import APIRouter, Request, Depends, Form, Query, HTTPException
from fastapi.responses import RedirectResponse
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
    get_templates,
    get_csrf_serializer,
    validate_csrf_token,
    get_user_preferences,
)

router = APIRouter(prefix="/config", tags=["WebUI Config"])
templates = get_templates()

STRATEGIES_PATH = Path("config/strategies.yaml")
LABELS_PATH = Path("config/labels.yaml")

STRATEGY_KEYS = ["quick", "standard", "deep", "large"]


def _atomic_yaml_write(path: Path, full_config: dict):
    """原子写入 YAML 配置文件

    先写临时文件，再 rename，确保不会因写入中途出错而损坏配置。
    """
    yaml_str = yaml.dump(full_config, allow_unicode=True, default_flow_style=False, sort_keys=False)
    # round-trip 验证
    parsed = yaml.safe_load(yaml_str)
    if parsed is None:
        raise ValueError("YAML 序列化验证失败")

    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp", prefix=f"{path.stem}_")
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

    return templates.TemplateResponse("config_strategies.html", {
        "request": request,
        "current_user": user,
        "csrf_token": get_csrf_serializer().dumps({}),
        "active_page": "config",
        "user_prefs": user_prefs,
        "strategies": config_data.get("strategies", {}),
        "file_filters": config_data.get("file_filters", {}),
        "batch": config_data.get("batch", {}),
        "context_enhancement": config_data.get("context_enhancement", {}),
        "review_policy": config_data.get("review_policy", {}),
        "active_tab": tab,
    })


# ========== POST: 保存策略配置 ==========

@router.post("/strategies/save")
async def save_strategies_section(
    request: Request,
    user: dict = Depends(require_super_admin),
    csrf_token: str = Form(...),
    section: str = Form(...),
):
    """保存策略配置的某个 section"""
    if not validate_csrf_token(csrf_token):
        raise HTTPException(status_code=403, detail="CSRF 验证失败")

    try:
        form = await request.form()
        config = _load_yaml(STRATEGIES_PATH)

        if section == "strategies":
            # 收集 4 个策略的 conditions 和 prompt
            strategies = {}
            for key in STRATEGY_KEYS:
                name = form.get(f"strategy_{key}_name", key)
                max_files = int(form.get(f"strategy_{key}_max_files", 999999))
                max_lines = int(form.get(f"strategy_{key}_max_lines", 99999999))
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
                "skip_extensions": [x.strip() for x in skip_ext_raw.splitlines() if x.strip()],
                "skip_paths": [x.strip() for x in skip_paths_raw.splitlines() if x.strip()],
                "code_extensions": [x.strip() for x in code_ext_raw.splitlines() if x.strip()],
            }

        elif section == "batch":
            config["batch"] = {
                "max_files_per_batch": int(form.get("max_files_per_batch", 10)),
                "max_lines_per_batch": int(form.get("max_lines_per_batch", 2000)),
            }

        elif section == "context_enhancement":
            config["context_enhancement"] = {
                "enable_project_structure": form.get("enable_project_structure") is not None,
                "max_structure_files": int(form.get("max_structure_files", 500)),
                "enable_ai_tools": form.get("enable_ai_tools") is not None,
                "max_tool_iterations": int(form.get("max_tool_iterations", 20)),
                "max_file_size": int(form.get("max_file_size", 200000)),
                "enable_ai_tools_in_batch": form.get("enable_ai_tools_in_batch") is not None,
            }

        elif section == "review_policy":
            config["review_policy"] = {
                "enabled": form.get("rp_enabled") is not None,
                "approve_threshold": int(form.get("approve_threshold", 8)),
                "block_threshold": int(form.get("block_threshold", 4)),
                "block_on_critical": form.get("block_on_critical") is not None,
                "max_major_issues": int(form.get("max_major_issues", 1)),
                "ignored_patterns": [
                    x.strip() for x in form.get("ignored_patterns", "").splitlines() if x.strip()
                ],
                "repo_overrides": config.get("review_policy", {}).get("repo_overrides", {}),
                "enable_idempotency_check": form.get("enable_idempotency_check") is not None,
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

    except (ValueError, yaml.YAMLError) as e:
        logger.error(f"策略配置保存失败: {e}")
        return RedirectResponse(
            url=f"/webui/config/strategies?tab={section}&error=save_failed",
            status_code=302,
        )
    except Exception as e:
        logger.error(f"策略配置保存异常: {e}")
        return RedirectResponse(
            url=f"/webui/config/strategies?tab={section}&error=save_failed",
            status_code=302,
        )

    return RedirectResponse(
        url=f"/webui/config/strategies?tab={section}&saved=1",
        status_code=302,
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
    return templates.TemplateResponse("config_labels.html", {
        "request": request,
        "current_user": user,
        "csrf_token": get_csrf_serializer().dumps({}),
        "active_page": "config",
        "user_prefs": user_prefs,
        "labels": label_config.get_labels(),
        "recommendation": label_config.get_recommendation_settings(),
    })


# ========== POST: 保存标签定义 ==========

@router.post("/labels/save-labels")
async def save_labels_definitions(
    request: Request,
    user: dict = Depends(require_super_admin),
    csrf_token: str = Form(...),
):
    """保存标签定义（全量覆盖）"""
    if not validate_csrf_token(csrf_token):
        raise HTTPException(status_code=403, detail="CSRF 验证失败")

    try:
        form = await request.form()
        config = _load_yaml(LABELS_PATH)

        # 收集所有标签行（不假设连续索引，因为 JS 删除行会产生间隔）
        labels = {}
        for key in form:
            if key.startswith("label_name_"):
                idx = key[len("label_name_"):]
                name = str(form[key]).strip()
                if name:
                    color = str(form.get(f"label_color_{idx}", "0366d6")).strip().lstrip("#")
                    desc = str(form.get(f"label_desc_{idx}", "")).strip()
                    labels[name] = {"color": color, "description": desc}

        config["labels"] = labels
        _atomic_yaml_write(LABELS_PATH, config)
        reload_label_config()
        label_service.reload_labels()
        logger.info(f"标签定义已更新 ({len(labels)} 个), by={user['sub']}")

    except Exception as e:
        logger.error(f"标签定义保存失败: {e}")
        return RedirectResponse(url="/webui/config/labels?error=save_failed", status_code=302)

    return RedirectResponse(url="/webui/config/labels?saved=1", status_code=302)


# ========== POST: 保存推荐设置 ==========

@router.post("/labels/save-settings")
async def save_recommendation_settings(
    request: Request,
    user: dict = Depends(require_super_admin),
    csrf_token: str = Form(...),
):
    """保存标签推荐设置"""
    if not validate_csrf_token(csrf_token):
        raise HTTPException(status_code=403, detail="CSRF 验证失败")

    try:
        form = await request.form()
        config = _load_yaml(LABELS_PATH)

        config["recommendation"] = {
            "enabled": form.get("rec_enabled") is not None,
            "confidence_threshold": float(form.get("confidence_threshold", 0.7)),
            "auto_create": form.get("auto_create") is not None,
        }

        _atomic_yaml_write(LABELS_PATH, config)
        reload_label_config()
        logger.info(f"标签推荐设置已更新, by={user['sub']}")

    except Exception as e:
        logger.error(f"标签推荐设置保存失败: {e}")
        return RedirectResponse(url="/webui/config/labels?error=save_failed", status_code=302)

    return RedirectResponse(url="/webui/config/labels?saved=1", status_code=302)
