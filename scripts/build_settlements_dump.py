"""
Build portable settlements dump from GeoNames.

Writes: data/settlements/settlements.csv.gz

Sources:
  - Full country dumps TJ, UZ, KG, AF (villages + towns, feature class P)
  - cities1000 worldwide (larger towns)

Usage (on a machine with network):
  python scripts/build_settlements_dump.py

Then commit/copy data/settlements/settlements.csv.gz with the project.
On a new server the bot imports the file into Postgres automatically if the
table is empty (or: python scripts/import_settlements.py).
"""

from __future__ import annotations

import csv
import gzip
import io
import sys
import zipfile
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.settlement_data import DUMP_FIELDS, SETTLEMENTS_DIR, SETTLEMENTS_DUMP, normalize_name, pick_display_name

GEONAMES = "https://download.geonames.org/export/dump"
COUNTRY_FILES = ("TJ", "UZ", "KG", "AF")
CITIES_FILE = "cities1000"

# Populated places
P_CODES = {
    "PPL",
    "PPLA",
    "PPLA2",
    "PPLA3",
    "PPLA4",
    "PPLC",
    "PPLG",
    "PPLR",
    "PPLS",
    "PPLX",
    "STLMT",
}


def _download_zip_text(name: str) -> str:
    url = f"{GEONAMES}/{name}.zip"
    print(f"download {url}")
    with urlopen(url, timeout=120) as resp:
        data = resp.read()
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        # country file is TJ.txt etc; cities1000.txt
        members = [n for n in zf.namelist() if n.endswith(".txt") and "readme" not in n.lower()]
        if not members:
            raise RuntimeError(f"no txt in {name}.zip")
        return zf.read(members[0]).decode("utf-8", errors="replace")


def _parse_places(raw: str) -> dict[int, dict]:
    """geoname_id -> place dict with names set."""
    places: dict[int, dict] = {}
    for line in raw.splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 15:
            continue
        try:
            gid = int(parts[0])
        except ValueError:
            continue
        name = parts[1].strip()
        asciiname = parts[2].strip()
        alternates = parts[3].strip()
        try:
            lat = float(parts[4])
            lon = float(parts[5])
        except ValueError:
            continue
        fclass = parts[6]
        fcode = parts[7]
        country = parts[8].strip().upper()
        admin1 = parts[10].strip()
        try:
            population = int(parts[14] or 0)
        except ValueError:
            population = 0
        if fclass != "P" or fcode not in P_CODES:
            continue
        if not name:
            continue
        names = {name}
        if asciiname:
            names.add(asciiname)
        if alternates:
            for alt in alternates.split(","):
                alt = alt.strip()
                if alt:
                    names.add(alt)
        prev = places.get(gid)
        if prev is None:
            places[gid] = {
                "id": gid,
                "lat": lat,
                "lon": lon,
                "country": country,
                "admin1": admin1,
                "names": names,
                "latin_fallback": name,
                "population": population,
            }
        else:
            prev["names"].update(names)
            if population > prev.get("population", 0):
                prev["population"] = population
            # Prefer coords from the higher-population record when merging dumps.
            if population >= prev.get("population", 0):
                prev["lat"] = lat
                prev["lon"] = lon
                prev["country"] = country
                prev["admin1"] = admin1
    return places


def main() -> None:
    SETTLEMENTS_DIR.mkdir(parents=True, exist_ok=True)
    places: dict[int, dict] = {}
    for code in COUNTRY_FILES:
        places.update(_parse_places(_download_zip_text(code)))
    places.update(_parse_places(_download_zip_text(CITIES_FILE)))

    rows: list[dict] = []
    seen_alias: set[tuple[int, str]] = set()
    for p in places.values():
        display = pick_display_name(p["names"], p["latin_fallback"])
        display_norm = normalize_name(display)
        for name in sorted(p["names"]):
            if len(name) > 128:
                continue
            norm = normalize_name(name)
            if len(norm) < 2:
                continue
            key = (p["id"], norm)
            if key in seen_alias:
                continue
            seen_alias.add(key)
            rows.append(
                {
                    "id": p["id"],
                    "name": name[:128],
                    "lat": f"{p['lat']:.6f}",
                    "lon": f"{p['lon']:.6f}",
                    "country": p["country"][:2],
                    "admin1": (p["admin1"] or "")[:128],
                    "is_primary": "1" if norm == display_norm else "0",
                    "population": str(int(p.get("population") or 0)),
                }
            )
        # Ensure display spelling exists as an alias row even if filtered earlier.
        if display and (p["id"], display_norm) not in seen_alias and len(display_norm) >= 2:
            seen_alias.add((p["id"], display_norm))
            rows.append(
                {
                    "id": p["id"],
                    "name": display[:128],
                    "lat": f"{p['lat']:.6f}",
                    "lon": f"{p['lon']:.6f}",
                    "country": p["country"][:2],
                    "admin1": (p["admin1"] or "")[:128],
                    "is_primary": "1",
                    "population": str(int(p.get("population") or 0)),
                }
            )

    rows.sort(key=lambda r: (r["country"], r["name"]))
    print(f"places={len(places)} aliases={len(rows)} -> {SETTLEMENTS_DUMP}")
    with gzip.open(SETTLEMENTS_DUMP, "wt", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=DUMP_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    size_mb = SETTLEMENTS_DUMP.stat().st_size / (1024 * 1024)
    print(f"done ({size_mb:.1f} MB gzipped)")


if __name__ == "__main__":
    main()
