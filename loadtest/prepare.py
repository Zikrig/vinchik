"""Prepare an isolated Postgres/Redis dataset for the webhook load test."""

from __future__ import annotations

import asyncio
import math
import os
from datetime import UTC, datetime

from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.redis import RedisStorage
from sqlalchemy import delete, or_, select
from sqlalchemy.dialects.postgresql import insert

from config import settings
from database.models import (
    DailyLikeStat,
    Gender,
    Like,
    LookingFor,
    Profile,
    RequiredChannel,
    User,
)
from database.session import async_session_maker, init_db
from services.settings_service import ensure_defaults, set_setting
from states.browse import BrowseStates


VIEWER_BASE = int(os.getenv("LOADTEST_VIEWER_BASE", "9000000000"))
VIEWER_COUNT = int(os.getenv("LOADTEST_VIEWERS", "300"))
CANDIDATE_BASE = int(os.getenv("LOADTEST_CANDIDATE_BASE", "9100000000"))
CANDIDATE_COUNT = int(os.getenv("LOADTEST_CANDIDATES", "5000"))
REQUIRED_CHANNELS = int(os.getenv("LOADTEST_REQUIRED_CHANNELS", "3"))
BOT_ID = int(os.getenv("BOT_TOKEN", "999999:LOAD_TEST_TOKEN").split(":", 1)[0])


def _photo_ids_for(index: int) -> list[str]:
    """Fake Telegram file_ids so browse hits sendPhoto / sendMediaGroup."""
    bucket = index % 10
    if bucket <= 6:
        count = 1
    elif bucket <= 8:
        count = 2
    else:
        count = 3
    return [f"loadtest-photo-{index}-{n}" for n in range(count)]


def _guard_isolation() -> None:
    if os.getenv("VINCHIK_LOADTEST", "") != "isolated":
        raise RuntimeError("Refusing to seed without VINCHIK_LOADTEST=isolated")
    if not settings.redis_url.rstrip("/").endswith("/15"):
        raise RuntimeError("Load test must use isolated Redis database 15")
    if settings.postgres_host != "db":
        raise RuntimeError("Load test expects isolated Compose DB host 'db'")
    if VIEWER_COUNT < 1 or CANDIDATE_COUNT < VIEWER_COUNT:
        raise RuntimeError("Need at least one candidate per load-test viewer")
    viewer_last = VIEWER_BASE + VIEWER_COUNT - 1
    candidate_last = CANDIDATE_BASE + CANDIDATE_COUNT - 1
    if CANDIDATE_BASE <= viewer_last and candidate_last >= VIEWER_BASE:
        raise RuntimeError("Viewer and candidate tg_id ranges must not overlap")
    if CANDIDATE_BASE <= 0:
        raise RuntimeError(
            "CANDIDATE_BASE must be positive so notify_like_batch is exercised"
        )


def _viewer_rows(now: datetime) -> tuple[list[dict], list[dict]]:
    users: list[dict] = []
    profiles: list[dict] = []
    for index in range(VIEWER_COUNT):
        user_id = VIEWER_BASE + index
        users.append(
            {
                "tg_id": user_id,
                "username": f"load_viewer_{index}",
                "language": "ru",
                "language_chosen": True,
                "last_activity_at": now,
                "reengage_level": 0,
                "is_blocked": False,
                "is_suspicious": False,
                "is_test": True,
            }
        )
        profiles.append(
            {
                "user_id": user_id,
                "name": f"Viewer {index}",
                "age": 25 + index % 8,
                "city_name": "Душанбе",
                "lat": 38.5598 + (index % 20) * 0.001,
                "lon": 68.7870 + (index % 20) * 0.001,
                "gender": Gender.male,
                "looking_for": LookingFor.female,
                "description": "Load-test viewer",
                "photo_file_id": None,
                "photo_file_ids": None,
                "is_active": True,
                "is_complete": True,
            }
        )
    return users, profiles


def _candidate_rows(now: datetime) -> tuple[list[dict], list[dict]]:
    users: list[dict] = []
    profiles: list[dict] = []
    for index in range(CANDIDATE_COUNT):
        user_id = CANDIDATE_BASE + index
        # Deterministic spiral: candidates cover every integer distance from
        # 1 through 480 km around Dushanbe without crossing the 500 km cap.
        distance_km = 1 + index % 480
        bearing = 2 * math.pi * ((index * 0.61803398875) % 1.0)
        angular_distance = distance_km / 6371.0088
        origin_lat = math.radians(38.5598)
        origin_lon = math.radians(68.7870)
        candidate_lat = math.asin(
            math.sin(origin_lat) * math.cos(angular_distance)
            + math.cos(origin_lat)
            * math.sin(angular_distance)
            * math.cos(bearing)
        )
        candidate_lon = origin_lon + math.atan2(
            math.sin(bearing)
            * math.sin(angular_distance)
            * math.cos(origin_lat),
            math.cos(angular_distance)
            - math.sin(origin_lat) * math.sin(candidate_lat),
        )
        photos = _photo_ids_for(index)
        users.append(
            {
                "tg_id": user_id,
                "username": f"load_candidate_{index}",
                "language": "ru",
                "language_chosen": True,
                "last_activity_at": now,
                "reengage_level": 0,
                "is_blocked": False,
                "is_suspicious": False,
                # Non-test + positive id → notify_like_batch is exercised.
                "is_test": False,
            }
        )
        profiles.append(
            {
                "user_id": user_id,
                "name": f"Candidate {index}",
                "age": 20 + index % 20,
                "city_name": "Душанбе",
                "lat": math.degrees(candidate_lat),
                "lon": math.degrees(candidate_lon),
                "gender": Gender.female,
                "looking_for": LookingFor.male,
                "description": f"Load-test candidate at ~{distance_km} km",
                "photo_file_id": photos[0],
                "photo_file_ids": photos,
                "is_active": True,
                "is_complete": True,
            }
        )
    return users, profiles


async def _upsert_users(session, rows: list[dict]) -> None:
    for start in range(0, len(rows), 1000):
        statement = insert(User).values(rows[start : start + 1000])
        await session.execute(
            statement.on_conflict_do_update(
                index_elements=[User.tg_id],
                set_={
                    "username": statement.excluded.username,
                    "language": statement.excluded.language,
                    "language_chosen": statement.excluded.language_chosen,
                    "last_activity_at": statement.excluded.last_activity_at,
                    "reengage_level": statement.excluded.reengage_level,
                    "is_blocked": statement.excluded.is_blocked,
                    "is_suspicious": statement.excluded.is_suspicious,
                    "is_test": statement.excluded.is_test,
                },
            )
        )


async def _upsert_profiles(session, rows: list[dict]) -> None:
    for start in range(0, len(rows), 1000):
        statement = insert(Profile).values(rows[start : start + 1000])
        await session.execute(
            statement.on_conflict_do_update(
                index_elements=[Profile.user_id],
                set_={
                    "name": statement.excluded.name,
                    "age": statement.excluded.age,
                    "city_name": statement.excluded.city_name,
                    "lat": statement.excluded.lat,
                    "lon": statement.excluded.lon,
                    "gender": statement.excluded.gender,
                    "looking_for": statement.excluded.looking_for,
                    "description": statement.excluded.description,
                    "photo_file_id": statement.excluded.photo_file_id,
                    "photo_file_ids": statement.excluded.photo_file_ids,
                    "is_active": statement.excluded.is_active,
                    "is_complete": statement.excluded.is_complete,
                },
            )
        )


async def _seed_database() -> None:
    await init_db()
    now = datetime.now(UTC)
    viewer_users, viewer_profiles = _viewer_rows(now)
    candidate_users, candidate_profiles = _candidate_rows(now)
    async with async_session_maker() as session:
        await ensure_defaults(session)
        await set_setting(session, "registration_only", "false")
        await set_setting(session, "max_distance_km", "500")
        await set_setting(session, "profile_reshow_days", "60")
        await set_setting(session, "test_users_visible", "true")

        viewer_last = VIEWER_BASE + VIEWER_COUNT - 1
        await session.execute(
            delete(Like).where(
                Like.from_user_id.between(VIEWER_BASE, viewer_last)
            )
        )
        await session.execute(
            delete(DailyLikeStat).where(
                DailyLikeStat.user_id.between(VIEWER_BASE, viewer_last)
            )
        )
        # Drop prior load-test candidates (legacy negative ids or previous seed).
        prior_candidates = select(User.tg_id).where(
            User.username.like("load_candidate_%")
        )
        await session.execute(
            delete(Like).where(
                or_(
                    Like.to_user_id.in_(prior_candidates),
                    Like.from_user_id.in_(prior_candidates),
                )
            )
        )
        await session.execute(
            delete(DailyLikeStat).where(
                DailyLikeStat.user_id.in_(prior_candidates)
            )
        )
        await session.execute(
            delete(Profile).where(Profile.user_id.in_(prior_candidates))
        )
        await session.execute(
            delete(User).where(User.username.like("load_candidate_%"))
        )
        await _upsert_users(session, viewer_users)
        await _upsert_users(session, candidate_users)
        await _upsert_profiles(session, viewer_profiles)
        await _upsert_profiles(session, candidate_profiles)

        await session.execute(delete(RequiredChannel))
        for index in range(max(0, REQUIRED_CHANNELS)):
            session.add(
                RequiredChannel(
                    channel_id=f"@loadtest_channel_{index + 1}",
                    title=f"Load-test channel {index + 1}",
                    invite_link=f"https://t.me/loadtest_channel_{index + 1}",
                    is_active=True,
                )
            )
        await session.commit()


async def _seed_fsm() -> None:
    storage = RedisStorage.from_url(settings.redis_url)
    try:
        await storage.redis.flushdb()
        for index in range(VIEWER_COUNT):
            user_id = VIEWER_BASE + index
            key = StorageKey(bot_id=BOT_ID, chat_id=user_id, user_id=user_id)
            await storage.set_state(key, BrowseStates.viewing)
            await storage.set_data(
                key,
                {"browse_target": CANDIDATE_BASE + index % CANDIDATE_COUNT},
            )
    finally:
        await storage.close()


async def main() -> None:
    _guard_isolation()
    await _seed_database()
    await _seed_fsm()
    print(
        "load-test seed ready: "
        f"viewers={VIEWER_COUNT}, candidates={CANDIDATE_COUNT}, "
        f"channels={REQUIRED_CHANNELS}, radius=500km, redis_db=15, "
        f"candidate_base={CANDIDATE_BASE}, notify_eligible=yes, "
        f"photos=1-3 fake file_ids"
    )


if __name__ == "__main__":
    asyncio.run(main())
