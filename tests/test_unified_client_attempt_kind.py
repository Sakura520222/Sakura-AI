"""验证 call_with_retry 把 attempt_kind 透传到 _retry_candidate.initial_attempt_kind。"""

import inspect

from backend.services.ai_reviewer.unified_client import UnifiedAIClient


def test_unified_call_with_retry_accepts_attempt_kind():
    """UnifiedAIClient.call_with_retry 签名包含 attempt_kind 参数。"""
    sig = inspect.signature(UnifiedAIClient.call_with_retry)
    assert "attempt_kind" in sig.parameters
    assert sig.parameters["attempt_kind"].default is None


def test_api_client_call_with_retry_accepts_attempt_kind():
    """AIApiClient.call_with_retry 签名包含 attempt_kind 参数。"""
    from backend.services.ai_reviewer.api_client import AIApiClient

    sig = inspect.signature(AIApiClient.call_with_retry)
    assert "attempt_kind" in sig.parameters
    assert sig.parameters["attempt_kind"].default is None
