from reliability_lab.cache import ResponseCache
from reliability_lab.circuit_breaker import CircuitBreaker
from reliability_lab.gateway import ReliabilityGateway
from reliability_lab.providers import FakeLLMProvider


def test_gateway_returns_response_with_route_reason() -> None:
    provider = FakeLLMProvider("primary", fail_rate=0.0, base_latency_ms=1, cost_per_1k_tokens=0.001)
    breaker = CircuitBreaker("primary", failure_threshold=2, reset_timeout_seconds=1)
    gateway = ReliabilityGateway([provider], {"primary": breaker}, ResponseCache(60, 0.5))
    result = gateway.complete("hello world")
    assert result.text
    assert result.route == "primary:primary"
    assert result.latency_ms > 0


def test_gateway_uses_backup_when_primary_circuit_opens() -> None:
    primary = FakeLLMProvider("primary", fail_rate=1.0, base_latency_ms=1, cost_per_1k_tokens=0.001)
    backup = FakeLLMProvider("backup", fail_rate=0.0, base_latency_ms=1, cost_per_1k_tokens=0.001)
    breakers = {
        "primary": CircuitBreaker("primary", failure_threshold=1, reset_timeout_seconds=60),
        "backup": CircuitBreaker("backup", failure_threshold=1, reset_timeout_seconds=60),
    }
    gateway = ReliabilityGateway([primary, backup], breakers)

    first = gateway.complete("hello world")
    second = gateway.complete("hello again")

    assert first.route == "fallback:backup"
    assert second.route == "fallback:backup"
    assert breakers["primary"].state.value == "open"
    assert any(t["to"] == "open" for t in breakers["primary"].transition_log)
