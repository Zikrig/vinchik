"""Load portable dump data/settlements/settlements.csv.gz into Postgres."""

from __future__ import annotations

import csv
import gzip
import logging
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import Settlement, SettlementAlias
from services.settlement_data import (
    SETTLEMENTS_DUMP,
    _NOT_FOR_DISPLAY,
    is_cyrillic_name,
    normalize_name,
    pick_display_name,
)

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
            try:
                population = int(row.get("population") or 0)
            except ValueError:
                population = 0

            names_by_place.setdefault(sid, []).append(name)

            if sid not in places:
                places[sid] = {
                    "display_name": name,
                    "lat": lat,
                    "lon": lon,
                    "country_code": country,
                    "admin1": admin1,
                    "primary_name": name if is_primary else "",
                    "population": population,
                }
            else:
                if is_primary:
                    places[sid]["primary_name"] = name
                places[sid]["lat"] = lat
                places[sid]["lon"] = lon
                if population > places[sid].get("population", 0):
                    places[sid]["population"] = population

            key = (sid, norm)
            if key in alias_keys:
                continue
            alias_keys.add(key)
            aliases.append((sid, name, norm))

    # Prefer Cyrillic modern titles; aliases stay for search only.
    for sid, meta in places.items():
        namelist = names_by_place.get(sid, [])
        primary = (meta.get("primary_name") or "").strip()
        # Dump is_primary already marks the modern label when rebuilt with pick_display_name.
        if (
            primary
            and is_cyrillic_name(primary)
            and normalize_name(primary) not in _NOT_FOR_DISPLAY
        ):
            display = primary
            if display.startswith("Санкт ") and "-" not in display:
                display = "Санкт-" + display[len("Санкт ") :]
            meta["display_name"] = display[:128]
            continue

        latin_official = max(
            (
                n
                for n in namelist
                if n.isascii()
                and ((" " in n) or ("-" in n))
                and 8 <= len(n) <= 40
                and "lungsod" not in n.lower()
                and " ng " not in n.lower()
            ),
            key=len,
            default="",
        ) or max(
            (
                n
                for n in namelist
                if n.isascii()
                and n.isalpha()
                and 5 <= len(n) <= 14
                and n[0].isupper()
            ),
            key=lambda n: (n.lower() in {"moscow", "moskva"}, len(n)),
            default=primary,
        )
        meta["display_name"] = pick_display_name(namelist, latin_official or primary)

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
                population=int(meta.get("population") or 0),
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
