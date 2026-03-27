"""PR标签服务

负责AI驱动的PR标签推荐和自动应用
"""

import json
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta
from loguru import logger

from backend.core.github_app import GitHubAppClient
from backend.core.config import get_settings

settings = get_settings()


class LabelService:
    """标签服务（单例模式）"""

    _instance = None
    _lock = None
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
            import threading

            cls._lock = threading.Lock()
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
            self.__class__._initialized = True
            logger.info("LabelService单例初始化完成")

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
            self.clear_cache()
            logger.info("标签配置已重新加载")
        except Exception as e:
            logger.error(f"重新加载标签配置失败: {e}")

    def clear_cache(self):
        """清除标签缓存"""
        self._label_cache.clear()

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
        labels = self.github_app.get_repo_labels(repo_owner, repo_name)

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
    ) -> Dict[str, Any]:
        """应用推荐的标签到PR

        Args:
            repo_owner: 仓库所有者
            repo_name: 仓库名称
            pr_number: PR编号
            recommendations: AI推荐的标签列表
            confidence_threshold: 自动应用的置信度阈值
            auto_create: 是否自动创建不存在的标签

        Returns:
            应用结果：{"applied": list, "suggested": list, "created": list}
        """
        result = {
            "applied": [],  # 自动应用的标签
            "suggested": [],  # 建议的标签（低置信度）
            "created": [],  # 新创建的标签
            "failed": [],  # 应用失败的标签
        }

        # 获取仓库现有标签
        existing_labels = await self.get_repo_labels(repo_owner, repo_name)

        # 处理每个推荐标签
        for rec in recommendations:
            label_name = rec["name"]
            confidence = rec["confidence"]

            # 检查标签是否存在
            if label_name not in existing_labels:
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

            # 根据置信度决定是否自动应用
            if confidence >= confidence_threshold:
                success = self.github_app.add_labels_to_pr(
                    repo_owner, repo_name, pr_number, [label_name]
                )
                if success:
                    result["applied"].append(
                        {
                            "name": label_name,
                            "confidence": confidence,
                            "reason": rec.get("reason", ""),
                        }
                    )
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
        lines = ["## 🏷️ 标签建议\n"]

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
            lines.append("*注：这些标签置信度较低，建议由开发者确认后手动添加*\n")

        # 新创建的标签
        if results["created"]:
            lines.append(f"📝 自动创建了 {len(results['created'])} 个新标签")

        return "\n".join(lines)

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
