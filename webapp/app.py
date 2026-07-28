from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
from aiogram import Bot
from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeSerializer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from database.models import RequiredChannel
from database.session import async_session_maker
from services.admin_tools import (
    DUSHANBE_CITY,
    DUSHANBE_LAT,
    DUSHANBE_LON,
    are_test_users_visible,
    clear_test_users,
    count_test_users,
    create_test_users,
    get_user_geo,
    set_test_users_visible,
    set_user_geo,
)
from services.accounts import (
    account_like_stats,
    clear_user_likes,
    filters_from_query,
    map_markers,
    search_accounts,
    set_account_premium,
    update_account_profile,
    update_account_user,
)
from services.users import load_user_with_profile
from services.media import local_photo_path
from services.premium import (
    approve_order,
    list_pending_orders,
    list_premium_users,
    notify_premium_activated,
    reject_order,
)
from services.reports import list_blocked_users, unban_user
from services.settings_service import (
    ensure_defaults,
    get_daily_like_limit,
    get_max_distance_km,
    get_payment_info,
    get_profile_reshow_days,
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
        if wants_json(request):
            return JSONResponse({"ok": False, "error": "auth"}, status_code=401)
        return RedirectResponse(settings.abs_path("/login"), status_code=303)
    return None


def wants_json(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    return "application/json" in accept or (
        request.headers.get("x-requested-with") == "XMLHttpRequest"
    )


def form_truthy(value: str | None) -> bool:
    return (value or "").lower() in {"1", "true", "on", "yes"}


def ok_response(request: Request, redirect: str, **payload):
    if wants_json(request):
        return JSONResponse({"ok": True, **payload})
    return RedirectResponse(redirect, status_code=303)


def err_response(request: Request, redirect: str, error: str = "error", **payload):
    if wants_json(request):
        return JSONResponse({"ok": False, "error": error, **payload}, status_code=400)
    return RedirectResponse(redirect, status_code=303)


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
            "reshow_days": await get_profile_reshow_days(session),
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
            "test_users_visible": await are_test_users_visible(session),
            "flash": request.query_params.get("flash"),
        }
        return TEMPLATES.TemplateResponse(request, "dashboard.html", ctx)

    @app.get("/accounts", response_class=HTMLResponse)
    async def accounts_list(request: Request, session: AsyncSession = Depends(get_db)):
        if (redir := require_auth(request)) is not None:
            return redir
        raw = {
            "q": request.query_params.get("q") or "",
            "is_test": request.query_params.get("is_test") or "any",
            "is_blocked": request.query_params.get("is_blocked") or "any",
            "is_active": request.query_params.get("is_active") or "any",
            "is_complete": request.query_params.get("is_complete") or "any",
            "gender": request.query_params.get("gender") or "any",
            "looking_for": request.query_params.get("looking_for") or "any",
            "language": request.query_params.get("language") or "any",
            "has_premium": request.query_params.get("has_premium") or "any",
        }
        filters = filters_from_query(raw)
        rows = await search_accounts(session, **filters, limit=500)
        markers = await map_markers(session, admin_ids=settings.admin_id_set, limit=50)
        return TEMPLATES.TemplateResponse(
            request,
            "accounts.html",
            {
                "rows": rows,
                "f": raw,
                "count": len(rows),
                "markers": markers,
                "map_limit": 50,
                "dushanbe_lat": DUSHANBE_LAT,
                "dushanbe_lon": DUSHANBE_LON,
            },
        )

    @app.get("/accounts/{user_id}", response_class=HTMLResponse)
    async def account_detail(
        user_id: int, request: Request, session: AsyncSession = Depends(get_db)
    ):
        if (redir := require_auth(request)) is not None:
            return redir
        user = await load_user_with_profile(session, user_id)
        if user is None:
            return RedirectResponse(settings.abs_path("/accounts"), status_code=303)
        likes = await account_like_stats(session, user_id)
        flash = request.query_params.get("flash")
        return TEMPLATES.TemplateResponse(
            request,
            "account_detail.html",
            {
                "u": user,
                "p": user.profile,
                "likes": likes,
                "is_admin": user_id in settings.admin_id_set,
                "flash": flash,
            },
        )

    @app.post("/accounts/{user_id}/user")
    async def account_save_user(
        user_id: int,
        request: Request,
        session: AsyncSession = Depends(get_db),
        username: str = Form(""),
        language: str = Form("ru"),
        is_test: str | None = Form(None),
        is_blocked: str | None = Form(None),
        reengage_level: int = Form(0),
    ):
        if (redir := require_auth(request)) is not None:
            return redir
        try:
            updated = await update_account_user(
                session,
                user_id,
                username=username,
                language=language,
                is_test=is_test is not None,
                is_blocked=is_blocked is not None,
                reengage_level=reengage_level,
            )
        except Exception:
            return err_response(
                request,
                settings.abs_path(f"/accounts/{user_id}?flash=error"),
                error="Не удалось сохранить",
            )
        if updated is None:
            return err_response(
                request, settings.abs_path("/accounts"), error="not_found"
            )
        return ok_response(
            request,
            settings.abs_path(f"/accounts/{user_id}?flash=saved"),
            message="Сохранено.",
        )

    @app.post("/accounts/{user_id}/profile")
    async def account_save_profile(
        user_id: int,
        request: Request,
        session: AsyncSession = Depends(get_db),
        name: str = Form(""),
        age: str = Form(""),
        city_name: str = Form(""),
        lat: str = Form(""),
        lon: str = Form(""),
        gender: str = Form(""),
        looking_for: str = Form(""),
        description: str = Form(""),
        photo_file_id: str = Form(""),
        is_active: str | None = Form(None),
        is_complete: str | None = Form(None),
        clear_photo: str | None = Form(None),
    ):
        if (redir := require_auth(request)) is not None:
            return redir

        def _opt_float(raw: str) -> float | None:
            raw = (raw or "").strip()
            if not raw:
                return None
            try:
                return float(raw)
            except ValueError:
                return None

        def _opt_int(raw: str) -> int | None:
            raw = (raw or "").strip()
            if not raw:
                return None
            try:
                return int(raw)
            except ValueError:
                return None

        try:
            updated = await update_account_profile(
                session,
                user_id,
                name=name,
                age=_opt_int(age),
                city_name=city_name,
                lat=_opt_float(lat),
                lon=_opt_float(lon),
                gender=gender or None,
                looking_for=looking_for or None,
                description=description,
                photo_file_id=photo_file_id,
                is_active=is_active is not None,
                is_complete=is_complete is not None,
                clear_photo=clear_photo is not None,
            )
        except Exception:
            return err_response(
                request,
                settings.abs_path(f"/accounts/{user_id}?flash=error"),
                error="Не удалось сохранить",
            )
        if updated is None:
            return err_response(
                request, settings.abs_path("/accounts"), error="not_found"
            )
        return ok_response(
            request,
            settings.abs_path(f"/accounts/{user_id}?flash=saved"),
            message="Сохранено.",
        )

    @app.post("/accounts/{user_id}/premium")
    async def account_save_premium(
        user_id: int,
        request: Request,
        session: AsyncSession = Depends(get_db),
        premium_until: str = Form(""),
        action: str = Form("set"),
        add_days: int = Form(0),
    ):
        if (redir := require_auth(request)) is not None:
            return redir
        try:
            if action == "clear":
                updated = await set_account_premium(session, user_id, clear=True)
            elif action == "extend":
                updated = await set_account_premium(
                    session, user_id, add_days=max(0, int(add_days))
                )
            else:
                updated = await set_account_premium(
                    session, user_id, premium_until_raw=premium_until
                )
        except Exception:
            return err_response(
                request,
                settings.abs_path(f"/accounts/{user_id}?flash=error"),
                error="Не удалось сохранить",
            )
        if updated is None:
            return err_response(
                request,
                settings.abs_path(f"/accounts/{user_id}?flash=error"),
                error="Не удалось сохранить",
            )
        premium_until_out = (
            updated.premium_until.strftime("%Y-%m-%d %H:%M")
            if updated.premium_until
            else None
        )
        return ok_response(
            request,
            settings.abs_path(f"/accounts/{user_id}?flash=saved"),
            message="Сохранено.",
            premium_until=premium_until_out,
        )

    @app.post("/accounts/{user_id}/clear-likes")
    async def account_clear_likes(
        user_id: int,
        request: Request,
        session: AsyncSession = Depends(get_db),
        clear_sent: str | None = Form(None),
        clear_received: str | None = Form(None),
        clear_daily: str | None = Form(None),
    ):
        if (redir := require_auth(request)) is not None:
            return redir
        user = await load_user_with_profile(session, user_id)
        if user is None:
            return err_response(
                request, settings.abs_path("/accounts"), error="not_found"
            )
        n = await clear_user_likes(
            session,
            user_id,
            sent=clear_sent is not None,
            received=clear_received is not None,
            daily_stats=clear_daily is not None,
        )
        return ok_response(
            request,
            settings.abs_path(f"/accounts/{user_id}?flash=likes_cleared_{n}"),
            message=f"Удалено записей: {n}.",
            n=n,
        )

    @app.get("/users/{user_id}/photo")
    async def user_photo(
        user_id: int, request: Request, session: AsyncSession = Depends(get_db)
    ):
        if (redir := require_auth(request)) is not None:
            return redir
        user = await load_user_with_profile(session, user_id)
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
        referer = request.headers.get("referer") or ""
        if f"/accounts/{user_id}" in referer:
            dest = settings.abs_path(f"/accounts/{user_id}?flash=saved")
        elif "/accounts" in referer:
            dest = settings.abs_path("/accounts")
        else:
            dest = settings.abs_path("/")
        return ok_response(
            request, dest, message="Разбанен.", remove=True, user_id=user_id
        )

    @app.post("/settings")
    async def save_settings(
        request: Request,
        daily_like_limit: int = Form(...),
        max_distance_km: float = Form(...),
        profile_reshow_days: int = Form(...),
        manager_contact: str = Form(...),
        payment_card: str = Form(...),
        payment_check_time: str = Form(...),
        session: AsyncSession = Depends(get_db),
    ):
        if (redir := require_auth(request)) is not None:
            return redir
        await set_setting(session, "daily_like_limit", str(daily_like_limit))
        capped = min(max(float(max_distance_km), 1.0), 20000.0)
        await set_setting(session, "max_distance_km", str(capped))
        await set_setting(
            session, "profile_reshow_days", str(max(0, int(profile_reshow_days)))
        )
        await set_setting(session, "manager_contact", manager_contact.strip())
        await set_setting(session, "payment_card", payment_card.strip())
        await set_setting(session, "payment_check_time", payment_check_time.strip())
        return ok_response(
            request, settings.abs_path("/"), message="Настройки сохранены."
        )

    @app.post("/settings/soft-launch")
    async def toggle_soft_launch(
        request: Request,
        registration_only: str = Form("0"),
        session: AsyncSession = Depends(get_db),
    ):
        if (redir := require_auth(request)) is not None:
            return redir
        on = form_truthy(registration_only)
        await set_setting(session, "registration_only", "true" if on else "false")
        flash = "soft_on" if on else "soft_off"
        return ok_response(
            request,
            settings.abs_path(f"/?flash={flash}"),
            on=on,
            message="Soft-launch включён." if on else "Soft-launch выключен — лента открыта.",
        )

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
        ch = RequiredChannel(
            channel_id=channel_id.strip(),
            title=title.strip(),
            invite_link=invite_link.strip(),
            is_active=True,
        )
        session.add(ch)
        await session.commit()
        await session.refresh(ch)
        return ok_response(
            request,
            settings.abs_path("/"),
            message="Канал добавлен.",
            channel={
                "id": ch.id,
                "channel_id": ch.channel_id,
                "title": ch.title,
                "invite_link": ch.invite_link,
                "is_active": ch.is_active,
            },
        )

    @app.post("/channels/{channel_pk}/toggle")
    async def toggle_channel(
        channel_pk: int, request: Request, session: AsyncSession = Depends(get_db)
    ):
        if (redir := require_auth(request)) is not None:
            return redir
        ch = await session.get(RequiredChannel, channel_pk)
        active = False
        if ch:
            ch.is_active = not ch.is_active
            active = ch.is_active
            await session.commit()
        return ok_response(
            request,
            settings.abs_path("/"),
            id=channel_pk,
            active=active,
            message="Канал обновлён.",
        )

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
        return ok_response(
            request,
            settings.abs_path("/"),
            id=channel_pk,
            remove=True,
            message="Канал удалён.",
        )

    @app.post("/orders/{order_id}/approve")
    async def order_approve(
        order_id: int, request: Request, session: AsyncSession = Depends(get_db)
    ):
        if (redir := require_auth(request)) is not None:
            return redir
        result = await approve_order(session, order_id, admin_id=0)
        if result:
            _, user = result
            bot = Bot(token=settings.bot_token)
            try:
                await notify_premium_activated(bot, user)
            finally:
                await bot.session.close()
        return ok_response(
            request,
            settings.abs_path("/"),
            id=order_id,
            remove=True,
            message="Заявка одобрена.",
        )

    @app.post("/orders/{order_id}/reject")
    async def order_reject(
        order_id: int, request: Request, session: AsyncSession = Depends(get_db)
    ):
        if (redir := require_auth(request)) is not None:
            return redir
        await reject_order(session, order_id, admin_id=0)
        return ok_response(
            request,
            settings.abs_path("/"),
            id=order_id,
            remove=True,
            message="Заявка отклонена.",
        )

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
            return err_response(
                request,
                settings.abs_path("/?flash=bad_admin"),
                error="Неверный admin id.",
            )
        await set_user_geo(session, admin_tg_id, lat, lon, city_name)
        return ok_response(
            request,
            settings.abs_path("/?flash=geo_saved"),
            message="Геолокация админа сохранена.",
        )

    @app.post("/test-users/create")
    async def test_users_create(
        request: Request,
        count: int = Form(10),
        session: AsyncSession = Depends(get_db),
    ):
        if (redir := require_auth(request)) is not None:
            return redir
        n = await create_test_users(session, count)
        total = await count_test_users(session)
        return ok_response(
            request,
            settings.abs_path(f"/?flash=test_created_{n}"),
            n=n,
            count=total,
            message=f"Создано тестовых: {n}.",
        )

    @app.post("/test-users/visibility")
    async def test_users_visibility(
        request: Request,
        visible: str = Form("0"),
        session: AsyncSession = Depends(get_db),
    ):
        if (redir := require_auth(request)) is not None:
            return redir
        on = form_truthy(visible)
        n = await set_test_users_visible(session, on)
        flash = "test_shown" if on else "test_hidden"
        return ok_response(
            request,
            settings.abs_path(f"/?flash={flash}_{n}"),
            visible=on,
            n=n,
            count=await count_test_users(session),
            message=(
                f"Тестовые показаны в ленте: {n}."
                if on
                else f"Тестовые скрыты из ленты: {n}."
            ),
        )

    @app.post("/test-users/clear")
    async def test_users_clear(
        request: Request, session: AsyncSession = Depends(get_db)
    ):
        if (redir := require_auth(request)) is not None:
            return redir
        n = await clear_test_users(session)
        return ok_response(
            request,
            settings.abs_path(f"/?flash=test_cleared_{n}"),
            n=n,
            count=0,
            message=f"Удалено тестовых: {n}.",
        )

    return app
