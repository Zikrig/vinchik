"""Patch population (+ optional fcode) into existing settlements.csv.gz via GeoNames cities1000 + TJ/UZ/KG/AF."""

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

from services.settlement_data import DUMP_FIELDS, SETTLEMENTS_DUMP  # noqa: E402

GEONAMES = "https://download.geonames.org/export/dump"
SOURCES = ("cities1000", "TJ", "UZ", "KG", "AF")


def _download_zip_text(name: str) -> str:
    url = f"{GEONAMES}/{name}.zip"
    print(f"download {url}")
    with urlopen(url, timeout=180) as resp:
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

    fields = list(DUMP_FIELDS)
    if "population" not in fields:
        fields.append("population")

    rows: list[dict] = []
    with gzip.open(SETTLEMENTS_DUMP, "rt", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                sid = int(row["id"])
            except (KeyError, ValueError):
                continue
            item = {k: row.get(k, "") for k in DUMP_FIELDS if k != "population"}
            item["population"] = str(pops.get(sid, int(row.get("population") or 0) or 0))
            rows.append(item)

    # rewrite DUMP_FIELDS in settlement_data is updated separately; write with population
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
    tmp = SETTLEMENTS_DUMP.with_suffix(".tmp.gz")
    with gzip.open(tmp, "wt", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=out_fields)
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(SETTLEMENTS_DUMP)
    moscow = pops.get(524901, 0)
    print(f"done rows={len(rows)} moscow_pop={moscow} -> {SETTLEMENTS_DUMP}")


if __name__ == "__main__":
    main()
