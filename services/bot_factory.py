from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode

from config import settings


def build_bot() -> Bot:
    session = None
    if settings.telegram_api_base_url:
        session = AiohttpSession(
            api=TelegramAPIServer.from_base(
                settings.telegram_api_base_url.rstrip("/")
            )
        )
    return Bot(
        token=settings.bot_token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
