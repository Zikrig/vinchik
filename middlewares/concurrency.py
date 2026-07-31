from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject


class UpdateConcurrencyMiddleware(BaseMiddleware):
    """Keep overload from turning into unbounded DB/Telegram contention."""

    def __init__(self, limit: int) -> None:
        self._semaphore = asyncio.Semaphore(max(1, limit))

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        async with self._semaphore:
            return await handler(event, data)
