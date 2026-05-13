import pytest

from reliability_lab.cache import ResponseCache


@pytest.mark.todo
@pytest.mark.xfail(reason="Students should improve semantic similarity and false-hit guardrails")
def test_semantic_cache_should_not_false_hit_different_intent() -> None:
    cache = ResponseCache(ttl_seconds=60, similarity_threshold=0.3)
    cache.set("Summarize refund policy for 2024 deadline", "Old refund policy")
    cached, _ = cache.get("Summarize refund policy for 2026 deadline")
    assert cached is None


def test_privacy_query_is_not_cached() -> None:
    cache = ResponseCache(ttl_seconds=60, similarity_threshold=0.3)
    cache.set("Give me the current account balance for user 123.", "Balance: $500")
    cached, score = cache.get("Give me the current account balance for user 123.")
    assert cached is None
    assert score == 0.0


def test_high_risk_metadata_is_not_cached() -> None:
    cache = ResponseCache(ttl_seconds=60, similarity_threshold=0.3)
    cache.set("Show account summary", "Private data", {"expected_risk": "privacy"})
    cached, score = cache.get("Show account summary")
    assert cached is None
    assert score == 0.0
