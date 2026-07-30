from __future__ import annotations

import ipaddress
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from database.models import PremiumOrder, RequiredChannel
from database.session import async_session_maker, init_db
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
from services.users import load_user_with_profile, is_premium
from services.media import LOCAL_PREFIX, local_photo_path
from services.premium import (
    approve_order,
    list_pending_orders,
    list_premium_users,
    notify_premium_activated,
    reject_order,
)
from services.reports import ban_user, list_blocked_users, unban_user
from services.moderation import clear_suspicious, list_suspicious_users
from services.channels import (
    ChannelResolveError,
    add_resolved_channel,
    delete_channel as remove_required_channel,
    resolve_channel_ref,
    toggle_channel as flip_required_channel,
)
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
TEMPLATES.env.globals["is_premium"] = is_premium
signer = URLSafeTimedSerializer(settings.web_secret_key, salt="vinchik-admin")
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_MAX_FAILURES = 5
_login_failures: dict[str, list[float]] = {}
login_redis = Redis.from_url(settings.redis_url, decode_responses=True)


async def get_db():
    async with async_session_maker() as session:
        yield session


def is_logged_in(request: Request) -> bool:
    token = request.cookies.get("admin_session")
    if not token:
        return False
    try:
        data = signer.loads(
            token,
            max_age=settings.admin_session_max_age_seconds,
        )
        return data.get("ok") is True
    except (BadSignature, SignatureExpired):
        return False


def _login_client_key(request: Request) -> str:
    peer = request.client.host if request.client else "unknown"
    try:
        peer_ip = ipaddress.ip_address(peer)
    except ValueError:
        return peer
    trusted_proxy = False
    for raw in settings.web_trusted_proxy_ips.split(","):
        try:
            if peer_ip in ipaddress.ip_network(raw.strip(), strict=False):
                trusted_proxy = True
                break
        except ValueError:
            continue
    if trusted_proxy:
        forwarded = request.headers.get("x-real-ip", "").strip()
        try:
            if forwarded:
                return str(ipaddress.ip_address(forwarded))
        except ValueError:
            pass
    return peer


def _prune_local_login_failures() -> None:
    cutoff = time.monotonic() - LOGIN_WINDOW_SECONDS
    for key, attempts in list(_login_failures.items()):
        fresh = [stamp for stamp in attempts if stamp >= cutoff]
        if fresh:
            _login_failures[key] = fresh
        else:
            _login_failures.pop(key, None)


async def _login_is_limited(request: Request) -> bool:
    key = _login_client_key(request)
    try:
        attempts = await login_redis.eval(
            """
            local count = redis.call('INCR', KEYS[1])
            if count == 1 then
                redis.call('EXPIRE', KEYS[1], ARGV[1])
            end
            return count
            """,
            1,
            f"vinchik:web-login:{key}",
            LOGIN_WINDOW_SECONDS,
        )
        return int(attempts) > LOGIN_MAX_FAILURES
    except Exception:
        _prune_local_login_failures()
        if key not in _login_failures and len(_login_failures) >= 10_000:
            _login_failures.pop(next(iter(_login_failures)))
        _login_failures.setdefault(key, []).append(time.monotonic())
        return len(_login_failures[key]) > LOGIN_MAX_FAILURES


async def _clear_login_failures(request: Request) -> None:
    key = _login_client_key(request)
    _login_failures.pop(key, None)
    try:
        await login_redis.delete(f"vinchik:web-login:{key}")
    except Exception:
        pass


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


def serialize_account_user(user) -> dict:
    return {
        "reengage_level": int(user.reengage_level or 0),
        "is_test": bool(user.is_test),
        "is_blocked": bool(user.is_blocked),
        "is_suspicious": bool(user.is_suspicious),
        "suspicious_reason": user.suspicious_reason or "",
    }


def serialize_account_profile(user) -> dict:
    p = user.profile
    lang = user.language or "ru"
    if p is None:
        return {
            "language": lang,
            "name": "",
            "age": "",
            "city_name": "",
            "gender": "",
            "looking_for": "",
            "lat": "",
            "lon": "",
            "photo_file_id": "",
            "description": "",
            "is_active": False,
            "is_complete": False,
            "clear_photo": False,
        }
    return {
        "language": lang,
        "name": p.name or "",
        "age": "" if p.age is None else p.age,
        "city_name": p.city_name or "",
        "gender": p.gender.value if p.gender else "",
        "looking_for": p.looking_for.value if p.looking_for else "",
        "lat": "" if p.lat is None else p.lat,
        "lon": "" if p.lon is None else p.lon,
        "photo_file_id": p.photo_file_id or "",
        "description": p.description or "",
        "is_active": bool(p.is_active),
        "is_complete": bool(p.is_complete),
        "clear_photo": False,
    }


def serialize_account_hero(user) -> dict:
    p = user.profile
    return {
        "name": (p.name if p and p.name else "Без имени"),
        "age": p.age if p and p.age is not None else None,
        "username": user.username or "",
        "is_test": bool(user.is_test),
        "is_blocked": bool(user.is_blocked),
        "is_suspicious": bool(user.is_suspicious),
        "is_active": bool(p.is_active) if p else False,
        "is_complete": bool(p.is_complete) if p else False,
        "has_premium": is_premium(user),
        "has_photo": bool(p and p.photo_file_id),
        "gender": p.gender.value if p and p.gender else "",
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    async with async_session_maker() as session:
        await ensure_defaults(session)
    # One long-lived client instead of a fresh aiohttp session per request.
    app.state.bot = Bot(token=settings.bot_token)
    try:
        yield
    finally:
        await app.state.bot.session.close()
        await login_redis.aclose()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Vinchik Admin",
        root_path=settings.web_root_path or "",
        lifespan=lifespan,
    )

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        if is_logged_in(request):
            return RedirectResponse(settings.abs_path("/"), status_code=303)
        return TEMPLATES.TemplateResponse(
            request, "login.html", {"error": None}
        )

    @app.post("/login")
    async def login(request: Request, password: str = Form(...)):
        if await _login_is_limited(request):
            return TEMPLATES.TemplateResponse(
                request,
                "login.html",
                {"error": "Слишком много попыток. Попробуйте через 15 минут."},
                status_code=429,
            )
        # compare_digest rejects non-ASCII str — compare the encoded forms.
        if not secrets.compare_digest(
            password.encode("utf-8"), settings.admin_web_password.encode("utf-8")
        ):
            return TEMPLATES.TemplateResponse(
                request,
                "login.html",
                {"error": "Неверный пароль"},
                status_code=401,
            )
        await _clear_login_failures(request)
        resp = RedirectResponse(settings.abs_path("/"), status_code=303)
        forwarded_proto = request.headers.get("x-forwarded-proto", "")
        cookie_secure = request.url.scheme == "https" or forwarded_proto == "https"
        resp.set_cookie(
            "admin_session",
            signer.dumps({"ok": True}),
            httponly=True,
            samesite="lax",
            secure=cookie_secure,
            max_age=settings.admin_session_max_age_seconds,
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

    @app.get("/bans", response_class=HTMLResponse)
    async def bans_page(request: Request, session: AsyncSession = Depends(get_db)):
        if (redir := require_auth(request)) is not None:
            return redir
        return TEMPLATES.TemplateResponse(
            request,
            "bans.html",
            {
                "blocked": await list_blocked_users(session),
                "suspicious": await list_suspicious_users(session),
                "flash": request.query_params.get("flash"),
            },
        )

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
                "premium_active": is_premium(user),
                "flash": flash,
            },
        )

    @app.post("/accounts/{user_id}/user")
    async def account_save_user(
        user_id: int,
        request: Request,
        session: AsyncSession = Depends(get_db),
        is_test: str | None = Form(None),
        is_blocked: str | None = Form(None),
        is_suspicious: str | None = Form(None),
        suspicious_reason: str = Form(""),
        reengage_level: int = Form(0),
    ):
        if (redir := require_auth(request)) is not None:
            return redir
        try:
            updated = await update_account_user(
                session,
                user_id,
                is_test=is_test is not None,
                is_blocked=is_blocked is not None,
                reengage_level=reengage_level,
                is_suspicious=is_suspicious is not None,
                suspicious_reason=suspicious_reason,
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
            fields=serialize_account_user(updated),
            hero=serialize_account_hero(updated),
            profile_fields=serialize_account_profile(updated),
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
        language: str = Form(""),
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
                language=language or None,
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
            fields=serialize_account_profile(updated),
            hero=serialize_account_hero(updated),
        )

    @app.get("/settlements/search")
    async def settlements_search(
        request: Request,
        session: AsyncSession = Depends(get_db),
        q: str = "",
    ):
        if (redir := require_auth(request)) is not None:
            return redir
        from services.settlements import (
            choice_button_label,
            disambiguation_choices,
            format_confirm,
            nearest_settlements,
            search_settlements,
        )

        hits = await search_settlements(session, q or "", limit=12)
        choices = disambiguation_choices(hits, max_n=8)
        items = []
        for hit in choices:
            label = await choice_button_label(session, hit)
            neighbours = await nearest_settlements(
                session, hit.lat, hit.lon, exclude_id=hit.id, limit=2
            )
            items.append(
                {
                    "id": hit.id,
                    "name": hit.display_name,
                    "label": label,
                    "lat": hit.lat,
                    "lon": hit.lon,
                    "confirm": format_confirm(hit, neighbours, "ru"),
                    "admin1": hit.admin1 or "",
                    "country_code": hit.country_code or "",
                }
            )
        return JSONResponse({"ok": True, "q": (q or "").strip(), "items": items})

    @app.post("/accounts/{user_id}/premium")
    async def account_save_premium(
        user_id: int,
        request: Request,
        session: AsyncSession = Depends(get_db),
        premium_until: str = Form(""),
        premium_action: str = Form("set"),
        add_days: int = Form(0),
    ):
        if (redir := require_auth(request)) is not None:
            return redir
        try:
            if premium_action == "clear":
                updated = await set_account_premium(session, user_id, clear=True)
            elif premium_action == "extend":
                updated = await set_account_premium(
                    session, user_id, add_days=max(0, int(add_days))
                )
            else:
                updated = await set_account_premium(
                    session, user_id, premium_until_raw=premium_until
                )
        except Exception as exc:
            return err_response(
                request,
                settings.abs_path(f"/accounts/{user_id}?flash=error"),
                error=f"Не удалось сохранить: {exc}",
            )
        if updated is None:
            return err_response(
                request,
                settings.abs_path(f"/accounts/{user_id}?flash=error"),
                error="Не удалось сохранить",
            )
        premium_until_out = (
            updated.premium_until.strftime("%Y-%m-%dT%H:%M")
            if updated.premium_until
            else None
        )
        premium_until_label = (
            updated.premium_until.strftime("%Y-%m-%d %H:%M")
            if updated.premium_until
            else None
        )
        return ok_response(
            request,
            settings.abs_path(f"/accounts/{user_id}?flash=saved"),
            message="Сохранено.",
            premium_until=premium_until_out,
            premium_until_label=premium_until_label,
            premium_active=is_premium(updated),
            fields={"premium_until": premium_until_out or ""},
            hero=serialize_account_hero(updated),
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
        likes = await account_like_stats(session, user_id)
        return ok_response(
            request,
            settings.abs_path(f"/accounts/{user_id}?flash=likes_cleared_{n}"),
            message=f"Удалено записей: {n}.",
            n=n,
            likes=likes,
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
            return FileResponse(local)
        if user.profile.photo_file_id.startswith(LOCAL_PREFIX):
            return Response(status_code=404)
        try:
            f = await request.app.state.bot.get_file(user.profile.photo_file_id)
        except TelegramAPIError:
            return Response(status_code=404)
        if not f.file_path:
            return Response(status_code=404)
        url = f"https://api.telegram.org/file/bot{settings.bot_token}/{f.file_path}"
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return Response(status_code=404)
            ctype = resp.headers.get("content-type", "image/jpeg")
            return Response(content=resp.content, media_type=ctype)

    @app.post("/users/{user_id}/unban")
    async def user_unban(
        user_id: int, request: Request, session: AsyncSession = Depends(get_db)
    ):
        if (redir := require_auth(request)) is not None:
            return redir
        user = await unban_user(session, user_id)
        if user is None:
            return err_response(
                request,
                settings.abs_path("/bans"),
                error="user_not_found",
                message="Пользователь не найден.",
            )
        referer = request.headers.get("referer") or ""
        if f"/accounts/{user_id}" in referer:
            dest = settings.abs_path(f"/accounts/{user_id}?flash=saved")
        elif "/accounts" in referer:
            dest = settings.abs_path("/accounts")
        elif "/bans" in referer:
            dest = settings.abs_path("/bans?flash=unbanned")
        else:
            dest = settings.abs_path("/bans?flash=unbanned")
        return ok_response(
            request, dest, message="Разбанен.", remove=True, user_id=user_id
        )

    @app.post("/users/{user_id}/ban")
    async def user_ban(
        user_id: int, request: Request, session: AsyncSession = Depends(get_db)
    ):
        if (redir := require_auth(request)) is not None:
            return redir
        user = await ban_user(session, user_id)
        if user is None:
            return err_response(
                request,
                settings.abs_path("/bans"),
                error="user_not_found",
                message="Пользователь не найден.",
            )
        dest = settings.abs_path("/bans?flash=banned")
        return ok_response(request, dest, message="Забанен.", user_id=user_id)

    @app.post("/users/{user_id}/clear-suspicious")
    async def user_clear_suspicious(
        user_id: int, request: Request, session: AsyncSession = Depends(get_db)
    ):
        if (redir := require_auth(request)) is not None:
            return redir
        await clear_suspicious(session, user_id)
        dest = settings.abs_path("/bans?flash=cleared")
        return ok_response(
            request, dest, message="Флаг снят.", remove=True, user_id=user_id
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
            request,
            settings.abs_path("/"),
            message="Настройки сохранены.",
            fields={
                "daily_like_limit": int(daily_like_limit),
                "max_distance_km": capped,
                "profile_reshow_days": max(0, int(profile_reshow_days)),
                "manager_contact": manager_contact.strip(),
                "payment_card": payment_card.strip(),
                "payment_check_time": payment_check_time.strip(),
            },
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
        session: AsyncSession = Depends(get_db),
    ):
        if (redir := require_auth(request)) is not None:
            return redir
        try:
            resolved = await resolve_channel_ref(request.app.state.bot, channel_id)
            ch, created = await add_resolved_channel(session, resolved)
        except ChannelResolveError as exc:
            return err_response(
                request,
                settings.abs_path("/"),
                error=str(exc),
                message=str(exc),
            )
        except TelegramAPIError:
            message = "Telegram недоступен, попробуйте ещё раз."
            return err_response(
                request, settings.abs_path("/"), error=message, message=message
            )
        verb = "добавлен" if created else "обновлён"
        return ok_response(
            request,
            settings.abs_path("/"),
            message=f"Канал {verb}. Бот должен оставаться админом канала.",
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
        ch = await flip_required_channel(session, channel_pk)
        if ch is None:
            return err_response(
                request,
                settings.abs_path("/"),
                error="channel_not_found",
                message="Канал не найден.",
            )
        return ok_response(
            request,
            settings.abs_path("/"),
            id=channel_pk,
            active=ch.is_active,
            message="Канал обновлён.",
        )

    @app.post("/channels/{channel_pk}/delete")
    async def delete_channel(
        channel_pk: int, request: Request, session: AsyncSession = Depends(get_db)
    ):
        if (redir := require_auth(request)) is not None:
            return redir
        removed = await remove_required_channel(session, channel_pk)
        if not removed:
            return err_response(
                request,
                settings.abs_path("/"),
                error="channel_not_found",
                message="Канал не найден.",
            )
        return ok_response(
            request,
            settings.abs_path("/"),
            id=channel_pk,
            remove=True,
            message="Канал удалён.",
        )

    @app.get("/orders/{order_id}/receipt")
    async def order_receipt(
        order_id: int, request: Request, session: AsyncSession = Depends(get_db)
    ):
        if (redir := require_auth(request)) is not None:
            return redir
        order = await session.get(PremiumOrder, order_id)
        if not order or not order.receipt_file_id:
            return Response(status_code=404)
        try:
            f = await request.app.state.bot.get_file(order.receipt_file_id)
        except TelegramAPIError:
            return Response(status_code=404)
        if not f.file_path:
            return Response(status_code=404)
        url = f"https://api.telegram.org/file/bot{settings.bot_token}/{f.file_path}"
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return Response(status_code=404)
            ctype = resp.headers.get("content-type") or "application/octet-stream"
            if order.receipt_kind == "photo" and not ctype.startswith("image/"):
                ctype = "image/jpeg"
            filename = Path(f.file_path).name
            headers = {}
            if order.receipt_kind == "document":
                headers["Content-Disposition"] = f'inline; filename="{filename}"'
            return Response(content=resp.content, media_type=ctype, headers=headers)

    @app.post("/orders/{order_id}/approve")
    async def order_approve(
        order_id: int, request: Request, session: AsyncSession = Depends(get_db)
    ):
        if (redir := require_auth(request)) is not None:
            return redir
        result = await approve_order(session, order_id, admin_id=None)
        if result is None:
            return err_response(
                request,
                settings.abs_path("/"),
                error="order_already_processed",
                message="Заявка уже обработана или не найдена.",
            )
        _, user = result
        await notify_premium_activated(request.app.state.bot, user)
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
        result = await reject_order(session, order_id, admin_id=None)
        if result is None:
            return err_response(
                request,
                settings.abs_path("/"),
                error="order_already_processed",
                message="Заявка уже обработана или не найдена.",
            )
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
