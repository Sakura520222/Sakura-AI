"""Configuration admission bounds both operands of relation candidate recall."""

import pytest
from pydantic import ValidationError

from backend.core import config
from tests.test_pr_issue_budget import changed_file
from tests.test_pr_recall_threshold_and_zero_lines import (
    assert_preserved,
)
from tests.test_pr_recall_threshold_and_zero_lines import (
    config_db as _config_db,
)
from tests.test_pr_recall_threshold_and_zero_lines import (
    sync_case as _sync_case,
)

config_db = _config_db
sync_case = _sync_case
MULTIPLIER = "issue_candidate_pool_multiplier"
PR_LIMIT = "semantic_issue_max_links"
RERANK_THRESHOLD = "rerank_score_threshold"


@pytest.mark.parametrize(
    "key,value",
    [
        (MULTIPLIER, 0),
        (MULTIPLIER, -1),
        (MULTIPLIER, 11),
        (MULTIPLIER, 10**9),
        (MULTIPLIER, True),
        (MULTIPLIER, False),
        (PR_LIMIT, 0),
        (PR_LIMIT, 201),
        (PR_LIMIT, 10**9),
        (PR_LIMIT, True),
        (PR_LIMIT, False),
        (RERANK_THRESHOLD, float("nan")),
        (RERANK_THRESHOLD, float("inf")),
        (RERANK_THRESHOLD, -0.1),
        (RERANK_THRESHOLD, 1.1),
        (RERANK_THRESHOLD, True),
        (RERANK_THRESHOLD, False),
    ],
)
def test_settings_reject_invalid_relation_recall_configuration(key, value):
    with pytest.raises(ValidationError):
        config.Settings(**{key: value})


@pytest.mark.parametrize(
    "key,minimum,maximum,default",
    [(MULTIPLIER, 1, 10, 3), (PR_LIMIT, 1, 200, 5), (RERANK_THRESHOLD, 0.0, 1.0, 0.6)],
)
def test_settings_and_dynamic_metadata_share_bounds_and_defaults(
    key, minimum, maximum, default
):
    assert config.DYNAMIC_CONFIG_RANGES[key] == (minimum, maximum)
    assert getattr(config.Settings(), key) == default
    for value in (minimum, maximum):
        assert getattr(config.Settings(**{key: str(value)}), key) == value


@pytest.mark.parametrize(
    "key,value", [(MULTIPLIER, "11"), (PR_LIMIT, "201"), (RERANK_THRESHOLD, "NaN")]
)
def test_environment_uses_the_same_relation_config_limits(monkeypatch, key, value):
    monkeypatch.setenv(key.upper(), value)
    with pytest.raises(ValidationError):
        config.Settings(_env_file=None)


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["stored", "assignment"])
@pytest.mark.parametrize("bad", [201, 10**9, True, False, 0, -1, None, 1.5])
async def test_invalid_pr_limit_cannot_bypass_the_pool_budget(
    config_db, sync_case, monkeypatch, source, bad
):
    if source == "stored":
        config_db.store(PR_LIMIT, str(bad))
        monkeypatch.setitem(config._dynamic_config_cache, PR_LIMIT, ("5", float("inf")))
    else:
        setattr(config_db.settings, PR_LIMIT, bad)
    case = sync_case([changed_file()], candidates=[])
    result = await case.service.synchronize(case.repo, "o", "r", 618)
    assert not result.succeeded and result.failure == "ValueError"
    assert_preserved(config_db, case)
    case.retriever.retrieve.assert_not_awaited()
    case.client.call_with_retry.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [1, 5, 200])
async def test_supported_pr_limit_reaches_retrieval_unchanged(
    config_db, sync_case, limit
):
    config_db.store(PR_LIMIT, str(limit))
    case = sync_case([changed_file()], candidates=[])
    result = await case.service.synchronize(case.repo, "o", "r", 618)
    assert result.succeeded
    assert case.retriever.retrieve.await_args.kwargs["top_k"] == limit
