"""WebUI 配置管理路由（超级管理员专用）

全局配置页 /config（R3）：单页吃下平铺动态配置组与策略/标签节表单；
保存链路保留既有 POST 端点（/config/general/save、/config/strategies/save、
/config/labels/*），旧 GET 页面统一 302 到 /config。
"""

from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.config import BASIC_CONFIG_KEYS
from backend.core.config_sections import get_sections_for_target
from backend.core.time_service import filename_timestamp
from backend.models.database import AppConfig
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
):
    """保存策略配置的某个 section（统一节配置存储）"""
    try:
        form = await request.form()
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
                        "partial_commit": form.get("sakura_partial_commit")
                        is not None,
                    },
                    "knowledge_extraction": {
                        "min_reflections": int(
                            form.get("sakura_min_reflections", 15)
                        ),
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
            depgraph_mode = form.get("pr_dependency_graph_mode", "ai")
            if depgraph_mode not in {"ai", "static"}:
                depgraph_mode = "ai"
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
            except (ValueError, TypeError):
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
        logger.error(f"配置验证失败: {e}")
        return toast_redirect(
            f"/config?section={section}",
            "toast.config_validation_failed",
            "error",
            lang=detect_language(),
            error=str(e),
        )
    except Exception as e:
        logger.error(f"策略配置保存异常: {e}", exc_info=True)
        return toast_redirect(
            f"/config?section={section}",
            "toast.save_failed",
            "error",
            lang=detect_language(),
        )

    return toast_redirect(
        f"/config?section={section}",
        "toast.strategy_saved",
        lang=detect_language(),
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
):
    """保存标签定义（全量覆盖，统一节配置存储）"""
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
                        raise ValueError(f"标签颜色不能为空: {name}")
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
        logger.warning(f"标签验证失败: {e}")
        return toast_redirect(
            "/config?section=labels",
            "toast.label_validation_failed",
            "error",
            lang=detect_language(),
            error=str(e),
        )
    except Exception as e:
        logger.error(f"标签定义保存失败: {e}")
        return toast_redirect(
            "/config?section=labels", "toast.save_failed", "error", lang=detect_language()
        )

    return toast_redirect(
        "/config?section=labels",
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

        confidence_threshold = float(form.get("confidence_threshold", 0.7))
        if not 0.0 <= confidence_threshold <= 1.0:
            raise ValueError(
                f"置信度阈值必须在 0.0-1.0 之间: {confidence_threshold}"
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
        logger.error(f"推荐设置验证失败: {e}")
        return toast_redirect(
            "/config?section=labels",
            "toast.label_settings_validation_failed",
            "error",
            lang=detect_language(),
            error=str(e),
        )
    except Exception as e:
        logger.error(f"标签推荐设置保存失败: {e}", exc_info=True)
        return toast_redirect(
            "/config?section=labels", "toast.save_failed", "error", lang=detect_language()
        )

    return toast_redirect(
        "/config?section=labels", "toast.label_settings_saved", lang=detect_language()
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
                        conflict_rules[source] = blocked

        await section_config_service.save_section(
            db, "label.conflict_rules", conflict_rules
        )
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
            "/config?section=labels",
            "toast.conflict_rules_validation_failed",
            "error",
            lang=detect_language(),
            error=str(e),
        )
    except Exception as e:
        logger.error(f"冲突规则保存失败: {e}", exc_info=True)
        return toast_redirect(
            "/config?section=labels", "toast.save_failed", "error", lang=detect_language()
        )

    return toast_redirect(
        "/config?section=labels",
        "toast.conflict_rules_saved",
        lang=detect_language(),
        count=len(conflict_rules),
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
):
    """下载全部用户及个人配置、两步验证和通行密钥的 JSON 备份。"""
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
            lang=detect_language(),
            reason=str(exc),
        )
    except Exception as exc:
        logger.error("用户信息备份导出失败: {}", exc, exc_info=True)
        return toast_redirect(
            "/config/backup",
            "toast.user_backup_export_failed",
            "error",
            lang=detect_language(),
            reason="internal error",
        )


@router.post("/backup/export/{scope}")
async def download_config_backup(
    scope: str,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
):
    """下载全局、AI、系统配置或完整的版本化 JSON 备份。"""
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
            lang=detect_language(),
            reason=str(exc),
        )
    except Exception as exc:
        logger.error("配置备份导出失败: {}", exc, exc_info=True)
        return toast_redirect(
            "/config/backup",
            "toast.config_backup_export_failed",
            "error",
            lang=detect_language(),
            reason="internal error",
        )


@router.post("/backup/import")
async def upload_config_backup(
    backup_file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
):
    """校验并精确恢复备份中包含的配置分类。"""
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
        lang = detect_language()
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
            lang=detect_language(),
            reason=str(exc),
        )
    except Exception as exc:
        logger.error("配置备份导入失败: {}", exc, exc_info=True)
        if result is not None:
            return toast_redirect(
                "/config/backup",
                "toast.config_backup_imported_restart",
                "error",
                lang=detect_language(),
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
            lang=detect_language(),
        )
    finally:
        await backup_file.close()


@router.post("/backup/users/import")
async def upload_user_backup(
    backup_file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
):
    """校验并合并全部用户及其受支持的安全信息。"""
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
        lang = detect_language()
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
            lang=detect_language(),
            reason=str(exc),
        )
    except Exception as exc:
        logger.error("用户信息备份导入失败: {}", exc, exc_info=True)
        await db.rollback()
        return toast_redirect(
            "/config/backup",
            "toast.user_backup_import_failed",
            "error",
            lang=detect_language(),
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
                        translated_group := _i18n.t(f"config.group.{group_id}", lang=lang)
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
        labels=label_data.get("labels", {}),
        recommendation=label_data.get("recommendation", {}),
        conflict_rules=label_data.get("conflict_rules", {}),
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
):
    """保存全局配置页的全部平铺键（通用逐键 upsert 循环）。

    review_basic / web_search 等原手写键段已注册进 DYNAMIC_CONFIG_GROUPS，
    与其余动态组共用同一循环；bool 未勾选时表单不提交，按 Settings
    字段类型回填 "false"（与既有 checkbox 语义一致）。
    """
    try:
        form = await request.form()
        changed = {}

        # ========== 动态配置保存（覆盖全部 DYNAMIC 组键） ==========
        from backend.core.config import (
            DYNAMIC_CONFIG_GROUPS,
            DYNAMIC_CONFIG_RANGES,
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

                # 验证（上界 None 表示仅约束下界，不设硬编码上限）
                if key in DYNAMIC_CONFIG_RANGES:
                    min_v, max_v = DYNAMIC_CONFIG_RANGES[key]
                    try:
                        num_val = float(val)
                    except ValueError:
                        return toast_redirect(
                            "/config",
                            "toast.numeric_required",
                            "error",
                            lang=detect_language(),
                            field_key=key,
                        )
                    if num_val < min_v or (
                        max_v is not None and num_val > max_v
                    ):
                        if max_v is None:
                            return toast_redirect(
                                "/config",
                                "toast.value_min_required",
                                "error",
                                lang=detect_language(),
                                field_key=key,
                                min_v=min_v,
                            )
                        return toast_redirect(
                            "/config",
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
                            "/config",
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
                "/config",
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
            "/config", "toast.config_saved_live", lang=detect_language()
        )

    except ValueError:
        return toast_redirect(
            "/config",
            "toast.invalid_param",
            "error",
            lang=detect_language(),
        )
    except Exception as e:
        logger.error(f"全局配置保存失败: {e}", exc_info=True)
        return toast_redirect(
            "/config",
            "toast.save_failed",
            "error",
            lang=detect_language(),
        )
