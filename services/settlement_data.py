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
_CYR_RE = re.compile(r"[\u0400-\u04FF]")


def normalize_name(raw: str) -> str:
    text = unicodedata.normalize("NFKC", (raw or "").strip().lower())
    text = text.replace("ё", "е")
    text = _PUNCT_RE.sub(" ", text)
    text = _SPACE_RE.sub(" ", text).strip()
    return text


def cyrillic_ratio(raw: str) -> float:
    letters = [c for c in (raw or "") if c.isalpha()]
    if not letters:
        return 0.0
    cyr = sum(1 for c in letters if _CYR_RE.match(c))
    return cyr / len(letters)


def is_cyrillic_name(raw: str) -> bool:
    """Prefer Russian / Tajik Cyrillic spellings over Latin GeoNames titles."""
    return cyrillic_ratio(raw) >= 0.5


def pick_display_name(names: list[str] | set[str], fallback: str = "") -> str:
    """Choose a Cyrillic display label when available, else fallback/Latin."""
    candidates = [n.strip() for n in names if n and n.strip()]
    cyr = [n for n in candidates if is_cyrillic_name(n) and 2 <= len(n) <= 64]
    if cyr:
        cyr.sort(key=lambda n: (-cyrillic_ratio(n), len(n), n))
        return cyr[0][:128]
    if fallback.strip():
        return fallback.strip()[:128]
    return (candidates[0][:128] if candidates else "")
