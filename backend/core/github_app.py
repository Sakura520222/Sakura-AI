"""GitHub App集成模块"""

import hmac
import hashlib
from typing import Optional, Dict, Any
from github import Github, GithubIntegration
from loguru import logger
from backend.core.config import get_settings

settings = get_settings()


class GitHubAppClient:
    """GitHub App客户端（线程安全单例模式）"""

    _instance = None
    _lock = None
    _initialized = False

    def __new__(cls):
        """确保只有一个实例"""
        if cls._instance is None:
            import threading

            cls._lock = threading.Lock()
            with cls._lock:
                # 双重检查
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        """初始化（只执行一次）"""
        if not self._initialized:
            self.integration = None
            self._app_client = None
            self._lock = None
            self._init_integration()
            self.__class__._initialized = True
            logger.info("GitHubAppClient单例初始化完成")

    def _init_integration(self):
        """初始化GitHub Integration"""
        try:
            self.integration = self._create_integration()
        except Exception as e:
            logger.error(f"GitHub App客户端初始化失败: {e}", exc_info=True)
            raise

    def _create_integration(self) -> GithubIntegration:
        """创建GitHub Integration实例"""
        try:
            # 获取配置
            app_id = str(settings.github_app_id)  # 确保是字符串
            private_key = settings.github_private_key

            logger.info(
                f"开始创建GitHub Integration, App ID: {app_id} (类型: {type(app_id).__name__})"
            )

            # 清理私钥格式：先处理转义换行，再去除首尾所有空白字符
            private_key = private_key.replace("\\n", "\n").strip()
            logger.debug(f"私钥处理完成，长度: {len(private_key)} 字符")

            # 验证私钥标记（使用 in 关键字比 endswith 更稳健）
            if "-----BEGIN" not in private_key:
                logger.error("私钥格式错误：缺少 BEGIN 标记")
                raise ValueError("私钥格式无效：缺少BEGIN标记")

            if "-----END" not in private_key:
                logger.error("私钥格式错误：缺少 END 标记")
                logger.debug(f"私钥结尾检查: '{private_key[-50:]}'")
                raise ValueError("私钥格式无效：缺少END标记")

            # 输出调试信息（脱敏）
            logger.debug(f"私钥预览: {private_key[:50]}...{private_key[-50:]}")

            # 创建 GithubIntegration 实例（app_id保持为字符串）
            logger.info("正在创建GithubIntegration实例...")
            integration = GithubIntegration(
                integration_id=app_id,  # 传入字符串，不转换为int
                private_key=private_key,
            )
            logger.info(f"✓ GitHub Integration创建成功, App ID: {app_id}")
            return integration

        except ValueError as e:
            logger.error(f"GitHub App配置验证失败: {e}")
            raise
        except Exception as e:
            logger.error(f"GitHub App初始化失败: {e}", exc_info=True)
            raise

    def get_app_client(self) -> Github:
        """获取App级别的GitHub客户端"""
        if self._app_client is None:
            # 使用 JWT token 访问 App 级别 API
            token = self.integration.create_jwt()
            self._app_client = Github(login_or_token=token)
        return self._app_client

    def get_installation_client(
        self, repo_owner: str, repo_name: str
    ) -> Optional[Github]:
        """获取安装级别的GitHub客户端（用于访问特定仓库）"""
        try:
            # 获取安装ID
            installation = self.integration.get_installation(
                owner=repo_owner, repo=repo_name
            )

            # 获取安装访问令牌（新版 PyGithub API）
            auth_token = self.integration.get_access_token(installation.id)
            token = auth_token.token

            # 创建客户端
            client = Github(login_or_token=token)
            logger.info(f"成功获取仓库 {repo_owner}/{repo_name} 的访问令牌")
            return client
        except Exception as e:
            logger.error(f"获取仓库 {repo_owner}/{repo_name} 的安装客户端失败: {e}")
            return None

    def get_repo_client(self, repo_owner: str, repo_name: str) -> Optional[Github]:
        """根据仓库信息获取GitHub客户端（带重试机制）"""
        max_retries = 2
        last_error = None

        for attempt in range(max_retries):
            try:
                logger.debug(
                    f"尝试获取仓库客户端 [{attempt + 1}/{max_retries}]: {repo_owner}/{repo_name}"
                )

                # 检查integration是否存在
                if self.integration is None:
                    logger.warning("Integration为空，尝试重新创建...")
                    self._init_integration()

                # 获取安装信息
                logger.debug("正在获取installation信息...")
                installation = self.integration.get_installation(
                    owner=repo_owner, repo=repo_name
                )
                logger.debug(f"获取installation成功，ID: {installation.id}")

                # 获取访问令牌（新版 PyGithub API）
                logger.debug("正在生成访问令牌...")
                auth_token = self.integration.get_access_token(installation.id)
                token = auth_token.token
                logger.debug(f"访问令牌生成成功，前缀: {token[:10]}...")

                # 创建客户端
                client = Github(login_or_token=token)
                logger.info(f"✓ 成功获取仓库 {repo_owner}/{repo_name} 的访问令牌")
                return client

            except Exception as e:
                last_error = e
                logger.error(
                    f"获取仓库客户端失败 [尝试 {attempt + 1}/{max_retries}]: {e}",
                    exc_info=True,
                )

                # 如果是最后一次尝试失败，重新创建integration
                if attempt == 0:
                    logger.warning("第一次尝试失败，重新创建Integration...")
                    try:
                        self._init_integration()
                    except Exception as init_error:
                        logger.error(f"重新创建Integration失败: {init_error}")

        # 所有尝试都失败
        logger.error(
            f"获取仓库 {repo_owner}/{repo_name} 的客户端失败，已重试 {max_retries} 次"
        )
        logger.error(f"最后错误: {last_error}")
        return None

    def get_repo_labels(
        self, repo_owner: str, repo_name: str
    ) -> Dict[str, Dict[str, Any]]:
        """获取仓库的所有标签

        Args:
            repo_owner: 仓库所有者
            repo_name: 仓库名称

        Returns:
            标签字典，格式：{标签名: {"name": str, "color": str, "description": str}}
        """
        try:
            client = self.get_repo_client(repo_owner, repo_name)
            if not client:
                logger.error(f"无法获取 {repo_owner}/{repo_name} 的客户端")
                return {}

            repo = client.get_repo(f"{repo_owner}/{repo_name}")
            labels = repo.get_labels()

            labels_dict = {}
            for label in labels:
                labels_dict[label.name] = {
                    "name": label.name,
                    "color": label.color,
                    "description": label.description or "",
                }

            logger.info(
                f"成功获取仓库 {repo_owner}/{repo_name} 的 {len(labels_dict)} 个标签"
            )
            return labels_dict

        except Exception as e:
            logger.error(f"获取仓库标签失败: {e}", exc_info=True)
            return {}

    def add_labels_to_pr(
        self,
        repo_owner: str,
        repo_name: str,
        pr_number: int,
        label_names: list,
    ) -> bool:
        """给PR添加标签

        Args:
            repo_owner: 仓库所有者
            repo_name: 仓库名称
            pr_number: PR编号
            label_names: 标签名称列表

        Returns:
            是否成功
        """
        try:
            if not label_names:
                logger.warning("标签列表为空，跳过添加")
                return False

            client = self.get_repo_client(repo_owner, repo_name)
            if not client:
                logger.error(f"无法获取 {repo_owner}/{repo_name} 的客户端")
                return False

            repo = client.get_repo(f"{repo_owner}/{repo_name}")
            pr = repo.get_pull(pr_number)

            # GitHub API 限制每次最多添加 10 个标签
            BATCH_SIZE = 10
            for i in range(0, len(label_names), BATCH_SIZE):
                batch = label_names[i : i + BATCH_SIZE]
                pr.add_to_labels(*batch)
                logger.info(f"成功给 PR #{pr_number} 添加标签: {batch}")

            return True

        except Exception as e:
            logger.error(f"给PR添加标签失败: {e}", exc_info=True)
            return False

    def remove_labels_from_pr(
        self,
        repo_owner: str,
        repo_name: str,
        pr_number: int,
        label_names: list,
    ) -> bool:
        """从PR移除标签

        Args:
            repo_owner: 仓库所有者
            repo_name: 仓库名称
            pr_number: PR编号
            label_names: 标签名称列表

        Returns:
            是否成功
        """
        try:
            if not label_names:
                return True

            client = self.get_repo_client(repo_owner, repo_name)
            if not client:
                logger.error(f"无法获取 {repo_owner}/{repo_name} 的客户端")
                return False

            repo = client.get_repo(f"{repo_owner}/{repo_name}")
            pr = repo.get_pull(pr_number)

            for label_name in label_names:
                try:
                    pr.remove_from_labels(label_name)
                    logger.info(f"成功从 PR #{pr_number} 移除标签: {label_name}")
                except Exception as e:
                    logger.warning(f"移除标签 {label_name} 失败: {e}")

            return True

        except Exception as e:
            logger.error(f"从PR移除标签失败: {e}", exc_info=True)
            return False

    def create_label(
        self,
        repo_owner: str,
        repo_name: str,
        label_name: str,
        color: str = "0366d6",
        description: str = "",
    ) -> bool:
        """创建新标签

        Args:
            repo_owner: 仓库所有者
            repo_name: 仓库名称
            label_name: 标签名称
            color: 标签颜色（6位十六进制）
            description: 标签描述

        Returns:
            是否成功
        """
        try:
            client = self.get_repo_client(repo_owner, repo_name)
            if not client:
                logger.error(f"无法获取 {repo_owner}/{repo_name} 的客户端")
                return False

            repo = client.get_repo(f"{repo_owner}/{repo_name}")

            # 检查标签是否已存在
            try:
                repo.get_label(label_name)
                logger.info(f"标签 {label_name} 已存在，跳过创建")
                return True
            except Exception:
                # 标签不存在，继续创建
                pass

            repo.create_label(name=label_name, color=color, description=description)
            logger.info(f"成功创建标签: {label_name} (颜色: {color})")
            return True

        except Exception as e:
            logger.error(f"创建标签失败: {e}", exc_info=True)
            return False

    def has_existing_review(
        self,
        repo_owner: str,
        repo_name: str,
        pr_number: int,
        bot_username: str,
        event: str,
    ) -> bool:
        """检查是否已存在相同类型的Review（幂等性检查）

        Args:
            repo_owner: 仓库所有者
            repo_name: 仓库名称
            pr_number: PR编号
            bot_username: 机器人用户名
            event: Review事件类型 (APPROVE, REQUEST_CHANGES, COMMENT)

        Returns:
            是否已存在相同类型的Review
        """
        try:
            client = self.get_repo_client(repo_owner, repo_name)
            if not client:
                logger.warning(
                    f"无法获取 {repo_owner}/{repo_name} 的客户端，跳过幂等性检查"
                )
                return False

            repo = client.get_repo(f"{repo_owner}/{repo_name}")
            pr = repo.get_pull(pr_number)

            # 获取所有Reviews
            reviews = pr.get_reviews()

            # 检查是否有来自机器人的相同类型的Review
            for review in reviews:
                if (
                    review.user.login == bot_username
                    and review.state.upper() == event.upper()
                ):
                    logger.info(
                        f"发现已存在的Review: {repo_owner}/{repo_name}#{pr_number}, "
                        f"state={review.state}, user={review.user.login}, "
                        f"submitted_at={review.submitted_at}"
                    )
                    return True

            return False

        except Exception as e:
            logger.error(f"检查现有Review失败: {e}", exc_info=True)
            # 出错时返回False，允许继续提交
            return False

    def submit_review(
        self,
        repo_owner: str,
        repo_name: str,
        pr_number: int,
        event: str,
        body: str,
        bot_username: str = None,
        enable_idempotency_check: bool = True,
    ) -> bool:
        """提交审查决定到GitHub

        Args:
            repo_owner: 仓库所有者
            repo_name: 仓库名称
            pr_number: PR编号
            event: Review事件类型 (APPROVE, REQUEST_CHANGES, COMMENT)
            body: Review评论内容
            bot_username: 机器人用户名（用于幂等性检查）
            enable_idempotency_check: 是否启用幂等性检查

        Returns:
            是否成功提交
        """
        try:
            client = self.get_repo_client(repo_owner, repo_name)
            if not client:
                logger.error(f"无法获取 {repo_owner}/{repo_name} 的客户端")
                return False

            repo = client.get_repo(f"{repo_owner}/{repo_name}")
            pr = repo.get_pull(pr_number)

            # 幂等性检查：避免重复提交相同类型的Review
            if enable_idempotency_check and bot_username:
                if self.has_existing_review(
                    repo_owner, repo_name, pr_number, bot_username, event
                ):
                    logger.info(
                        f"跳过重复提交Review: {repo_owner}/{repo_name}#{pr_number}, "
                        f"event={event}"
                    )
                    return True

            # 提交Review
            pr.create_review(event=event, body=body)

            logger.info(
                f"✅ 成功提交Review: {repo_owner}/{repo_name}#{pr_number}, "
                f"event={event}, body_length={len(body)}"
            )
            return True

        except Exception as e:
            logger.error(f"提交Review失败: {e}", exc_info=True)
            return False

    def submit_review_with_inline_comments(
        self,
        repo_owner: str,
        repo_name: str,
        pr_number: int,
        event: str,
        body: str,
        inline_comments: list = None,
        bot_username: str = None,
        enable_idempotency_check: bool = True,
    ) -> bool:
        """提交审查决定到GitHub（包含行内评论）

        Args:
            repo_owner: 仓库所有者
            repo_name: 仓库名称
            pr_number: PR编号
            event: Review事件类型 (APPROVE, REQUEST_CHANGES, COMMENT)
            body: Review评论内容
            inline_comments: 行内评论列表，格式：[{"path": str, "line": int, "body": str}]
            bot_username: 机器人用户名（用于幂等性检查）
            enable_idempotency_check: 是否启用幂等性检查

        Returns:
            是否成功提交
        """
        try:
            client = self.get_repo_client(repo_owner, repo_name)
            if not client:
                logger.error(f"无法获取 {repo_owner}/{repo_name} 的客户端")
                return False

            repo = client.get_repo(f"{repo_owner}/{repo_name}")
            pr = repo.get_pull(pr_number)

            # 幂等性检查：避免重复提交相同类型的Review
            if enable_idempotency_check and bot_username:
                if self.has_existing_review(
                    repo_owner, repo_name, pr_number, bot_username, event
                ):
                    logger.info(
                        f"跳过重复提交Review: {repo_owner}/{repo_name}#{pr_number}, "
                        f"event={event}"
                    )
                    return True

            # 构建行内评论格式
            comments = []
            if inline_comments:
                for comment in inline_comments:
                    comments.append(
                        {
                            "path": comment.get("file_path"),
                            "line": comment.get("line_number"),
                            "body": comment.get("body", ""),
                        }
                    )

            # 提交Review（包含行内评论）
            pr.create_review(event=event, body=body, comments=comments)

            logger.info(
                f"✅ 成功提交Review: {repo_owner}/{repo_name}#{pr_number}, "
                f"event={event}, body_length={len(body)}, inline_comments={len(comments)}"
            )
            return True

        except Exception as e:
            # 安全提取错误信息，避免PyGithub内部的KeyError导致信息丢失
            error_type = type(e).__name__

            if hasattr(e, "status") and hasattr(e, "data"):
                logger.error(f"提交Review失败: GitHub API返回错误 (status={e.status})")
                logger.error(f"响应数据: {e.data}")
                if isinstance(e.data, dict):
                    msg = e.data.get("message") or e.data.get("error", "未知错误")
                    logger.error(f"错误信息: {msg}")
            else:
                logger.error(f"提交Review失败: {error_type}: {str(e)}")

            logger.debug("完整异常信息:", exc_info=True)
            return False

    def get_bot_username(self, repo_owner: str = None, repo_name: str = None) -> str:
        """获取机器人用户名（用于幂等性检查）

        Args:
            repo_owner: 仓库所有者（已废弃，保留参数兼容性）
            repo_name: 仓库名称（已废弃，保留参数兼容性）

        Returns:
            机器人用户名或App slug
        """
        try:
            # 使用App客户端而不是Installation客户端
            # Installation Token无法访问 /user 端点，需要使用App Token
            app_client = self.get_app_client()
            if not app_client:
                logger.warning("无法获取App客户端")
                return None

            # 获取App信息（使用App Token可以访问）
            app = app_client.get_app()

            # 返回App的slug作为标识符
            # GitHub App的slug格式通常是: username-appname
            app_slug = app.slug

            logger.debug(f"成功获取GitHub App标识: {app_slug}")
            return app_slug

        except Exception as e:
            logger.error(f"获取机器人用户名失败: {e}")
            logger.warning("将使用配置文件中的bot_username作为备选")
            # 备选方案：从配置文件读取
            return getattr(settings, "bot_username", None)


def verify_webhook_signature(payload: bytes, signature: str) -> bool:
    """验证Webhook签名"""
    try:
        # GitHub签名格式: sha256=<hash>
        if not signature.startswith("sha256="):
            logger.warning(f"无效的签名格式: {signature}")
            return False

        # 提取签名哈希
        hash_signature = signature.split("=")[1]

        # 计算预期签名
        secret = settings.github_webhook_secret.encode("utf-8")
        expected_signature = hmac.new(secret, payload, hashlib.sha256).hexdigest()

        # 使用安全的字符串比较
        is_valid = hmac.compare_digest(hash_signature, expected_signature)

        if not is_valid:
            logger.warning("Webhook签名验证失败")

        return is_valid
    except Exception as e:
        logger.error(f"验证Webhook签名时出错: {e}")
        return False


def extract_pr_info_from_webhook(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """从Webhook payload中提取PR信息"""
    try:
        # 检查是否为PR事件
        action = payload.get("action")
        pull_request = payload.get("pull_request")
        repository = payload.get("repository")
        installation = payload.get("installation")

        if not pull_request or not repository or not installation:
            logger.warning("Webhook payload中缺少必要字段")
            return None

        # 提取信息
        pr_info = {
            "action": action,
            "pr_id": pull_request["id"],
            "pr_number": pull_request["number"],
            "repo_owner": repository["owner"]["login"],
            "repo_name": repository["name"],
            "repo_full_name": repository["full_name"],
            "installation_id": installation["id"],
            "author": pull_request["user"]["login"],
            "title": pull_request["title"],
            "branch": pull_request["head"]["ref"],
            "base_branch": pull_request["base"]["ref"],
            "diff_url": pull_request["diff_url"],
            "patch_url": pull_request["patch_url"],
            "html_url": pull_request["html_url"],
            "state": pull_request["state"],
            "draft": pull_request.get("draft", False),
            "merged": pull_request.get("merged", False),
        }

        logger.info(
            f"成功提取PR信息: {pr_info['repo_full_name']}#{pr_info['pr_number']}"
        )
        return pr_info

    except Exception as e:
        logger.error(f"提取PR信息时出错: {e}")
        return None


async def get_pr_info_from_url(pr_url: str) -> Optional[Dict[str, Any]]:
    """从PR URL获取完整信息（模拟webhook payload格式）

    Args:
        pr_url: PR URL，格式如 https://github.com/owner/repo/pull/123

    Returns:
        与webhook payload格式一致的pr_info字典

    Raises:
        ValueError: URL格式无效
        Exception: 获取PR信息失败
    """
    import re

    try:
        # 1. 解析 URL
        # 支持多种格式：
        # - https://github.com/owner/repo/pull/123
        # - https://github.com/owner/repo/pull/123/files
        # - github.com/owner/repo/pull/123
        pattern = r"github\.com/([^/]+)/([^/]+)/pull/(\d+)"
        match = re.search(pattern, pr_url)

        if not match:
            raise ValueError(
                f"无效的PR URL格式: {pr_url}\n"
                f"正确格式: https://github.com/owner/repo/pull/123"
            )

        repo_owner = match.group(1)
        repo_name = match.group(2)
        pr_number = int(match.group(3))

        logger.info(f"解析PR URL成功: {repo_owner}/{repo_name}#{pr_number}")

        # 2. 获取 GitHub 客户端
        client_instance = GitHubAppClient()
        client = client_instance.get_repo_client(repo_owner, repo_name)

        if not client:
            raise Exception(
                f"无法获取仓库 {repo_owner}/{repo_name} 的访问权限\n"
                f"请确保 GitHub App 已安装到此仓库"
            )

        # 3. 获取 installation_id
        try:
            installation = client_instance.integration.get_installation(
                owner=repo_owner, repo=repo_name
            )
            installation_id = installation.id
        except Exception as e:
            raise Exception(
                f"无法获取 installation_id: {e}\n请确保 GitHub App 已安装到此仓库"
            )

        # 4. 获取 PR 详细信息
        repo_full_name = f"{repo_owner}/{repo_name}"
        repo = client.get_repo(repo_full_name)
        pr = repo.get_pull(pr_number)

        # 5. 构造与 webhook 完全一致的 pr_info 字典
        pr_info = {
            "action": "manual",  # 手动触发
            "pr_id": pr.id,
            "pr_number": pr.number,
            "repo_owner": repo_owner,
            "repo_name": repo_name,
            "repo_full_name": repo_full_name,
            "installation_id": installation_id,
            "author": pr.user.login,
            "title": pr.title,
            "branch": pr.head.ref,
            "base_branch": pr.base.ref,
            "diff_url": pr.diff_url,
            "patch_url": pr.patch_url,
            "html_url": pr.html_url,
            "state": pr.state,
            "draft": pr.draft,
            "merged": pr.merged,
        }

        logger.info(
            f"成功获取PR信息: {repo_full_name}#{pr_number}, "
            f"author={pr_info['author']}, state={pr.state}"
        )

        return pr_info

    except ValueError:
        raise
    except Exception as e:
        logger.error(f"从URL获取PR信息失败: {e}", exc_info=True)
        raise
