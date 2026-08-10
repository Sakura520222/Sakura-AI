"""ReviewWorker 角色模型解析契约测试。"""

from pathlib import Path

from backend.workers import review_worker


def test_review_worker_does_not_read_legacy_model_or_endpoint_settings():
    source = Path(review_worker.__file__).read_text(encoding="utf-8")

    for legacy_access in (
        "settings.openai_model",
        "settings.summary_model",
        "settings.openai_api_base",
        "settings.openai_api_key",
        "settings.summary_api_base",
        "settings.summary_api_key",
    ):
        assert legacy_access not in source
