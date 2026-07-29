"""Patch population into existing settlements.csv.gz via GeoNames TJ + RU dumps."""

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

from services.settlement_data import SETTLEMENTS_DUMP  # noqa: E402

GEONAMES = "https://download.geonames.org/export/dump"
SOURCES = ("TJ", "RU")


def _download_zip_text(name: str) -> str:
    url = f"{GEONAMES}/{name}.zip"
    print(f"download {url}")
    with urlopen(url, timeout=600) as resp:
        data = resp.read()
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        members = [n for n in zf.namelist() if n.endswith(".txt") and "readme" not in n.lower()]
        return zf.read(members[0]).decode("utf-8", errors="replace")


def _populations(raw: str) -> dict[int, int]:
    out: dict[int, int] = {}
    for line in raw.splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 15:
            continue
        try:
            gid = int(parts[0])
            pop = int(parts[14] or 0)
        except ValueError:
            continue
        if pop <= 0:
            continue
        prev = out.get(gid, 0)
        if pop > prev:
            out[gid] = pop
    return out


def main() -> None:
    if not SETTLEMENTS_DUMP.is_file():
        raise SystemExit(f"missing {SETTLEMENTS_DUMP}")

    pops: dict[int, int] = {}
    for src in SOURCES:
        pops.update(_populations(_download_zip_text(src)))
    print(f"population ids={len(pops)}")

    out_fields = (
        "id",
        "name",
        "lat",
        "lon",
        "country",
        "admin1",
        "is_primary",
        "population",
    )
    rows: list[dict] = []
    kept = 0
    skipped = 0
    with gzip.open(SETTLEMENTS_DUMP, "rt", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            cc = (row.get("country") or "").upper()
            if cc not in {"TJ", "RU"}:
                skipped += 1
                continue
            try:
                sid = int(row["id"])
            except (KeyError, ValueError):
                continue
            kept += 1
            rows.append(
                {
                    "id": row["id"],
                    "name": row.get("name", ""),
                    "lat": row.get("lat", ""),
                    "lon": row.get("lon", ""),
                    "country": cc,
                    "admin1": row.get("admin1", ""),
                    "is_primary": row.get("is_primary", "0"),
                    "population": str(pops.get(sid, int(row.get("population") or 0) or 0)),
                }
            )

    tmp = SETTLEMENTS_DUMP.with_suffix(".tmp.gz")
    with gzip.open(tmp, "wt", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=out_fields)
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(SETTLEMENTS_DUMP)
    print(
        f"done kept_aliases={kept} skipped_other_countries={skipped} "
        f"moscow_pop={pops.get(524901, 0)} -> {SETTLEMENTS_DUMP}"
    )


if __name__ == "__main__":
    main()
