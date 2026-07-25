from __future__ import annotations

import logging
import math

import httpx

logger = logging.getLogger(__name__)


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


async def reverse_geocode(lat: float, lon: float) -> str:
    url = "https://nominatim.openstreetmap.org/reverse"
    params = {"lat": lat, "lon": lon, "format": "json", "accept-language": "ru"}
    headers = {"User-Agent": "vinchik-dating-bot/1.0"}
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            addr = data.get("address") or {}
            for key in ("city", "town", "village", "municipality", "state", "county"):
                if addr.get(key):
                    return str(addr[key])
            return data.get("display_name", "Неизвестно")[:128]
    except Exception:
        logger.exception("reverse_geocode failed")
        return "Неизвестно"
