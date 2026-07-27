from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
from aiogram import Bot
from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeSerializer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from config import settings
from database.models import RequiredChannel, User
from database.session import async_session_maker
from services.admin_tools import (
    DUSHANBE_CITY,
    DUSHANBE_LAT,
    DUSHANBE_LON,
    clear_test_users,
    count_test_users,
    create_test_users,
    get_user_geo,
    set_user_geo,
)
from services.media import local_photo_path
from services.premium import (
    approve_order,
    list_pending_orders,
    list_premium_users,
    reject_order,
)
from services.reports import list_blocked_users, unban_user
from services.settings_service import (
    ensure_defaults,
    get_daily_like_limit,
    get_max_distance_km,
    get_payment_info,
    is_registration_only,
    set_setting,
)

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
TEMPLATES.env.globals["url"] = settings.abs_path
signer = URLSafeSerializer(settings.web_secret_key, salt="vinchik-admin")


async def get_db():
    async with async_session_maker() as session:
        yield session


def is_logged_in(request: Request) -> bool:
    token = request.cookies.get("admin_session")
    if not token:
        return False
    try:
        data = signer.loads(token)
        return data.get("ok") is True
    except BadSignature:
        return False


def require_auth(request: Request):
    if not is_logged_in(request):
        return RedirectResponse(settings.abs_path("/login"), status_code=303)
    return None


def create_app() -> FastAPI:
    app = FastAPI(title="Vinchik Admin", root_path=settings.web_root_path or "")

    @app.on_event("startup")
    async def startup() -> None:
        # схему создаёт bot (init_db); web только сиды, с ретраями
        last_exc: Exception | None = None
        for _ in range(30):
            try:
                async with async_session_maker() as session:
                    await ensure_defaults(session)
                last_exc = None
                break
            except Exception as exc:  # noqa: BLE001 — ждём готовности схемы
                last_exc = exc
                await asyncio.sleep(1)
        if last_exc is not None:
            raise last_exc

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        if is_logged_in(request):
            return RedirectResponse(settings.abs_path("/"), status_code=303)
        return TEMPLATES.TemplateResponse(
            request, "login.html", {"error": None}
        )

    @app.post("/login")
    async def login(request: Request, password: str = Form(...)):
        if password != settings.admin_web_password:
            return TEMPLATES.TemplateResponse(
                request,
                "login.html",
                {"error": "Неверный пароль"},
                status_code=401,
            )
        resp = RedirectResponse(settings.abs_path("/"), status_code=303)
        resp.set_cookie(
            "admin_session",
            signer.dumps({"ok": True}),
            httponly=True,
            samesite="lax",
            path=settings.web_root_path or "/",
        )
        return resp

    @app.get("/logout")
    async def logout():
        resp = RedirectResponse(settings.abs_path("/login"), status_code=303)
        resp.delete_cookie("admin_session", path=settings.web_root_path or "/")
        return resp

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request, session: AsyncSession = Depends(get_db)):
        if (redir := require_auth(request)) is not None:
            return redir
        pending = await list_pending_orders(session)
        premiums = await list_premium_users(session)
        channels = (
            await session.execute(select(RequiredChannel).order_by(RequiredChannel.id))
        ).scalars().all()
        pay = await get_payment_info(session)
        blocked = await list_blocked_users(session)
        admin_ids = sorted(settings.admin_id_set)
        admin_geo = {}
        for aid in admin_ids:
            admin_geo[aid] = await get_user_geo(session, aid)
        ctx = {
            "limit": await get_daily_like_limit(session),
            "distance": await get_max_distance_km(session),
            "registration_only": await is_registration_only(session),
            "manager": pay["manager"],
            "payment_card": pay["card"],
            "payment_check_time": pay["check_time"],
            "pending": pending,
            "premiums": premiums,
            "channels": channels,
            "blocked": blocked,
            "admin_ids": admin_ids,
            "admin_geo": admin_geo,
            "dushanbe_lat": DUSHANBE_LAT,
            "dushanbe_lon": DUSHANBE_LON,
            "dushanbe_city": DUSHANBE_CITY,
            "test_users_count": await count_test_users(session),
            "flash": request.query_params.get("flash"),
        }
        return TEMPLATES.TemplateResponse(request, "dashboard.html", ctx)

    @app.get("/users/{user_id}/photo")
    async def user_photo(
        user_id: int, request: Request, session: AsyncSession = Depends(get_db)
    ):
        if (redir := require_auth(request)) is not None:
            return redir
        user = await session.get(User, user_id, options=[selectinload(User.profile)])
        if not user or not user.profile or not user.profile.photo_file_id:
            return Response(status_code=404)
        local = local_photo_path(user.profile.photo_file_id)
        if local is not None:
            return FileResponse(local, media_type="image/png")
        bot = Bot(token=settings.bot_token)
        try:
            f = await bot.get_file(user.profile.photo_file_id)
            if not f.file_path:
                return Response(status_code=404)
            url = f"https://api.telegram.org/file/bot{settings.bot_token}/{f.file_path}"
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.get(url)
                if resp.status_code != 200:
                    return Response(status_code=404)
                ctype = resp.headers.get("content-type", "image/jpeg")
                return Response(content=resp.content, media_type=ctype)
        finally:
            await bot.session.close()

    @app.post("/users/{user_id}/unban")
    async def user_unban(
        user_id: int, request: Request, session: AsyncSession = Depends(get_db)
    ):
        if (redir := require_auth(request)) is not None:
            return redir
        await unban_user(session, user_id)
        return RedirectResponse(settings.abs_path("/"), status_code=303)

    @app.post("/settings")
    async def save_settings(
        request: Request,
        daily_like_limit: int = Form(...),
        max_distance_km: float = Form(...),
        manager_contact: str = Form(...),
        payment_card: str = Form(...),
        payment_check_time: str = Form(...),
        registration_only: str | None = Form(None),
        session: AsyncSession = Depends(get_db),
    ):
        if (redir := require_auth(request)) is not None:
            return redir
        await set_setting(session, "daily_like_limit", str(daily_like_limit))
        await set_setting(session, "max_distance_km", str(max_distance_km))
        await set_setting(session, "manager_contact", manager_contact.strip())
        await set_setting(session, "payment_card", payment_card.strip())
        await set_setting(session, "payment_check_time", payment_check_time.strip())
        await set_setting(
            session, "registration_only", "true" if registration_only else "false"
        )
        return RedirectResponse(settings.abs_path("/"), status_code=303)

    @app.post("/channels/add")
    async def add_channel(
        request: Request,
        channel_id: str = Form(...),
        title: str = Form(""),
        invite_link: str = Form(""),
        session: AsyncSession = Depends(get_db),
    ):
        if (redir := require_auth(request)) is not None:
            return redir
        session.add(
            RequiredChannel(
                channel_id=channel_id.strip(),
                title=title.strip(),
                invite_link=invite_link.strip(),
                is_active=True,
            )
        )
        await session.commit()
        return RedirectResponse(settings.abs_path("/"), status_code=303)

    @app.post("/channels/{channel_pk}/toggle")
    async def toggle_channel(
        channel_pk: int, request: Request, session: AsyncSession = Depends(get_db)
    ):
        if (redir := require_auth(request)) is not None:
            return redir
        ch = await session.get(RequiredChannel, channel_pk)
        if ch:
            ch.is_active = not ch.is_active
            await session.commit()
        return RedirectResponse(settings.abs_path("/"), status_code=303)

    @app.post("/channels/{channel_pk}/delete")
    async def delete_channel(
        channel_pk: int, request: Request, session: AsyncSession = Depends(get_db)
    ):
        if (redir := require_auth(request)) is not None:
            return redir
        ch = await session.get(RequiredChannel, channel_pk)
        if ch:
            await session.delete(ch)
            await session.commit()
        return RedirectResponse(settings.abs_path("/"), status_code=303)

    @app.post("/orders/{order_id}/approve")
    async def order_approve(
        order_id: int, request: Request, session: AsyncSession = Depends(get_db)
    ):
        if (redir := require_auth(request)) is not None:
            return redir
        await approve_order(session, order_id, admin_id=0)
        return RedirectResponse(settings.abs_path("/"), status_code=303)

    @app.post("/orders/{order_id}/reject")
    async def order_reject(
        order_id: int, request: Request, session: AsyncSession = Depends(get_db)
    ):
        if (redir := require_auth(request)) is not None:
            return redir
        await reject_order(session, order_id, admin_id=0)
        return RedirectResponse(settings.abs_path("/"), status_code=303)

    @app.post("/admin-geo")
    async def save_admin_geo(
        request: Request,
        admin_tg_id: int = Form(...),
        lat: float = Form(...),
        lon: float = Form(...),
        city_name: str = Form(DUSHANBE_CITY),
        session: AsyncSession = Depends(get_db),
    ):
        if (redir := require_auth(request)) is not None:
            return redir
        if admin_tg_id not in settings.admin_id_set:
            return RedirectResponse(
                settings.abs_path("/?flash=bad_admin"), status_code=303
            )
        await set_user_geo(session, admin_tg_id, lat, lon, city_name)
        return RedirectResponse(
            settings.abs_path("/?flash=geo_saved"), status_code=303
        )

    @app.post("/test-users/create")
    async def test_users_create(
        request: Request,
        count: int = Form(10),
        visible: str | None = Form(None),
        session: AsyncSession = Depends(get_db),
    ):
        if (redir := require_auth(request)) is not None:
            return redir
        n = await create_test_users(session, count, visible=bool(visible))
        return RedirectResponse(
            settings.abs_path(f"/?flash=test_created_{n}"), status_code=303
        )

    @app.post("/test-users/clear")
    async def test_users_clear(
        request: Request, session: AsyncSession = Depends(get_db)
    ):
        if (redir := require_auth(request)) is not None:
            return redir
        n = await clear_test_users(session)
        return RedirectResponse(
            settings.abs_path(f"/?flash=test_cleared_{n}"), status_code=303
        )

    return app
