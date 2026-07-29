"""Load portable dump data/settlements/settlements.csv.gz into Postgres."""

from __future__ import annotations

import csv
import gzip
import logging
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import Settlement, SettlementAlias
from services.settlement_data import SETTLEMENTS_DUMP, normalize_name, pick_display_name

logger = logging.getLogger(__name__)

BATCH = 5000


async def settlements_count(session: AsyncSession) -> int:
    return int(
        (await session.execute(select(func.count()).select_from(Settlement))).scalar_one()
    )


async def import_settlements_from_dump(
    session: AsyncSession,
    *,
    path: Path | None = None,
    replace: bool = True,
) -> int:
    dump = path or SETTLEMENTS_DUMP
    if not dump.is_file():
        logger.warning("settlements dump missing: %s", dump)
        return 0

    places: dict[int, dict] = {}
    aliases: list[tuple[int, str, str]] = []
    alias_keys: set[tuple[int, str]] = set()
    names_by_place: dict[int, list[str]] = {}

    with gzip.open(dump, "rt", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or "id" not in reader.fieldnames:
            raise RuntimeError(f"bad settlements dump header: {reader.fieldnames}")
        for row in reader:
            try:
                sid = int(row["id"])
                lat = float(row["lat"])
                lon = float(row["lon"])
            except (KeyError, ValueError):
                continue
            name = (row.get("name") or "").strip()[:128]
            if not name:
                continue
            norm = normalize_name(name)
            if len(norm) < 2:
                continue
            country = (row.get("country") or "")[:2]
            admin1 = (row.get("admin1") or "")[:128]
            is_primary = (row.get("is_primary") or "0").strip() == "1"

            names_by_place.setdefault(sid, []).append(name)

            if sid not in places:
                places[sid] = {
                    "display_name": name,
                    "lat": lat,
                    "lon": lon,
                    "country_code": country,
                    "admin1": admin1,
                    "primary_name": name if is_primary else "",
                }
            else:
                if is_primary:
                    places[sid]["primary_name"] = name
                places[sid]["lat"] = lat
                places[sid]["lon"] = lon

            key = (sid, norm)
            if key in alias_keys:
                continue
            alias_keys.add(key)
            aliases.append((sid, name, norm))

    # Prefer Cyrillic (ru/tg) display names over Latin GeoNames titles.
    for sid, meta in places.items():
        meta["display_name"] = pick_display_name(
            names_by_place.get(sid, []),
            meta.get("primary_name") or meta["display_name"],
        )

    if replace:
        await session.execute(delete(SettlementAlias))
        await session.execute(delete(Settlement))
        await session.commit()

    buf: list[Settlement] = []
    for sid, meta in places.items():
        buf.append(
            Settlement(
                id=sid,
                display_name=meta["display_name"][:128],
                lat=meta["lat"],
                lon=meta["lon"],
                country_code=meta["country_code"],
                admin1=meta["admin1"],
            )
        )
        if len(buf) >= BATCH:
            session.add_all(buf)
            await session.commit()
            buf.clear()
    if buf:
        session.add_all(buf)
        await session.commit()

    abuf: list[SettlementAlias] = []
    for sid, name, norm in aliases:
        abuf.append(SettlementAlias(settlement_id=sid, name=name, name_norm=norm))
        if len(abuf) >= BATCH:
            session.add_all(abuf)
            await session.commit()
            abuf.clear()
    if abuf:
        session.add_all(abuf)
        await session.commit()

    logger.info(
        "imported settlements places=%s aliases=%s file=%s",
        len(places),
        len(aliases),
        dump,
    )
    return len(aliases)


async def ensure_settlements_loaded(session: AsyncSession) -> None:
    if await settlements_count(session) > 0:
        return
    if not SETTLEMENTS_DUMP.is_file():
        logger.warning(
            "settlements table empty and dump missing (%s); text location search disabled",
            SETTLEMENTS_DUMP,
        )
        return
    logger.info("settlements table empty — importing %s", SETTLEMENTS_DUMP)
    await import_settlements_from_dump(session, replace=False)
