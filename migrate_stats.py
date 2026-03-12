"""
migrate_stats.py — перенос данных статистики из SQLite → PostgreSQL.

Запуск: poetry run python migrate_stats.py
"""

import asyncio
import sqlite3
import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent
SQLITE_PATH = BASE_DIR / "stats.sqlite"


async def migrate():
    if not SQLITE_PATH.exists():
        print(f"SQLite база не найдена: {SQLITE_PATH}")
        print("Нечего мигрировать.")
        return

    # Импортируем после загрузки окружения
    from dotenv import load_dotenv
    load_dotenv()

    from app.db.database import async_session_maker, init_db
    from app.db import models  # noqa: F401  — регистрирует модели в Base.metadata

    # Создаём таблицы если их нет
    await init_db()

    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from app.db.models import MyShowsUser, ApiUser, CategoryRequest

    conn = sqlite3.connect(SQLITE_PATH)
    cur = conn.cursor()

    async def insert_batched(db, model, rows, values_fn, conflict_cols, update_col, batch_size=1000):
        total = 0
        for i in range(0, len(rows), batch_size):
            batch = rows[i:i + batch_size]
            stmt = pg_insert(model).values([values_fn(r) for r in batch])
            stmt = stmt.on_conflict_do_update(
                index_elements=conflict_cols,
                set_={update_col: getattr(stmt.excluded, update_col)},
            )
            await db.execute(stmt)
            total += len(batch)
        return total

    async with async_session_maker() as db:

        # ── MyShows users ──────────────────────────────────────────
        cur.execute("SELECT login, date, requests FROM myshows_users")
        rows = cur.fetchall()
        if rows:
            n = await insert_batched(
                db, MyShowsUser, rows,
                lambda r: {"login": r[0], "date": r[1], "requests": r[2]},
                ["login", "date"], "requests",
            )
            print(f"  myshows_users: {n} записей")

        # ── API users ──────────────────────────────────────────────
        cur.execute("SELECT ip, date, requests, country, city, region, flag_emoji FROM api_users")
        rows = cur.fetchall()
        if rows:
            n = await insert_batched(
                db, ApiUser, rows,
                lambda r: {"ip": r[0], "date": r[1], "requests": r[2],
                           "country": r[3], "city": r[4], "region": r[5], "flag_emoji": r[6]},
                ["ip", "date"], "requests",
            )
            print(f"  api_users: {n} записей")

        # ── Category requests ──────────────────────────────────────
        cur.execute("SELECT category, ip, date, requests FROM category_requests")
        rows = cur.fetchall()
        if rows:
            n = await insert_batched(
                db, CategoryRequest, rows,
                lambda r: {"category": r[0], "ip": r[1], "date": r[2], "requests": r[3]},
                ["category", "ip", "date"], "requests",
            )
            print(f"  category_requests: {n} записей")

        await db.commit()

    conn.close()
    print("Миграция завершена.")


if __name__ == "__main__":
    asyncio.run(migrate())
