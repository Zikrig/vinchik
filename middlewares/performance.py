from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from services.performance import registry


class PerformanceMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        started = time.perf_counter()
        failed = False
        registry.update_started()
        try:
            return await handler(event, data)
        except Exception:
            failed = True
            raise
        finally:
            registry.update_finished()
            registry.record(
                "update.total",
                (time.perf_counter() - started) * 1000,
                error=failed,
            )
