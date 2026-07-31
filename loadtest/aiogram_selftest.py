"""Verify main bot factory against the mock, including HTTP 429 handling."""

from __future__ import annotations

import asyncio
import os

import uvicorn
from aiogram.exceptions import TelegramRetryAfter

from loadtest.telegram_mock import app, state


TOKEN = "999999:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
BASE_URL = "http://127.0.0.1:18082"


async def main() -> None:
    await state.configure(
        {"enforce_limits": False, "base_latency_ms": 0, "jitter_ms": 0}
    )
    await state.reset()
    server = uvicorn.Server(
        uvicorn.Config(
            app, host="127.0.0.1", port=18082, log_level="error"
        )
    )
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.02)
        if not server.started:
            raise RuntimeError("mock server did not start")

        os.environ["BOT_TOKEN"] = TOKEN
        os.environ["TELEGRAM_API_BASE_URL"] = BASE_URL
        from services.bot_factory import build_bot

        bot = build_bot()
        try:
            me = await bot.get_me()
            assert me.id == 999999 and me.is_bot
            message = await bot.send_message(12345, "hello")
            assert message.chat.id == 12345
            member = await bot.get_chat_member("@loadtest", 12345)
            assert member.status == "member"

            await state.configure(
                {
                    "enforce_limits": True,
                    "global_messages_per_second": 0.01,
                    "global_burst": 1,
                    "private_messages_per_second": 100,
                    "private_burst": 100,
                }
            )
            await state.reset()
            await bot.send_message(10001, "first")
            try:
                await bot.send_message(10002, "second")
            except TelegramRetryAfter as exc:
                assert exc.retry_after >= 1
            else:
                raise AssertionError("expected TelegramRetryAfter")
        finally:
            await bot.session.close()
    finally:
        server.should_exit = True
        await task
    assert (await state.snapshot())["requests"]["rate_limited"] == 1
    print("aiogram mock integration self-test: OK")


if __name__ == "__main__":
    asyncio.run(main())
