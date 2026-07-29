"""CLI: python scripts/import_settlements.py"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    from database.session import async_session_maker, init_db
    from services.settlements_import import import_settlements_from_dump

    await init_db()
    async with async_session_maker() as session:
        n = await import_settlements_from_dump(session, replace=True)
        print(f"imported alias rows: {n}")


if __name__ == "__main__":
    asyncio.run(main())
