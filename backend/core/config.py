"""配置管理模块"""

from collections import OrderedDict
from typing import Any, Dict, Literal, Optional, get_origin

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache
import time
import yaml
from pathlib import Path
from loguru import logger

from backend.core.ai_providers import get_provider_select_options


class Settings(BaseSettings):
    """应用配置"""

    model_config = SettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        protected_namespaces=("settings_",),
    )

    # GitHub App配置（Setup Wizard 模式下可为 None）
    github_app_id: Optional[str] = None
    github_private_key: Optional[str] = None
    github_webhook_secret: Optional[str] = None

    # OpenAI 兼容 AI 配置
    ai_provider: str = "openai"
    openai_api_base: str = "https://api.openai.com/v1"
    openai_api_key: Optional[str] = None
    openai_model: str = "gpt-4"
    openai_temperature: float = 0.3
    openai_max_tokens: int = 4000

    # 辅助模型配置（摘要、压缩等轻量任务，未设置时回退到主模型）
    summary_provider: str = ""  # 为空时跟随 ai_provider
    summary_model: str = ""  # 为空时使用 openai_model
    summary_api_base: str = ""  # 为空时使用 openai_api_base
    summary_api_key: str = ""  # 为空时使用 openai_api_key

    # 模型上下文配置
    model_context_window: int = 0  # 自定义上下文窗口大小（K tokens），0 表示自动检测
    auto_fetch_model_context: bool = True  # 是否自动从 API 获取模型上下文
    context_safety_threshold: float = 0.8  # 上下文安全阈值（0-1），默认使用 80%

    # 上下文压缩配置
    enable_context_compression: bool = True  # 是否启用上下文自动压缩
    context_compression_threshold: float = 0.85  # 压缩触发阈值（0-1），默认 85%
    context_compression_keep_rounds: int = 2  # 保留最近几轮对话不压缩

    # 数据库配置
    database_url: Optional[str] = None

    # Redis配置
    redis_url: str = "redis://127.0.0.1:6379/0"

    # 应用配置
    app_domain: str = "localhost"
    app_port: int = 8000
    log_level: str = "INFO"
    sakura_env: str = Field(
        "production",
        description="运行环境标识（production / development）",
    )
    sakura_dev_bootstrap: bool = Field(
        False,
        description="是否启用本地 Setup Wizard 调试模式",
    )
    sakura_skip_background_tasks: bool = Field(
        False,
        description="是否跳过 Telegram、SSE、扫描、配额等后台任务",
    )

    # 基础审查任务配置
    max_concurrent_reviews: int = Field(
        5,
        description="最大并发审查数量",
    )
    review_timeout_seconds: int = Field(
        300,
        description="审查任务整体超时时间（秒）",
    )
    enable_auto_review: bool = Field(
        True,
        description="是否启用 Webhook 自动审查",
    )

    # AI API 调用配置
    ai_api_timeout_seconds: float = Field(
        120.0,
        description="AI API 单次请求超时时间（秒）",
    )
    ai_api_max_retries: int = Field(
        5,
        description="AI API 最大重试次数",
    )
    ai_api_initial_retry_delay_seconds: float = Field(
        1.0,
        description="AI API 初始重试延迟（秒）",
    )
    ai_api_total_timeout_seconds: float = Field(
        900.0,
        description="AI API 重试总超时时间（秒）",
    )

    # 审查策略配置
    max_file_count: int = 100
    max_line_count: int = 10000
    batch_size: int = 10

    # AI工具配置
    enable_ai_tools: bool = True

    # Webhook配置
    webhook_path: str = "/api/webhook/github"

    # WebUI配置
    webui_secret_key: str = Field(
        "change-me-in-production",
        description="JWT 和 CSRF Token 签名密钥，生产环境必须改为强随机字符串（如 openssl rand -hex 32）",
    )
    webui_cookie_secure: bool = Field(
        False,
        description="Cookie Secure 属性，HTTPS 环境必须设为 True",
    )

    # 多因素认证配置 / Multi-factor authentication configuration
    two_factor_enabled: bool = Field(
        True,
        description="是否允许用户启用两步验证",
    )
    two_factor_issuer: str = Field(
        "Sakura AI Reviewer",
        description="TOTP 认证器中显示的发行方名称",
    )
    two_factor_pending_token_expire_minutes: int = Field(
        10,
        ge=1,
        le=60,
        description="OAuth 后等待二次验证的临时 Token 有效期（分钟）",
    )
    two_factor_verify_rate_limit: str = Field(
        "5/minute",
        description="二次验证接口限流规则",
    )
    two_factor_setup_rate_limit: str = Field(
        "10/minute",
        description="二次验证设置接口限流规则",
    )
    two_factor_recovery_code_count: int = Field(
        10,
        ge=4,
        le=20,
        description="生成的恢复码数量",
    )
    two_factor_recovery_code_length: int = Field(
        10,
        ge=8,
        le=32,
        description="恢复码随机字符长度",
    )
    two_factor_encryption_key: str = Field(
        "",
        description="TOTP Secret 加密密钥；为空时从 WEBUI_SECRET_KEY 派生",
    )
    passkeys_enabled: bool = Field(
        True,
        description="是否允许用户注册和使用通行密钥（Passkeys/WebAuthn）",
    )
    passkeys_rp_id: str = Field(
        "",
        description="WebAuthn Relying Party ID；为空时使用 app_domain",
    )
    passkeys_rp_name: str = Field(
        "Sakura AI Reviewer",
        description="WebAuthn Relying Party 显示名称",
    )
    passkeys_origin: str = Field(
        "",
        description="WebAuthn 允许的 Origin；为空时根据 app_domain/app_port 推导",
    )
    passkeys_challenge_ttl_seconds: int = Field(
        300,
        ge=60,
        le=900,
        description="WebAuthn challenge 有效期（秒）",
    )
    passkeys_authentication_rate_limit: str = Field(
        "10/minute",
        description="Passkey 认证接口限流规则",
    )

    # GitHub OAuth 配置
    # 获取步骤：GitHub → Settings → Developer settings → OAuth Apps → New OAuth App
    github_oauth_client_id: str = Field(
        "",
        description="GitHub OAuth App 的 Client ID",
    )
    github_oauth_client_secret: str = Field(
        "",
        description="GitHub OAuth App 的 Client Secret",
    )
    github_oauth_redirect_uri: str = Field(
        "",
        description="OAuth 回调地址，必须与 GitHub OAuth App 中配置的 Authorization callback URL 一致",
    )
    mobile_oauth_allowed_redirect_uris: str = Field(
        "",
        description="移动端 OAuth 允许的回调 URI（逗号分隔，为空时仅允许默认 redirect_uri）",
    )

    # Telegram Bot配置
    telegram_bot_token: Optional[str] = None
    telegram_bot_username: Optional[str] = None  # 启动时通过 getMe 自动填充
    telegram_admin_user_ids: str = ""  # 逗号分隔的超级管理员ID列表
    telegram_default_chat_id: str = ""  # 默认接收通知的聊天ID
    register_quota_multiplier: float = Field(
        0.2,
        ge=0.1,
        le=1.0,
        description="自注册用户配额倍率（0.1-1.0）",
    )

    # GitHub App机器人用户名（可选，用于幂等性检查）
    bot_username: Optional[str] = None  # 备用方案，当无法从GitHub API获取时使用

    # ========== 国际化配置 / i18n Configuration ==========
    default_language: str = Field(
        "zh-CN",
        description="默认界面语言（zh-CN / en）",
    )
    output_language: str = Field(
        "",
        description="AI 输出语言（为空时跟随请求上下文，可设为 zh-CN / en 强制指定）",
    )

    def validate_required_fields(self) -> list[str]:
        """返回值为 None 的必填字段名列表（用于非 bootstrap 模式启动校验）"""
        required = [
            "github_app_id",
            "github_private_key",
            "github_webhook_secret",
            "openai_api_key",
            "database_url",
            "telegram_bot_token",
        ]
        missing = []
        for field_name in required:
            if getattr(self, field_name, None) is None:
                missing.append(field_name)
        return missing

    @property
    def is_development(self) -> bool:
        """是否为本地开发环境"""
        return self.sakura_env.lower() in {"dev", "development", "local"}

    @property
    def webhook_url(self) -> str:
        """获取完整的Webhook URL"""
        return f"https://{self.app_domain}{self.webhook_path}"

    @property
    def github_oauth_auth_url(self) -> str:
        """GitHub OAuth 授权 URL"""
        return "https://github.com/login/oauth/authorize"

    @property
    def github_oauth_token_url(self) -> str:
        """GitHub OAuth Token URL"""
        return "https://github.com/login/oauth/access_token"

    @property
    def github_oauth_user_url(self) -> str:
        """GitHub OAuth 用户信息 API"""
        return "https://api.github.com/user"

    @property
    def telegram_admin_ids_list(self) -> list[int]:
        """获取超级管理员ID列表"""
        if not self.telegram_admin_user_ids:
            return []
        return [
            int(id.strip())
            for id in self.telegram_admin_user_ids.split(",")
            if id.strip()
        ]

    # ========== RAG 配置 ==========
    enable_rag: bool = True
    chroma_persist_dir: str = "./data/chroma"

    # 嵌入模型配置
    embedding_model: str = "BAAI/bge-m3"
    embedding_provider: str = "siliconflow"  # openai|ollama|hf|siliconflow
    embedding_base_url: str = "https://api.siliconflow.cn/v1"
    embedding_api_key: str = ""
    embedding_dimension: int = 1024
    embedding_batch_size: int = 64  # 每批处理的文本数量（SiliconFlow 限制为 64）

    # 重排序模型配置
    rerank_model: str = "BAAI/bge-reranker-v2-m3"
    rerank_provider: str = "siliconflow"  # huggingface|ollama|siliconflow|none
    rerank_base_url: str = "https://api.siliconflow.cn/v1/rerank"
    rerank_api_key: str = ""
    rerank_top_k: int = 5
    rerank_score_threshold: float = 0.3

    # 文档分块配置
    chunk_size: int = 1000
    chunk_overlap: int = 200
    max_chunks_per_doc: int = 500

    # 文件监控配置
    enable_file_monitor: bool = True
    file_monitor_debounce_sec: int = 5

    # 定时更新配置
    enable_scheduler: bool = True
    schedule_update_interval_minutes: int = 60

    # ========== Issue 分析配置 ==========
    enable_issue_analysis: bool = True
    enable_pr_issue_linking: bool = True
    issue_auto_comment: bool = True
    issue_confidence_threshold: float = 0.7
    issue_auto_create_labels: bool = True
    issue_auto_assign: bool = True
    issue_auto_rewrite_title: bool = False
    issue_assignee_confidence_threshold: float = 0.8
    issue_auto_assign_max: int = 3
    issue_detect_duplicates: bool = True
    issue_suggest_assignees: bool = True
    issue_suggest_milestones: bool = False
    issue_max_tool_iterations: int = 15
    issue_max_files_per_analysis: int = 10
    issue_max_directory_depth: int = 3
    issue_price_per_1k_prompt: float = 0.0
    issue_price_per_1k_completion: float = 0.0

    # ========== PR 审查价格配置 ==========
    review_price_per_1k_prompt: float = 0.0
    review_price_per_1k_completion: float = 0.0

    # ========== Web 搜索配置 ==========
    web_search_enabled: bool = True  # 是否启用 Web 搜索工具
    web_search_provider: str = "duckduckgo"  # 搜索提供商：duckduckgo(免费) | tavily
    web_search_api_key: str = ""  # API Key（tavily 需要，duckduckgo 不需要）
    web_search_max_results: int = 3  # 最大返回结果数
    web_search_max_content_length: int = 500  # 每个结果截断长度（字符）
    web_search_timeout: int = 15  # 搜索超时（秒）

    # ========== URL 抓取配置 ==========
    fetch_url_enabled: bool = False  # 是否启用 URL 抓取工具
    fetch_url_timeout: int = 15  # 抓取超时（秒）
    fetch_url_max_content_length: int = 5000  # 文本截断长度（字符）
    fetch_url_max_download_size: int = (
        1048576  # 原始 HTML 下载大小限制（字节，默认 1MB）
    )
    fetch_url_max_calls_per_session: int = 3  # 单次会话最大调用次数
    fetch_url_domain_policy: str = "off"  # 域名过滤策略：off / blacklist / whitelist
    fetch_url_domain_list: str = ""  # 域名列表（逗号分隔）
    fetch_url_force_https: bool = False  # 强制仅允许 HTTPS 协议

    # ========== 支付配置 ==========
    payment_enabled: bool = False  # 是否启用付费配额系统
    payment_order_expire_minutes: int = Field(
        30, description="未支付订单过期时间（分钟）"
    )
    payment_default_currency: str = Field("CNY", description="默认货币")

    # ========== 代码索引配置 ==========
    enable_code_index: bool = True  # 是否启用代码索引功能
    auto_index_pr_changes: bool = True  # PR审查时自动索引变更文件

    # 代码分块配置
    code_chunk_size: int = 500  # 代码块大小（字符数）
    code_chunk_overlap: int = 50  # 代码块重叠大小

    # 行内评论配置
    enable_inline_comments: bool = True  # 是否启用行内评论

    # ========== 增量审查历史上下文配置 ==========
    enable_incremental_history_context: bool = True  # 是否启用增量审查历史上下文
    enable_pr_summary: bool = False  # 是否启用 PR 变更自动总结
    incremental_history_max_reviews: int = 5  # 最多查询的历史审查轮数
    incremental_history_summary_max_tokens: int = 1500  # 摘要生成最大 token

    # ========== PR 依赖图配置 ==========
    enable_pr_dependency_graph: bool = False  # 是否启用 PR 依赖图生成
    pr_dependency_graph_mode: Literal["ai", "static"] = "ai"  # 依赖图生成模式
    pr_dependency_graph_max_nodes: int = 25  # 依赖图最大节点数
    pr_dependency_graph_max_files: int = 50  # 参与分析的最大文件数

    # ========== 语义 Issue 关联配置 ==========
    enable_semantic_issue_linking: bool = False  # 是否启用语义 Issue 关联
    semantic_issue_similarity_threshold: float = 0.65  # 语义相似度阈值
    semantic_issue_max_links: int = 5  # 最大关联 Issue 数量

    # 支持的编程语言
    code_index_languages: list[str] = [
        "python",
        "javascript",
        "typescript",
        "go",
        "java",
        "rust",
        "cpp",
        "c",
        "csharp",
        "php",
        "ruby",
        "swift",
        "kotlin",
    ]

    # 核心代码目录（用于定期索引）
    code_index_core_paths: list[str] = [
        "src/",
        "lib/",
        "backend/",
        "frontend/",
        "app/",
        "core/",
    ]

    # 依赖配置文件索引
    code_index_dependency_files: bool = True

    # ========== 仓库扫描配置 ==========
    enable_repo_scan: bool = True  # 是否启用仓库扫描
    scan_interval_minutes: int = 360  # 扫描间隔（分钟，默认6小时）
    scan_cooldown_hours: int = 24  # 同一仓库扫描冷却时间（小时）
    scan_max_tokens_per_repo: int = 0  # 单仓库扫描 Token 预算（0=无限制）
    scan_max_concurrent: int = 1  # 最大并发扫描数
    scan_auto_create_issue: bool = True  # 是否自动创建 GitHub Issue
    scan_send_telegram: bool = True  # 是否发送 Telegram 通知
    scan_min_severity_for_issue: str = "major"  # 创建 Issue 的最低严重性
    # 扫描独立对话配置
    scan_model: str = ""  # 扫描使用的模型，为空时使用 openai_model
    scan_max_iterations: int = 200  # 扫描最大工具调用轮次
    scan_context_safety_threshold: float = 0.8  # 扫描上下文安全阈值
    scan_compression_threshold: float = 0.85  # 扫描压缩触发阈值
    scan_temperature: float = 0.2  # 扫描 AI 温度参数

    # ========== Sakura 记忆系统配置 ==========
    sakura_memory_enabled: bool = True  # 是否启用 .sakura/ 记忆系统
    sakura_reflection_enabled: bool = True  # 是否启用审查后反思
    sakura_reflection_model: str = ""  # 反思使用的模型，为空时使用审查模型
    sakura_consolidation_interval: int = 5  # 触发合并的反思轮数
    sakura_consolidation_model: str = ""  # 合并使用的模型，为空时使用审查模型
    sakura_max_memory_chars: int = 2000  # memory.md 最大字符数
    sakura_max_sakura_chars: int = 5000  # SAKURA.md 最大字符数
    sakura_auto_init: bool = True  # 是否自动初始化 .sakura/ 目录
    sakura_consolidation_partial_commit: bool = (
        False  # 合并时一个文件失败是否仍提交另一个
    )
    sakura_issue_reflection_enabled: bool = True  # 是否启用 Issue 分析后反思
    sakura_issue_reflection_model: str = ""  # Issue 反思使用的模型，为空时使用审查模型
    sakura_use_summary_model: bool = False  # 反思/合并任务使用辅助模型凭据以降低成本


class StrategyConfig:
    """审查策略配置"""

    def __init__(self, config_path: str = "config/strategies.yaml"):
        self.config_path = Path(config_path)
        self._load_config()

    def _load_config(self):
        """加载配置文件"""
        if not self.config_path.exists():
            raise FileNotFoundError(f"策略配置文件不存在: {self.config_path}")

        with open(self.config_path, "r", encoding="utf-8") as f:
            self.config = yaml.safe_load(f)

    def get_strategy(self, strategy_name: str) -> dict:
        """获取指定策略"""
        return self.config["strategies"].get(strategy_name, {})

    def get_all_strategies(self) -> dict:
        """获取所有策略"""
        return self.config["strategies"]

    def get_file_filters(self) -> dict:
        """获取文件过滤规则"""
        return self.config.get("file_filters", {})

    def get_batch_config(self) -> dict:
        """获取批处理配置"""
        return self.config.get("batch", {})

    def determine_strategy(self, file_count: int, line_count: int) -> str:
        """根据PR规模确定审查策略"""
        strategies = self.get_all_strategies()

        # 按顺序检查策略（从小到大）
        for strategy_name, strategy_config in strategies.items():
            conditions = strategy_config.get("conditions", {})
            max_files = conditions.get("max_files", float("inf"))
            max_lines = conditions.get("max_lines", float("inf"))

            if file_count <= max_files and line_count <= max_lines:
                return strategy_name

        # 如果没有匹配的策略，使用large策略
        return "large"

    def should_skip_file(self, file_path: str) -> bool:
        """判断是否应该跳过该文件"""
        filters = self.get_file_filters()

        # 检查扩展名
        skip_extensions = filters.get("skip_extensions", [])
        for ext in skip_extensions:
            if file_path.endswith(ext):
                return True

        # 检查路径
        skip_paths = filters.get("skip_paths", [])
        for path in skip_paths:
            if path in file_path:
                return True

        return False

    def is_code_file(self, file_path: str) -> bool:
        """判断是否为代码文件"""
        filters = self.get_file_filters()
        code_extensions = filters.get("code_extensions", [])

        for ext in code_extensions:
            if file_path.endswith(ext):
                return True

        return False

    def get_issue_analysis_config(self) -> dict:
        """获取 Issue 分析配置"""
        return self.config.get("issue_analysis", {})

    def get_context_enhancement_config(self) -> dict:
        """获取上下文增强配置"""
        return self.config.get("context_enhancement", {})

    def is_model_supports_reasoning_content(self, model_name: str) -> bool:
        """检查模型是否支持 reasoning_content 字段

        Args:
            model_name: 模型名称（如 'deepseek-r1', 'glm-4.7'）

        Returns:
            True 如果模型支持 reasoning_content
        """
        # DeepSeek-R1 系列模型支持 reasoning_content
        deepseek_models = [
            "deepseek-r1",
            "deepseek-reasoner",
            "deepseek-r1-lite",
            "deepseek-r1-zero",
        ]

        model_lower = model_name.lower()
        return any(model_lower.startswith(ds_model) for ds_model in deepseek_models)


@lru_cache()
def get_settings() -> Settings:
    """获取配置单例"""
    return Settings()


@lru_cache()
def get_strategy_config() -> StrategyConfig:
    """获取策略配置单例"""
    return StrategyConfig()


def reload_strategy_config() -> StrategyConfig:
    """清除 lru_cache 并重新加载策略配置

    注意：已持有旧 StrategyConfig 引用的请求会继续使用旧配置，
    这是预期行为（保证单次请求内的配置一致性）。
    后续新请求将获取刷新后的配置。
    """
    get_strategy_config.cache_clear()
    return get_strategy_config()


class LabelConfig:
    """标签配置"""

    def __init__(self, config_path: str = "config/labels.yaml"):
        self.config_path = Path(config_path)
        self._load_config()

    def _load_config(self):
        """加载标签配置文件"""
        if not self.config_path.exists():
            self.config = {"labels": {}, "recommendation": {}}
            return
        with open(self.config_path, "r", encoding="utf-8") as f:
            self.config = yaml.safe_load(f) or {}

    def get_labels(self) -> dict:
        """获取所有标签定义"""
        return self.config.get("labels", {})

    def get_recommendation_settings(self) -> dict:
        """获取标签推荐设置"""
        return self.config.get("recommendation", {})

    def get_conflict_rules(self) -> Dict[str, list]:
        """获取标签冲突规则

        Returns:
            冲突规则字典，格式：{已有标签: [禁止添加的标签列表]}
        """
        return self.config.get("conflict_rules", {})


@lru_cache()
def get_label_config() -> LabelConfig:
    """获取标签配置单例"""
    return LabelConfig()


def reload_label_config() -> LabelConfig:
    """清除 lru_cache 并重新加载标签配置"""
    get_label_config.cache_clear()
    return get_label_config()


# ========== 动态配置（从数据库读取） ==========

# 可通过 WebUI 动态管理的配置键及其分组信息
DYNAMIC_CONFIG_GROUPS: OrderedDict[str, dict] = OrderedDict(
    [
        (
            "ai_model",
            {
                "label": "AI 模型配置",
                "icon": "cpu",
                "keys": [
                    "ai_provider",
                    "openai_api_base",
                    "openai_api_key",
                    "openai_model",
                ],
            },
        ),
        (
            "ai_api",
            {
                "label": "AI API 调用配置",
                "icon": "clock",
                "descriptions": {
                    "ai_api_timeout_seconds": "传递给 OpenAI 兼容 SDK 的单次请求超时时间，不等同于审查任务整体超时",
                    "ai_api_max_retries": "单次 AI 调用失败或空响应时允许的最大尝试次数",
                    "ai_api_initial_retry_delay_seconds": "首次重试前的基础延迟，后续按指数退避增加",
                    "ai_api_total_timeout_seconds": "一次 AI 调用重试循环允许的最长总耗时",
                },
                "keys": [
                    "ai_api_timeout_seconds",
                    "ai_api_max_retries",
                    "ai_api_initial_retry_delay_seconds",
                    "ai_api_total_timeout_seconds",
                ],
            },
        ),
        (
            "summary_model",
            {
                "label": "辅助模型配置",
                "icon": "zap",
                "descriptions": {
                    "summary_provider": "辅助模型厂商，留空则跟随主模型",
                    "summary_model": "用于摘要生成、上下文压缩等轻量任务，留空则使用主模型",
                    "summary_api_base": "辅助模型的 API 地址，留空则使用主模型地址",
                    "summary_api_key": "辅助模型的 API Key，留空则使用主模型 Key",
                },
                "keys": [
                    "summary_provider",
                    "summary_model",
                    "summary_api_base",
                    "summary_api_key",
                ],
            },
        ),
        (
            "rag",
            {
                "label": "RAG 配置",
                "icon": "database",
                "keys": [
                    "enable_rag",
                    "chroma_persist_dir",
                ],
            },
        ),
        (
            "embedding",
            {
                "label": "嵌入模型配置",
                "icon": "layers",
                "keys": [
                    "embedding_model",
                    "embedding_provider",
                    "embedding_base_url",
                    "embedding_api_key",
                    "embedding_dimension",
                ],
            },
        ),
        (
            "rerank",
            {
                "label": "重排序配置",
                "icon": "shuffle",
                "keys": [
                    "rerank_model",
                    "rerank_provider",
                    "rerank_base_url",
                    "rerank_api_key",
                    "rerank_score_threshold",
                ],
            },
        ),
        (
            "code_index",
            {
                "label": "代码索引配置",
                "icon": "file-code",
                "keys": [
                    "enable_code_index",
                    "auto_index_pr_changes",
                    "code_chunk_size",
                    "code_chunk_overlap",
                ],
            },
        ),
        (
            "context",
            {
                "label": "上下文管理配置",
                "icon": "compress",
                "keys": [
                    "model_context_window",
                    "context_safety_threshold",
                    "enable_context_compression",
                    "context_compression_threshold",
                    "context_compression_keep_rounds",
                ],
            },
        ),
        (
            "review_strategy",
            {
                "label": "审查策略配置",
                "icon": "shield",
                "keys": [
                    "max_file_count",
                    "max_line_count",
                    "enable_inline_comments",
                ],
            },
        ),
        (
            "incremental_review",
            {
                "label": "增量审查配置",
                "icon": "history",
                "keys": [
                    "enable_incremental_history_context",
                    "incremental_history_max_reviews",
                    "incremental_history_summary_max_tokens",
                ],
            },
        ),
        (
            "pr_summary",
            {
                "label": "PR 总结配置",
                "icon": "file-text",
                "keys": [
                    "enable_pr_summary",
                ],
            },
        ),
        (
            "pr_dependency_graph",
            {
                "label": "PR 依赖图配置",
                "icon": "git-branch",
                "keys": [
                    "enable_pr_dependency_graph",
                    "pr_dependency_graph_mode",
                    "pr_dependency_graph_max_nodes",
                    "pr_dependency_graph_max_files",
                ],
            },
        ),
        (
            "semantic_issue_linking",
            {
                "label": "语义 Issue 关联",
                "icon": "link",
                "keys": [
                    "enable_semantic_issue_linking",
                    "semantic_issue_similarity_threshold",
                    "semantic_issue_max_links",
                ],
            },
        ),
        (
            "issue_analysis",
            {
                "label": "Issue 分析配置",
                "icon": "check-circle",
                "descriptions": {
                    "enable_issue_analysis": "启用后，系统将自动分析新创建的 Issue",
                    "issue_auto_comment": "分析完成后自动在 Issue 下发布分析报告评论",
                    "issue_confidence_threshold": "标签置信度阈值（0-1），AI 建议标签的置信度达到此值才会自动应用",
                    "issue_auto_create_labels": "自动创建仓库中不存在的推荐标签",
                    "issue_auto_assign": "根据 AI 建议自动指派 Issue 负责人",
                    "issue_auto_rewrite_title": "AI 生成规范化标题并自动修改 Issue 标题（默认关闭）",
                    "issue_assignee_confidence_threshold": "指派人置信度阈值（0-1），达到此值才会自动指派",
                    "issue_auto_assign_max": "单个 Issue 最多自动指派的人数",
                    "issue_detect_duplicates": "启用后自动检测重复 Issue",
                    "issue_suggest_assignees": "AI 分析时推荐合适的指派人",
                    "issue_suggest_milestones": "AI 分析时推荐合适的里程碑",
                    "issue_max_tool_iterations": "AI 工具调用最大迭代次数，控制分析深度",
                    "issue_max_files_per_analysis": "单次分析最多读取的文件数",
                    "issue_max_directory_depth": "目录浏览的最大深度",
                },
                "keys": [
                    "enable_issue_analysis",
                    "issue_auto_comment",
                    "issue_confidence_threshold",
                    "issue_auto_create_labels",
                    "issue_auto_assign",
                    "issue_auto_rewrite_title",
                    "issue_assignee_confidence_threshold",
                    "issue_auto_assign_max",
                    "issue_detect_duplicates",
                    "issue_suggest_assignees",
                    "issue_suggest_milestones",
                    "issue_max_tool_iterations",
                    "issue_max_files_per_analysis",
                    "issue_max_directory_depth",
                ],
            },
        ),
        (
            "repo_scan",
            {
                "label": "仓库扫描配置",
                "icon": "shield-check",
                "descriptions": {
                    "enable_repo_scan": "启用后，系统将定期扫描已安装仓库的代码，检测潜在问题",
                    "scan_interval_minutes": "定时扫描的间隔时间（分钟），默认 360 分钟（6小时）",
                    "scan_cooldown_hours": "同一仓库两次扫描之间的最小间隔（小时）",
                    "scan_max_tokens_per_repo": "单个仓库每次扫描的 Token 消耗上限（0 表示无限制）",
                    "scan_auto_create_issue": "扫描完成后是否自动在仓库中创建 Issue 报告",
                    "scan_send_telegram": "扫描完成后是否发送 Telegram 通知",
                    "scan_min_severity_for_issue": "达到该严重性及以上时才创建 Issue（critical/major/minor/suggestion）",
                    "scan_model": "扫描使用的 AI 模型，为空时使用全局模型配置",
                    "scan_max_iterations": "扫描过程中 AI 工具调用的最大轮次数",
                    "scan_context_safety_threshold": "扫描对话上下文安全阈值（0-1），超过后触发压缩",
                    "scan_compression_threshold": "扫描对话压缩触发阈值（0-1），上下文占比达到该值时压缩历史",
                    "scan_temperature": "扫描 AI 温度参数，越低越确定性，越高越创造性",
                },
                "keys": [
                    "enable_repo_scan",
                    "scan_interval_minutes",
                    "scan_cooldown_hours",
                    "scan_max_tokens_per_repo",
                    "scan_auto_create_issue",
                    "scan_send_telegram",
                    "scan_min_severity_for_issue",
                    "scan_model",
                    "scan_max_iterations",
                    "scan_context_safety_threshold",
                    "scan_compression_threshold",
                    "scan_temperature",
                ],
            },
        ),
        (
            "sakura_memory",
            {
                "label": "Sakura 记忆系统",
                "icon": "brain",
                "descriptions": {
                    "sakura_memory_enabled": "启用 .sakura/ 记忆系统，在 PR 审查中自动积累项目知识",
                    "sakura_reflection_enabled": "启用审查后反思，AI 会分析自身审查质量并总结经验",
                    "sakura_reflection_model": "反思使用的 AI 模型，留空则使用审查模型",
                    "sakura_issue_reflection_enabled": "启用 Issue 分析后反思，AI 会分析自身分类、标签推荐等质量并总结经验",
                    "sakura_issue_reflection_model": "Issue 反思使用的 AI 模型，留空则使用审查模型",
                    "sakura_consolidation_interval": "积累多少轮反思后触发知识合并（建议 3-10）",
                    "sakura_consolidation_model": "合并使用的 AI 模型，留空则使用审查模型",
                    "sakura_max_memory_chars": "memory.md 文件的最大字符数限制",
                    "sakura_max_sakura_chars": "SAKURA.md 文件的最大字符数限制",
                    "sakura_auto_init": "首次审查时自动在仓库中初始化 .sakura/ 目录",
                    "sakura_consolidation_partial_commit": "合并时一个文件生成失败是否仍提交成功生成的文件",
                    "sakura_use_summary_model": "启用后反思、合并等后台任务将使用辅助模型的 API 凭据，降低成本",
                },
                "keys": [
                    "sakura_memory_enabled",
                    "sakura_reflection_enabled",
                    "sakura_reflection_model",
                    "sakura_issue_reflection_enabled",
                    "sakura_issue_reflection_model",
                    "sakura_consolidation_interval",
                    "sakura_consolidation_model",
                    "sakura_max_memory_chars",
                    "sakura_max_sakura_chars",
                    "sakura_auto_init",
                    "sakura_consolidation_partial_commit",
                    "sakura_use_summary_model",
                ],
            },
        ),
        (
            "fetch_url",
            {
                "label": "URL 抓取配置",
                "icon": "globe",
                "descriptions": {
                    "fetch_url_enabled": "启用后 AI 可抓取网页内容（需同时启用 Web 搜索）",
                    "fetch_url_timeout": "抓取超时时间（秒）",
                    "fetch_url_max_content_length": "提取的纯文本最大长度（字符），超出部分截断",
                    "fetch_url_max_download_size": "原始 HTML 最大下载大小（字节），防止内存耗尽",
                    "fetch_url_max_calls_per_session": "单次审查/分析会话中允许的最大抓取次数",
                    "fetch_url_domain_policy": "域名过滤策略：off（仅 IP 拦截）/ blacklist（黑名单）/ whitelist（白名单）",
                    "fetch_url_domain_list": "域名列表（逗号分隔），根据策略用作黑名单或白名单，支持 * 通配符",
                    "fetch_url_force_https": "强制仅允许 HTTPS 协议，拒绝 HTTP 明文传输",
                },
                "keys": [
                    "fetch_url_enabled",
                    "fetch_url_timeout",
                    "fetch_url_max_content_length",
                    "fetch_url_max_download_size",
                    "fetch_url_max_calls_per_session",
                    "fetch_url_domain_policy",
                    "fetch_url_domain_list",
                    "fetch_url_force_https",
                ],
            },
        ),
        (
            "i18n",
            {
                "label": "国际化配置",
                "icon": "globe",
                "descriptions": {
                    "default_language": "WebUI 默认界面语言（zh-CN / en）",
                    "output_language": "AI 输出语言，为空时跟随用户界面语言，可设为 zh-CN 或 en 强制指定",
                },
                "keys": [
                    "default_language",
                    "output_language",
                ],
            },
        ),
        (
            "payment",
            {
                "label": "付费配额配置",
                "icon": "credit-card",
                "keys": [
                    "payment_enabled",
                    "payment_order_expire_minutes",
                    "payment_default_currency",
                ],
            },
        ),
    ]
)

# 敏感字段（API Key 等）
DYNAMIC_CONFIG_SENSITIVE_KEYS = frozenset(
    {
        "openai_api_key",
        "summary_api_key",
        "embedding_api_key",
        "rerank_api_key",
        "github_webhook_secret",
        "webui_secret_key",
        "github_oauth_client_secret",
        "telegram_bot_token",
    }
)

# 选择类字段的选项
DYNAMIC_CONFIG_SELECT_OPTIONS: dict[str, list[dict]] = {
    "ai_provider": get_provider_select_options(),
    "summary_provider": get_provider_select_options(include_summary_follow=True),
    "embedding_provider": [
        {"value": "siliconflow", "label": "SiliconFlow"},
        {"value": "openai", "label": "OpenAI"},
        {"value": "ollama", "label": "Ollama"},
        {"value": "hf", "label": "HuggingFace"},
    ],
    "rerank_provider": [
        {"value": "siliconflow", "label": "SiliconFlow"},
        {"value": "none", "label": "禁用"},
    ],
    "default_language": [
        {"value": "zh-CN", "label": "简体中文"},
        {"value": "en", "label": "English"},
    ],
    "output_language": [
        {"value": "", "label": "跟随界面语言"},
        {"value": "zh-CN", "label": "简体中文"},
        {"value": "en", "label": "English"},
    ],
    "pr_dependency_graph_mode": [
        {"value": "ai", "label": "AI 生成（使用 LLM 分析）"},
        {"value": "static", "label": "静态分析（正则提取 import）"},
    ],
}

# 数值范围限制
DYNAMIC_CONFIG_RANGES: dict[str, tuple[float, float]] = {
    "embedding_dimension": (128, 4096),
    "rerank_score_threshold": (0.0, 1.0),
    "code_chunk_size": (100, 5000),
    "code_chunk_overlap": (0, 1000),
    "model_context_window": (0, 2000),
    "context_safety_threshold": (0.1, 1.0),
    "context_compression_threshold": (0.1, 1.0),
    "context_compression_keep_rounds": (1, 20),
    "max_file_count": (1, 100000),
    "max_line_count": (100, 100000000),
    "incremental_history_max_reviews": (1, 20),
    "incremental_history_summary_max_tokens": (500, 4096),
    "pr_dependency_graph_max_nodes": (5, 50),
    "pr_dependency_graph_max_files": (5, 200),
    "semantic_issue_similarity_threshold": (0.0, 1.0),
    "semantic_issue_max_links": (1, 20),
    "scan_interval_minutes": (30, 10080),  # 30分钟 ~ 7天
    "scan_cooldown_hours": (1, 168),  # 1小时 ~ 7天
    "scan_max_tokens_per_repo": (0, 5000000),
    "scan_max_iterations": (1, 5000),
    "scan_context_safety_threshold": (0.1, 1.0),
    "scan_compression_threshold": (0.1, 1.0),
    "scan_temperature": (0.0, 2.0),
    # Sakura 记忆系统
    "sakura_consolidation_interval": (1, 50),
    "sakura_max_memory_chars": (500, 10000),
    "sakura_max_sakura_chars": (1000, 20000),
    # Issue 分析
    "issue_confidence_threshold": (0.0, 1.0),
    "issue_assignee_confidence_threshold": (0.0, 1.0),
    "issue_auto_assign_max": (1, 10),
}

# 字段中文标签
DYNAMIC_CONFIG_LABELS: dict[str, str] = {
    "ai_provider": "AI 厂商",
    "openai_api_base": "API Base URL",
    "openai_api_key": "API Key",
    "openai_model": "模型名称",
    "ai_api_timeout_seconds": "AI API 请求超时（秒）",
    "ai_api_max_retries": "AI API 最大重试次数",
    "ai_api_initial_retry_delay_seconds": "AI API 初始重试延迟（秒）",
    "ai_api_total_timeout_seconds": "AI API 重试总超时（秒）",
    "summary_provider": "辅助模型厂商",
    "summary_model": "辅助模型名称",
    "summary_api_base": "辅助模型 API 地址",
    "summary_api_key": "辅助模型 API Key",
    "enable_rag": "启用 RAG",
    "chroma_persist_dir": "ChromaDB 存储路径",
    "embedding_model": "嵌入模型",
    "embedding_provider": "嵌入提供商",
    "embedding_base_url": "嵌入 API 地址",
    "embedding_api_key": "嵌入 API Key",
    "embedding_dimension": "嵌入维度",
    "rerank_model": "重排序模型",
    "rerank_provider": "重排序提供商",
    "rerank_base_url": "重排序 API 地址",
    "rerank_api_key": "重排序 API Key",
    "rerank_score_threshold": "重排序分数阈值",
    "enable_code_index": "启用代码索引",
    "auto_index_pr_changes": "自动索引 PR 变更",
    "code_chunk_size": "代码块大小",
    "code_chunk_overlap": "代码块重叠",
    "model_context_window": "上下文窗口大小",
    "context_safety_threshold": "上下文安全阈值",
    "enable_context_compression": "启用上下文压缩",
    "context_compression_threshold": "压缩触发阈值",
    "context_compression_keep_rounds": "保留对话轮数",
    "max_file_count": "最大文件数",
    "max_line_count": "最大行数",
    "enable_incremental_history_context": "启用增量审查历史上下文",
    "enable_inline_comments": "启用行内评论",
    "enable_pr_summary": "启用 PR 变更总结",
    "incremental_history_max_reviews": "历史审查轮数上限",
    "incremental_history_summary_max_tokens": "摘要生成最大 Token",
    "enable_pr_dependency_graph": "启用 PR 依赖图",
    "pr_dependency_graph_mode": "PR 依赖图模式",
    "pr_dependency_graph_max_nodes": "依赖图最大节点数",
    "pr_dependency_graph_max_files": "分析文件数上限",
    "enable_semantic_issue_linking": "启用语义 Issue 关联",
    "semantic_issue_similarity_threshold": "语义相似度阈值",
    "semantic_issue_max_links": "最大关联 Issue 数量",
    "payment_enabled": "启用付费配额系统",
    "payment_order_expire_minutes": "订单过期时间（分钟）",
    "payment_default_currency": "默认货币",
    # 核心配置标签
    "github_app_id": "GitHub App ID",
    "github_private_key": "GitHub App 私钥",
    "github_webhook_secret": "GitHub Webhook Secret",
    "telegram_bot_token": "Telegram Bot Token",
    "webui_secret_key": "WebUI 密钥",
    "app_domain": "应用域名",
    "app_port": "应用端口",
    "log_level": "日志级别",
    "bot_username": "Bot 用户名",
    "github_oauth_client_id": "GitHub OAuth Client ID",
    "github_oauth_client_secret": "GitHub OAuth Client Secret",
    "github_oauth_redirect_uri": "GitHub OAuth 回调地址",
    # 仓库扫描配置标签
    "enable_repo_scan": "启用仓库扫描",
    "scan_interval_minutes": "扫描间隔（分钟）",
    "scan_cooldown_hours": "扫描冷却时间（小时）",
    "scan_max_tokens_per_repo": "单仓库 Token 预算（0=无限制）",
    "scan_auto_create_issue": "自动创建 Issue 报告",
    "scan_send_telegram": "发送 Telegram 通知",
    "scan_min_severity_for_issue": "创建 Issue 最低严重性",
    "scan_model": "扫描模型名称",
    "scan_max_iterations": "扫描最大轮次",
    "scan_context_safety_threshold": "扫描上下文安全阈值",
    "scan_compression_threshold": "扫描压缩阈值",
    "scan_temperature": "扫描 AI 温度",
    # Sakura 记忆系统
    "sakura_memory_enabled": "启用记忆系统",
    "sakura_reflection_enabled": "启用审查反思",
    "sakura_reflection_model": "反思模型名称",
    "sakura_issue_reflection_enabled": "启用 Issue 反思",
    "sakura_issue_reflection_model": "Issue 反思模型名称",
    "sakura_consolidation_interval": "合并触发反思轮数",
    "sakura_consolidation_model": "合并模型名称",
    "sakura_max_memory_chars": "memory.md 最大字符数",
    "sakura_max_sakura_chars": "SAKURA.md 最大字符数",
    "sakura_auto_init": "自动初始化 .sakura/",
    "sakura_consolidation_partial_commit": "部分提交",
    "sakura_use_summary_model": "使用辅助模型",
    # 国际化配置
    "default_language": "默认界面语言",
    "output_language": "AI 输出语言",
    # URL 抓取配置
    "fetch_url_enabled": "启用 URL 抓取",
    "fetch_url_timeout": "抓取超时（秒）",
    "fetch_url_max_content_length": "最大内容长度",
    "fetch_url_max_download_size": "最大下载大小（字节）",
    "fetch_url_max_calls_per_session": "单次会话最大抓取次数",
    "fetch_url_domain_policy": "域名过滤策略",
    "fetch_url_domain_list": "域名列表",
    "fetch_url_force_https": "强制 HTTPS",
    # Issue 分析配置
    "enable_issue_analysis": "启用 Issue 分析",
    "issue_auto_comment": "自动发布分析评论",
    "issue_confidence_threshold": "标签置信度阈值",
    "issue_auto_create_labels": "自动创建标签",
    "issue_auto_assign": "自动指派负责人",
    "issue_auto_rewrite_title": "自动改写 Issue 标题",
    "issue_assignee_confidence_threshold": "指派人置信度阈值",
    "issue_auto_assign_max": "最大指派人数",
    "issue_detect_duplicates": "检测重复 Issue",
    "issue_suggest_assignees": "推荐指派人",
    "issue_suggest_milestones": "推荐里程碑",
    "issue_max_tool_iterations": "工具最大迭代次数",
    "issue_max_files_per_analysis": "单次分析最大文件数",
    "issue_max_directory_depth": "目录最大深度",
}

# 内存 TTL 缓存（进程级，多 Worker 部署时各进程独立，配置变更仅当前进程可见）
_dynamic_config_cache: OrderedDict[str, tuple[str, float]] = OrderedDict()
_CACHE_TTL = 60  # 秒
_MAX_CACHE_SIZE = 200

# ========== 用户级动态配置 / User-scoped dynamic configuration ==========

# 第一阶段仅开放偏好类配置，禁止 API Key、并发、配额等基础设施配置被用户覆盖。
USER_DYNAMIC_CONFIG_KEYS = frozenset({"output_language"})

_user_dynamic_config_cache: OrderedDict[tuple[int, str], tuple[str, float]] = (
    OrderedDict()
)
_MAX_USER_CONFIG_CACHE_SIZE = 1000


def _get_field_type(key: str) -> type:
    """从 Settings 字段定义获取类型"""
    field_info = Settings.model_fields.get(key)
    if field_info is None:
        return str
    ann = field_info.annotation
    if get_origin(ann) is Literal:
        return str
    # 处理 Optional[X] 等
    if hasattr(ann, "__origin__"):
        return ann.__args__[0] if ann.__args__ else str
    return ann if isinstance(ann, type) else str


def get_dynamic_config_input_type(key: str) -> str:
    """根据 Settings 字段类型推断 WebUI 输入类型"""
    if key in DYNAMIC_CONFIG_SELECT_OPTIONS:
        return "select"
    if key in DYNAMIC_CONFIG_SENSITIVE_KEYS:
        return "password"
    field_type = _get_field_type(key)
    if field_type is bool:
        return "boolean"
    if field_type in (int, float):
        return "number"
    return "text"


async def get_dynamic_config(key: str) -> Any:
    """从数据库读取配置值，回退到 Settings 默认值

    Args:
        key: 配置键名（对应 Settings 字段名）

    Returns:
        配置值（已转换类型）
    """
    expected_type = _get_field_type(key)

    # 1. 检查内存缓存
    cached = _dynamic_config_cache.get(key)
    if cached is not None:
        value, expire_time = cached
        if time.time() < expire_time:
            return _cast_config_type(value, expected_type)
        _dynamic_config_cache.pop(key, None)

    # 2. 从数据库读取
    db_value = await _read_config_from_db(key)
    if db_value is not None:
        _dynamic_config_cache[key] = (db_value, time.time() + _CACHE_TTL)
        _evict_config_cache()
        return _cast_config_type(db_value, expected_type)

    # 3. 回退到 Settings 默认值
    settings = get_settings()
    return getattr(settings, key, None)


def validate_user_dynamic_config_value(key: str, value: Any) -> str:
    """校验并标准化用户级配置值。

    Args:
        key: 配置键名
        value: 原始配置值

    Returns:
        标准化后的字符串值

    Raises:
        ValueError: 配置键不允许用户覆盖，或值不合法
    """
    if key not in USER_DYNAMIC_CONFIG_KEYS:
        raise ValueError(f"配置项不允许用户覆盖: {key}")

    normalized = "" if value is None else str(value)
    if key == "output_language":
        allowed = {option["value"] for option in DYNAMIC_CONFIG_SELECT_OPTIONS[key]}
        if normalized not in allowed:
            raise ValueError("output_language 仅允许为空、zh-CN 或 en")
    return normalized


async def get_user_dynamic_config(key: str, user_id: int | None = None) -> Any:
    """读取用户级动态配置，回退到全局动态配置。

    解析链：UserConfig → AppConfig/get_dynamic_config → Settings 默认值。
    """
    if key not in USER_DYNAMIC_CONFIG_KEYS or not user_id:
        return await get_dynamic_config(key)

    expected_type = _get_field_type(key)
    cache_key = (int(user_id), key)
    cached = _user_dynamic_config_cache.get(cache_key)
    if cached is not None:
        value, expire_time = cached
        if time.time() < expire_time:
            _user_dynamic_config_cache.move_to_end(cache_key)
            return _cast_config_type(value, expected_type)
        _user_dynamic_config_cache.pop(cache_key, None)

    db_value = await _read_user_config_from_db(int(user_id), key)
    if db_value is not None:
        _user_dynamic_config_cache[cache_key] = (db_value, time.time() + _CACHE_TTL)
        _evict_user_config_cache()
        return _cast_config_type(db_value, expected_type)

    return await get_dynamic_config(key)


async def get_user_dynamic_config_state(key: str, user_id: int) -> dict[str, Any]:
    """返回用户配置展示所需的状态信息。"""
    if key not in USER_DYNAMIC_CONFIG_KEYS:
        raise ValueError(f"配置项不允许用户覆盖: {key}")

    user_value = await _read_user_config_from_db(user_id, key)
    global_value = await get_dynamic_config(key)
    effective_value = (
        _cast_config_type(user_value, _get_field_type(key))
        if user_value is not None
        else global_value
    )
    return {
        "key": key,
        "label": DYNAMIC_CONFIG_LABELS.get(key, key),
        "description": DYNAMIC_CONFIG_GROUPS.get("i18n", {})
        .get("descriptions", {})
        .get(key, ""),
        "input_type": get_dynamic_config_input_type(key),
        "options": DYNAMIC_CONFIG_SELECT_OPTIONS.get(key, []),
        "user_value": user_value,
        "global_value": global_value,
        "effective_value": effective_value,
        "is_overridden": user_value is not None,
    }


async def _read_user_config_from_db(user_id: int, key: str) -> Optional[str]:
    """从 UserConfig 表读取用户配置值。"""
    try:
        # 延迟导入避免 config ↔ database 初始化阶段循环引用。
        from backend.models.database import UserConfig, async_session
        from sqlalchemy import select

        async with async_session() as session:
            result = await session.execute(
                select(UserConfig.config_value).where(
                    UserConfig.user_id == user_id,
                    UserConfig.config_key == key,
                )
            )
            row = result.scalar_one_or_none()
            if row is not None:
                return str(row)
            return None
    except Exception as e:
        logger.debug(f"从数据库读取用户配置 [{user_id}:{key}] 失败: {e}")
        return None


def invalidate_user_dynamic_config_cache(
    user_id: int | None = None, keys: list[str] | None = None
):
    """清除用户级动态配置缓存。"""
    if user_id is None:
        _user_dynamic_config_cache.clear()
        return

    if keys is None:
        for cache_key in list(_user_dynamic_config_cache.keys()):
            if cache_key[0] == int(user_id):
                _user_dynamic_config_cache.pop(cache_key, None)
        return

    for key in keys:
        _user_dynamic_config_cache.pop((int(user_id), key), None)


async def _read_config_from_db(key: str) -> Optional[str]:
    """从 AppConfig 表读取配置值"""
    try:
        from backend.models.database import async_session, AppConfig
        from sqlalchemy import select

        async with async_session() as session:
            result = await session.execute(
                select(AppConfig.key_value).where(AppConfig.key_name == key)
            )
            row = result.scalar_one_or_none()
            if row is not None:
                return str(row)
            return None
    except Exception as e:
        logger.debug(f"从数据库读取配置 [{key}] 失败: {e}")
        return None


def invalidate_dynamic_config_cache(keys: list[str] | None = None):
    """清除动态配置缓存"""
    if keys is None:
        _dynamic_config_cache.clear()
    else:
        for k in keys:
            _dynamic_config_cache.pop(k, None)


def get_cached_config(key: str) -> Optional[str]:
    """从动态配置缓存中同步读取值（无 I/O）

    用于 i18n 等同步上下文中读取运行时配置。
    返回 None 表示缓存未命中或已过期。
    """
    cached = _dynamic_config_cache.get(key)
    if cached is not None:
        value, expire_time = cached
        if time.time() < expire_time:
            return value
    return None


# 核心配置键（Setup Wizard 写入、运行时从 DB 加载）
# 与 setup_service._ENV_TO_SETTINGS_KEY 的 values 集合对应，新增配置需同步更新两处
CORE_CONFIG_KEYS = frozenset(
    {
        "github_app_id",
        "github_private_key",
        "github_webhook_secret",
        "ai_provider",
        "openai_api_key",
        "openai_api_base",
        "openai_model",
        "telegram_bot_token",
        "webui_secret_key",
        "app_domain",
        "app_port",
        "log_level",
        "bot_username",
        "github_oauth_client_id",
        "github_oauth_client_secret",
        "github_oauth_redirect_uri",
        "database_url",
        "summary_provider",
    }
)

# WebUI 基础配置键（存储在 AppConfig 中，也需要加载到 Settings 单例）
BASIC_CONFIG_KEYS = frozenset(
    {
        "max_concurrent_reviews",
        "review_timeout_seconds",
        "enable_auto_review",
        "web_search_enabled",
        "web_search_provider",
        "web_search_api_key",
        "web_search_max_results",
        "web_search_max_content_length",
        "web_search_timeout",
    }
)


def get_all_dynamic_config_keys() -> list[str]:
    """获取所有动态配置键名"""
    keys = []
    for group in DYNAMIC_CONFIG_GROUPS.values():
        keys.extend(group["keys"])
    return keys


def get_all_db_config_keys() -> list[str]:
    """获取所有应从 DB 加载的配置键（动态配置 + 核心配置 + 基础配置）"""
    keys = get_all_dynamic_config_keys()
    for key_group in (CORE_CONFIG_KEYS, BASIC_CONFIG_KEYS):
        for key in key_group:
            if key not in keys:
                keys.append(key)
    return keys


def mask_sensitive_value(value: str) -> str:
    """脱敏敏感值"""
    if not value or len(value) <= 8:
        return "****"
    return f"{value[:4]}{'*' * (len(value) - 8)}{value[-4:]}"


def _cast_config_type(value: Any, expected_type: type) -> Any:
    """类型转换"""
    if value is None:
        return None
    if expected_type is bool:
        if isinstance(value, str):
            return value.lower() in ("true", "1", "yes")
        return bool(value)
    try:
        return expected_type(value)
    except (ValueError, TypeError):
        return value


def _evict_config_cache():
    """LRU 缓存淘汰"""
    while len(_dynamic_config_cache) > _MAX_CACHE_SIZE:
        _dynamic_config_cache.popitem(last=False)


def _evict_user_config_cache():
    """用户级动态配置 LRU 缓存淘汰。"""
    while len(_user_dynamic_config_cache) > _MAX_USER_CONFIG_CACHE_SIZE:
        _user_dynamic_config_cache.popitem(last=False)


async def load_dynamic_configs_to_settings():
    """从数据库加载全部配置到 Settings 单例

    启动时调用一次，覆盖所有已迁移到 DB 的配置项（动态配置 + 核心配置 + 基础配置）。
    让所有使用 settings.xxx 的服务直接拿到 DB 中的值。
    """
    settings = get_settings()
    all_keys = get_all_db_config_keys()
    if not all_keys:
        return

    try:
        from backend.models.database import async_session, AppConfig
        from sqlalchemy import select

        async with async_session() as session:
            result = await session.execute(
                select(AppConfig).where(AppConfig.key_name.in_(all_keys))
            )
            config_map = {c.key_name: c.key_value for c in result.scalars().all()}
    except Exception as e:
        logger.warning(f"批量加载动态配置失败: {e}")
        return

    loaded = 0
    for key in all_keys:
        db_value = config_map.get(key)
        if db_value is not None:
            field_type = _get_field_type(key)
            typed_value = _cast_config_type(db_value, field_type)
            try:
                setattr(settings, key, typed_value)
                loaded += 1
            except Exception as e:
                logger.warning(f"加载动态配置 [{key}] 到 Settings 失败: {e}")
    logger.info(f"已从数据库加载 {loaded} 项动态配置到 Settings")


def update_settings_field(key: str, value: str):
    """WebUI 保存配置时同步更新 Settings 单例（即时生效）"""
    settings = get_settings()
    field_type = _get_field_type(key)
    typed_value = _cast_config_type(value, field_type)
    try:
        setattr(settings, key, typed_value)
    except Exception as e:
        logger.warning(f"更新 Settings 字段 [{key}] 失败: {e}")
