"""Settlement dump path + name normalization (shared by build/import/search)."""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

# Portable dump: copy data/settlements/ with the project to another server.
SETTLEMENTS_DIR = Path(__file__).resolve().parents[1] / "data" / "settlements"
SETTLEMENTS_DUMP = SETTLEMENTS_DIR / "settlements.csv.gz"

# CSV columns in the dump (one row per searchable alias).
# All alias rows are kept for SEARCH; only is_primary / pick_display_name
# decide what users see as the modern place title.
DUMP_FIELDS = (
    "id",  # GeoNames id
    "name",
    "lat",
    "lon",
    "country",
    "admin1",
    "is_primary",  # 1 = modern display_name for the place
    "population",
)

_PUNCT_RE = re.compile(r"[^\w\s]+", re.UNICODE)
_SPACE_RE = re.compile(r"\s+")
_CYR_RE = re.compile(r"[\u0400-\u04FF]")
_RARE_CYR = set("ъѣѳѷѧѫѯѱѡ҃")

# Historical / obsolete titles — still searchable, never shown when a modern
# Cyrillic alternative exists for the same place.
_NOT_FOR_DISPLAY = {
    "петроград",
    "ленинград",
    "сталинград",
    "свердловск",
    "горький",
    "куйбышев",
    "калинин",
    "орджоникидзе",
    "фрунзе",
    "устинов",
    "андропов",
    "брежнев",
    "черненко",
    "муско",
    "москова",
    "москъва",
    "маскав",
    "масква",
}

# Rough Cyrillic→Latin for matching GeoNames official Latin titles.
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
    return normalize_name(raw).translate(_CYR_TO_LAT)


def _ascii_titles(candidates: list[str]) -> list[str]:
    out: list[str] = []
    for n in candidates:
        if not n.isascii():
            continue
        cleaned = _SPACE_RE.sub(" ", n.replace("-", " ").replace(".", " ")).strip()
        if not cleaned or not all(c.isalpha() or c.isspace() for c in cleaned):
            continue
        letters = cleaned.replace(" ", "")
        if not (4 <= len(letters) <= 40) or len(cleaned) > 28:
            continue
        if "lungsod" in cleaned.lower() or " ng " in cleaned.lower():
            continue
        out.append(n)
    return out


def pick_display_name(names: list[str] | set[str], fallback: str = "") -> str:
    """Modern display label for UI.

    Aliases (including historical names) stay in the dump for SEARCH only.
    Display prefers a modern Cyrillic spelling that matches the GeoNames
    official ``name`` (``fallback``), never Петроград/Ленинград/Муско when a
    better option exists.
    """
    candidates = [n.strip() for n in names if n and n.strip()]
    cyr_all = [n for n in candidates if is_cyrillic_name(n) and 2 <= len(n) <= 48]
    modern = [n for n in cyr_all if normalize_name(n) not in _NOT_FOR_DISPLAY]
    cyr = modern or cyr_all

    official = (fallback or "").strip()
    official_fold = _latin_fold(official) if official else ""
    # Official GeoNames title first, then other Latin titles (for fold matching).
    refs: list[str] = []
    if official_fold and len(official_fold) >= 4:
        refs.append(official_fold)
    for n in sorted(_ascii_titles(candidates), key=lambda s: (-len(s), s))[:10]:
        fold = _latin_fold(n)
        if fold and fold not in refs:
            refs.append(fold)

    if cyr:
        def _key(n: str) -> tuple:
            letters = "".join(c for c in n if c.isalpha())
            pure = 1 if letters and all(_CYR_RE.match(c) for c in letters) else 0
            rare = sum(1 for c in n.lower() if c in _RARE_CYR)
            length_pen = 0 if 3 <= len(n) <= 32 else abs(len(n) - 12)
            fold = _latin_fold(n)
            near_official = (
                SequenceMatcher(None, fold, official_fold).ratio() if official_fold else 0.0
            )
            near_any = max(
                (SequenceMatcher(None, fold, r).ratio() for r in refs),
                default=0.0,
            )
            # Ascending: lower is better.
            return (
                -round(near_official, 2),
                -round(near_any, 2),
                -pure,
                rare,
                length_pen,
                -len(n) if near_official >= 0.75 or near_any >= 0.85 else len(n),
                n,
            )

        cyr.sort(key=_key)
        chosen = cyr[0]
        dnorm = normalize_name(chosen)
        for n in cyr:
            if normalize_name(n) == dnorm and "-" in n:
                chosen = n
                break
        else:
            if chosen.startswith("Санкт ") and "-" not in chosen:
                chosen = "Санкт-" + chosen[len("Санкт ") :]
        return chosen[:128]

    if official:
        return official[:128]
    return (candidates[0][:128] if candidates else "")
