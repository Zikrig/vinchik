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
    "population",
)

_PUNCT_RE = re.compile(r"[^\w\s]+", re.UNICODE)
_SPACE_RE = re.compile(r"\s+")
_CYR_RE = re.compile(r"[\u0400-\u04FF]")
_RARE_CYR = set("ъѣѳѷѧѫѯѱѡ҃")
# Rough Cyrillic→Latin for matching GeoNames asciiname (Moscow / Moskva).
_CYR_TO_LAT = str.maketrans(
    {
        "а": "a",
        "б": "b",
        "в": "v",
        "г": "g",
        "д": "d",
        "е": "e",
        "ё": "e",
        "ж": "zh",
        "з": "z",
        "и": "i",
        "й": "i",
        "к": "k",
        "л": "l",
        "м": "m",
        "н": "n",
        "о": "o",
        "п": "p",
        "р": "r",
        "с": "s",
        "т": "t",
        "у": "u",
        "ф": "f",
        "х": "h",
        "ц": "ts",
        "ч": "ch",
        "ш": "sh",
        "щ": "sch",
        "ы": "y",
        "э": "e",
        "ю": "yu",
        "я": "ya",
        "ғ": "g",
        "қ": "q",
        "ҳ": "h",
        "ҷ": "j",
        "ӣ": "i",
        "ӯ": "u",
    }
)


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


def _latin_fold(raw: str) -> str:
    text = normalize_name(raw)
    return text.translate(_CYR_TO_LAT)


def _ascii_aliases(candidates: list[str]) -> list[str]:
    out: list[str] = []
    for n in candidates:
        if not n.isascii() or " " in n or "-" in n:
            continue
        if not n.isalpha():
            continue
        if not (4 <= len(n) <= 12):
            continue
        out.append(n)
    return out


def _twin_quality(ascii_name: str) -> tuple:
    """Higher is better — Title-case names like Moscow / Moskva beat Musko."""
    title = ascii_name[0].isupper() and ascii_name[1:].islower()
    return (
        1 if title else 0,
        1 if 6 <= len(ascii_name) <= 7 else 0,
        -abs(len(ascii_name) - 6),
    )


def pick_display_name(names: list[str] | set[str], fallback: str = "") -> str:
    """Choose a Cyrillic display label when available, else fallback/Latin."""
    candidates = [n.strip() for n in names if n and n.strip()]
    cyr = [n for n in candidates if is_cyrillic_name(n) and 2 <= len(n) <= 48]
    ascii_names = _ascii_aliases(candidates)
    fold_to_ascii: dict[str, list[str]] = {}
    for a in ascii_names:
        fold_to_ascii.setdefault(_latin_fold(a), []).append(a)

    if cyr:
        all_folds = list(fold_to_ascii.keys())

        def _family_size(fold: str) -> int:
            if len(fold) < 4:
                return 0
            prefix = fold[:4]
            return sum(1 for f in all_folds if f.startswith(prefix))

        def _key(n: str) -> tuple:
            letters = "".join(c for c in n if c.isalpha())
            pure = 1 if letters and all(_CYR_RE.match(c) for c in letters) else 0
            rare = sum(1 for c in n.lower() if c in _RARE_CYR)
            length_pen = 0 if 4 <= len(n) <= 12 else abs(len(n) - 8)
            fold = _latin_fold(n)
            twins = fold_to_ascii.get(fold, [])
            if twins:
                tq = max(_twin_quality(t) for t in twins)
                has_twin = 1
            else:
                tq = (0, 0, -99)
                has_twin = 0
            family = _family_size(fold)
            # Ascending: lower is better.
            # Exact latin twin + largest latin-name family (mosk* >> mask* / musk*).
            return (
                -has_twin,
                -family,
                tuple(-x for x in tq),
                -pure,
                rare,
                length_pen,
                len(n),
                n,
            )

        cyr.sort(key=_key)
        return cyr[0][:128]
    if fallback.strip():
        return fallback.strip()[:128]
    return (candidates[0][:128] if candidates else "")
