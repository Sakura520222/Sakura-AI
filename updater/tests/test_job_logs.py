from __future__ import annotations

from sakura_ai_updater.job_logs import JobLogBuffer, JobLogStore


def test_ring_buffer_marks_overflow_without_truncating_entries():
    buffer = JobLogBuffer(max_entries=2)
    buffer.append("first")
    buffer.append("second", level="warning")
    buffer.append("third" * 100)
    payload = buffer.to_dict("upd_1")
    assert payload["job_id"] == "upd_1"
    assert payload["truncated"] is True
    assert [entry["msg"] for entry in payload["logs"]] == ["second", "third" * 100]


def test_job_log_store_isolated_per_job():
    store = JobLogStore(max_entries=3)
    store.append("a", "one", step="checking")
    store.append("b", "two", step="pull")
    assert store.snapshot("a")["logs"][0]["msg"] == "one"
    assert store.snapshot("b")["logs"][0]["msg"] == "two"
    assert store.snapshot("missing")["logs"] == []

