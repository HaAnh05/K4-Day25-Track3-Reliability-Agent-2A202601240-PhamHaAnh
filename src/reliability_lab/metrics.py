from __future__ import annotations

import csv
import json
from pathlib import Path
from statistics import median
from typing import Iterable

from pydantic import BaseModel, Field


class RunMetrics(BaseModel):
    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    fallback_successes: int = 0
    static_fallbacks: int = 0
    cache_hits: int = 0
    circuit_open_count: int = 0
    recovery_time_ms: float | None = None
    estimated_cost: float = 0.0
    estimated_cost_saved: float = 0.0
    latencies_ms: list[float] = Field(default_factory=list)
    scenarios: dict[str, str] = Field(default_factory=dict)

    @property
    def availability(self) -> float:
        return self.successful_requests / self.total_requests if self.total_requests else 0.0

    @property
    def error_rate(self) -> float:
        return self.failed_requests / self.total_requests if self.total_requests else 0.0

    @property
    def cache_hit_rate(self) -> float:
        return self.cache_hits / self.total_requests if self.total_requests else 0.0

    @property
    def fallback_success_rate(self) -> float:
        denom = self.fallback_successes + self.static_fallbacks
        return self.fallback_successes / denom if denom else 0.0

    def percentile(self, q: float) -> float:
        return percentile(self.latencies_ms, q)

    def check_slos(
        self,
        min_availability: float = 0.99,
        max_latency_p95_ms: float = 2500.0,
        min_fallback_success_rate: float = 0.95,
        min_cache_hit_rate: float = 0.10,
        max_recovery_time_ms: float = 5000.0,
    ) -> dict[str, dict[str, object]]:
        """Evaluate metrics against standard production SLO targets (Bonus feature)."""
        recovery_ok = (
            self.recovery_time_ms is None or self.recovery_time_ms <= max_recovery_time_ms
        )
        return {
            "availability": {
                "target": f">={min_availability*100:.1f}%",
                "actual": f"{self.availability*100:.2f}%",
                "met": self.availability >= min_availability,
            },
            "latency_p95": {
                "target": f"<{max_latency_p95_ms}ms",
                "actual": f"{self.percentile(95):.2f}ms",
                "met": self.percentile(95) < max_latency_p95_ms,
            },
            "fallback_success_rate": {
                "target": f">={min_fallback_success_rate*100:.1f}%",
                "actual": f"{self.fallback_success_rate*100:.2f}%",
                "met": self.fallback_success_rate >= min_fallback_success_rate,
            },
            "cache_hit_rate": {
                "target": f">={min_cache_hit_rate*100:.1f}%",
                "actual": f"{self.cache_hit_rate*100:.2f}%",
                "met": self.cache_hit_rate >= min_cache_hit_rate,
            },
            "recovery_time": {
                "target": f"<{max_recovery_time_ms}ms",
                "actual": f"{self.recovery_time_ms:.2f}ms" if self.recovery_time_ms is not None else "N/A",
                "met": recovery_ok,
            },
        }

    def to_report_dict(self) -> dict[str, object]:
        return {
            "total_requests": self.total_requests,
            "availability": round(self.availability, 4),
            "error_rate": round(self.error_rate, 4),
            "latency_p50_ms": round(self.percentile(50), 2),
            "latency_p95_ms": round(self.percentile(95), 2),
            "latency_p99_ms": round(self.percentile(99), 2),
            "fallback_success_rate": round(self.fallback_success_rate, 4),
            "cache_hit_rate": round(self.cache_hit_rate, 4),
            "circuit_open_count": self.circuit_open_count,
            "recovery_time_ms": self.recovery_time_ms,
            "estimated_cost": round(self.estimated_cost, 6),
            "estimated_cost_saved": round(self.estimated_cost_saved, 6),
            "scenarios": self.scenarios,
        }

    def write_json(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(self.to_report_dict(), indent=2, ensure_ascii=False))

    def write_csv(self, path: str | Path) -> None:
        """Export metrics to a single-row CSV file.

        1. Get report dict via self.to_report_dict()
        2. Flatten the "scenarios" dict: each scenario becomes "scenario_{name}" column
        3. Write a single-row CSV with csv.DictWriter
        4. Create parent directories if needed
        """
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        report = self.to_report_dict()
        # Flatten scenarios into top-level columns
        scenarios: dict[str, str] = report.pop("scenarios", {})  # type: ignore[arg-type]
        flat: dict[str, object] = dict(report)
        for name, result in scenarios.items():
            flat[f"scenario_{name}"] = result

        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(flat.keys()))
            writer.writeheader()
            writer.writerow(flat)


def percentile(values: Iterable[float], q: float) -> float:
    values_sorted = sorted(values)
    if not values_sorted:
        return 0.0
    if q == 50:
        return float(median(values_sorted))
    k = (len(values_sorted) - 1) * q / 100
    lower = int(k)
    upper = min(lower + 1, len(values_sorted) - 1)
    weight = k - lower
    return values_sorted[lower] * (1 - weight) + values_sorted[upper] * weight
