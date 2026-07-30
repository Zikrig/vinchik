from urllib.parse import quote

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from config import settings
from locales import t


def language_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=t("lang_tg", "tg"), callback_data="lang:tg"),
                InlineKeyboardButton(text=t("lang_ru", "ru"), callback_data="lang:ru"),
            ]
        ]
    )


def keep_kb(lang: str, callback: str = "keep") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t("keep_current", lang), callback_data=callback)]]
    )


def gender_kb(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=t("gender_male", lang), callback_data="gender:male"),
                InlineKeyboardButton(text=t("gender_female", lang), callback_data="gender:female"),
            ]
        ]
    )


def looking_kb(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t("looking_female", lang), callback_data="looking:female")],
            [InlineKeyboardButton(text=t("looking_male", lang), callback_data="looking:male")],
            [InlineKeyboardButton(text=t("looking_any", lang), callback_data="looking:any")],
        ]
    )


def location_kb(lang: str, has_current: bool) -> tuple[ReplyKeyboardMarkup, InlineKeyboardMarkup]:
    reply = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t("send_location", lang), request_location=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text=t("btn_location_text", lang), callback_data="loc:text")]
    ]
    if has_current:
        rows.append([InlineKeyboardButton(text=t("keep_current", lang), callback_data="keep:location")])
    return reply, InlineKeyboardMarkup(inline_keyboard=rows)


def location_confirm_kb(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=t("location_yes", lang), callback_data="loc:yes"),
                InlineKeyboardButton(text=t("location_no", lang), callback_data="loc:no"),
            ]
        ]
    )


def location_pick_kb(lang: str, choices: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    """choices: (settlement_id, button_label)."""
    rows: list[list[InlineKeyboardButton]] = []
    for sid, label in choices:
        text = (label or "?")[:64]
        rows.append(
            [InlineKeyboardButton(text=text, callback_data=f"loc:pick:{sid}")]
        )
    rows.append(
        [InlineKeyboardButton(text=t("location_pick_none", lang), callback_data="loc:no")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def about_kb(lang: str, has_current: bool) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=t("skip_text", lang), callback_data="about:skip")]]
    if has_current:
        rows.append([InlineKeyboardButton(text=t("keep_current", lang), callback_data="keep:about")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def photo_keep_kb(lang: str) -> InlineKeyboardMarkup:
    return keep_kb(lang, "keep:photo")


def photo_step_kb(lang: str, count: int, *, can_keep: bool = False) -> InlineKeyboardMarkup:
    """Collect 0–3 profile photos: Done/Skip + optional keep."""
    rows: list[list[InlineKeyboardButton]] = []
    if can_keep and count == 0:
        rows.append(
            [InlineKeyboardButton(text=t("keep_current", lang), callback_data="keep:photo")]
        )
    done_key = "btn_photo_done" if count > 0 else "btn_photo_skip"
    rows.append(
        [InlineKeyboardButton(text=t(done_key, lang), callback_data="photo:done")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def my_profile_kb(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t("btn_refill", lang), callback_data="profile:refill")],
            [InlineKeyboardButton(text=t("btn_edit_photo", lang), callback_data="profile:edit_photo")],
            [InlineKeyboardButton(text=t("btn_edit_text", lang), callback_data="profile:edit_text")],
            [InlineKeyboardButton(text=t("btn_browse", lang), callback_data="browse:start")],
            [InlineKeyboardButton(text=t("btn_main_menu", lang), callback_data="menu:root")],
        ]
    )


def browse_reply_kb(lang: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text=t("btn_like", lang)),
                KeyboardButton(text=t("btn_message", lang)),
                KeyboardButton(text=t("btn_dislike", lang)),
            ],
            [
                KeyboardButton(text=t("btn_report", lang)),
                KeyboardButton(text=t("btn_premium", lang)),
                KeyboardButton(text=t("btn_sleep", lang)),
            ],
        ],
        resize_keyboard=True,
    )


def message_compose_kb(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t("btn_cancel", lang), callback_data="msg:cancel")]
        ]
    )


def premium_cta_kb(lang: str, *, with_main_menu: bool = False) -> InlineKeyboardMarkup:
    """Promo entry to premium (after registration / like limit)."""
    rows = [
        [InlineKeyboardButton(text=t("menu_premium", lang), callback_data="menu:premium")]
    ]
    if with_main_menu:
        rows.append(
            [InlineKeyboardButton(text=t("btn_main_menu", lang), callback_data="menu:root")]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def empty_feed_kb(lang: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=t("menu_browse", lang), callback_data="browse:start")]]
    share = _share_bot_button(lang)
    if share is not None:
        rows.append([share])
    rows.append([InlineKeyboardButton(text=t("back", lang), callback_data="menu:root")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _share_bot_button(lang: str) -> InlineKeyboardButton | None:
    username = (settings.bot_username or "").lstrip("@").strip()
    if not username:
        return None
    bot_url = f"https://t.me/{username}"
    share_url = (
        "https://t.me/share/url"
        f"?url={quote(bot_url, safe='')}"
        f"&text={quote(t('share_bot_text', lang), safe='')}"
    )
    return InlineKeyboardButton(text=t("menu_share", lang), url=share_url)


def main_menu_kb(lang: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=t("menu_browse", lang), callback_data="browse:start")],
        [InlineKeyboardButton(text=t("menu_my", lang), callback_data="menu:my")],
        [InlineKeyboardButton(text=t("menu_premium", lang), callback_data="menu:premium")],
        [InlineKeyboardButton(text=t("menu_settings", lang), callback_data="menu:settings")],
        [InlineKeyboardButton(text=t("menu_stop", lang), callback_data="menu:stop")],
    ]
    share = _share_bot_button(lang)
    if share is not None:
        rows.append([share])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def settings_kb(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t("settings_language", lang), callback_data="settings:lang")],
            [
                InlineKeyboardButton(
                    text=t("settings_channels", lang), callback_data="settings:channels"
                )
            ],
            [InlineKeyboardButton(text=t("back", lang), callback_data="menu:root")],
        ]
    )


def profile_enable_kb(lang: str) -> InlineKeyboardMarkup:
    """Offered when the feed is requested while the profile is switched off."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("btn_profile_enable", lang), callback_data="profile:enable"
                )
            ],
            [InlineKeyboardButton(text=t("btn_main_menu", lang), callback_data="menu:root")],
        ]
    )


def stop_confirm_kb(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t("stop_yes", lang), callback_data="stop:yes")],
            [InlineKeyboardButton(text=t("stop_no", lang), callback_data="menu:root")],
        ]
    )


def channels_kb(lang: str, channels) -> InlineKeyboardMarkup:
    from services.channels import channel_button_url

    rows = []
    for ch in channels:
        link = channel_button_url(ch)
        title = ch.title or ch.channel_id
        if link:
            rows.append(
                [InlineKeyboardButton(text=f"{t('btn_subscribe', lang)}: {title}", url=link)]
            )
    rows.append(
        [
            InlineKeyboardButton(
                text=t("btn_subscribed", lang), callback_data="channels:check"
            ),
            InlineKeyboardButton(
                text=t("menu_premium", lang), callback_data="menu:premium"
            ),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)
