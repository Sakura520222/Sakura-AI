"""PR标签服务

负责AI驱动的PR标签推荐和自动应用
"""

import asyncio
import json
import re
import threading
from typing import Dict, List, Optional, Any, Tuple
from datetime import datetime, timedelta
from loguru import logger

from backend.core.github_app import GitHubAppClient
from backend.core.config import get_settings

settings = get_settings()


class LabelService:
    """标签服务（单例模式）"""

    _instance = None
    _lock = threading.Lock()
    _initialized = False

    # 默认标签配置（当仓库没有标签时使用）
    DEFAULT_LABELS = {
        "bug": {"color": "d73a4a", "description": "Something isn't working"},
        "documentation": {
            "color": "0075ca",
            "description": "Improvements or additions to documentation",
        },
        "duplicate": {
            "color": "cfd3d7",
            "description": "This issue or pull request already exists",
        },
        "enhancement": {"color": "a2eeef", "description": "New feature or request"},
        "good first issue": {"color": "7057ff", "description": "Good for newcomers"},
        "help wanted": {"color": "008672", "description": "Extra attention is needed"},
        "invalid": {"color": "e4e669", "description": "This doesn't seem right"},
        "question": {
            "color": "d876e3",
            "description": "Further information is requested",
        },
        "wontfix": {"color": "ffffff", "description": "This will not be worked on"},
        "refactor": {
            "color": "fbca04",
            "description": "Code refactoring (non-functional change)",
        },
        "performance": {"color": "5319e7", "description": "Performance optimization"},
        "test": {"color": "bfd4f2", "description": "Test related changes"},
        "dependencies": {"color": "0366d6", "description": "Dependency updates"},
        "ci": {"color": "ffefdb", "description": "CI/CD configuration changes"},
        "style": {"color": "c5def5", "description": "Code style adjustments"},
        "build": {"color": "ededed", "description": "Build system changes"},
    }

    def __new__(cls):
        """确保只有一个实例"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        """初始化（只执行一次）"""
        if not self._initialized:
            self.github_app = GitHubAppClient()
            # 标签缓存：{repo_full_name: {"labels": dict, "updated_at": datetime}}
            self._label_cache: Dict[str, Dict[str, Any]] = {}
            self._cache_ttl = timedelta(hours=1)  # 缓存1小时
            # 标签冲突规则（从 labels.yaml 加载）
            self._conflict_rules: Dict[str, List[str]] = {}
            self._load_conflict_rules()
            self.__class__._initialized = True
            logger.info("LabelService单例初始化完成")

    def _load_conflict_rules(self) -> None:
        """从 labels.yaml 加载标签冲突规则"""
        try:
            from backend.core.config import get_label_config

            config = get_label_config()
            self._conflict_rules = config.get_conflict_rules()
            if self._conflict_rules:
                logger.info(
                    f"已加载 {len(self._conflict_rules)} 条标签冲突规则: "
                    f"{self._conflict_rules}"
                )
        except Exception as e:
            logger.warning(f"加载标签冲突规则失败，将使用默认规则: {e}")
            self._conflict_rules = self._default_conflict_rules()

    @staticmethod
    def _default_conflict_rules() -> Dict[str, List[str]]:
        """默认标签冲突规则

        规则含义：当 PR 已有 key 中的标签时，不应自动添加 value 列表中的标签。
        """
        return {
            "enhancement": ["bug"],
            "refactor": ["bug"],
            "documentation": ["bug", "enhancement"],
            "test": ["bug", "enhancement"],
        }

    def check_label_conflict(
        self, existing_labels: List[str], new_label: str
    ) -> Optional[str]:
        """检查新标签是否与PR已有标签存在冲突

        Args:
            existing_labels: PR 已有的标签列表
            new_label: 待添加的新标签名称

        Returns:
            冲突的已有标签名称（如果存在冲突），否则返回 None
        """
        for existing in existing_labels:
            # 检查 existing 标签是否禁止 new_label
            blocked = self._conflict_rules.get(existing, [])
            if new_label in blocked:
                return existing
        return None

    def _get_default_labels(self) -> dict:
        """获取默认标签（优先从 labels.yaml 加载）"""
        try:
            from backend.core.config import get_label_config

            yaml_labels = get_label_config().get_labels()
            if yaml_labels:
                return yaml_labels
        except Exception:
            pass
        return self.DEFAULT_LABELS

    def reload_labels(self):
        """重新加载标签配置"""
        try:
            from backend.core.config import reload_label_config

            reload_label_config()
            self._load_conflict_rules()
            self.clear_cache()
            logger.info("标签配置已重新加载")
        except Exception as e:
            logger.error(f"重新加载标签配置失败: {e}")

    async def get_repo_labels(
        self, repo_owner: str, repo_name: str, use_cache: bool = True
    ) -> Dict[str, Dict[str, Any]]:
        """获取仓库的标签列表（支持缓存）

        Args:
            repo_owner: 仓库所有者
            repo_name: 仓库名称
            use_cache: 是否使用缓存

        Returns:
            标签字典，格式：{标签名: {"name": str, "color": str, "description": str}}
        """
        repo_full_name = f"{repo_owner}/{repo_name}"
        current_time = datetime.now()

        # 检查缓存
        if use_cache and repo_full_name in self._label_cache:
            cache_entry = self._label_cache[repo_full_name]
            if current_time - cache_entry["updated_at"] < self._cache_ttl:
                logger.debug(f"使用缓存的标签列表: {repo_full_name}")
                return cache_entry["labels"]

        # 从GitHub获取
        logger.info(f"从GitHub获取标签列表: {repo_full_name}")
        labels = await asyncio.to_thread(
            self.github_app.get_repo_labels, repo_owner, repo_name
        )

        # 如果仓库没有任何标签，使用默认标签
        if not labels:
            logger.warning(f"仓库 {repo_full_name} 没有标签，使用默认标签列表")
            labels = self._get_default_labels()

        # 更新缓存
        self._label_cache[repo_full_name] = {
            "labels": labels,
            "updated_at": current_time,
        }

        return labels

    async def get_pr_existing_labels(
        self, repo_owner: str, repo_name: str, pr_number: int
    ) -> List[str] | None:
        """获取PR当前的已有标签

        Args:
            repo_owner: 仓库所有者
            repo_name: 仓库名称
            pr_number: PR编号

        Returns:
            PR已有标签名称列表；获取失败时返回 None（与空列表区分）
        """
        try:
            labels = await asyncio.to_thread(
                self.github_app.get_pr_labels,
                repo_owner,
                repo_name,
                pr_number,
            )
            return labels
        except Exception as e:
            logger.error(f"获取PR已有标签失败: {e}", exc_info=True)
            return None

    def format_labels_for_ai(self, labels: Dict[str, Dict[str, Any]]) -> str:
        """格式化标签列表供AI理解

        Args:
            labels: 标签字典

        Returns:
            格式化的标签描述文本
        """
        lines = ["## 可用的PR标签\n"]

        for label_name, label_info in labels.items():
            desc = label_info.get("description", "")
            lines.append(f"- **{label_name}**: {desc}")

        lines.append(
            "\n请从上述标签中选择最合适的标签（可以选择多个），"
            "并根据代码变更的实际情况给出推荐。"
        )

        return "\n".join(lines)

    def parse_ai_label_recommendation(self, ai_response: str) -> List[Dict[str, Any]]:
        """解析AI的标签推荐结果

        Args:
            ai_response: AI返回的标签推荐文本

        Returns:
            推荐标签列表，格式：[{"name": str, "confidence": float, "reason": str}]
        """
        recommendations = []

        try:
            # 尝试解析JSON格式
            if "```json" in ai_response:
                # 提取JSON代码块
                start = ai_response.find("```json") + 7
                end = ai_response.find("```", start)
                json_str = ai_response[start:end].strip()
                data = json.loads(json_str)

                if isinstance(data, dict) and "labels" in data:
                    for item in data["labels"]:
                        recommendations.append(
                            {
                                "name": item.get("name", ""),
                                "confidence": float(item.get("confidence", 0.5)),
                                "reason": item.get("reason", ""),
                            }
                        )
            else:
                # 尝试直接解析整个响应为JSON
                data = json.loads(ai_response)
                if isinstance(data, dict) and "labels" in data:
                    for item in data["labels"]:
                        recommendations.append(
                            {
                                "name": item.get("name", ""),
                                "confidence": float(item.get("confidence", 0.5)),
                                "reason": item.get("reason", ""),
                            }
                        )

            logger.info(f"成功解析AI标签推荐，共 {len(recommendations)} 个")
            return recommendations

        except json.JSONDecodeError:
            # 如果不是JSON格式，尝试文本解析
            logger.warning("AI响应不是JSON格式，尝试文本解析")
            return self._parse_text_labels(ai_response)

        except Exception as e:
            logger.error(f"解析AI标签推荐失败: {e}", exc_info=True)
            return []

    def _parse_text_labels(self, text: str) -> List[Dict[str, Any]]:
        """从文本中解析标签推荐（备用方案）

        Args:
            text: AI返回的文本

        Returns:
            推荐标签列表
        """
        recommendations = []
        lines = text.split("\n")

        for line in lines:
            line = line.strip()
            # 查找格式：- 标签名 (置信度%) - 理由
            if line.startswith("-") or line.startswith("*"):
                # 提取标签名
                parts = line[1:].strip().split("(")
                if len(parts) > 0:
                    label_name = parts[0].strip()

                    # 提取置信度
                    confidence = 0.5
                    reason = ""
                    if len(parts) > 1:
                        rest = parts[1]
                        if "%" in rest:
                            confidence_str = rest.split("%")[0].strip()
                            try:
                                confidence = float(confidence_str) / 100
                            except ValueError:
                                pass

                        # 提取理由
                        if "-" in rest:
                            reason_parts = rest.split("-", 1)
                            if len(reason_parts) > 1:
                                reason = reason_parts[1].strip()

                    if label_name:
                        recommendations.append(
                            {
                                "name": label_name,
                                "confidence": confidence,
                                "reason": reason,
                            }
                        )

        return recommendations

    async def apply_labels_to_pr(
        self,
        repo_owner: str,
        repo_name: str,
        pr_number: int,
        recommendations: List[Dict[str, Any]],
        confidence_threshold: float = 0.7,
        auto_create: bool = False,
        existing_labels: List[str] | None = None,
    ) -> Dict[str, Any]:
        """应用推荐的标签到PR

        Args:
            repo_owner: 仓库所有者
            repo_name: 仓库名称
            pr_number: PR编号
            recommendations: AI推荐的标签列表
            confidence_threshold: 自动应用的置信度阈值
            auto_create: 是否自动创建不存在的标签
            existing_labels: PR已有的标签列表（用于冲突检测）

        Returns:
            应用结果：{"applied": list, "suggested": list, "created": list, "conflict_blocked": list}
        """
        result = {
            "applied": [],  # 自动应用的标签
            "suggested": [],  # 建议的标签（低置信度）
            "created": [],  # 新创建的标签
            "failed": [],  # 应用失败的标签
            "conflict_blocked": [],  # 因与已有标签冲突而跳过的标签
        }

        # 如果未传入已有标签，尝试获取
        labels_fetch_failed = False
        if existing_labels is None:
            existing_labels = await self.get_pr_existing_labels(
                repo_owner, repo_name, pr_number
            )
            # None 表示获取失败（与空列表区分）
            if existing_labels is None:
                labels_fetch_failed = True
                existing_labels = []
                logger.warning(
                    f"PR #{pr_number} 已有标签获取失败，将禁用自动应用仅输出建议"
                )

        # 维护动态标签集合：初始为 PR 已有标签 + 每次成功应用后即时更新
        effective_labels = set(existing_labels)

        # 获取仓库现有标签
        repo_labels = await self.get_repo_labels(repo_owner, repo_name)

        # 处理每个推荐标签
        for rec in recommendations:
            label_name = rec["name"]
            confidence = rec["confidence"]

            # 跳过PR已有的标签（无需重复添加）
            if label_name in effective_labels:
                logger.info(f"标签 {label_name} 已存在于 PR #{pr_number}，跳过")
                continue

            # 检查标签是否与已有标签（含本次已应用的）冲突
            conflict_with = self.check_label_conflict(
                list(effective_labels), label_name
            )
            if conflict_with:
                logger.info(
                    f"标签 {label_name} 与已有标签 {conflict_with} 冲突，跳过自动应用"
                )
                result["conflict_blocked"].append(
                    {
                        "name": label_name,
                        "confidence": confidence,
                        "reason": rec.get("reason", ""),
                        "conflict_with": conflict_with,
                    }
                )
                continue

            # 检查标签是否存在于仓库
            if label_name not in repo_labels:
                if auto_create:
                    # 自动创建标签
                    default_info = self.DEFAULT_LABELS.get(
                        label_name, {"color": "0366d6", "description": ""}
                    )
                    success = self.github_app.create_label(
                        repo_owner,
                        repo_name,
                        label_name,
                        default_info["color"],
                        default_info["description"],
                    )
                    if success:
                        result["created"].append(label_name)
                        logger.info(f"自动创建标签: {label_name}")
                    else:
                        result["failed"].append(label_name)
                        continue
                else:
                    logger.warning(f"标签 {label_name} 不存在，跳过")
                    result["failed"].append(label_name)
                    continue

            # 当已有标签获取失败时，降级为仅建议不自动应用
            if labels_fetch_failed:
                result["suggested"].append(
                    {
                        "name": label_name,
                        "confidence": confidence,
                        "reason": rec.get("reason", ""),
                    }
                )
                continue

            # 根据置信度决定是否自动应用
            if confidence >= confidence_threshold:
                success = await asyncio.to_thread(
                    self.github_app.add_labels_to_pr,
                    repo_owner,
                    repo_name,
                    pr_number,
                    [label_name],
                )
                if success:
                    result["applied"].append(
                        {
                            "name": label_name,
                            "confidence": confidence,
                            "reason": rec.get("reason", ""),
                        }
                    )
                    # 成功应用后即时加入动态集合，后续标签的冲突检测会考虑
                    effective_labels.add(label_name)
                else:
                    result["failed"].append(label_name)
            else:
                result["suggested"].append(
                    {
                        "name": label_name,
                        "confidence": confidence,
                        "reason": rec.get("reason", ""),
                    }
                )

        return result

    def format_label_results(self, results: Dict[str, Any]) -> str:
        """格式化标签应用结果（用于评论展示）

        Args:
            results: apply_labels_to_pr 的返回结果

        Returns:
            格式化的Markdown文本
        """
        # Hidden marker to identify Sakura review label sections for checkbox toggle
        lines = ["<!-- sakura-label-section -->", "## 🏷️ 标签建议\n"]

        # 已应用的标签
        if results["applied"]:
            lines.append("### ✅ 已自动应用的标签\n")
            for item in results["applied"]:
                conf_pct = int(item["confidence"] * 100)
                reason = item.get("reason", "")
                lines.append(
                    f"- [x] **{item['name']}** ({conf_pct}%)"
                    + (f" - {reason}" if reason else "")
                )
            lines.append("")

        # 建议的标签
        if results["suggested"]:
            lines.append("### 💡 建议的标签（需确认）\n")
            for item in results["suggested"]:
                conf_pct = int(item["confidence"] * 100)
                reason = item.get("reason", "")
                lines.append(
                    f"- [ ] **{item['name']}** ({conf_pct}%)"
                    + (f" - {reason}" if reason else "")
                )
            lines.append("")
            lines.append(
                "*注：勾选复选框即可应用标签，取消勾选即可移除标签"
                "（仅 PR 作者或仓库管理员/协作者可操作）*\n"
            )

        # 新创建的标签
        if results["created"]:
            lines.append(f"📝 自动创建了 {len(results['created'])} 个新标签")

        # 因冲突被阻止的标签
        if results.get("conflict_blocked"):
            lines.append("### 🚫 因冲突未应用的标签\n")
            for item in results["conflict_blocked"]:
                conf_pct = int(item["confidence"] * 100)
                conflict_with = item.get("conflict_with", "")
                lines.append(
                    f"- ~~**{item['name']}**~~ ({conf_pct}%) - "
                    f"与已有标签 `{conflict_with}` 冲突"
                )
            lines.append(
                "\n*注：这些标签与 PR 已有标签存在语义冲突，已自动跳过。"
                "如需添加，请手动操作。*\n"
            )

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Checkbox toggle helpers (interactive label apply/remove)
    # ------------------------------------------------------------------

    @staticmethod
    def is_sakura_label_comment(body: str) -> bool:
        """Check if a comment body contains the Sakura label section marker.

        Args:
            body: The comment body text.

        Returns:
            True if the comment was generated by Sakura's label recommender.
        """
        return "sakura-label-section" in body

    @staticmethod
    def parse_label_checkboxes(body: str) -> Dict[str, bool]:
        """Extract label checkboxes and their checked state from a comment body.

        Matches patterns like:
        - ``- [x] **label-name** (85%) - reason``
        - ``- [ ] **label-name** (70%) - reason``

        Args:
            body: The comment body text.

        Returns:
            A dict mapping label name → checked state (True=checked, False=unchecked).
        """
        checkboxes: Dict[str, bool] = {}
        # Match: - [x] **label** or - [ ] **label**
        pattern = re.compile(r"^- \[([ xX])\] \*\*(.+?)\*\*", re.MULTILINE)
        for match in pattern.finditer(body):
            checked_char = match.group(1).lower()
            label_name = match.group(2).strip()
            checkboxes[label_name] = checked_char == "x"
        return checkboxes

    @staticmethod
    def parse_checkbox_changes(
        old_body: str, new_body: str
    ) -> Tuple[List[str], List[str]]:
        """Compare old and new comment bodies to detect checkbox state changes.

        Args:
            old_body: The comment body before the edit.
            new_body: The comment body after the edit.

        Returns:
            A tuple of (labels_to_add, labels_to_remove).
        """
        old_checkboxes = LabelService.parse_label_checkboxes(old_body)
        new_checkboxes = LabelService.parse_label_checkboxes(new_body)

        labels_to_add: List[str] = []
        labels_to_remove: List[str] = []

        all_labels = set(old_checkboxes.keys()) | set(new_checkboxes.keys())
        for label in all_labels:
            old_checked = old_checkboxes.get(label, False)
            new_checked = new_checkboxes.get(label, False)
            if not old_checked and new_checked:
                labels_to_add.append(label)
            elif old_checked and not new_checked:
                labels_to_remove.append(label)

        return labels_to_add, labels_to_remove

    async def handle_label_checkbox_toggle(
        self,
        repo_owner: str,
        repo_name: str,
        pr_number: int,
        labels_to_add: list[str],
        labels_to_remove: list[str],
        operator: str,
        pr_author: str,
    ) -> Dict[str, Any]:
        """Apply or remove labels based on checkbox toggle in review comment.

        Args:
            repo_owner: Repository owner.
            repo_name: Repository name.
            pr_number: PR number.
            labels_to_add: Label names to add.
            labels_to_remove: Label names to remove.
            operator: GitHub username of the person who toggled the checkbox.
            pr_author: GitHub username of the PR author.

        Returns:
            Result dict with applied/removed/failed lists.
        """
        result: Dict[str, Any] = {
            "applied": [],
            "removed": [],
            "failed": [],
        }

        # Apply labels
        for label_name in labels_to_add:
            try:
                success = await asyncio.to_thread(
                    self.github_app.add_labels_to_pr,
                    repo_owner,
                    repo_name,
                    pr_number,
                    [label_name],
                )
                if success:
                    result["applied"].append(label_name)
                    logger.info(
                        f"[checkbox-toggle] 已为 PR {repo_owner}/{repo_name}#{pr_number} "
                        f"添加标签: {label_name} (操作者: {operator})"
                    )
                else:
                    result["failed"].append(label_name)
            except Exception as e:
                logger.warning(f"[checkbox-toggle] 添加标签 {label_name} 失败: {e}")
                result["failed"].append(label_name)

        # Remove labels
        for label_name in labels_to_remove:
            try:
                success = await asyncio.to_thread(
                    self.github_app.remove_labels_from_pr,
                    repo_owner,
                    repo_name,
                    pr_number,
                    [label_name],
                )
                if success:
                    result["removed"].append(label_name)
                    logger.info(
                        f"[checkbox-toggle] 已从 PR {repo_owner}/{repo_name}#{pr_number} "
                        f"移除标签: {label_name} (操作者: {operator})"
                    )
                else:
                    result["failed"].append(label_name)
            except Exception as e:
                logger.warning(f"[checkbox-toggle] 移除标签 {label_name} 失败: {e}")
                result["failed"].append(label_name)

        return result

    def clear_cache(self, repo_full_name: Optional[str] = None):
        """清除标签缓存

        Args:
            repo_full_name: 要清除的仓库，None表示清除所有缓存
        """
        if repo_full_name:
            if repo_full_name in self._label_cache:
                del self._label_cache[repo_full_name]
                logger.info(f"已清除 {repo_full_name} 的标签缓存")
        else:
            self._label_cache.clear()
            logger.info("已清除所有标签缓存")


# 全局单例实例
label_service = LabelService()
