"""配置管理模块"""

from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache
import yaml
from pathlib import Path


class Settings(BaseSettings):
    """应用配置"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        protected_namespaces=("settings_",),
    )

    # GitHub App配置
    github_app_id: int
    github_private_key: str
    github_webhook_secret: str

    # OpenAI配置
    openai_api_base: str = "https://api.openai.com/v1"
    openai_api_key: str
    openai_model: str = "gpt-4"
    openai_temperature: float = 0.3
    openai_max_tokens: int = 4000

    # 模型上下文配置
    model_context_window: int = 0  # 自定义上下文窗口大小（K tokens），0 表示自动检测
    auto_fetch_model_context: bool = True  # 是否自动从 API 获取模型上下文
    context_safety_threshold: float = 0.8  # 上下文安全阈值（0-1），默认使用 80%

    # 上下文压缩配置
    enable_context_compression: bool = True  # 是否启用上下文自动压缩
    context_compression_threshold: float = 0.85  # 压缩触发阈值（0-1），默认 85%
    context_compression_keep_rounds: int = 2  # 保留最近几轮对话不压缩

    # 数据库配置
    database_url: str

    # Redis配置
    redis_url: str = "redis://redis:6379/0"

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

    # 标签推荐配置
    enable_label_recommendation: bool = True
    label_confidence_threshold: float = 0.7
    label_auto_create: bool = False

    # Webhook配置
    webhook_path: str = "/api/webhook/github"

    # Telegram Bot配置
    telegram_bot_token: str
    telegram_admin_user_ids: str = ""  # 逗号分隔的超级管理员ID列表
    telegram_default_chat_id: str = ""  # 默认接收通知的聊天ID

    # GitHub App机器人用户名（可选，用于幂等性检查）
    bot_username: str = None  # 备用方案，当无法从GitHub API获取时使用

    @property
    def webhook_url(self) -> str:
        """获取完整的Webhook URL"""
        return f"https://{self.app_domain}{self.webhook_path}"

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

    # ========== 代码索引配置 ==========
    enable_code_index: bool = True  # 是否启用代码索引功能
    auto_index_pr_changes: bool = True  # PR审查时自动索引变更文件

    # 代码分块配置
    code_chunk_size: int = 500  # 代码块大小（字符数）
    code_chunk_overlap: int = 50  # 代码块重叠大小

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
