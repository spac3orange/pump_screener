#!/usr/bin/env python3
"""Rehearse an Alembic upgrade on a *copy* of a database and prove existing data is untouched.

1. Refuses unless the database name contains "staging", "rehearsal" or "test".
2. Fingerprints every table that exists before the upgrade (row count + md5 of all rows over
   the pre-upgrade columns).
3. ``alembic upgrade head``; fingerprints must be identical.
4. With ``--roundtrip``: downgrade to the starting revision, compare again, upgrade again.

    export DATABASE_URL=postgresql+asyncpg://.../screener_rehearsal
    python scripts/rehearse_migration.py --roundtrip
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command

SAFE_MARKERS = ("staging", "rehearsal", "test")


def _alembic(url: str, action: str, revision: str) -> None:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", url)
    getattr(command, action)(cfg, revision)


async def _revision(url: str) -> str | None:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            exists = (await conn.execute(text("SELECT to_regclass('alembic_version')"))).scalar()
            if exists is None:
                return None
            return (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar()
    finally:
        await engine.dispose()


Columns = dict[str, list[str]]


async def _columns(url: str) -> Columns:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            rows = await conn.execute(
                text(
                    "SELECT table_name, column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name <> 'alembic_version' "
                    "ORDER BY table_name, ordinal_position"
                )
            )
            cols: Columns = {}
            for table, column in rows:
                cols.setdefault(table, []).append(column)
            return cols
    finally:
        await engine.dispose()


async def _fingerprints(url: str, columns: Columns) -> dict[str, tuple[int, str]]:
    """Row count + md5 over the given (pre-upgrade) columns, so added columns don't count."""
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            result: dict[str, tuple[int, str]] = {}
            for table, cols in columns.items():
                row_expr = "row(" + ", ".join(f't."{c}"' for c in cols) + ")::text"
                q = text(
                    f"SELECT count(*), coalesce(md5(string_agg({row_expr}, '|' "
                    f"ORDER BY {row_expr})), '') FROM \"{table}\" t"
                )
                count, digest = (await conn.execute(q)).one()
                result[table] = (int(count), digest)
            return result
    finally:
        await engine.dispose()


def _compare(label: str, before: dict, after: dict) -> bool:
    ok = True
    for table, fp in before.items():
        if after.get(table) != fp:
            ok = False
            print(f"  CHANGED {table}: {fp} -> {after.get(table)}")
    if label == "after downgrade" and set(after) != set(before):
        ok = False
        print(f"  LEFTOVER tables: {sorted(set(after) - set(before))}")
    print(f"{label}: {'OK' if ok else 'FAILED'} ({len(before)} pre-existing tables compared)")
    return ok


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--roundtrip", action="store_true")
    args = parser.parse_args()
    url = os.environ.get("DATABASE_URL", "")
    db_name = url.rsplit("/", 1)[-1].split("?", 1)[0]
    if not any(marker in db_name for marker in SAFE_MARKERS):
        print(
            f"Refusing: database '{db_name}' is not a staging/rehearsal/test copy", file=sys.stderr
        )
        return 2

    start_rev = await _revision(url)
    print(f"database={db_name} start_revision={start_rev}")
    columns = await _columns(url)
    before = await _fingerprints(url, columns)
    for table, (count, _) in before.items():
        print(f"  {table}: {count} rows")

    t0 = time.perf_counter()
    await asyncio.to_thread(_alembic, url, "upgrade", "head")
    print(f"upgrade head took {time.perf_counter() - t0:.2f}s, revision={await _revision(url)}")
    ok = _compare("after upgrade", before, await _fingerprints(url, columns))

    if args.roundtrip and start_rev is not None:
        await asyncio.to_thread(_alembic, url, "downgrade", start_rev)
        after_down = await _columns(url)
        ok &= _compare("after downgrade", before, await _fingerprints(url, after_down))
        if after_down != columns:
            ok = False
            print("  schema after downgrade differs from the starting schema")
        await asyncio.to_thread(_alembic, url, "upgrade", "head")
        ok &= _compare("after re-upgrade", before, await _fingerprints(url, columns))
        print(f"final revision={await _revision(url)}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
