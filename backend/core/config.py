"""配置管理模块"""

from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache
import yaml
from pathlib import Path


class Settings(BaseSettings):
    """应用配置"""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", case_sensitive=False, extra="ignore"
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

    @property
    def webhook_url(self) -> str:
        """获取完整的Webhook URL"""
        return f"https://{self.app_domain}{self.webhook_path}"

    @property
    def telegram_admin_ids_list(self) -> list[int]:
        """获取超级管理员ID列表"""
        if not self.telegram_admin_user_ids:
            return []
        return [int(id.strip()) for id in self.telegram_admin_user_ids.split(",") if id.strip()]


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


@lru_cache()
def get_settings() -> Settings:
    """获取配置单例"""
    return Settings()


@lru_cache()
def get_strategy_config() -> StrategyConfig:
    """获取策略配置单例"""
    return StrategyConfig()
