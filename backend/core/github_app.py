"""GitHub App集成模块"""

import hmac
import hashlib
from typing import List, Optional, Dict, Any

import httpx
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
        """初始化"""
        if not self._initialized:
            self.integration = None
            self._app_client = None
            self._lock = None
            self.__class__._initialized = True
            logger.info("GitHubAppClient单例初始化完成")

    def _init_integration(self):
        """初始化GitHub Integration"""
        try:
            self.integration = self._create_integration()
        except Exception as e:
            logger.error(f"GitHub App客户端初始化失败: {e}", exc_info=True)
            self.integration = None

    def _create_integration(self) -> Optional[GithubIntegration]:
        """创建GitHub Integration实例"""
        try:
            # 获取配置
            app_id = settings.github_app_id
            private_key = settings.github_private_key

            # 未配置时跳过初始化（bootstrap 模式或未配置 GitHub App）
            if not app_id or not private_key:
                logger.warning(
                    "GitHub App 未配置（app_id 或 private_key 为空），跳过初始化"
                )
                return None

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

    def get_app_client(self) -> Optional[Github]:
        """获取App级别的GitHub客户端"""
        if self.integration is None:
            logger.warning("GitHub Integration 未初始化，无法获取 App 客户端")
            return None
        if self._app_client is None:
            # 使用 JWT token 访问 App 级别 API
            token = self.integration.create_jwt()
            self._app_client = Github(login_or_token=token)
        return self._app_client

    async def exchange_user_code(self, code: str) -> Dict[str, Any]:
        """GitHub App user-to-server：用授权码交换 user access token。"""
        return await exchange_user_access_token(
            settings.star_aid_github_app_client_id,
            settings.star_aid_github_app_client_secret,
            code,
            settings.star_aid_github_app_callback_url or None,
        )

    async def refresh_user_token(self, refresh_token: str) -> Dict[str, Any]:
        """GitHub App user-to-server：刷新 user access token。"""
        return await refresh_user_access_token(
            settings.star_aid_github_app_client_id,
            settings.star_aid_github_app_client_secret,
            refresh_token,
        )

    async def get_user_access_token(self, user_id: int) -> Optional[str]:
        """从 star_aid 凭据服务获取可用 user access token。"""
        from backend.models.database import async_session
        from backend.services import star_aid_github_service

        if async_session is None:
            return None
        async with async_session() as session:
            token, _ = await star_aid_github_service.get_effective_access_token(
                session, int(user_id)
            )
            return token

    async def get_user_client(self, user_id: int) -> Optional[Github]:
        """基于 GitHub App user access token 创建 PyGithub 客户端。"""
        token = await self.get_user_access_token(user_id)
        if not token:
            return None
        return Github(login_or_token=token)

    def get_all_installations_with_repos(self) -> list[dict]:
        """获取所有 GitHub App 安装及其仓库列表

        Returns:
            [{"installation_id": int, "account_login": str, "account_type": str,
              "repos": [{"full_name": str, "name": str, "private": bool, "html_url": str}]}]
        """
        if not self.integration:
            logger.warning("GitHub Integration 未初始化，尝试重新初始化...")
            self._init_integration()
            if not self.integration:
                logger.error("重新初始化 GitHub Integration 仍然失败，无法获取安装仓库")
                return []

        installations = list(self.integration.get_installations())
        logger.info(f"获取到 {len(installations)} 个 installation")
        result = []
        for inst in installations:
            try:
                logger.info(
                    f"处理 installation: id={inst.id}, target_type={inst.target_type}"
                )
                repos = list(inst.get_repos())
                logger.info(f"installation {inst.id} 有 {len(repos)} 个仓库")
                # 从 html_url 解析 account_login，格式如：
                # https://github.com/settings/installations/123 → User
                # https://github.com/organizations/xxx/settings/installations/123 → Organization
                account_login = ""
                html_url = getattr(inst, "html_url", "")
                if "/organizations/" in html_url:
                    # https://github.com/organizations/OWNER/settings/installations/ID
                    parts = html_url.split("/organizations/")
                    if len(parts) > 1:
                        account_login = parts[1].split("/")[0]
                elif repos:
                    # 从第一个仓库的 full_name 提取 owner
                    account_login = repos[0].full_name.split("/")[0]

                # 回退：从原始 API 数据获取（适用于 0 仓库的新安装）
                # NOTE: raw_data/_rawData 为 PyGithub 内部属性，版本升级后可能失效
                if not account_login:
                    raw = getattr(inst, "raw_data", None) or getattr(
                        inst, "_rawData", {}
                    )
                    account_info = (
                        raw.get("account", {}) if isinstance(raw, dict) else {}
                    )
                    account_login = account_info.get("login", "")

                result.append(
                    {
                        "installation_id": inst.id,
                        "account_login": account_login,
                        "account_type": inst.target_type,
                        "repos": [
                            {
                                "full_name": repo.full_name,
                                "name": repo.name,
                                "private": repo.private,
                                "html_url": repo.html_url,
                            }
                            for repo in repos
                        ],
                    }
                )
            except Exception as e:
                logger.warning(
                    f"获取 installation {inst.id} 仓库失败: {e}", exc_info=True
                )
        return result

    def check_user_installed(self, username: Optional[str]) -> Optional[bool]:
        """轻量检查指定用户/组织是否安装 GitHub App（不拉取仓库列表）

        Args:
            username: GitHub 用户名或组织名。空值/None 返回 False。

        Returns:
            True: 已安装, False: 未安装,
            None: 无法检测（如 Integration 未初始化、API 异常）
        """
        if not username:
            return False

        if not self.integration:
            logger.warning("GitHub Integration 未初始化，尝试重新初始化...")
            self._init_integration()
            if not self.integration:
                logger.error("重新初始化 GitHub Integration 仍然失败，无法检查安装状态")
                return None

        target_username = username.lower()
        try:
            installations = list(self.integration.get_installations())
            logger.debug(
                f"轻量检查 GitHub App 安装状态，共 {len(installations)} 个 installation"
            )

            checked_accounts = []
            for inst in installations:
                account_login = ""
                raw = getattr(inst, "raw_data", None) or getattr(inst, "_rawData", {})
                account_info = raw.get("account", {}) if isinstance(raw, dict) else {}
                if isinstance(account_info, dict):
                    account_login = account_info.get("login", "") or ""

                # 个人用户 installation 的 html_url 通常是
                # https://github.com/settings/installations/ID，不包含用户名；为保持轻量
                # 检查，这里不通过 get_repos() 回退提取 owner，仅依赖 raw_data.account.login。
                if not account_login:
                    html_url = getattr(inst, "html_url", "") or ""
                    if "/organizations/" in html_url:
                        parts = html_url.split("/organizations/")
                        if len(parts) > 1:
                            account_login = parts[1].split("/")[0]

                if account_login:
                    checked_accounts.append(account_login)

                if account_login.lower() == target_username:
                    return True

            logger.debug(
                f"未匹配到 GitHub App installation: username={username}, "
                f"checked_accounts={checked_accounts}"
            )
            return False
        except Exception as e:
            logger.warning(f"轻量检查 GitHub App 安装状态失败: {e}", exc_info=True)
            return None

    def get_installation_client(
        self, repo_owner: str, repo_name: str
    ) -> Optional[Github]:
        """获取安装级别的GitHub客户端（用于访问特定仓库）"""
        try:
            if self.integration is None:
                logger.warning("GitHub Integration 未初始化")
                return None
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

                # 如果不是最后一次，等待后重试
                if attempt < max_retries - 1:
                    wait = 2
                    logger.info(f"等待 {wait} 秒后重试...")
                    import time

                    time.sleep(wait)

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

    def get_pr_labels(
        self, repo_owner: str, repo_name: str, pr_number: int
    ) -> List[str]:
        """获取PR当前的标签列表

        Args:
            repo_owner: 仓库所有者
            repo_name: 仓库名称
            pr_number: PR编号

        Returns:
            PR已有标签名称列表
        """
        try:
            client = self.get_repo_client(repo_owner, repo_name)
            if not client:
                logger.error(f"无法获取 {repo_owner}/{repo_name} 的客户端")
                return []

            repo = client.get_repo(f"{repo_owner}/{repo_name}")
            pr = repo.get_pull(pr_number)
            pr_labels = [label.name for label in pr.labels]

            logger.info(
                f"成功获取 PR {repo_owner}/{repo_name}#{pr_number} "
                f"的 {len(pr_labels)} 个标签: {pr_labels}"
            )
            return pr_labels

        except Exception as e:
            logger.error(f"获取PR标签失败: {e}", exc_info=True)
            return []

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

            # GitHub bot 用户名在 Review 中显示为 "app-slug[bot]"
            bot_names = {bot_username, f"{bot_username}[bot]"}

            # 检查是否有来自机器人的相同类型的Review
            for review in reviews:
                if (
                    review.user.login in bot_names
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

    def dismiss_bot_reviews(
        self,
        repo_owner: str,
        repo_name: str,
        pr_number: int,
        bot_username: str,
    ) -> int:
        """撤回指定PR上所有来自bot的Review

        Args:
            repo_owner: 仓库所有者
            repo_name: 仓库名称
            pr_number: PR编号
            bot_username: 机器人用户名

        Returns:
            撤回的Review数量
        """
        try:
            client = self.get_repo_client(repo_owner, repo_name)
            if not client:
                logger.warning(
                    f"无法获取 {repo_owner}/{repo_name} 的客户端，跳过撤回Review"
                )
                return 0

            repo = client.get_repo(f"{repo_owner}/{repo_name}")
            pr = repo.get_pull(pr_number)

            reviews = pr.get_reviews()
            dismissed_count = 0

            # GitHub bot 用户名在 Review 中显示为 "app-slug[bot]"
            bot_names = {bot_username, f"{bot_username}[bot]"}

            for review in reviews:
                if review.user.login in bot_names:
                    try:
                        review.dismiss(message="PR 已更新，重新进行审查")
                        dismissed_count += 1
                        logger.info(
                            f"已撤回Review: {repo_owner}/{repo_name}#{pr_number}, "
                            f"review_id={review.id}"
                        )
                    except Exception as e:
                        error_msg = str(e)
                        # 已 dismiss 过的 review 忽略
                        if "Can not dismiss a dismissed" in error_msg:
                            logger.debug(f"跳过已dismiss的Review (id={review.id})")
                        # COMMENTED 状态的 review 无法 dismiss，改为折叠旧内容
                        elif "Can not dismiss a commented" in error_msg:
                            try:
                                old_body = review.body or ""
                                if old_body:
                                    collapsed_body = (
                                        f"<details><summary>旧审查（已被新审查替代，点击展开）</summary>\n\n"
                                        f"{old_body}\n\n"
                                        f"</details>"
                                    )
                                    # PyGithub PullRequestReview 没有 url 属性，手动构造 API URL
                                    headers, _ = review._requester.requestJsonAndCheck(
                                        "PATCH",
                                        f"/repos/{repo_owner}/{repo_name}/pulls/{pr_number}/reviews/{review.id}",
                                        input={"body": collapsed_body},
                                    )
                                    logger.info(
                                        f"已折叠Review: {repo_owner}/{repo_name}#{pr_number}, "
                                        f"review_id={review.id}"
                                    )
                            except Exception as edit_err:
                                logger.warning(
                                    f"折叠Review失败 (id={review.id}): {edit_err}"
                                )
                        else:
                            logger.warning(f"撤回Review失败 (id={review.id}): {e}")

            logger.info(
                f"撤回Review完成: {repo_owner}/{repo_name}#{pr_number}, "
                f"共撤回 {dismissed_count} 条"
            )
            return dismissed_count

        except Exception as e:
            logger.error(f"撤回Review失败: {e}", exc_info=True)
            return 0

    def delete_all_bot_comments(
        self,
        repo_owner: str,
        repo_name: str,
        pr_number: int,
        bot_username: str,
    ) -> dict:
        """删除指定PR上所有来自bot的评论（Issue Comments + Review Comments）

        Args:
            repo_owner: 仓库所有者
            repo_name: 仓库名称
            pr_number: PR编号
            bot_username: 机器人用户名

        Returns:
            {"issue_comments": 删除数量, "review_comments": 删除数量}
        """
        # GitHub bot 用户名在评论中显示为 "app-slug[bot]"
        bot_names = {bot_username, f"{bot_username}[bot]"}

        issue_deleted = 0
        review_deleted = 0

        try:
            client = self.get_repo_client(repo_owner, repo_name)
            if not client:
                logger.warning(
                    f"无法获取 {repo_owner}/{repo_name} 的客户端，跳过删除评论"
                )
                return {"issue_comments": 0, "review_comments": 0}

            repo = client.get_repo(f"{repo_owner}/{repo_name}")
            pr = repo.get_pull(pr_number)

            # 1. 删除 bot 的 Issue Comments（占位评论、错误评论等）
            try:
                for comment in pr.get_issue_comments():
                    if comment.user.login in bot_names:
                        try:
                            comment.delete()
                            issue_deleted += 1
                            logger.info(
                                f"已删除Issue评论: {repo_owner}/{repo_name}#{pr_number}, "
                                f"comment_id={comment.id}"
                            )
                        except Exception as e:
                            logger.warning(f"删除Issue评论失败 (id={comment.id}): {e}")
            except Exception as e:
                logger.warning(f"获取Issue评论失败: {e}")

            # 2. 删除 bot 的 Review Comments（行内评论）
            try:
                for comment in pr.get_review_comments():
                    if comment.user.login in bot_names:
                        try:
                            comment.delete()
                            review_deleted += 1
                            logger.info(
                                f"已删除Review评论: {repo_owner}/{repo_name}#{pr_number}, "
                                f"comment_id={comment.id}"
                            )
                        except Exception as e:
                            logger.warning(f"删除Review评论失败 (id={comment.id}): {e}")
            except Exception as e:
                logger.warning(f"获取Review评论失败: {e}")

            logger.info(
                f"删除评论完成: {repo_owner}/{repo_name}#{pr_number}, "
                f"Issue评论={issue_deleted}, Review评论={review_deleted}"
            )
            return {"issue_comments": issue_deleted, "review_comments": review_deleted}

        except Exception as e:
            logger.error(f"删除bot评论失败: {e}", exc_info=True)
            return {"issue_comments": issue_deleted, "review_comments": review_deleted}

    def check_collaborator_permission(
        self,
        repo_owner: str,
        repo_name: str,
        username: str,
    ) -> str:
        """检查用户在仓库中的权限级别

        Args:
            repo_owner: 仓库所有者
            repo_name: 仓库名称
            username: GitHub用户名

        Returns:
            权限级别字符串 (admin, write, read, none)，无法校验时返回 "unknown"
        """
        try:
            client = self.get_repo_client(repo_owner, repo_name)
            if not client:
                logger.warning(
                    f"无法获取 {repo_owner}/{repo_name} 的客户端，无法完成权限检查"
                )
                return "unknown"

            repo = client.get_repo(f"{repo_owner}/{repo_name}")
            permission = repo.get_collaborator_permission(username)
            logger.info(
                f"用户 {username} 在 {repo_owner}/{repo_name} 的权限: {permission}"
            )
            return permission

        except Exception as e:
            logger.warning(
                f"检查用户权限失败，无法确认权限 "
                f"(repo={repo_owner}/{repo_name}, user={username}): {e}",
                exc_info=True,
            )
            return "unknown"

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
                logger.info(f"准备提交 {len(inline_comments)} 条行内评论:")
                for i, comment in enumerate(inline_comments, 1):
                    file_path = comment.get("file_path")
                    line_number = comment.get("line_number")
                    start_line = comment.get("start_line")
                    body_preview = comment.get("body", "")[:50]

                    comment_dict = {
                        "path": file_path,
                        "line": line_number,
                        "body": comment.get("body", ""),
                    }
                    # 添加 start_line 支持（跨多行评论）
                    if start_line:
                        comment_dict["start_line"] = start_line
                        logger.info(
                            f"  [{i}/{len(inline_comments)}] {file_path}:{start_line}-{line_number} ({body_preview}...)"
                        )
                    else:
                        logger.info(
                            f"  [{i}/{len(inline_comments)}] {file_path}:{line_number} ({body_preview}...)"
                        )
                    comments.append(comment_dict)

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

                is_resolvable_error = False

                if isinstance(e.data, dict):
                    msg = e.data.get("message") or e.data.get("error", "未知错误")
                    logger.error(f"错误信息: {msg}")

                    if e.status == 422:
                        errors = e.data.get("errors", [])
                        errors_str = str(errors) if errors else ""

                        if "Line could not be resolved" in errors_str or (
                            "line" in errors_str.lower()
                            and "could not" in errors_str.lower()
                        ):
                            is_resolvable_error = True
                        if "Path could not be resolved" in errors_str:
                            is_resolvable_error = True

                        # 记录所有评论的路径，帮助定位问题
                        if inline_comments:
                            logger.error(
                                f"  尝试提交的 {len(inline_comments)} 条评论的详情:"
                            )
                            for i, comment in enumerate(inline_comments, 1):
                                file_path = comment.get("file_path")
                                line_number = comment.get("line_number")
                                start_line = comment.get("start_line")
                                if start_line:
                                    logger.error(
                                        f"    [{i}] {file_path}:{start_line}-{line_number}"
                                    )
                                else:
                                    logger.error(f"    [{i}] {file_path}:{line_number}")

                # 422 行号/路径无法解析时，降级为无行内评论的 Review
                if is_resolvable_error and body and inline_comments:
                    logger.warning(
                        "422 行号/路径无法解析，尝试降级为无行内评论的 Review..."
                    )
                    try:
                        pr.create_review(event=event, body=body, comments=[])
                        logger.info(
                            f"✅ 降级成功: 已提交无行内评论的 Review "
                            f"({repo_owner}/{repo_name}#{pr_number})"
                        )
                        return True
                    except Exception as fallback_error:
                        logger.error(f"降级提交也失败: {fallback_error}")
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
            if self.integration is None:
                logger.info("GitHub Integration 尚未初始化，尝试延迟创建...")
                self._init_integration()
                if self.integration is None:
                    logger.warning("GitHub Integration 初始化失败，无法获取 bot 用户名")
                    return getattr(settings, "bot_username", None) or "unknown-bot"
            # 直接使用 integration 对象获取 App 信息
            app = self.integration.get_app()
            app_slug = app.slug

            logger.debug(f"成功获取GitHub App标识: {app_slug}")
            return app_slug

        except Exception as e:
            logger.error(f"获取机器人用户名失败: {e}")
            logger.warning("将使用配置文件中的bot_username作为备选")
            # 备选方案：从配置文件读取
            return getattr(settings, "bot_username", None)

    @staticmethod
    def _build_check_run_output(
        title: Optional[str],
        summary: Optional[str],
        text: Optional[str],
    ) -> Optional[dict]:
        """构建 Check Run output dict，仅包含非空字段。

        GitHub API 要求 output 至少包含 title + summary；title/summary 的完整性
        由调用方（CheckRunService）保证，底层方法保持灵活。
        """
        output: dict = {}
        if title:
            output["title"] = title
        if summary:
            output["summary"] = summary
        if text:
            output["text"] = text
        return output or None

    def create_check_run(
        self,
        repo_owner: str,
        repo_name: str,
        name: str,
        head_sha: str,
        status: str = "queued",
        conclusion: Optional[str] = None,
        output_title: Optional[str] = None,
        output_summary: Optional[str] = None,
        output_text: Optional[str] = None,
    ) -> Optional[dict]:
        """创建 GitHub Check Run。

        Args:
            repo_owner: 仓库所有者
            repo_name: 仓库名称
            name: Check Run 名称
            head_sha: 绑定的 commit SHA
            status: queued / in_progress / completed
            conclusion: success / failure / neutral / cancelled（completed 时有意义）
            output_title/summary/text: Check Run 输出内容（可选）

        Returns:
            {"id": int, "status": str, "conclusion": str|None}，失败返回 None。
        """
        try:
            client = self.get_repo_client(repo_owner, repo_name)
            if not client:
                logger.error(
                    f"无法获取 {repo_owner}/{repo_name} 的客户端，跳过创建 Check Run"
                )
                return None

            repo = client.get_repo(f"{repo_owner}/{repo_name}")
            kwargs: dict = {"name": name, "head_sha": head_sha, "status": status}
            if conclusion:
                kwargs["conclusion"] = conclusion
            output = self._build_check_run_output(
                output_title, output_summary, output_text
            )
            if output:
                kwargs["output"] = output

            check_run = repo.create_check_run(**kwargs)
            logger.info(
                f"已创建 Check Run {name} for {repo_owner}/{repo_name}@{head_sha} "
                f"(id={check_run.id}, status={status})"
            )
            return {"id": check_run.id, "status": status, "conclusion": conclusion}
        except Exception as e:
            logger.error(
                f"创建 Check Run 失败 {repo_owner}/{repo_name}@{head_sha}: {e}",
                exc_info=True,
            )
            return None

    def update_check_run(
        self,
        repo_owner: str,
        repo_name: str,
        check_run_id: int,
        status: Optional[str] = None,
        conclusion: Optional[str] = None,
        output_title: Optional[str] = None,
        output_summary: Optional[str] = None,
        output_text: Optional[str] = None,
    ) -> bool:
        """更新指定 Check Run。

        Returns:
            是否成功。
        """
        try:
            client = self.get_repo_client(repo_owner, repo_name)
            if not client:
                logger.error(
                    f"无法获取 {repo_owner}/{repo_name} 的客户端，跳过更新 Check Run"
                )
                return False

            repo = client.get_repo(f"{repo_owner}/{repo_name}")
            check_run = repo.get_check_run(check_run_id)
            kwargs: dict = {}
            if status:
                kwargs["status"] = status
            if conclusion:
                kwargs["conclusion"] = conclusion
            output = self._build_check_run_output(
                output_title, output_summary, output_text
            )
            if output:
                kwargs["output"] = output

            if kwargs:
                check_run.edit(**kwargs)
            logger.info(
                f"已更新 Check Run id={check_run_id} "
                f"(status={status}, conclusion={conclusion})"
            )
            return True
        except Exception as e:
            logger.error(
                f"更新 Check Run 失败 {repo_owner}/{repo_name} id={check_run_id}: {e}",
                exc_info=True,
            )
            return False

    def cleanup_stale_check_runs(
        self,
        repo_owner: str,
        repo_name: str,
        head_sha: str,
        name: str,
    ) -> Optional[int]:
        """收敛同 commit 同名 Check Run，返回最新 active run id。

        同一 commit 上可能存在多个同名 active（queued/in_progress）Check Run
        （如历史重复创建 bug 的产物，会一直显示悬挂转圈）。保留 id 最大（最新）
        的 active run，将其余 active run 结束为 completed + neutral（停止转圈，
        显示为灰圆历史）。已 completed 的 run 视为正常历史，不动。

        创建者归属：仅本 GitHub App 创建的 run 可被 update（GitHub Actions 等
        其他来源的 run 由各自所有者管理）。此方法对所有同名 run 调 edit，若对方
        无权修改会失败并记 warning，不影响其余清理。

        Returns:
            最新的 active run id；无 active run 时返回 None（调用方创建新的）。
        """
        try:
            client = self.get_repo_client(repo_owner, repo_name)
            if not client:
                return None

            repo = client.get_repo(f"{repo_owner}/{repo_name}")
            commit = repo.get_commit(head_sha)
            active = [
                cr
                for cr in commit.get_check_runs()
                if cr.name == name and cr.status != "completed"
            ]
            if not active:
                return None
            active.sort(key=lambda cr: cr.id, reverse=True)
            latest_id = active[0].id
            for stale in active[1:]:
                try:
                    stale.edit(status="completed", conclusion="neutral")
                    logger.info(
                        f"已收敛悬挂 Check Run {name} id={stale.id} "
                        f"({repo_owner}/{repo_name}@{head_sha}) -> completed+neutral"
                    )
                except Exception as e:
                    logger.warning(f"收敛悬挂 Check Run id={stale.id} 失败: {e}")
            return latest_id
        except Exception as e:
            logger.warning(
                f"收敛 Check Run 失败 {repo_owner}/{repo_name}@{head_sha}: {e}"
            )
            return None

    def get_check_run_annotations(
        self,
        repo_owner: str,
        repo_name: str,
        check_run_id: str,
    ) -> list:
        """获取指定 Check Run 的文件级 annotations。

        用于外部 CI 失败详情采集（check_run.completed 后主动拉取结构化标注）。
        失败时返回空列表，调用方安全降级（不写入 annotations 字段）。

        Args:
            repo_owner: 仓库所有者
            repo_name: 仓库名称
            check_run_id: Check Run id（字符串形式）

        Returns:
            annotation dict 列表：[{"path", "start_line", "end_line",
            "annotation_level", "title", "message", "raw_details"}, ...]
        """
        try:
            client = self.get_repo_client(repo_owner, repo_name)
            if not client:
                return []
            repo = client.get_repo(f"{repo_owner}/{repo_name}")
            check_run = repo.get_check_run(int(check_run_id))
            return [
                {
                    "path": getattr(a, "path", ""),
                    "start_line": getattr(a, "start_line", None),
                    "end_line": getattr(a, "end_line", None),
                    "annotation_level": getattr(a, "annotation_level", ""),
                    "title": getattr(a, "title", ""),
                    "message": getattr(a, "message", ""),
                    "raw_details": getattr(a, "raw_details", ""),
                }
                for a in check_run.get_annotations()
            ]
        except Exception as e:
            logger.warning(
                f"获取 Check Run annotations 失败 "
                f"{repo_owner}/{repo_name} check_run_id={check_run_id}: {e}"
            )
            return []

    def get_pr_number_for_commit(
        self,
        repo_owner: str,
        repo_name: str,
        head_sha: str,
    ) -> Optional[int]:
        """GET /repos/{o}/{r}/commits/{sha}/pulls 兜底封装。

        CI 失败事件三层降级的第三层：当 check_run.pull_requests 为空且映射表
        未命中时（典型 Fork 场景），调此端点解 pr_number。PyGithub 无直接 API，
        用 requester 调 REST。失败返回 None（调用方忽略该 CI 事件）。

        Args:
            repo_owner: 仓库所有者
            repo_name: 仓库名称
            head_sha: commit SHA

        Returns:
            关联的 PR 编号；无关联或失败返回 None。
        """
        try:
            client = self.get_repo_client(repo_owner, repo_name)
            if not client:
                return None
            # PyGithub 无 commit→pulls 的公开封装，用内部 requester 调 REST。
            # _requester 为私有 API，PyGithub 升级时需验证兼容性。
            # requestJsonAndCheck 返回 (headers, data) 两元组（data 已解析为 list/dict），
            # 不同于 requestJson 的 (status, headers, body:str) 三元组。
            _, data = client._requester.requestJsonAndCheck(
                "GET",
                f"/repos/{repo_owner}/{repo_name}/commits/{head_sha}/pulls",
            )
            if isinstance(data, list):
                for pr in data:
                    number = pr.get("number") if isinstance(pr, dict) else None
                    if number is not None:
                        return int(number)
            return None
        except Exception as e:
            logger.debug(
                f"commit pulls 兜底失败 {repo_owner}/{repo_name}@{head_sha}: {e}"
            )
            return None

    def get_issue(self, repo_owner: str, repo_name: str, issue_number: int):
        """获取 Issue 详情"""
        client = self.get_repo_client(repo_owner, repo_name)
        repo = client.get_repo(f"{repo_owner}/{repo_name}")
        try:
            return repo.get_issue(issue_number)
        except Exception as e:
            logger.error(
                f"获取 Issue 失败: {repo_owner}/{repo_name}#{issue_number}: {e}"
            )
            return None

    def get_issue_comments(
        self, repo_owner: str, repo_name: str, issue_number: int
    ) -> list:
        """获取 Issue 的评论列表"""
        client = self.get_repo_client(repo_owner, repo_name)
        repo = client.get_repo(f"{repo_owner}/{repo_name}")
        try:
            issue = repo.get_issue(issue_number)
            return list(issue.get_comments())
        except Exception as e:
            logger.error(
                f"获取 Issue 评论失败: {repo_owner}/{repo_name}#{issue_number}: {e}"
            )
            return []

    def get_repo_issues(
        self,
        repo_owner: str,
        repo_name: str,
        state: str = "open",
        labels: list = None,
        per_page: int = 30,
    ) -> list:
        """获取仓库的 Issues 列表"""
        client = self.get_repo_client(repo_owner, repo_name)
        repo = client.get_repo(f"{repo_owner}/{repo_name}")
        try:
            return list(repo.get_issues(state=state, labels=labels, per_page=per_page))
        except Exception as e:
            logger.error(f"获取 Issues 列表失败: {repo_owner}/{repo_name}: {e}")
            return []

    def search_issues(
        self,
        repo_owner: str,
        repo_name: str,
        query: str,
        state: str = "open",
        per_page: int = 10,
        search_type: str = "issue",
    ) -> list:
        """搜索仓库的 Issues 或 PRs

        Args:
            search_type: "issue" 搜索 Issue, "pr" 搜索 Pull Request
        """
        client = self.get_repo_client(repo_owner, repo_name)
        try:
            type_qual = (
                f"is:{search_type}" if search_type in ("issue", "pr") else "is:issue"
            )
            qual = f"repo:{repo_owner}/{repo_name} {query} {type_qual} is:{state}"
            return list(client.search_issues(qual)[:per_page])
        except IndexError:
            return []
        except Exception as e:
            logger.error(f"搜索 Issues 失败: {qual}: {e}")
            return []

    def create_issue_comment(
        self, repo_owner: str, repo_name: str, issue_number: int, body: str
    ) -> bool:
        """在 Issue 上创建评论"""
        client = self.get_repo_client(repo_owner, repo_name)
        repo = client.get_repo(f"{repo_owner}/{repo_name}")
        try:
            repo.get_issue(issue_number).create_comment(body)
            return True
        except Exception as e:
            logger.error(
                f"创建 Issue 评论失败: {repo_owner}/{repo_name}#{issue_number}: {e}"
            )
            return False

    def add_labels_to_issue(
        self, repo_owner: str, repo_name: str, issue_number: int, label_names: list
    ) -> bool:
        """给 Issue 添加标签"""
        client = self.get_repo_client(repo_owner, repo_name)
        repo = client.get_repo(f"{repo_owner}/{repo_name}")
        try:
            issue = repo.get_issue(issue_number)
            issue.add_to_labels(*label_names)
            return True
        except Exception as e:
            logger.error(
                f"添加 Issue 标签失败: {repo_owner}/{repo_name}#{issue_number}: {e}"
            )
            return False

    def add_assignees_to_issue(
        self, repo_owner: str, repo_name: str, issue_number: int, assignees: list[str]
    ) -> bool:
        """给 Issue 添加指派人"""
        client = self.get_repo_client(repo_owner, repo_name)
        repo = client.get_repo(f"{repo_owner}/{repo_name}")
        try:
            issue = repo.get_issue(issue_number)
            issue.add_to_assignees(*assignees)
            return True
        except Exception as e:
            logger.error(
                f"添加 Issue 指派人失败: {repo_owner}/{repo_name}#{issue_number}: {e}"
            )
            return False

    def update_issue_title(
        self, repo_owner: str, repo_name: str, issue_number: int, new_title: str
    ) -> bool:
        """修改 Issue 标题"""
        try:
            new_title = new_title.strip()
            if not new_title:
                logger.warning(
                    f"修改 Issue 标题跳过: 标题为空 {repo_owner}/{repo_name}#{issue_number}"
                )
                return False
            client = self.get_repo_client(repo_owner, repo_name)
            if client is None:
                logger.error(
                    f"修改 Issue 标题失败: 无法获取仓库客户端 {repo_owner}/{repo_name}"
                )
                return False
            repo = client.get_repo(f"{repo_owner}/{repo_name}")
            issue = repo.get_issue(issue_number)
            issue.edit(title=new_title)
            return True
        except Exception as e:
            logger.error(
                f"修改 Issue 标题失败: {repo_owner}/{repo_name}#{issue_number}: {e}"
            )
            return False

    def get_repo_collaborators(self, repo_owner: str, repo_name: str) -> list:
        """获取仓库协作者列表"""
        try:
            client = self.get_repo_client(repo_owner, repo_name)
            repo = client.get_repo(f"{repo_owner}/{repo_name}")
            return [c.login for c in repo.get_collaborators()]
        except Exception as e:
            logger.warning(f"获取协作者列表失败（权限不足或API限制）: {e}")
            return []

    def get_repo_milestones(
        self, repo_owner: str, repo_name: str, state: str = "open"
    ) -> list:
        """获取仓库的里程碑列表"""
        try:
            client = self.get_repo_client(repo_owner, repo_name)
            repo = client.get_repo(f"{repo_owner}/{repo_name}")
            return [
                {
                    "number": m.number,
                    "title": m.title,
                    "description": m.description or "",
                }
                for m in repo.get_milestones(state=state)
            ]
        except Exception as e:
            logger.warning(f"获取里程碑列表失败: {e}")
            return []


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
            "body": pull_request.get("body") or "",
            "branch": pull_request["head"]["ref"],
            "head_sha": pull_request.get("head", {}).get("sha"),
            "base_branch": pull_request["base"]["ref"],
            "diff_url": pull_request["diff_url"],
            "patch_url": pull_request["patch_url"],
            "html_url": pull_request["html_url"],
            "state": pull_request["state"],
            "draft": pull_request.get("draft", False),
            "merged": pull_request.get("merged", False),
            "sender": payload.get("sender", {}).get("login", ""),
            "before": payload.get("before"),
            "after": payload.get("after"),
        }

        logger.info(
            f"成功提取PR信息: {pr_info['repo_full_name']}#{pr_info['pr_number']}"
        )
        return pr_info

    except Exception as e:
        logger.error(f"提取PR信息时出错: {e}")
        return None


def extract_issue_info_from_webhook(
    payload: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """从 Webhook payload 中提取 Issue 信息"""
    action = payload.get("action", "")
    if not action:
        return None

    issue = payload.get("issue", {})
    if not issue:
        return None

    repository = payload.get("repository", {})

    return {
        "action": action,
        "issue_number": issue.get("number"),
        "repo_owner": repository.get("owner", {}).get("login", ""),
        "repo_name": repository.get("name", ""),
        "repo_full_name": repository.get("full_name", ""),
        "installation_id": payload.get("installation", {}).get("id"),
        "author": issue.get("user", {}).get("login", ""),
        "title": issue.get("title", ""),
        "body": issue.get("body", ""),
        "state": issue.get("state", ""),
        "labels": [label.get("name", "") for label in issue.get("labels", [])],
        "html_url": issue.get("html_url", ""),
    }


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


# ========== GitHub App user-to-server token 协议层 ==========
# 这些函数只负责 OAuth/token 端点的 HTTP 协议，不读取任何业务配置；
# client_id / client_secret 由调用方（star_aid_github_service）注入，
# 从而保持 core 层对 star_aid 配置的反向解耦。

GITHUB_LOGIN_OAUTH_TOKEN_URL = "https://github.com/login/oauth/access_token"


async def exchange_user_access_token(
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: Optional[str] = None,
) -> Dict[str, Any]:
    """GitHub App web application flow：用授权码交换 user access token。

    成功时返回包含 ``access_token`` / ``expires_in`` / ``refresh_token``
    / ``refresh_token_expires_in`` / ``token_type`` 的字典；授权码无效时
    GitHub 仍返回 200，但 body 含 ``error`` 字段，由调用方检查。

    Raises:
        RuntimeError: GitHub 返回非 200 状态码。
    """
    data: Dict[str, Any] = {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
    }
    if redirect_uri:
        data["redirect_uri"] = redirect_uri

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            GITHUB_LOGIN_OAUTH_TOKEN_URL,
            data=data,
            headers={"Accept": "application/json"},
            timeout=15,
        )

    if resp.status_code != 200:
        raise RuntimeError(
            f"github user token exchange failed: status={resp.status_code}"
        )
    return resp.json()


async def refresh_user_access_token(
    client_id: str,
    client_secret: str,
    refresh_token: str,
) -> Dict[str, Any]:
    """用 refresh_token 刷新 GitHub App user access token。

    GitHub 在每次刷新时会签发新的 refresh_token，旧 refresh_token 随即失效，
    调用方必须用返回的新 refresh_token 覆盖存储。

    Raises:
        RuntimeError: GitHub 返回非 200 状态码。
    """
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            GITHUB_LOGIN_OAUTH_TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            headers={"Accept": "application/json"},
            timeout=15,
        )

    if resp.status_code != 200:
        raise RuntimeError(
            f"github user token refresh failed: status={resp.status_code}"
        )
    return resp.json()
