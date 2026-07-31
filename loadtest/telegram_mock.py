"""Isolated Telegram Bot API mock with approximate flood limits."""

from __future__ import annotations

import asyncio
import json
import math
import os
import random
import time
from collections import Counter, deque
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class MockConfig:
    enforce_limits: bool = _env_bool("MOCK_ENFORCE_LIMITS", True)
    base_latency_ms: float = _env_float("MOCK_BASE_LATENCY_MS", 80.0)
    jitter_ms: float = _env_float("MOCK_JITTER_MS", 40.0)
    global_messages_per_second: float = _env_float(
        "MOCK_GLOBAL_MESSAGES_PER_SECOND", 30.0
    )
    global_burst: float = _env_float("MOCK_GLOBAL_BURST", 30.0)
    private_messages_per_second: float = _env_float(
        "MOCK_PRIVATE_MESSAGES_PER_SECOND", 1.0
    )
    private_burst: float = _env_float("MOCK_PRIVATE_BURST", 3.0)
    group_messages_per_minute: float = _env_float(
        "MOCK_GROUP_MESSAGES_PER_MINUTE", 20.0
    )
    group_burst: float = _env_float("MOCK_GROUP_BURST", 20.0)


class TokenBucket:
    def __init__(self, rate: float, capacity: float) -> None:
        self.rate = max(rate, 0.0001)
        self.capacity = max(capacity, 1.0)
        self.tokens = self.capacity
        self.updated_at = time.monotonic()

    def consume(self, amount: float = 1.0) -> tuple[bool, float]:
        now = time.monotonic()
        elapsed = max(0.0, now - self.updated_at)
        self.updated_at = now
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        if self.tokens >= amount:
            self.tokens -= amount
            return True, 0.0
        return False, max((amount - self.tokens) / self.rate, 0.001)


class MockState:
    def __init__(self) -> None:
        self.config = MockConfig()
        self.lock = asyncio.Lock()
        self.started_at = time.time()
        self.total = 0
        self.success = 0
        self.rate_limited = 0
        self.methods: Counter[str] = Counter()
        self.method_429: Counter[str] = Counter()
        self.latencies_ms: deque[float] = deque(maxlen=100_000)
        self.message_id = 1000
        self.global_bucket: TokenBucket | None = None
        self.chat_buckets: dict[str, TokenBucket] = {}
        self.update_condition = asyncio.Condition()
        self.pending_updates: list[dict[str, Any]] = []
        # Monotonic across resets so aiogram polling offsets stay valid.
        self.next_update_id = 1
        self.webhook_url = ""
        self.pushed_updates = 0
        self._reset_buckets()

    def _reset_buckets(self) -> None:
        cfg = self.config
        self.global_bucket = TokenBucket(
            cfg.global_messages_per_second, cfg.global_burst
        )
        self.chat_buckets.clear()

    async def reset(self) -> None:
        async with self.lock:
            self.started_at = time.time()
            self.total = 0
            self.success = 0
            self.rate_limited = 0
            self.methods.clear()
            self.method_429.clear()
            self.latencies_ms.clear()
            self.pushed_updates = 0
            self._reset_buckets()
        await self.clear_pending_updates()

    async def configure(self, values: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "enforce_limits": bool,
            "base_latency_ms": float,
            "jitter_ms": float,
            "global_messages_per_second": float,
            "global_burst": float,
            "private_messages_per_second": float,
            "private_burst": float,
            "group_messages_per_minute": float,
            "group_burst": float,
        }
        async with self.lock:
            for key, converter in allowed.items():
                if key not in values:
                    continue
                raw = values[key]
                if converter is bool:
                    value = (
                        raw
                        if isinstance(raw, bool)
                        else str(raw).lower() in {"1", "true", "yes", "on"}
                    )
                else:
                    value = max(0.0, float(raw))
                setattr(self.config, key, value)
            self._reset_buckets()
            return vars(self.config).copy()

    async def next_message_id(self) -> int:
        async with self.lock:
            self.message_id += 1
            return self.message_id

    async def check_message_limit(
        self, chat_id: str, amount: int
    ) -> tuple[bool, int]:
        if not self.config.enforce_limits:
            return True, 0
        async with self.lock:
            assert self.global_bucket is not None
            ok, retry = self.global_bucket.consume(amount)
            if not ok:
                return False, max(1, math.ceil(retry))

            is_group = chat_id.startswith("-") or chat_id.startswith("@")
            bucket = self.chat_buckets.get(chat_id)
            if bucket is None:
                if is_group:
                    bucket = TokenBucket(
                        self.config.group_messages_per_minute / 60.0,
                        self.config.group_burst,
                    )
                else:
                    bucket = TokenBucket(
                        self.config.private_messages_per_second,
                        self.config.private_burst,
                    )
                self.chat_buckets[chat_id] = bucket
            ok, retry = bucket.consume(amount)
            return ok, 0 if ok else max(1, math.ceil(retry))

    async def record(
        self,
        method: str,
        status: int,
        latency_ms: float,
        *,
        sample_latency: bool = True,
    ) -> None:
        async with self.lock:
            self.total += 1
            self.methods[method] += 1
            if sample_latency:
                self.latencies_ms.append(latency_ms)
            if status == 429:
                self.rate_limited += 1
                self.method_429[method] += 1
            else:
                self.success += 1

    async def push_update(self, update: dict[str, Any]) -> dict[str, Any]:
        async with self.update_condition:
            body = dict(update)
            if "update_id" in body:
                uid = _as_int(body.get("update_id"), self.next_update_id)
                body["update_id"] = uid
                self.next_update_id = max(self.next_update_id, uid + 1)
            else:
                body["update_id"] = self.next_update_id
                self.next_update_id += 1
            self.pending_updates.append(body)
            self.pushed_updates += 1
            self.update_condition.notify_all()
            return body

    async def clear_pending_updates(self) -> int:
        async with self.update_condition:
            cleared = len(self.pending_updates)
            self.pending_updates.clear()
            self.update_condition.notify_all()
            return cleared

    async def pending_count(self) -> int:
        async with self.update_condition:
            return len(self.pending_updates)

    async def get_updates(
        self,
        *,
        offset: int | None,
        limit: int,
        timeout: float,
    ) -> list[dict[str, Any]]:
        deadline = time.monotonic() + max(0.0, timeout)
        async with self.update_condition:
            if offset is not None and offset > 0:
                self.pending_updates = [
                    item
                    for item in self.pending_updates
                    if int(item["update_id"]) >= offset
                ]
            while True:
                if self.pending_updates:
                    return list(self.pending_updates[: max(1, limit)])
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return []
                try:
                    await asyncio.wait_for(
                        self.update_condition.wait(), timeout=remaining
                    )
                except TimeoutError:
                    return []

    async def snapshot(self) -> dict[str, Any]:
        pending = await self.pending_count()
        async with self.lock:
            samples = sorted(self.latencies_ms)

            def percentile(fraction: float) -> float:
                if not samples:
                    return 0.0
                index = min(
                    len(samples) - 1,
                    int((len(samples) - 1) * fraction),
                )
                return round(samples[index], 2)

            return {
                "uptime_seconds": round(time.time() - self.started_at, 2),
                "config": vars(self.config).copy(),
                "requests": {
                    "total": self.total,
                    "success": self.success,
                    "rate_limited": self.rate_limited,
                    "rate_limited_percent": round(
                        self.rate_limited * 100 / max(self.total, 1), 2
                    ),
                },
                "methods": dict(self.methods),
                "method_429": dict(self.method_429),
                "latency_ms": {
                    "p50": percentile(0.50),
                    "p95": percentile(0.95),
                    "p99": percentile(0.99),
                },
                "updates": {
                    "pending": pending,
                    "pushed": self.pushed_updates,
                    "next_update_id": self.next_update_id,
                    "webhook_url": self.webhook_url,
                },
            }


state = MockState()
app = FastAPI(title="Vinchik Telegram Bot API Mock", docs_url=None, redoc_url=None)

MESSAGE_METHODS = {
    "sendmessage",
    "sendphoto",
    "sendmediagroup",
    "sendvoice",
    "sendvideonote",
    "senddocument",
    "sendaudio",
    "sendanimation",
    "forwardmessage",
    "copymessage",
}


async def _request_data(request: Request) -> dict[str, Any]:
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        payload = await request.json()
        return payload if isinstance(payload, dict) else {}
    form = await request.form()
    return {key: value for key, value in form.multi_items()}


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


async def _message(data: dict[str, Any], *, text: str | None = None) -> dict[str, Any]:
    chat_id = _as_int(data.get("chat_id"), 1)
    message: dict[str, Any] = {
        "message_id": await state.next_message_id(),
        "date": int(time.time()),
        "chat": {
            "id": chat_id,
            "type": "private" if chat_id >= 0 else "supergroup",
        },
    }
    body = text if text is not None else data.get("text")
    if body is not None:
        message["text"] = str(body)
    if data.get("caption") is not None:
        message["caption"] = str(data["caption"])
    return message


def _media_count(data: dict[str, Any]) -> int:
    raw = data.get("media")
    if raw is None:
        return 1
    try:
        parsed = json.loads(str(raw))
        return max(1, len(parsed)) if isinstance(parsed, list) else 1
    except (TypeError, ValueError, json.JSONDecodeError):
        return 1


async def _result_for(method: str, data: dict[str, Any]) -> Any:
    normalized = method.lower()
    if normalized == "getme":
        return {
            "id": 999999,
            "is_bot": True,
            "first_name": "Vinchik Load Test",
            "username": "vinchik_load_test_bot",
        }
    if normalized == "getchatmember":
        user_id = _as_int(data.get("user_id"), 1)
        return {
            "status": "member",
            "user": {
                "id": user_id,
                "is_bot": False,
                "first_name": "Load User",
            },
        }
    if normalized == "getchatmembercount":
        return 1000
    if normalized == "getchatadministrators":
        return []
    if normalized == "getchat":
        chat_id = _as_int(data.get("chat_id"), -1000000000001)
        return {
            "id": chat_id,
            "type": "supergroup" if chat_id < 0 else "private",
            "title": "Mock chat",
            "accent_color_id": 0,
            "max_reaction_count": 11,
        }
    if normalized == "getfile":
        file_id = str(data.get("file_id") or "mock-file")
        return {
            "file_id": file_id,
            "file_unique_id": f"unique-{file_id}",
            "file_size": 128,
            "file_path": "mock/file.bin",
        }
    if normalized == "getwebhookinfo":
        return {
            "url": state.webhook_url,
            "has_custom_certificate": False,
            "pending_update_count": await state.pending_count(),
        }
    if normalized == "setwebhook":
        state.webhook_url = str(data.get("url") or "")
        if str(data.get("drop_pending_updates", "")).lower() in {
            "1",
            "true",
            "yes",
            "on",
        }:
            await state.clear_pending_updates()
        return True
    if normalized == "deletewebhook":
        state.webhook_url = ""
        if str(data.get("drop_pending_updates", "")).lower() in {
            "1",
            "true",
            "yes",
            "on",
        }:
            await state.clear_pending_updates()
        return True
    if normalized == "getupdates":
        # Handled in bot_api (long poll); keep a safe fallback.
        return []
    if normalized == "sendmediagroup":
        return [await _message(data) for _ in range(_media_count(data))]
    if normalized == "copymessage":
        return {"message_id": await state.next_message_id()}
    if normalized in MESSAGE_METHODS:
        return await _message(data)
    if normalized in {
        "editmessagetext",
        "editmessagecaption",
        "editmessagemedia",
        "editmessagereplymarkup",
    }:
        return await _message(data, text=str(data.get("text") or "edited"))
    return True


@app.get("/__mock__/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "service": "telegram-mock"}


@app.get("/__mock__/stats")
async def stats() -> dict[str, Any]:
    return await state.snapshot()


@app.post("/__mock__/reset")
async def reset() -> dict[str, Any]:
    await state.reset()
    return {"ok": True}


@app.post("/__mock__/config")
async def configure(request: Request) -> dict[str, Any]:
    payload = await request.json()
    values = payload if isinstance(payload, dict) else {}
    return {"ok": True, "config": await state.configure(values)}


@app.post("/__mock__/push")
async def push_update(request: Request) -> JSONResponse:
    payload = await request.json()
    if isinstance(payload, list):
        updates = [await state.push_update(item) for item in payload if isinstance(item, dict)]
        return JSONResponse({"ok": True, "count": len(updates), "updates": updates})
    if not isinstance(payload, dict):
        return JSONResponse(
            {"ok": False, "description": "JSON object or array required"},
            status_code=400,
        )
    update = await state.push_update(payload)
    return JSONResponse({"ok": True, "count": 1, "update": update})


@app.api_route("/file/bot{token}/{file_path:path}", methods=["GET", "HEAD"])
async def fake_file(token: str, file_path: str) -> JSONResponse:
    del token, file_path
    return JSONResponse(
        content={"ok": True, "result": "mock-file-content"},
        headers={"X-Telegram-Mock": "true"},
    )


_NO_FAKE_LATENCY = {
    "getupdates",
    "getme",
    "deletewebhook",
    "setwebhook",
    "getwebhookinfo",
}


@app.post("/bot{token}/{method}")
async def bot_api(token: str, method: str, request: Request) -> JSONResponse:
    del token
    started = time.perf_counter()
    data = await _request_data(request)
    normalized = method.lower()

    if normalized == "getupdates":
        offset_raw = data.get("offset")
        if offset_raw in (None, ""):
            offset = None
        else:
            parsed = _as_int(offset_raw, 0)
            offset = parsed if parsed > 0 else None
        limit = max(1, min(100, _as_int(data.get("limit"), 100)))
        timeout = max(0.0, float(data.get("timeout") or 0))
        result = await state.get_updates(
            offset=offset,
            limit=limit,
            timeout=timeout,
        )
        elapsed_ms = (time.perf_counter() - started) * 1000
        await state.record(method, 200, elapsed_ms, sample_latency=False)
        return JSONResponse(content={"ok": True, "result": result})

    if normalized not in _NO_FAKE_LATENCY:
        latency_ms = max(
            0.0,
            state.config.base_latency_ms
            + random.uniform(-state.config.jitter_ms, state.config.jitter_ms),
        )
        if latency_ms:
            await asyncio.sleep(latency_ms / 1000.0)

    if normalized in MESSAGE_METHODS:
        chat_id = str(data.get("chat_id") or "0")
        amount = _media_count(data) if normalized == "sendmediagroup" else 1
        allowed, retry_after = await state.check_message_limit(chat_id, amount)
        if not allowed:
            elapsed_ms = (time.perf_counter() - started) * 1000
            await state.record(method, 429, elapsed_ms)
            return JSONResponse(
                status_code=429,
                content={
                    "ok": False,
                    "error_code": 429,
                    "description": f"Too Many Requests: retry after {retry_after}",
                    "parameters": {"retry_after": retry_after},
                },
            )

    result = await _result_for(method, data)
    await state.record(
        method, 200, (time.perf_counter() - started) * 1000
    )
    return JSONResponse(content={"ok": True, "result": result})
