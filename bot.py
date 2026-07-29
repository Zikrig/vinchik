import asyncio
import logging

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

from config import settings
from database.session import async_session_maker, init_db
from handlers import setup_routers
from middlewares.db import DbSessionMiddleware
from services.reengage import reengage_loop
from services.moderation import moderation_loop
from services.settings_service import ensure_defaults

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def build_dispatcher() -> Dispatcher:
    try:
        storage = RedisStorage.from_url(settings.redis_url)
    except Exception:
        logger.warning("Redis unavailable, using MemoryStorage")
        storage = MemoryStorage()

    dp = Dispatcher(storage=storage)
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


async def run_polling(bot: Bot, dp: Dispatcher) -> None:
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Bot starting (long polling)")
    await dp.start_polling(bot)


async def run_webhook(bot: Bot, dp: Dispatcher) -> None:
    if not settings.webhook_base_url:
        raise RuntimeError("USE_WEBHOOK=true requires WEBHOOK_BASE_URL (https://your.domain)")

    dp.startup.register(on_startup_webhook)
    dp.shutdown.register(on_shutdown_webhook)

    app = web.Application()
    SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
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

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = build_dispatcher()
    asyncio.create_task(reengage_loop(bot))
    asyncio.create_task(moderation_loop())

    if settings.use_webhook:
        await run_webhook(bot, dp)
    else:
        await run_polling(bot, dp)


if __name__ == "__main__":
    asyncio.run(main())
