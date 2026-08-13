from datetime import UTC, datetime

import pytest

from backend.webui.sse import _serialize_sse_value


def test_sse_serializes_nested_instants_as_utc_z():
    value = _serialize_sse_value(
        {
            "created_at": datetime(2026, 8, 12, 12, 0, tzinfo=UTC),
            "items": [datetime(2026, 8, 12, 13, 0, tzinfo=UTC)],
        }
    )
    assert value == {
        "created_at": "2026-08-12T12:00:00.000000Z",
        "items": ["2026-08-12T13:00:00.000000Z"],
    }


def test_sse_rejects_naive_instants():
    with pytest.raises(ValueError, match="aware"):
        _serialize_sse_value(datetime(2026, 8, 12, 12, 0))
