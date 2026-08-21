"""配置节注册表与进程级节存储。
Config section registry and process-level section store.

统一配置存储读取链（docs/plans/2026-08-16-unified-config-store.md §3.1/§3.2）：

    内置默认（config_section_defaults，单一事实源）
      ← app_config 节键 JSON 覆盖（叶子级深度合并）
      → 进程级 _section_store
      → StrategyConfig / LabelConfig facade（签名不变）

- 启动时由 lifespan 调用 ``await load_section_configs(db)`` 填充 store；
  store 未初始化（bootstrap/无 DB 环境）时读取方直接得到内置默认。
- 写侧（SectionConfigService）通过 update_section_store()/clear_section_store()
  在事务落库后同步本存储，保证单进程内热更新即时生效。
"""

import asyncio
import json
from collections import OrderedDict
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml
from loguru import logger

from backend.core.config_section_defaults import (
    LABEL_SECTION_DEFAULTS,
    STRATEGY_SECTION_DEFAULTS,
)

# 配置节注册表：app_config 键名 → 节描述
#   target: 归属 facade（"strategy" → StrategyConfig，"label" → LabelConfig）
#   section: 组装到 facade self.config 中的顶层节名
#   defaults: 内置默认值（引用常量，禁止原地修改）
SECTION_REGISTRY: OrderedDict[str, dict[str, Any]] = OrderedDict(
    [
        (
            "strategy.strategies",
            {
                "target": "strategy",
                "section": "strategies",
                "defaults": STRATEGY_SECTION_DEFAULTS["strategies"],
            },
        ),
        (
            "strategy.file_filters",
            {
                "target": "strategy",
                "section": "file_filters",
                "defaults": STRATEGY_SECTION_DEFAULTS["file_filters"],
            },
        ),
        (
            "strategy.context_enhancement",
            {
                "target": "strategy",
                "section": "context_enhancement",
                "defaults": STRATEGY_SECTION_DEFAULTS["context_enhancement"],
            },
        ),
        (
            "strategy.review_policy",
            {
                "target": "strategy",
                "section": "review_policy",
                "defaults": STRATEGY_SECTION_DEFAULTS["review_policy"],
            },
        ),
        (
            "strategy.issue_analysis",
            {
                "target": "strategy",
                "section": "issue_analysis",
                "defaults": STRATEGY_SECTION_DEFAULTS["issue_analysis"],
            },
        ),
        (
            "strategy.pr_summary",
            {
                "target": "strategy",
                "section": "pr_summary",
                "defaults": STRATEGY_SECTION_DEFAULTS["pr_summary"],
            },
        ),
        (
            "strategy.pr_dependency_graph",
            {
                "target": "strategy",
                "section": "pr_dependency_graph",
                "defaults": STRATEGY_SECTION_DEFAULTS["pr_dependency_graph"],
            },
        ),
        (
            "strategy.scan",
            {
                "target": "strategy",
                "section": "scan",
                "defaults": STRATEGY_SECTION_DEFAULTS["scan"],
            },
        ),
        (
            "label.definitions",
            {
                "target": "label",
                "section": "labels",
                "defaults": LABEL_SECTION_DEFAULTS["labels"],
            },
        ),
        (
            "label.recommendation",
            {
                "target": "label",
                "section": "recommendation",
                "defaults": LABEL_SECTION_DEFAULTS["recommendation"],
            },
        ),
        (
            "label.conflict_rules",
            {
                "target": "label",
                "section": "conflict_rules",
                "defaults": LABEL_SECTION_DEFAULTS["conflict_rules"],
            },
        ),
    ]
)

# 进程级节存储：app_config 键名 → 该节的用户覆盖值（原始覆盖，未合并默认）
_section_store: dict[str, dict[str, Any]] = {}


def deep_merge(defaults: dict, overrides: dict) -> dict:
    """叶子级深度合并 / Leaf-level deep merge.

    语义：
    - 双方均为 dict 的键递归合并（嵌套 dict 内新增/覆盖叶子互不影响）；
    - 其余类型（list、标量、None、类型不匹配的 dict）整体替换，不逐项合并；
    - 返回全新结构，不修改入参。

    升级语义：新版本新增的默认叶子会自动出现在合并结果中（覆盖不含该键），
    用户自定义叶子继续生效；「删除某个默认叶子」不支持，需整节 reset。
    """
    merged = deepcopy(defaults)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def get_section_defaults(section_key: str) -> dict:
    """获取某节的内置默认值（深拷贝，调用方可安全修改）。"""
    spec = SECTION_REGISTRY.get(section_key)
    if spec is None:
        raise KeyError(f"未注册的配置节: {section_key}")
    return deepcopy(spec["defaults"])


def get_section_config(section_key: str) -> dict:
    """获取某节的有效配置：内置默认 ← DB 覆盖深度合并。

    store 中无该键覆盖（未加载/无 DB/键缺失）时返回内置默认的深拷贝。
    """
    spec = SECTION_REGISTRY.get(section_key)
    if spec is None:
        raise KeyError(f"未注册的配置节: {section_key}")
    override = _section_store.get(section_key)
    if override is None:
        return deepcopy(spec["defaults"])
    return deep_merge(spec["defaults"], override)


def get_sections_for_target(target: str) -> dict[str, dict[str, Any]]:
    """组装某个 facade（"strategy"/"label"）的全部节，键为顶层节名。"""
    return {
        spec["section"]: get_section_config(key)
        for key, spec in SECTION_REGISTRY.items()
        if spec["target"] == target
    }


def update_section_store(section_key: str, data: dict) -> None:
    """写侧辅助：写入某节的覆盖值（应与 DB 落库同事务流程调用）。"""
    if section_key not in SECTION_REGISTRY:
        raise KeyError(f"未注册的配置节: {section_key}")
    if not isinstance(data, dict):
        raise ValueError(f"配置节 [{section_key}] 覆盖值必须是 dict")
    _section_store[section_key] = deepcopy(data)


def clear_section_store(section_key: str | None = None) -> None:
    """写侧辅助：清除节覆盖、回退内置默认（None = 全部清除）。"""
    if section_key is None:
        _section_store.clear()
        return
    _section_store.pop(section_key, None)


async def load_section_configs(db: Any) -> None:
    """启动时从 app_config 表读取各节键的 JSON 覆盖值填充 _section_store。

    键缺失、值为空或 JSON 非法（非 JSON / 非 dict）时跳过该键，
    读取方回退内置默认，不因脏数据阻塞启动。

    Args:
        db: 可执行 select 的 AsyncSession（由 lifespan 在 init_db 后传入）。
    """
    # 延迟导入避免 core.config_sections ↔ models.database 初始化阶段循环引用
    from sqlalchemy import select

    from backend.models.database import AppConfig

    _section_store.clear()
    result = await db.execute(
        select(AppConfig.key_name, AppConfig.key_value).where(
            AppConfig.key_name.in_(SECTION_REGISTRY)
        )
    )
    loaded_keys: list[str] = []
    for key_name, key_value in result.all():
        if key_value is None:
            continue
        try:
            data = json.loads(str(key_value))
        except (TypeError, ValueError) as exc:
            logger.warning(f"配置节 [{key_name}] JSON 解析失败，回退内置默认: {exc}")
            continue
        if not isinstance(data, dict):
            logger.warning(f"配置节 [{key_name}] 值不是 JSON 对象，回退内置默认")
            continue
        _section_store[key_name] = data
        loaded_keys.append(key_name)
    logger.info(
        f"配置节存储加载完成: {len(loaded_keys)}/{len(SECTION_REGISTRY)} 键存在 DB 覆盖"
        f"{loaded_keys if loaded_keys else ''}"
    )


# ============================================================================
# 一次性迁移：旧 YAML 文件差异节导入 DB（one-shot migration, §3.4）
# ============================================================================

# 旧 YAML 文件位置（facade 目标 → 文件路径，相对 cwd；与旧 StrategyConfig/
# LabelConfig 构造默认值一致）。仅在 DB 无任何节键的过渡期读取，仓库已移除
# 这两个文件；部署卷内残留副本由迁移逻辑读一次后不再触碰。
LEGACY_YAML_FILES: OrderedDict[str, str] = OrderedDict(
    [
        ("strategy", "config/strategies.yaml"),
        ("label", "config/labels.yaml"),
    ]
)

# 剪枝哨兵：子树与内置默认无差异，无需落库
_PRUNED = object()


def _prune_default_equal_leaves(defaults: Any, value: Any) -> Any:
    """递归剔除与内置默认相同的叶子/子树，返回仅含差异的覆盖树。

    返回 ``_PRUNED`` 表示整棵子树与默认一致（或为其值等价子集），无需落库；
    DB 只存用户差异，升级变更默认值时未改动叶子自动跟随新默认，与
    SectionConfigService.save_section 的「无覆盖=用默认」语义保持一致。
    """
    if isinstance(defaults, dict) and isinstance(value, dict):
        pruned: dict[str, Any] = {}
        for key, child in value.items():
            if key in defaults:
                kept = _prune_default_equal_leaves(defaults[key], child)
                if kept is not _PRUNED:
                    pruned[key] = kept
            else:
                # 默认中不存在的键：用户自定义，整体保留
                pruned[key] = deepcopy(child)
        return pruned if pruned else _PRUNED
    if value == defaults:
        return _PRUNED
    return deepcopy(value)


def _load_yaml_mapping(path: Path) -> dict[str, Any] | None:
    """容错读取旧 YAML 文件；缺失/解析失败/非对象时返回 None（记 warning）。"""
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        logger.warning(f"旧配置文件 [{path}] 读取失败，跳过该文件迁移: {exc}")
        return None
    if not isinstance(value, dict):
        logger.warning(f"旧配置文件 [{path}] 顶层不是对象，跳过该文件迁移")
        return None
    return value


async def migrate_yaml_files_to_db(
    db: Any,
    *,
    strategies_path: str | Path | None = None,
    labels_path: str | Path | None = None,
) -> list[str]:
    """一次性迁移：把旧 YAML 文件中与默认有差异的节导入 app_config 节键。

    语义（docs/plans/2026-08-16-unified-config-store.md §3.4）：
    - 仅当 DB 无任何 strategy.*/label.* 节键时执行；已迁移过或用户已通过
      WebUI/API 配置（存在任一节键）则整体跳过，不覆盖已有键（幂等）；
    - 逐节与内置默认深度比较并叶子级剪枝，仅把存在差异的节写入覆盖键；
      与默认完全一致的节不插键，保持「无覆盖=用默认」；
    - 旧文件不存在或解析失败则跳过对应文件，不阻塞启动。

    Args:
        db: AsyncSession（迁移在函数内单事务提交）。
        strategies_path: 旧 config/strategies.yaml 路径（默认按 cwd 相对解析，
            测试可传入 tmp_path 覆盖）。
        labels_path: 旧 config/labels.yaml 路径。

    Returns:
        本次导入的节键列表（无导入时为空列表）。

    Note:
        同步路径 ``init_database`` 不执行本迁移：它仅建表/插默认行，而迁移
        依赖节注册表与磁盘旧文件；Setup 完成后应用重启必然进入 lifespan
        异步路径，迁移在此统一执行，避免出现双份实现漂移。
    """
    from sqlalchemy import select

    from backend.models.database import AppConfig

    if strategies_path is None:
        strategies_path = LEGACY_YAML_FILES["strategy"]
    if labels_path is None:
        labels_path = LEGACY_YAML_FILES["label"]
    paths = {"strategy": Path(strategies_path), "label": Path(labels_path)}

    # 1. DB 已有任一节键 → 视为已迁移或用户已配置，整体跳过
    result = await db.execute(
        select(AppConfig.key_name).where(AppConfig.key_name.in_(SECTION_REGISTRY))
    )
    existing_keys = {row[0] for row in result.all()}
    if existing_keys:
        logger.debug(f"检测到已存在节配置键，跳过旧 YAML 迁移: {sorted(existing_keys)}")
        return []

    # 2. 容错读取旧文件（文件 IO/YAML 解析放线程池，避免阻塞事件循环）
    file_sections: dict[str, dict[str, Any]] = {}
    for target, path in paths.items():
        if path.is_file():
            mapping = await asyncio.to_thread(_load_yaml_mapping, path)
            if mapping is not None:
                file_sections[target] = mapping

    # 3. 逐节剪枝，仅保留与内置默认有差异的节
    imported_keys: list[str] = []
    pending: list[AppConfig] = []
    for section_key, spec in SECTION_REGISTRY.items():
        mapping = file_sections.get(spec["target"])
        if mapping is None or spec["section"] not in mapping:
            continue
        raw_section = mapping[spec["section"]]
        if not isinstance(raw_section, dict):
            logger.warning(
                f"旧文件节 [{spec['target']}.{spec['section']}] 不是对象，跳过导入"
            )
            continue
        override = _prune_default_equal_leaves(spec["defaults"], raw_section)
        if override is _PRUNED:
            continue
        pending.append(
            AppConfig(
                key_name=section_key,
                key_value=json.dumps(override, ensure_ascii=False, sort_keys=True),
                description=section_key,
            )
        )
        imported_keys.append(section_key)

    # 4. 单事务提交，失败回滚由调用方决定是否重试（幂等语义保证安全）
    if pending:
        try:
            db.add_all(pending)
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        logger.info(f"旧 YAML 配置迁移完成，导入差异节: {imported_keys}")

    return imported_keys
