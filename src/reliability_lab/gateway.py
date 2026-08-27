from __future__ import annotations

import time
from dataclasses import dataclass

from reliability_lab.cache import ResponseCache, SharedRedisCache
from reliability_lab.circuit_breaker import CircuitBreaker, CircuitOpenError
from reliability_lab.providers import FakeLLMProvider, ProviderError, ProviderResponse


@dataclass(slots=True)
class GatewayResponse:
    text: str
    route: str
    provider: str | None
    cache_hit: bool
    latency_ms: float
    estimated_cost: float
    error: str | None = None


class ReliabilityGateway:
    """Routes requests through cache, circuit breakers, and fallback providers.

    Includes cost-budget tracking and cost-aware dynamic routing (Bonus feature).
    """

    def __init__(
        self,
        providers: list[FakeLLMProvider],
        breakers: dict[str, CircuitBreaker],
        cache: ResponseCache | SharedRedisCache | None = None,
        cost_budget: float | None = None,
    ):
        self.providers = providers
        self.breakers = breakers
        self.cache = cache
        self.cost_budget = cost_budget
        self.cumulative_cost: float = 0.0

    def complete(self, prompt: str) -> GatewayResponse:
        """Return a reliable response or a static fallback.

        Pipeline:
        1. CACHE CHECK — return immediately on cache hit (free & fast)
        2. COST BUDGET CHECK — if budget 100% exhausted, skip paid providers
        3. COST-AWARE PROVIDER CHAIN — sort by cost if >= 80% budget, try each via breaker
        4. STATIC FALLBACK — all providers failed or budget exhausted
        """
        start = time.perf_counter()

        # 1. CACHE CHECK
        if self.cache is not None:
            cached_text, score = self.cache.get(prompt)
            if cached_text is not None:
                return GatewayResponse(
                    text=cached_text,
                    route=f"cache_hit:{score:.2f}",
                    provider=None,
                    cache_hit=True,
                    latency_ms=0.0,
                    estimated_cost=0.0,
                )

        # 2. COST BUDGET CHECK
        if self.cost_budget is not None and self.cumulative_cost >= self.cost_budget:
            latency_ms = (time.perf_counter() - start) * 1000
            return GatewayResponse(
                text="The service is temporarily degraded due to budget limit. Please try again soon.",
                route="static_fallback",
                provider=None,
                cache_hit=False,
                latency_ms=latency_ms,
                estimated_cost=0.0,
                error="cost_budget_exceeded",
            )

        # 3. PROVIDER FALLBACK CHAIN
        # Cost-aware ordering: if cumulative cost >= 80% budget, prioritize cheaper models
        providers_to_try = list(self.providers)
        if self.cost_budget is not None and self.cumulative_cost >= self.cost_budget * 0.8:
            providers_to_try = sorted(providers_to_try, key=lambda p: p.cost_per_1k_tokens)

        last_error: str | None = None
        for i, provider in enumerate(providers_to_try):
            breaker = self.breakers[provider.name]
            try:
                response: ProviderResponse = breaker.call(provider.complete, prompt)
                self.cumulative_cost += response.estimated_cost

                # Store in cache on success
                if self.cache is not None:
                    self.cache.set(prompt, response.text, {"provider": provider.name})

                # Determine route: "primary" if original first provider, else "fallback"
                route = "primary" if provider.name == self.providers[0].name else "fallback"
                return GatewayResponse(
                    text=response.text,
                    route=route,
                    provider=response.provider,
                    cache_hit=False,
                    latency_ms=response.latency_ms,
                    estimated_cost=response.estimated_cost,
                )
            except (ProviderError, CircuitOpenError) as e:
                last_error = str(e)
                continue

        # 4. STATIC FALLBACK — all providers failed
        latency_ms = (time.perf_counter() - start) * 1000
        return GatewayResponse(
            text="The service is temporarily degraded. Please try again soon.",
            route="static_fallback",
            provider=None,
            cache_hit=False,
            latency_ms=latency_ms,
            estimated_cost=0.0,
            error=last_error,
        )
