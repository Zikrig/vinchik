from __future__ import annotations

from pathlib import Path

from aiogram.types import FSInputFile, InputMediaPhoto

from database.models import Profile

LOCAL_PREFIX = "local:"
TEST_PHOTO_REL = "data/test.png"
TEST_PHOTO_MARKER = f"{LOCAL_PREFIX}{TEST_PHOTO_REL}"
MAX_PROFILE_PHOTOS = 3


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def local_photo_path(marker: str) -> Path | None:
    if not marker.startswith(LOCAL_PREFIX):
        return None
    rel = marker[len(LOCAL_PREFIX) :]
    path = (project_root() / rel).resolve()
    root = project_root().resolve()
    if not str(path).startswith(str(root)):
        return None
    return path if path.is_file() else None


def as_photo_input(file_id: str | None) -> FSInputFile | str | None:
    """Telegram file_id or FSInputFile for local: markers."""
    if not file_id:
        return None
    path = local_photo_path(file_id)
    if path is not None:
        return FSInputFile(path)
    if file_id.startswith(LOCAL_PREFIX):
        return None
    return file_id


def profile_photo_ids(profile: Profile | None) -> list[str]:
    if profile is None:
        return []
    raw = getattr(profile, "photo_file_ids", None)
    if isinstance(raw, list) and raw:
        out = [str(x) for x in raw if x][:MAX_PROFILE_PHOTOS]
        if out:
            return out
    if profile.photo_file_id:
        return [profile.photo_file_id]
    return []


def set_profile_photos(profile: Profile, ids: list[str]) -> None:
    clean = [str(x) for x in ids if x][:MAX_PROFILE_PHOTOS]
    profile.photo_file_ids = clean or None
    profile.photo_file_id = clean[0] if clean else None


def media_photos_for_profile(
    profile: Profile, *, caption: str | None = None
) -> list[InputMediaPhoto]:
    media: list[InputMediaPhoto] = []
    for i, fid in enumerate(profile_photo_ids(profile)):
        inp = as_photo_input(fid)
        if inp is None:
            continue
        if i == 0 and caption is not None:
            media.append(InputMediaPhoto(media=inp, caption=caption))
        else:
            media.append(InputMediaPhoto(media=inp))
    return media
