"""Postgres dump and replace. Used by the unlisted admin page."""

from __future__ import annotations

import asyncio
import gzip
import logging
import os
import re
import secrets
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from config import settings

logger = logging.getLogger(__name__)

ARCHIVE_PATH = "/archive"
MAX_UPLOAD_BYTES = 200 * 1024 * 1024
MAX_SQL_BYTES = 512 * 1024 * 1024
# Ниже лимита nginx по умолчанию (1 МБ), вместе с заголовками multipart.
CHUNK_BYTES = 256 * 1024
_MAX_CHUNK_BYTES = 700 * 1024
_DUMP_TIMEOUT_SECONDS = 600
_GZIP_MAGIC = b"\x1f\x8b"


class ArchiveError(Exception):
    pass


def _pg_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PGHOST"] = settings.postgres_host
    env["PGPORT"] = str(settings.postgres_port)
    env["PGUSER"] = settings.postgres_user
    env["PGPASSWORD"] = settings.postgres_password
    env["PGDATABASE"] = _db_name()
    return env


def _db_name() -> str:
    name = settings.postgres_db
    if not name or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_" for c in name):
        raise ArchiveError("Некорректное имя базы")
    return name


def _skip_line(line: bytes) -> bool:
    # COPY rows are tab-separated. Never drop those.
    if b"\t" in line:
        return False
    raw = line.strip().lower()
    # pg_dump 17+ emits this; Postgres 16 rejects it.
    if raw.startswith(b"set transaction_timeout"):
        return True
    # The restore session opens one transaction itself.
    return raw in {b"begin;", b"commit;", b"rollback;", b"start transaction;"}


def dump_filename() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    return f"vinchik-{stamp}.sql.gz"


async def _exec(args: list[str], *, stdin: bytes | None = None) -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            env=_pg_env(),
            stdin=asyncio.subprocess.PIPE if stdin is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise ArchiveError("В образе нет postgresql-client") from exc
    try:
        _out, err = await asyncio.wait_for(
            proc.communicate(stdin),
            timeout=_DUMP_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise ArchiveError("Команда к базе не завершилась за 10 минут") from None
    text = (err or b"").decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        logger.error("db archive command failed: %s", args[0])
        raise ArchiveError(text[-2000:] or f"код {proc.returncode}")
    return text


async def create_dump() -> Path:
    fd, name = tempfile.mkstemp(prefix="vinchik-", suffix=".sql.gz")
    os.close(fd)
    path = Path(name)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _DUMP_TIMEOUT_SECONDS
    try:
        proc = await asyncio.create_subprocess_exec(
            "pg_dump",
            "--format=p",
            "--no-owner",
            "--no-acl",
            "--clean",
            "--if-exists",
            "--encoding=UTF8",
            env=_pg_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        path.unlink(missing_ok=True)
        raise ArchiveError("В образе нет postgresql-client") from exc
    assert proc.stdout is not None and proc.stderr is not None
    err_task = asyncio.create_task(proc.stderr.read())
    written = 0
    try:
        with gzip.open(path, "wb") as out:
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise ArchiveError("Снятие дампа не завершилось за 10 минут")
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=remaining)
                if not line:
                    break
                if _skip_line(line):
                    continue
                written += len(line)
                if written > MAX_SQL_BYTES:
                    raise ArchiveError("Дамп больше 512 МБ")
                out.write(line)
        stderr = await err_task
        await proc.wait()
    except ArchiveError:
        err_task.cancel()
        proc.kill()
        await proc.wait()
        path.unlink(missing_ok=True)
        raise
    except (asyncio.TimeoutError, FileNotFoundError) as exc:
        err_task.cancel()
        proc.kill()
        await proc.wait()
        path.unlink(missing_ok=True)
        if isinstance(exc, FileNotFoundError):
            raise ArchiveError("В образе нет postgresql-client") from exc
        raise ArchiveError("Снятие дампа не завершилось за 10 минут") from exc
    except Exception:
        err_task.cancel()
        proc.kill()
        await proc.wait()
        path.unlink(missing_ok=True)
        raise
    if proc.returncode != 0:
        path.unlink(missing_ok=True)
        text = stderr.decode("utf-8", errors="replace").strip()
        logger.error("pg_dump failed")
        raise ArchiveError(text[-2000:] or f"pg_dump код {proc.returncode}")
    if written == 0:
        path.unlink(missing_ok=True)
        raise ArchiveError("pg_dump вернул пустой файл")
    return path


async def save_upload(chunks) -> Path:
    fd, name = tempfile.mkstemp(prefix="vinchik-in-", suffix=".dump")
    os.close(fd)
    path = Path(name)
    total = 0
    try:
        with path.open("wb") as out:
            async for chunk in chunks:
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    raise ArchiveError("Файл больше 200 МБ")
                out.write(chunk)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    if total == 0:
        path.unlink(missing_ok=True)
        raise ArchiveError("Файл пустой")
    return path


def _upload_dir(upload_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", upload_id):
        raise ArchiveError("Некорректный идентификатор загрузки")
    root = (Path(tempfile.gettempdir()) / "vinchik-archive").resolve()
    path = (root / upload_id).resolve()
    if path.parent != root:
        raise ArchiveError("Некорректный идентификатор загрузки")
    return path


def write_chunk(upload_id: str, index: int, data: bytes) -> str:
    if index < 0:
        raise ArchiveError("Некорректный номер куска")
    if len(data) > _MAX_CHUNK_BYTES:
        raise ArchiveError("Кусок больше 700 КБ")
    if not upload_id:
        if index != 0:
            raise ArchiveError("Кусок не по порядку")
        upload_id = secrets.token_urlsafe(18)
        folder = _upload_dir(upload_id)
        folder.mkdir(parents=True, exist_ok=False)
    else:
        folder = _upload_dir(upload_id)
        if not folder.is_dir():
            raise ArchiveError("Загрузка не найдена")
    existing = sorted(folder.glob("*.part"))
    if index != len(existing):
        raise ArchiveError("Кусок не по порядку")
    (folder / f"{index:06d}.part").write_bytes(data)
    total = sum(part.stat().st_size for part in folder.glob("*.part"))
    if total > MAX_UPLOAD_BYTES:
        shutil.rmtree(folder, ignore_errors=True)
        raise ArchiveError("Файл больше 200 МБ")
    return upload_id


def discard_upload(upload_id: str) -> None:
    if not upload_id:
        return
    folder = _upload_dir(upload_id)
    shutil.rmtree(folder, ignore_errors=True)


def assemble_upload(upload_id: str) -> Path:
    folder = _upload_dir(upload_id)
    if not folder.is_dir():
        raise ArchiveError("Загрузка не найдена")
    parts = sorted(folder.glob("*.part"))
    if not parts:
        shutil.rmtree(folder, ignore_errors=True)
        raise ArchiveError("Файл пустой")
    fd, name = tempfile.mkstemp(prefix="vinchik-in-", suffix=".dump")
    os.close(fd)
    path = Path(name)
    try:
        with path.open("wb") as out:
            for index, part in enumerate(parts):
                if part.name != f"{index:06d}.part":
                    raise ArchiveError("Куски не по порядку")
                with part.open("rb") as src:
                    shutil.copyfileobj(src, out)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    if path.stat().st_size == 0:
        path.unlink(missing_ok=True)
        raise ArchiveError("Файл пустой")
    return path


def _open_sql(path: Path):
    head = path.read_bytes()[:2]
    if head == _GZIP_MAGIC:
        return gzip.open(path, "rb")
    return path.open("rb")


def psql_args(sql: str) -> list[str]:
    return ["psql", "-v", "ON_ERROR_STOP=1", "-c", sql]


def _lock_prefix(db: str) -> bytes:
    # One session holds the only allowed connection for the whole restore,
    # so the bot cannot write between DROP and CREATE.
    return (
        f'ALTER DATABASE "{db}" WITH CONNECTION LIMIT 1;\n'
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        "WHERE datname = current_database() AND pid <> pg_backend_pid();\n"
        "BEGIN;\n"
    ).encode()


async def restore_dump(path: Path) -> None:
    from database.session import engine

    head = path.read_bytes()[:5]
    if head == b"PGDMP":
        raise ArchiveError("Нужен файл .sql.gz с этой страницы")
    await engine.dispose()
    db = _db_name()
    try:
        await _restore_sql(path, db)
    finally:
        try:
            await _exec(psql_args(f'ALTER DATABASE "{db}" WITH CONNECTION LIMIT -1'))
        except ArchiveError:
            logger.exception("failed to reset connection limit")


async def _restore_sql(path: Path, db: str) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _DUMP_TIMEOUT_SECONDS
    try:
        proc = await asyncio.create_subprocess_exec(
            "psql",
            "-v",
            "ON_ERROR_STOP=1",
            env=_pg_env(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise ArchiveError("В образе нет postgresql-client") from exc
    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
    out_task = asyncio.create_task(proc.stdout.read())
    err_task = asyncio.create_task(proc.stderr.read())
    written = 0
    complete = False
    stderr = b""
    try:
        proc.stdin.write(_lock_prefix(db))
        await proc.stdin.drain()
        with _open_sql(path) as src:
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise ArchiveError("Загрузка дампа не завершилась за 10 минут")
                line = src.readline()
                if not line:
                    complete = True
                    break
                if _skip_line(line):
                    continue
                written += len(line)
                if written > MAX_SQL_BYTES:
                    raise ArchiveError("Распакованный дамп больше 512 МБ")
                try:
                    proc.stdin.write(line)
                    await proc.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    break
        if complete:
            try:
                proc.stdin.write(b"COMMIT;\n")
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                complete = False
        proc.stdin.close()
        stderr = await asyncio.wait_for(err_task, timeout=max(1, deadline - loop.time()))
        await out_task
        await asyncio.wait_for(proc.wait(), timeout=max(1, deadline - loop.time()))
    except Exception as exc:
        proc.kill()
        await proc.wait()
        err_task.cancel()
        out_task.cancel()
        if isinstance(exc, ArchiveError):
            raise
        if isinstance(exc, gzip.BadGzipFile):
            raise ArchiveError("Файл не похож на дамп .sql.gz") from exc
        if isinstance(exc, asyncio.TimeoutError):
            raise ArchiveError("Загрузка дампа не завершилась за 10 минут") from exc
        logger.exception("restore failed")
        raise ArchiveError("Не удалось загрузить дамп") from exc
    if written == 0:
        proc.kill()
        await proc.wait()
        raise ArchiveError("В файле нет SQL")
    if proc.returncode != 0:
        text = stderr.decode("utf-8", errors="replace").strip()
        logger.error("psql restore failed")
        raise ArchiveError(text[-2000:] or f"psql код {proc.returncode}")
