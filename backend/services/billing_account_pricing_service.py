"""Configured account identities and model discovery for the pricing editor."""

import hashlib
from collections import OrderedDict

from backend.core.ai_protocol import account_store
from backend.core.ai_protocol.account_probe import probe_account
from backend.core.config import get_dynamic_config
from backend.core.time_service import monotonic

# Discovery is a metadata request, never a completion or billable AI probe.
# Reuse results briefly during multi-version editing; invalidate on account edits.
_MODEL_CACHE = OrderedDict()
_MODEL_CACHE_SECONDS = 60
_MODEL_CACHE_LIMIT = 128


def safe_account(account):
    models = list(
        dict.fromkeys(
            model.strip()
            for model in account.models
            if isinstance(model, str) and model.strip()
        )
    )
    if account.default_model and account.default_model not in models:
        models.append(account.default_model)
    return {
        "id": account.id,
        "name": account.name or account.id,
        "provider_id": account.provider_id,
        "models": models,
        "default_model": account.default_model,
        "enabled": account.enabled,
    }


async def configured_pricing_sources():
    accounts = await account_store.list_accounts()
    auxiliary = []
    for feature in ("embedding", "rerank"):
        provider = await get_dynamic_config(f"{feature}_provider", fresh=True)
        model = await get_dynamic_config(f"{feature}_model", fresh=True)
        if (
            provider not in {None, "", "none", "local"}
            and isinstance(model, str)
            and model.strip()
        ):
            auxiliary.append(
                {
                    "feature": feature,
                    "provider_id": str(provider),
                    "model_id": model.strip(),
                }
            )
    return [safe_account(account) for account in accounts], auxiliary


async def get_pricing_account(account_id):
    if not isinstance(account_id, str) or not account_id or len(account_id) > 128:
        return None
    return await account_store.get_account(account_id)


async def pricing_account_models(account, *, refresh=False):
    saved = safe_account(account)["models"]
    fingerprint = hashlib.sha256(
        (
            account.protocol
            + "\0"
            + account.api_base
            + "\0"
            + account.api_key
            + "\0"
            + str(account.updated_at)
        ).encode()
    ).hexdigest()
    key = (account.id, fingerprint)
    cached = _MODEL_CACHE.get(key)
    now = monotonic()
    if not refresh and cached and now - cached[0] < _MODEL_CACHE_SECONDS:
        _MODEL_CACHE.move_to_end(key)
        return {**cached[1], "source": "cached"}
    result = await probe_account(
        provider_id=account.provider_id,
        protocol=account.protocol,
        api_base=account.api_base,
        api_key=account.api_key,
        model=account.default_model,
    )
    successful = isinstance(result, dict) and result.get("success") is True
    discovered = result.get("models", []) if successful else []
    models = list(
        dict.fromkeys(
            [
                *saved,
                *(
                    model.strip()
                    for model in discovered
                    if isinstance(model, str) and model.strip() and len(model) <= 255
                ),
            ]
        )
    )
    public = {
        "account_id": account.id,
        "models": models,
        "default_model": account.default_model,
        "source": "discovered" if successful else "saved",
        "discovery_failed": not successful,
    }
    if successful:
        _MODEL_CACHE[key] = (now, public)
        _MODEL_CACHE.move_to_end(key)
        while len(_MODEL_CACHE) > _MODEL_CACHE_LIMIT:
            _MODEL_CACHE.popitem(last=False)
    return public
