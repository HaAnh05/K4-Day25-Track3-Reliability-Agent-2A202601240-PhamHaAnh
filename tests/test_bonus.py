"""Bonus tests for extra credit features:
1. Cost-aware routing and budget limit in Gateway
2. Redis graceful degradation to in-memory cache
3. Concurrency support in chaos simulation
4. Automated SLO evaluation engine
"""
from __future__ import annotations

import pytest

from reliability_lab.cache import ResponseCache, SharedRedisCache
from reliability_lab.circuit_breaker import CircuitBreaker
from reliability_lab.config import (
    CacheConfig,
    CircuitBreakerConfig,
    LabConfig,
    LoadTestConfig,
    ProviderConfig,
    ScenarioConfig,
)
from reliability_lab.chaos import run_scenario
from reliability_lab.gateway import ReliabilityGateway
from reliability_lab.metrics import RunMetrics
from reliability_lab.providers import FakeLLMProvider


def test_cost_budget_routing_80_percent_degrades_to_cheaper() -> None:
    """When cumulative cost reaches 80% budget, gateway routes to cheaper provider first."""
    expensive = FakeLLMProvider("expensive", fail_rate=0.0, base_latency_ms=1, cost_per_1k_tokens=0.10)
    cheap = FakeLLMProvider("cheap", fail_rate=0.0, base_latency_ms=1, cost_per_1k_tokens=0.01)

    breakers = {
        "expensive": CircuitBreaker("expensive", failure_threshold=3, reset_timeout_seconds=10),
        "cheap": CircuitBreaker("cheap", failure_threshold=3, reset_timeout_seconds=10),
    }

    # Set small budget of $0.005, initial cost is 0
    gw = ReliabilityGateway([expensive, cheap], breakers, cost_budget=0.005)

    # First request: uses expensive (primary)
    res1 = gw.complete("first request")
    assert res1.provider == "expensive"

    # Manually set cumulative_cost to 85% of budget ($0.0045)
    gw.cumulative_cost = 0.0045

    # Next request: should automatically prioritize cheaper provider
    res2 = gw.complete("second request after budget 80%")
    assert res2.provider == "cheap"
    assert res2.route == "fallback"


def test_cost_budget_100_percent_cuts_off_paid_providers() -> None:
    """When budget is 100% exhausted, gateway cuts off paid providers and falls back safely."""
    provider = FakeLLMProvider("primary", fail_rate=0.0, base_latency_ms=1, cost_per_1k_tokens=0.01)
    breaker = CircuitBreaker("primary", failure_threshold=3, reset_timeout_seconds=10)
    cache = ResponseCache(60, 0.90)

    gw = ReliabilityGateway([provider], {"primary": breaker}, cache=cache, cost_budget=0.01)

    # Prime the cache
    cache.set("cached query about astronomy", "astronomy response")

    # Set cumulative cost at 100% budget
    gw.cumulative_cost = 0.01

    # Cached query should still work (free)
    res_cache = gw.complete("cached query about astronomy")
    assert res_cache.cache_hit
    assert res_cache.text == "astronomy response"

    # Non-cached query should fail-fast with static fallback
    res_uncached = gw.complete("completely different unrelated prompt")
    assert res_uncached.route == "static_fallback"
    assert res_uncached.error == "cost_budget_exceeded"


def test_redis_graceful_degradation_to_memory() -> None:
    """SharedRedisCache falls back gracefully to in-memory cache if Redis is down."""
    # Point to an unreachable Redis port
    cache = SharedRedisCache(
        redis_url="redis://localhost:59999/0",
        ttl_seconds=60,
        similarity_threshold=0.5,
        prefix="rl:bad:",
    )
    assert not cache.ping()

    # set and get should still work seamlessly without throwing errors
    cache.set("hello world", "fallback response")
    cached, score = cache.get("hello world")
    assert cached == "fallback response"
    assert score == 1.0


def test_concurrent_chaos_simulation() -> None:
    """Scenario runner executes cleanly with multiple worker threads."""
    config = LabConfig(
        providers=[
            ProviderConfig(name="primary", fail_rate=0.0, base_latency_ms=5, cost_per_1k_tokens=0.01),
        ],
        circuit_breaker=CircuitBreakerConfig(failure_threshold=3, reset_timeout_seconds=2.0, success_threshold=1),
        cache=CacheConfig(enabled=False, ttl_seconds=60, similarity_threshold=0.5),
        load_test=LoadTestConfig(requests=20),
    )
    queries = ["query A", "query B", "query C"]
    scenario = ScenarioConfig(name="concurrent_test", description="multi-threaded load")

    metrics = run_scenario(config, queries, scenario, concurrency=4)
    assert metrics.total_requests == 20
    assert metrics.successful_requests == 20
    assert metrics.availability == 1.0


def test_slo_evaluation_compliance() -> None:
    """SLO evaluation engine accurately validates system compliance."""
    m = RunMetrics(
        total_requests=100,
        successful_requests=99,
        failed_requests=1,
        fallback_successes=95,
        static_fallbacks=5,
        cache_hits=20,
        latencies_ms=[100.0, 200.0, 300.0],
        recovery_time_ms=1500.0,
    )
    slos = m.check_slos()
    assert slos["availability"]["met"] is True
    assert slos["latency_p95"]["met"] is True
    assert slos["fallback_success_rate"]["met"] is True
    assert slos["cache_hit_rate"]["met"] is True
    assert slos["recovery_time"]["met"] is True
