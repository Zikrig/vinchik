import asyncio
import logging

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

from config import settings
from database.session import async_session_maker, init_db
from handlers import setup_routers
from middlewares.concurrency import UpdateConcurrencyMiddleware
from middlewares.db import DbSessionMiddleware
from middlewares.performance import PerformanceMiddleware
from services.bot_factory import build_bot
from services.reengage import reengage_loop
from services.moderation import moderation_loop
from services.performance import registry as performance_registry
from services.settings_service import ensure_defaults

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
if settings.performance_metrics_enabled:
    # Per-update tracebacks can themselves saturate Docker logging during a
    # deliberate overload; HTTP/error counters remain available in the report.
    logging.getLogger("aiogram.event").setLevel(logging.CRITICAL)
    logging.getLogger("aiohttp.server").setLevel(logging.CRITICAL)

# asyncio keeps only weak references to tasks — hold them or they may be GC'd.
_background_tasks: set[asyncio.Task] = set()


def _spawn_background(coro, name: str) -> None:
    task = asyncio.create_task(coro, name=name)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    task.add_done_callback(lambda t: _log_background_exit(name, t))


def _log_background_exit(name: str, task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("background task %s stopped: %r", name, exc)


async def build_dispatcher() -> Dispatcher:
    storage = RedisStorage.from_url(settings.redis_url)
    try:
        await storage.redis.ping()
    except Exception as exc:
        await storage.close()
        raise RuntimeError("Redis unavailable; FSM storage cannot start safely") from exc

    dp = Dispatcher(storage=storage)
    if settings.performance_metrics_enabled:
        dp.update.outer_middleware(PerformanceMiddleware())
    dp.update.outer_middleware(
        UpdateConcurrencyMiddleware(settings.update_concurrency_limit)
    )
    dp.update.middleware(DbSessionMiddleware())
    dp.include_router(setup_routers())
    return dp


async def on_startup_webhook(bot: Bot) -> None:
    url = f"{settings.webhook_base_url.rstrip('/')}{settings.webhook_path}"
    await bot.set_webhook(
        url=url,
        secret_token=settings.webhook_secret or None,
        drop_pending_updates=True,
    )
    logger.info("Webhook set: %s", url)


async def on_shutdown_webhook(bot: Bot) -> None:
    await bot.delete_webhook(drop_pending_updates=False)
    logger.info("Webhook removed")


def _performance_routes(app: web.Application) -> None:
    if not settings.performance_metrics_enabled:
        return

    async def performance_health(request: web.Request) -> web.Response:
        del request
        return web.json_response(
            {
                "ok": True,
                "metrics_enabled": settings.performance_metrics_enabled,
                "delivery": "webhook" if settings.use_webhook else "polling",
                "handle_in_background": settings.webhook_handle_in_background,
                "telegram_api": (
                    "custom" if settings.telegram_api_base_url else "official"
                ),
            }
        )

    async def performance_stats(request: web.Request) -> web.Response:
        del request
        return web.json_response(performance_registry.snapshot())

    async def performance_reset(request: web.Request) -> web.Response:
        del request
        performance_registry.reset()
        return web.json_response({"ok": True})

    app.router.add_get("/__performance__/health", performance_health)
    app.router.add_get("/__performance__/stats", performance_stats)
    app.router.add_post("/__performance__/reset", performance_reset)


async def _start_metrics_site() -> web.AppRunner | None:
    """HTTP probes for loadtest; same host/port as webhook listener."""
    if not settings.performance_metrics_enabled:
        return None
    app = web.Application()
    _performance_routes(app)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, settings.webhook_host, settings.webhook_port)
    await site.start()
    logger.info(
        "Performance metrics on %s:%s",
        settings.webhook_host,
        settings.webhook_port,
    )
    return runner


async def run_polling(bot: Bot, dp: Dispatcher) -> None:
    await bot.delete_webhook(drop_pending_updates=True)
    runner = await _start_metrics_site()
    logger.info("Bot starting (long polling)")
    try:
        await dp.start_polling(bot)
    finally:
        if runner is not None:
            await runner.cleanup()


async def run_webhook(bot: Bot, dp: Dispatcher) -> None:
    if not settings.webhook_base_url:
        raise RuntimeError("USE_WEBHOOK=true requires WEBHOOK_BASE_URL (https://your.domain)")

    dp.startup.register(on_startup_webhook)
    dp.shutdown.register(on_shutdown_webhook)

    app = web.Application()
    _performance_routes(app)
    SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        handle_in_background=settings.webhook_handle_in_background,
        secret_token=settings.webhook_secret or None,
    ).register(app, path=settings.webhook_path)
    setup_application(app, dp, bot=bot)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, settings.webhook_host, settings.webhook_port)
    await site.start()
    logger.info(
        "Bot webhook listening on %s:%s%s",
        settings.webhook_host,
        settings.webhook_port,
        settings.webhook_path,
    )
    await asyncio.Event().wait()


async def main() -> None:
    await init_db()
    async with async_session_maker() as session:
        await ensure_defaults(session)
        from services.settlements_import import ensure_settlements_loaded

        await ensure_settlements_loaded(session)

    bot = build_bot()
    dp = await build_dispatcher()
    _spawn_background(reengage_loop(bot), "reengage")
    _spawn_background(moderation_loop(), "moderation")

    if settings.use_webhook:
        await run_webhook(bot, dp)
    else:
        await run_polling(bot, dp)


if __name__ == "__main__":
    asyncio.run(main())
