"""Temporary in-process performance probes used by the isolated load test."""

from __future__ import annotations

import time
from collections import defaultdict, deque
from functools import wraps
from typing import Any, Awaitable, Callable, ParamSpec, TypeVar

from config import settings


P = ParamSpec("P")
R = TypeVar("R")


class PerformanceRegistry:
    def __init__(self) -> None:
        self.started_at = time.time()
        self.samples: dict[str, deque[float]] = defaultdict(
            lambda: deque(maxlen=100_000)
        )
        self.errors: dict[str, int] = defaultdict(int)
        self.calls: dict[str, int] = defaultdict(int)
        self.active_updates = 0
        self.max_active_updates = 0

    def reset(self) -> None:
        self.started_at = time.time()
        self.samples.clear()
        self.errors.clear()
        self.calls.clear()
        self.max_active_updates = self.active_updates

    def record(self, name: str, elapsed_ms: float, *, error: bool = False) -> None:
        if not settings.performance_metrics_enabled:
            return
        self.calls[name] += 1
        self.samples[name].append(elapsed_ms)
        if error:
            self.errors[name] += 1

    def update_started(self) -> None:
        if not settings.performance_metrics_enabled:
            return
        self.active_updates += 1
        self.max_active_updates = max(
            self.max_active_updates, self.active_updates
        )

    def update_finished(self) -> None:
        if not settings.performance_metrics_enabled:
            return
        self.active_updates = max(0, self.active_updates - 1)

    @staticmethod
    def _percentile(values: list[float], fraction: float) -> float:
        if not values:
            return 0.0
        index = min(len(values) - 1, int((len(values) - 1) * fraction))
        return round(values[index], 2)

    def snapshot(self) -> dict[str, Any]:
        operations: dict[str, Any] = {}
        for name in sorted(self.calls):
            values = sorted(self.samples[name])
            operations[name] = {
                "calls": self.calls[name],
                "errors": self.errors[name],
                "latency_ms": {
                    "p50": self._percentile(values, 0.50),
                    "p95": self._percentile(values, 0.95),
                    "p99": self._percentile(values, 0.99),
                    "max": round(max(values, default=0.0), 2),
                },
            }
        return {
            "enabled": settings.performance_metrics_enabled,
            "uptime_seconds": round(time.time() - self.started_at, 2),
            "updates": {
                "active": self.active_updates,
                "max_active": self.max_active_updates,
            },
            "operations": operations,
        }


registry = PerformanceRegistry()


def timed(name: str) -> Callable[
    [Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]
]:
    def decorator(
        function: Callable[P, Awaitable[R]],
    ) -> Callable[P, Awaitable[R]]:
        @wraps(function)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            if not settings.performance_metrics_enabled:
                return await function(*args, **kwargs)
            started = time.perf_counter()
            failed = False
            try:
                return await function(*args, **kwargs)
            except Exception:
                failed = True
                raise
            finally:
                registry.record(
                    name,
                    (time.perf_counter() - started) * 1000,
                    error=failed,
                )

        return wrapper

    return decorator
