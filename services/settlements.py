"""Search settlements by name; nearest neighbours for confirm copy."""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from database.models import Settlement, SettlementAlias
from services.geo import haversine_km
from services.settlement_data import normalize_name


@dataclass(frozen=True)
class SettlementHit:
    id: int
    display_name: str
    lat: float
    lon: float
    country_code: str
    admin1: str
    matched_name: str
    score: float


def _score(query_norm: str, alias_norm: str) -> float:
    if alias_norm == query_norm:
        return 1.0
    if alias_norm.startswith(query_norm) or query_norm.startswith(alias_norm):
        return 0.92
    return SequenceMatcher(None, query_norm, alias_norm).ratio()


async def search_settlements(
    session: AsyncSession, raw_query: str, *, limit: int = 12
) -> list[SettlementHit]:
    q = normalize_name(raw_query)
    if len(q) < 2:
        return []

    async def _fetch(where_clause, cap: int) -> list[SettlementAlias]:
        result = await session.execute(
            select(SettlementAlias)
            .options(selectinload(SettlementAlias.settlement))
            .where(where_clause)
            .limit(cap)
        )
        return list(result.scalars().all())

    # Prefer exact name matches first (many villages share a name).
    rows = await _fetch(SettlementAlias.name_norm == q, 80)
    if len(rows) < 8:
        rows.extend(await _fetch(SettlementAlias.name_norm.like(f"{q}%"), 80))
    if len(rows) < 8 and len(q) >= 3:
        rows.extend(await _fetch(SettlementAlias.name_norm.like(f"%{q}%"), 60))

    best_by_place: dict[int, SettlementHit] = {}
    for alias in rows:
        s = alias.settlement
        if s is None:
            continue
        score = _score(q, alias.name_norm)
        if score < 0.55:
            continue
        hit = SettlementHit(
            id=s.id,
            display_name=s.display_name,
            lat=s.lat,
            lon=s.lon,
            country_code=s.country_code,
            admin1=s.admin1 or "",
            matched_name=alias.name,
            score=score,
        )
        prev = best_by_place.get(s.id)
        if prev is None or hit.score > prev.score:
            best_by_place[s.id] = hit

    ranked = sorted(best_by_place.values(), key=lambda h: (-h.score, h.display_name))
    return ranked[:limit]


def disambiguation_choices(hits: list[SettlementHit], *, max_n: int = 8) -> list[SettlementHit]:
    """If several close matches (often same name) — user must pick."""
    if not hits:
        return []
    if len(hits) == 1:
        return hits
    best = hits[0].score
    close = [h for h in hits if h.score >= best - 0.08 and h.score >= 0.75]
    if len(close) >= 2:
        return close[:max_n]
    # Same display name in different places
    top_name = normalize_name(hits[0].display_name)
    same = [h for h in hits if normalize_name(h.display_name) == top_name]
    if len(same) >= 2:
        return same[:max_n]
    return [hits[0]]


async def choice_button_label(session: AsyncSession, hit: SettlementHit) -> str:
    neighbours = await nearest_settlements(
        session, hit.lat, hit.lon, exclude_id=hit.id, limit=2
    )
    near = [n.display_name for n in neighbours if n.display_name]
    if len(near) >= 2:
        label = f"{hit.display_name} · {near[0]}, {near[1]}"
    elif len(near) == 1:
        label = f"{hit.display_name} · {near[0]}"
    elif hit.admin1:
        label = f"{hit.display_name} · {hit.admin1}"
    elif hit.country_code:
        label = f"{hit.display_name} · {hit.country_code}"
    else:
        label = hit.display_name
    return label[:64]


async def nearest_settlements(
    session: AsyncSession,
    lat: float,
    lon: float,
    *,
    exclude_id: int,
    limit: int = 2,
) -> list[Settlement]:
    # Bounding box ~80 km, then exact haversine.
    dlat = 0.8
    dlon = 0.8
    result = await session.execute(
        select(Settlement)
        .where(
            Settlement.id != exclude_id,
            Settlement.lat.between(lat - dlat, lat + dlat),
            Settlement.lon.between(lon - dlon, lon + dlon),
        )
        .limit(120)
    )
    candidates = list(result.scalars().all())
    scored = [
        (haversine_km(lat, lon, s.lat, s.lon), s)
        for s in candidates
        if s.display_name
    ]
    scored.sort(key=lambda x: x[0])
    out: list[Settlement] = []
    seen_names: set[str] = set()
    for _dist, s in scored:
        key = normalize_name(s.display_name)
        if key in seen_names:
            continue
        seen_names.add(key)
        out.append(s)
        if len(out) >= limit:
            break
    return out


async def get_settlement(session: AsyncSession, settlement_id: int) -> Settlement | None:
    return await session.get(Settlement, settlement_id)


def format_confirm(hit: SettlementHit, neighbours: list[Settlement], lang: str) -> str:
    from locales import t

    near = [n.display_name for n in neighbours if n.display_name]
    if len(near) >= 2:
        return t(
            "location_confirm_near2",
            lang,
            place=hit.display_name,
            near_a=near[0],
            near_b=near[1],
        )
    if len(near) == 1:
        return t(
            "location_confirm_near1",
            lang,
            place=hit.display_name,
            near_a=near[0],
        )
    admin = hit.admin1 or hit.country_code or "—"
    return t("location_confirm_admin", lang, place=hit.display_name, admin=admin)
