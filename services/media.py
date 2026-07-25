from __future__ import annotations

from pathlib import Path

from aiogram.types import FSInputFile

LOCAL_PREFIX = "local:"
TEST_PHOTO_REL = "data/test.png"
TEST_PHOTO_MARKER = f"{LOCAL_PREFIX}{TEST_PHOTO_REL}"


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
