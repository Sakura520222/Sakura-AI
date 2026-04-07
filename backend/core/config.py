"""配置管理模块"""

from collections import OrderedDict
from typing import Any, Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache
import time
import yaml
from pathlib import Path
from loguru import logger


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

    # OpenAI配置
    openai_api_base: str = "https://api.openai.com/v1"
    openai_api_key: Optional[str] = None
    openai_model: str = "gpt-4"
    openai_temperature: float = 0.3
    openai_max_tokens: int = 4000

    # 辅助模型配置（摘要、压缩等轻量任务，未设置时回退到主模型）
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

    # Telegram Bot配置
    telegram_bot_token: Optional[str] = None
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

    # ========== Web 搜索配置 ==========
    web_search_enabled: bool = False  # 是否启用 Web 搜索工具
    web_search_provider: str = "duckduckgo"  # 搜索提供商：duckduckgo(免费) | tavily
    web_search_api_key: str = ""  # API Key（tavily 需要，duckduckgo 不需要）
    web_search_max_results: int = 3  # 最大返回结果数
    web_search_max_content_length: int = 500  # 每个结果截断长度（字符）
    web_search_timeout: int = 15  # 搜索超时（秒）

    # ========== 代码索引配置 ==========
    enable_code_index: bool = True  # 是否启用代码索引功能
    auto_index_pr_changes: bool = True  # PR审查时自动索引变更文件

    # 代码分块配置
    code_chunk_size: int = 500  # 代码块大小（字符数）
    code_chunk_overlap: int = 50  # 代码块重叠大小

    # ========== 增量审查历史上下文配置 ==========
    enable_incremental_history_context: bool = True  # 是否启用增量审查历史上下文
    enable_pr_summary: bool = False  # 是否启用 PR 变更自动总结
    incremental_history_max_reviews: int = 5  # 最多查询的历史审查轮数
    incremental_history_summary_max_tokens: int = 1500  # 摘要生成最大 token

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
                    "openai_api_base",
                    "openai_api_key",
                    "openai_model",
                ],
            },
        ),
        (
            "summary_model",
            {
                "label": "辅助模型配置",
                "icon": "zap",
                "descriptions": {
                    "summary_model": "用于摘要生成、上下文压缩等轻量任务，留空则使用主模型",
                    "summary_api_base": "辅助模型的 API 地址，留空则使用主模型地址",
                    "summary_api_key": "辅助模型的 API Key，留空则使用主模型 Key",
                },
                "keys": [
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
    ]
)

# 敏感字段（API Key 等）
DYNAMIC_CONFIG_SENSITIVE_KEYS = frozenset({
    "openai_api_key",
    "summary_api_key",
    "embedding_api_key",
    "rerank_api_key",
    "github_webhook_secret",
    "webui_secret_key",
    "github_oauth_client_secret",
    "telegram_bot_token",
})

# 选择类字段的选项
DYNAMIC_CONFIG_SELECT_OPTIONS: dict[str, list[dict]] = {
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
}

# 字段中文标签
DYNAMIC_CONFIG_LABELS: dict[str, str] = {
    "openai_api_base": "API Base URL",
    "openai_api_key": "API Key",
    "openai_model": "模型名称",
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
    "enable_pr_summary": "启用 PR 变更总结",
    "incremental_history_max_reviews": "历史审查轮数上限",
    "incremental_history_summary_max_tokens": "摘要生成最大 Token",
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
}

# 内存 TTL 缓存（进程级，多 Worker 部署时各进程独立，配置变更仅当前进程可见）
_dynamic_config_cache: OrderedDict[str, tuple[str, float]] = OrderedDict()
_CACHE_TTL = 60  # 秒
_MAX_CACHE_SIZE = 200


def _get_field_type(key: str) -> type:
    """从 Settings 字段定义获取类型"""
    field_info = Settings.model_fields.get(key)
    if field_info is None:
        return str
    ann = field_info.annotation
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


# 核心配置键（Setup Wizard 写入、运行时从 DB 加载）
# 与 setup_service._ENV_TO_SETTINGS_KEY 的 values 集合对应，新增配置需同步更新两处
CORE_CONFIG_KEYS = frozenset({
    "github_app_id",
    "github_private_key",
    "github_webhook_secret",
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
})


def get_all_dynamic_config_keys() -> list[str]:
    """获取所有动态配置键名"""
    keys = []
    for group in DYNAMIC_CONFIG_GROUPS.values():
        keys.extend(group["keys"])
    return keys


def get_all_db_config_keys() -> list[str]:
    """获取所有应从 DB 加载的配置键（动态配置 + 核心配置）"""
    keys = get_all_dynamic_config_keys()
    for key in CORE_CONFIG_KEYS:
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


async def load_dynamic_configs_to_settings():
    """从数据库加载全部配置到 Settings 单例

    启动时调用一次，覆盖所有已迁移到 DB 的配置项（动态配置 + 核心配置）。
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
