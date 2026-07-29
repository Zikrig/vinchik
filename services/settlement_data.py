"""Settlement dump path + name normalization (shared by build/import/search)."""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

# Portable dump: copy data/settlements/ with the project to another server.
SETTLEMENTS_DIR = Path(__file__).resolve().parents[1] / "data" / "settlements"
SETTLEMENTS_DUMP = SETTLEMENTS_DIR / "settlements.csv.gz"

# CSV columns in the dump (one row per searchable alias).
DUMP_FIELDS = (
    "id",  # GeoNames id
    "name",
    "lat",
    "lon",
    "country",
    "admin1",
    "is_primary",  # 1 = display_name for the place
)

_PUNCT_RE = re.compile(r"[^\w\s]+", re.UNICODE)
_SPACE_RE = re.compile(r"\s+")


def normalize_name(raw: str) -> str:
    text = unicodedata.normalize("NFKC", (raw or "").strip().lower())
    text = text.replace("ё", "е")
    text = _PUNCT_RE.sub(" ", text)
    text = _SPACE_RE.sub(" ", text).strip()
    return text
