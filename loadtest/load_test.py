"""Independent open-loop plateaus (default 5/10/15/25 RPS).

Delivery:
- webhook — POST updates to the bot webhook (HTTP latency = full handler when
  WEBHOOK_HANDLE_IN_BACKGROUND=false);
- polling — push updates into the Telegram mock queue; the bot long-polls
  getUpdates. Handler latency comes from /__performance__ stats.
"""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import time
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx


WEBHOOK_URL = os.getenv("LOADTEST_WEBHOOK_URL", "http://bot:8081/webhook/bot")
BOT_HEALTH_URL = os.getenv(
    "LOADTEST_BOT_HEALTH_URL", "http://bot:8081/__performance__/health"
)
BOT_STATS_URL = os.getenv(
    "LOADTEST_BOT_STATS_URL", "http://bot:8081/__performance__/stats"
)
BOT_RESET_URL = os.getenv(
    "LOADTEST_BOT_RESET_URL", "http://bot:8081/__performance__/reset"
)
MOCK_URL = os.getenv("LOADTEST_MOCK_URL", "http://telegram-mock:8082")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "loadtest-secret")
DELIVERY = os.getenv("LOADTEST_DELIVERY", "webhook").strip().lower()
VIEWER_BASE = int(os.getenv("LOADTEST_VIEWER_BASE", "9000000000"))
VIEWER_COUNT = int(os.getenv("LOADTEST_VIEWERS", "300"))
CANDIDATE_BASE = int(os.getenv("LOADTEST_CANDIDATE_BASE", "9100000000"))
REQUIRED_CHANNELS = int(os.getenv("LOADTEST_REQUIRED_CHANNELS", "3"))
CHANNEL_MEMBERSHIP_CACHE_SECONDS = int(
    os.getenv("CHANNEL_MEMBERSHIP_CACHE_SECONDS", "0")
)
LIKE_PERCENT = max(0, min(100, int(os.getenv("LOADTEST_LIKE_PERCENT", "20"))))
RESULTS_DIR = Path(os.getenv("LOADTEST_RESULTS_DIR", "/results"))
RUN_MODE = os.getenv("LOADTEST_RUN_MODE", "capacity").strip().lower()
RPS_LEVELS = tuple(
    float(item.strip())
    for item in os.getenv("LOADTEST_RPS_LEVELS", "5,10,15,25").split(",")
    if item.strip()
)
LEVEL_SECONDS = float(os.getenv("LOADTEST_LEVEL_SECONDS", "20"))
REQUEST_TIMEOUT_SECONDS = float(
    os.getenv("LOADTEST_REQUEST_TIMEOUT_SECONDS", "30")
)
IDLE_TIMEOUT_SECONDS = float(os.getenv("LOADTEST_IDLE_TIMEOUT_SECONDS", "45"))
BOT_DB_POOL_SIZE = int(os.getenv("LOADTEST_BOT_DB_POOL_SIZE", "5"))
BOT_DB_MAX_OVERFLOW = int(os.getenv("LOADTEST_BOT_DB_MAX_OVERFLOW", "5"))
BOT_UPDATE_CONCURRENCY_LIMIT = int(
    os.getenv("LOADTEST_BOT_UPDATE_CONCURRENCY_LIMIT", "24")
)


@dataclass(frozen=True)
class Stage:
    name: str
    seconds: float
    rps: float


STAGES = tuple(
    Stage(f"{rps:g}rps", LEVEL_SECONDS, rps)
    for rps in RPS_LEVELS
)


@dataclass
class RequestResult:
    status: int
    latency_ms: float
    error: str | None = None


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    index = min(len(values) - 1, int((len(values) - 1) * fraction))
    return round(values[index], 2)


def _stage_summary(
    stage: Stage, requests: list[RequestResult], elapsed: float
) -> dict[str, Any]:
    latencies = sorted(item.latency_ms for item in requests)
    statuses = Counter(str(item.status) for item in requests)
    failed = sum(
        1
        for item in requests
        if item.error is not None or not 200 <= item.status < 300
    )
    total = len(requests)
    return {
        "name": stage.name,
        "target_rps": stage.rps,
        "offered_rps": round(total / max(stage.seconds, 0.001), 2),
        "duration_seconds": round(elapsed, 2),
        "requests": total,
        "achieved_rps": round(total / max(elapsed, 0.001), 2),
        "statuses": dict(statuses),
        "failed": failed,
        "failed_percent": round(failed * 100 / max(total, 1), 2),
        "latency_ms": {
            "mean": round(statistics.fmean(latencies) if latencies else 0.0, 2),
            "p50": _percentile(latencies, 0.50),
            "p95": _percentile(latencies, 0.95),
            "p99": _percentile(latencies, 0.99),
            "max": round(max(latencies, default=0.0), 2),
        },
    }


class LoadGenerator:
    def __init__(self, client: httpx.AsyncClient, *, delivery: str) -> None:
        self.client = client
        self.delivery = delivery
        self.sequence = 0

    def _payload(self) -> dict[str, Any]:
        sequence = self.sequence
        self.sequence += 1
        index = sequence % VIEWER_COUNT
        user_id = VIEWER_BASE + index
        body: dict[str, Any] = {
            "message": {
                "message_id": 20_000_000 + sequence,
                "date": int(time.time()),
                "chat": {
                    "id": user_id,
                    "type": "private",
                    "first_name": "Load",
                    "username": f"load_viewer_{index}",
                },
                "from": {
                    "id": user_id,
                    "is_bot": False,
                    "first_name": "Load",
                    "username": f"load_viewer_{index}",
                    "language_code": "ru",
                },
                "text": "❤️" if sequence % 100 < LIKE_PERCENT else "👎",
            },
        }
        # Webhook path can carry update_id; polling lets the mock assign ids so
        # aiogram offsets stay aligned across stage resets.
        if self.delivery == "webhook":
            body["update_id"] = 10_000_000 + sequence
        return body

    async def request(self) -> RequestResult:
        started = time.perf_counter()
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
                if self.delivery == "polling":
                    response = await self.client.post(
                        f"{MOCK_URL}/__mock__/push",
                        json=self._payload(),
                    )
                else:
                    response = await self.client.post(
                        WEBHOOK_URL,
                        json=self._payload(),
                        headers={
                            "Content-Type": "application/json",
                            "X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET,
                        },
                    )
            return RequestResult(
                response.status_code,
                (time.perf_counter() - started) * 1000,
            )
        except Exception as exc:
            return RequestResult(
                0,
                (time.perf_counter() - started) * 1000,
                f"{type(exc).__name__}: {exc}",
            )

    async def run_stage(self, stage: Stage) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        started = loop.time()
        tasks: set[asyncio.Task[RequestResult]] = set()
        results: list[RequestResult] = []
        request_count = max(1, round(stage.seconds * stage.rps))
        for index in range(request_count):
            scheduled_at = started + index / stage.rps
            if scheduled_at > loop.time():
                await asyncio.sleep(scheduled_at - loop.time())
            tasks.add(asyncio.create_task(self.request()))
            completed = {task for task in tasks if task.done()}
            for task in completed:
                tasks.remove(task)
                results.append(task.result())
        if tasks:
            results.extend(await asyncio.gather(*tasks))
        return _stage_summary(stage, results, loop.time() - started)


def _apply_polling_handler_metrics(
    summary: dict[str, Any], application: dict[str, Any], elapsed: float
) -> None:
    """Replace push latency with handler latency from performance probes."""
    op = (application.get("operations") or {}).get("update.total") or {}
    calls = int(op.get("calls") or 0)
    errors = int(op.get("errors") or 0)
    latency = op.get("latency_ms") or {}
    summary["delivery"] = "polling"
    summary["push"] = {
        "requests": summary["requests"],
        "failed": summary["failed"],
        "failed_percent": summary["failed_percent"],
        "latency_ms": summary["latency_ms"],
    }
    summary["requests"] = calls
    summary["failed"] = errors
    summary["failed_percent"] = round(errors * 100 / max(calls, 1), 2)
    summary["achieved_rps"] = round(calls / max(elapsed, 0.001), 2)
    summary["latency_ms"] = {
        "mean": latency.get("p50", 0.0),
        "p50": latency.get("p50", 0.0),
        "p95": latency.get("p95", 0.0),
        "p99": latency.get("p99", 0.0),
        "max": latency.get("max", 0.0),
    }
    summary["statuses"] = {
        "handler_ok": max(0, calls - errors),
        "handler_error": errors,
    }


async def _wait_ready(client: httpx.AsyncClient) -> dict[str, Any]:
    deadline = time.monotonic() + 300
    last_error = ""
    while time.monotonic() < deadline:
        try:
            bot = await client.get(BOT_HEALTH_URL)
            mock = await client.get(f"{MOCK_URL}/__mock__/health")
            if bot.status_code == 200 and mock.status_code == 200:
                info = bot.json()
                bot_delivery = str(info.get("delivery") or "").lower()
                if bot_delivery and bot_delivery != DELIVERY:
                    raise RuntimeError(
                        f"Bot delivery={bot_delivery!r} but "
                        f"LOADTEST_DELIVERY={DELIVERY!r}. "
                        "Set LOADTEST_USE_WEBHOOK=true/false to match."
                    )
                return info
            last_error = f"bot={bot.status_code}, mock={mock.status_code}"
        except RuntimeError:
            raise
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        await asyncio.sleep(1)
    raise RuntimeError(f"Services did not become ready: {last_error}")


async def _reset_level(
    client: httpx.AsyncClient, *, enforce_limits: bool
) -> None:
    response = await client.post(
        f"{MOCK_URL}/__mock__/config",
        json={"enforce_limits": enforce_limits},
    )
    response.raise_for_status()
    for url in (f"{MOCK_URL}/__mock__/reset", BOT_RESET_URL):
        response = await client.post(url)
        response.raise_for_status()


async def _wait_idle(client: httpx.AsyncClient) -> dict[str, Any]:
    deadline = time.monotonic() + IDLE_TIMEOUT_SECONDS
    latest: dict[str, Any] = {}
    while time.monotonic() < deadline:
        latest = (await client.get(BOT_STATS_URL)).json()
        active = latest.get("updates", {}).get("active", 0) == 0
        pending = 0
        if DELIVERY == "polling":
            mock = (await client.get(f"{MOCK_URL}/__mock__/stats")).json()
            pending = int((mock.get("updates") or {}).get("pending") or 0)
        if active and pending == 0:
            return latest
        await asyncio.sleep(0.25)
    return latest


def _level_passed(summary: dict[str, Any]) -> bool:
    if summary.get("push") is not None:
        pushed = int(summary["push"]["requests"])
        handled = int(summary["requests"])
        if handled < pushed * 0.95:
            return False
    return (
        summary["offered_rps"] >= summary["target_rps"] * 0.99
        and summary["failed_percent"] < 1.0
        and summary["latency_ms"]["p95"] < 3000
    )


async def _run_mode(
    client: httpx.AsyncClient, generator: LoadGenerator, mode: str
) -> dict[str, Any]:
    enforce_limits = mode == "telegram-realistic"
    print(
        f"\n[{mode}/{DELIVERY}] levels="
        f"{','.join(f'{item:g}' for item in RPS_LEVELS)} RPS"
    )
    levels = []
    for stage in STAGES:
        await _reset_level(client, enforce_limits=enforce_limits)
        stage_started = time.perf_counter()
        summary = await generator.run_stage(stage)
        application = await _wait_idle(client)
        elapsed = time.perf_counter() - stage_started
        if DELIVERY == "polling":
            _apply_polling_handler_metrics(summary, application, elapsed)
        else:
            summary["delivery"] = "webhook"
        mock = (await client.get(f"{MOCK_URL}/__mock__/stats")).json()
        if enforce_limits and mock["requests"]["rate_limited"] > 0:
            verdict = "TELEGRAM_LIMITED"
        else:
            verdict = "PASS" if _level_passed(summary) else "FAIL"
        summary["verdict"] = verdict
        summary["application"] = application
        summary["mock"] = mock
        levels.append(summary)
        print(
            f"  {stage.rps:g} RPS: offered={summary['offered_rps']:.1f}, "
            f"completed={summary['achieved_rps']:.1f} RPS, "
            f"p95={summary['latency_ms']['p95']:.0f}ms, "
            f"failed={summary['failed_percent']:.2f}%, verdict={verdict}"
        )
    if any(level["verdict"] == "TELEGRAM_LIMITED" for level in levels):
        verdict = "TELEGRAM_LIMITED"
    else:
        verdict = (
            "PASS"
            if all(level["verdict"] == "PASS" for level in levels)
            else "FAIL"
        )
    return {
        "mode": mode,
        "delivery": DELIVERY,
        "verdict": verdict,
        "levels": levels,
    }


def _write_report(report: dict[str, Any]) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = RESULTS_DIR / f"loadtest-{stamp}.json"
    body = json.dumps(report, ensure_ascii=False, indent=2)
    path.write_text(body, encoding="utf-8")
    (RESULTS_DIR / "latest.json").write_text(body, encoding="utf-8")
    return path


async def main() -> int:
    if os.getenv("VINCHIK_LOADTEST", "") != "isolated":
        raise RuntimeError("VINCHIK_LOADTEST=isolated is required")
    if RUN_MODE not in {"both", "capacity", "telegram-realistic"}:
        raise RuntimeError("Invalid LOADTEST_RUN_MODE")
    if DELIVERY not in {"webhook", "polling"}:
        raise RuntimeError("LOADTEST_DELIVERY must be webhook or polling")
    async with httpx.AsyncClient(
        limits=httpx.Limits(max_connections=1200, max_keepalive_connections=300),
        timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS + 5),
    ) as client:
        bot_info = await _wait_ready(client)
        generator = LoadGenerator(client, delivery=DELIVERY)
        modes = (
            ["capacity", "telegram-realistic"]
            if RUN_MODE == "both"
            else [RUN_MODE]
        )
        runs = [
            await _run_mode(client, generator, mode)
            for mode in modes
        ]

    capacity = next(
        (run for run in runs if run["mode"] == "capacity"), None
    )
    realistic = next(
        (run for run in runs if run["mode"] == "telegram-realistic"), None
    )
    if capacity:
        level_results = ", ".join(
            f"{level['target_rps']:g} RPS={level['verdict']}"
            for level in capacity["levels"]
        )
        conclusion = (
            f"Application capacity ladder ({DELIVERY}): {level_results}."
        )
    else:
        conclusion = "Capacity mode was not executed."
    if realistic and realistic["verdict"] == "TELEGRAM_LIMITED":
        conclusion += " Telegram-like outbound limits were exceeded."

    report = {
        "created_at": datetime.now(UTC).isoformat(),
        "configuration": {
            "delivery": DELIVERY,
            "rps_levels": list(RPS_LEVELS),
            "level_seconds": LEVEL_SECONDS,
            "distance_range_km": [1, 480],
            "maximum_allowed_radius_km": 500,
            "db_pool_size": BOT_DB_POOL_SIZE,
            "db_max_overflow": BOT_DB_MAX_OVERFLOW,
            "update_concurrency_limit": BOT_UPDATE_CONCURRENCY_LIMIT,
            "viewers": VIEWER_COUNT,
            "candidate_base": CANDIDATE_BASE,
            "required_channels": REQUIRED_CHANNELS,
            "channel_membership_cache_seconds": CHANNEL_MEMBERSHIP_CACHE_SECONDS,
            "photos_enabled": True,
            "notify_eligible_candidates": True,
            "like_percent": LIKE_PERCENT,
            "bot": bot_info,
        },
        "runs": runs,
        "conclusion": conclusion,
    }
    path = _write_report(report)
    print(f"\nConclusion: {conclusion}\nReport: {path}")
    return 2 if capacity and capacity["verdict"] != "PASS" else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
