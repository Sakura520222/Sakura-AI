"""WebUI 国际化（i18n）模块

基于 YAML 翻译文件的轻量级 i18n 实现。
支持中英文双语，按用户偏好动态切换。

Usage:
    # 在 Jinja2 模板中:
    {{ _("dashboard.title") }}
    {{ _("reviews.count", count=5) }}

    # 在 Python 代码中:
    from backend.webui.i18n import i18n
    text = i18n.t("comment.review_passed", lang="en")
"""

import threading
from pathlib import Path
from typing import Any

import yaml
from loguru import logger

# 翻译文件目录 / Translation files directory
_TRANSLATIONS_DIR = Path(__file__).parent / "translations"

# 支持的语言列表 / Supported languages
SUPPORTED_LANGUAGES = ["zh-CN", "en"]

# 默认语言 / Default language
DEFAULT_LANGUAGE = "zh-CN"


class I18n:
    """国际化翻译管理器 / Internationalization translation manager"""

    def __init__(self, translations_dir: Path = _TRANSLATIONS_DIR):
        self._translations_dir = translations_dir
        self._translations: dict[str, dict[str, str]] = {}
        self._loaded = False
        self._lock = threading.Lock()

    def _ensure_loaded(self):
        """延迟加载翻译文件 / Lazy-load translation files"""
        if self._loaded:
            return
        with self._lock:
            if self._loaded:
                return
            for lang in SUPPORTED_LANGUAGES:
                filepath = self._translations_dir / f"{lang}.yaml"
                if filepath.exists():
                    try:
                        with open(filepath, "r", encoding="utf-8") as f:
                            data = yaml.safe_load(f)
                            if data:
                                self._translations[lang] = self._flatten(data)
                                logger.debug(
                                    f"Loaded {len(self._translations[lang])} "
                                    f"translation keys for {lang}"
                                )
                    except Exception as e:
                        logger.error(f"Failed to load translations for {lang}: {e}")
                        self._translations[lang] = {}
                else:
                    logger.warning(f"Translation file not found: {filepath}")
                    self._translations[lang] = {}

        self._loaded = True

    @staticmethod
    def _flatten(data: dict, prefix: str = "") -> dict[str, str]:
        """将嵌套字典展平为点分隔的键 / Flatten nested dict to dot-separated keys

        Example:
            {"nav": {"dashboard": "仪表盘"}} -> {"nav.dashboard": "仪表盘"}
        """
        result = {}
        for key, value in data.items():
            full_key = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict):
                result.update(I18n._flatten(value, full_key))
            else:
                result[full_key] = str(value) if value is not None else ""
        return result

    def reload(self):
        """强制重新加载翻译文件 / Force reload translation files"""
        self._loaded = False
        self._translations.clear()
        self._ensure_loaded()

    def t(
        self,
        key: str,
        lang: str = DEFAULT_LANGUAGE,
        **kwargs: Any,
    ) -> str:
        """翻译指定键 / Translate a key

        Args:
            key: 翻译键（点分隔，如 "nav.dashboard"）
            lang: 目标语言代码
            **kwargs: 格式化参数（如 count=5）

        Returns:
            翻译后的文本，找不到时返回键本身
        """
        self._ensure_loaded()

        # 尝试获取目标语言翻译
        translations = self._translations.get(lang, {})
        text = translations.get(key)

        # 回退到默认语言
        if text is None and lang != DEFAULT_LANGUAGE:
            default_translations = self._translations.get(DEFAULT_LANGUAGE, {})
            text = default_translations.get(key)

        # 最终回退：返回键本身
        if text is None:
            return key

        # 应用格式化参数
        if kwargs:
            try:
                return text.format(**kwargs)
            except KeyError, IndexError:
                return text

        return text

    def get_all_translations(self, lang: str = DEFAULT_LANGUAGE) -> dict[str, str]:
        """获取某语言的所有翻译 / Get all translations for a language"""
        self._ensure_loaded()
        return self._translations.get(lang, {})


# 全局单例 / Global singleton
i18n = I18n()


def _(key: str, **kwargs: Any) -> str:
    """简化的翻译函数（用于 Jinja2 模板全局函数）

    注意：在模板中使用时，语言由模板上下文中的 `lang` 变量决定。
    此函数依赖请求上下文，通过 Jinja2 Environment 的 globals 注入时
    # 需要配合 `make_translation_func` 使用。

    Args:
        key: 翻译键
        **kwargs: 格式化参数

    Returns:
        翻译后的文本
    """
    return i18n.t(key, **kwargs)


def make_translation_func(lang: str = DEFAULT_LANGUAGE):
    """创建绑定到特定语言的翻译函数 / Create a language-bound translation function

    用于注入到 Jinja2 模板上下文中。

    Args:
        lang: 目标语言代码

    Returns:
        绑定了特定语言的翻译函数
    """

    def translate(key: str, **kwargs: Any) -> str:
        return i18n.t(key, lang=lang, **kwargs)

    return translate


def detect_language(user_prefs: dict | None = None) -> str:
    """检测应使用的语言 / Detect which language to use

    优先级：用户偏好 > 动态配置缓存 > Settings 环境变量 > 硬编码默认值

    Args:
        user_prefs: 用户偏好字典（包含 language 字段）

    Returns:
        语言代码（如 "zh-CN" 或 "en"）
    """
    if user_prefs and user_prefs.get("language"):
        lang = user_prefs["language"]
        if lang in SUPPORTED_LANGUAGES:
            return lang

    # 读取动态配置缓存（由异步请求填充，同步读取无 I/O）
    try:
        from backend.core.config import get_cached_config

        cached_value = get_cached_config("default_language")
        if cached_value is not None and cached_value in SUPPORTED_LANGUAGES:
            return cached_value
    except Exception as e:
        logger.debug(f"Failed to read cached default_language: {e}")

    # 回退到 Settings 环境变量
    try:
        from backend.core.config import get_settings

        settings = get_settings()
        if settings.default_language in SUPPORTED_LANGUAGES:
            return settings.default_language
    except Exception as e:
        logger.debug(f"Failed to read default_language from settings: {e}")

    return DEFAULT_LANGUAGE


# =============================================================================
# 免认证页面（登录页 / Setup Wizard）语言切换支持
# =============================================================================

# 语言切换 Cookie：免认证页面共享，独立于用户偏好动态配置
LANG_COOKIE = "preferred_language"


def resolve_language(request) -> str:
    """解析免认证页面的语言。

    优先级：?lang= 查询参数 > preferred_language Cookie > detect_language()。

    Args:
        request: FastAPI Request 对象

    Returns:
        语言代码（如 "zh-CN" 或 "en"）
    """
    qlang = request.query_params.get("lang")
    if qlang in SUPPORTED_LANGUAGES:
        return qlang
    clang = request.cookies.get(LANG_COOKIE)
    if clang in SUPPORTED_LANGUAGES:
        return clang
    return detect_language()


def set_language_cookie(response, lang: str) -> None:
    """在响应上写入语言切换 Cookie（跨免认证页面共享）。"""
    response.set_cookie(
        key=LANG_COOKIE,
        value=lang,
        httponly=True,
        samesite="lax",
        path="/",
    )
