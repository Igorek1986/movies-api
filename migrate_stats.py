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

    async with async_session_maker() as db:

        # ── MyShows users ──────────────────────────────────────────
        cur.execute("SELECT login, date, requests FROM myshows_users")
        rows = cur.fetchall()
        if rows:
            stmt = pg_insert(MyShowsUser).values([
                {"login": r[0], "date": r[1], "requests": r[2]} for r in rows
            ])
            stmt = stmt.on_conflict_do_update(
                index_elements=["login", "date"],
                set_={"requests": stmt.excluded.requests},
            )
            await db.execute(stmt)
            print(f"  myshows_users: {len(rows)} записей")

        # ── API users ──────────────────────────────────────────────
        cur.execute("SELECT ip, date, requests, country, city, region, flag_emoji FROM api_users")
        rows = cur.fetchall()
        if rows:
            stmt = pg_insert(ApiUser).values([
                {
                    "ip": r[0], "date": r[1], "requests": r[2],
                    "country": r[3], "city": r[4], "region": r[5], "flag_emoji": r[6],
                }
                for r in rows
            ])
            stmt = stmt.on_conflict_do_update(
                index_elements=["ip", "date"],
                set_={"requests": stmt.excluded.requests},
            )
            await db.execute(stmt)
            print(f"  api_users: {len(rows)} записей")

        # ── Category requests ──────────────────────────────────────
        cur.execute("SELECT category, ip, date, requests FROM category_requests")
        rows = cur.fetchall()
        if rows:
            stmt = pg_insert(CategoryRequest).values([
                {"category": r[0], "ip": r[1], "date": r[2], "requests": r[3]} for r in rows
            ])
            stmt = stmt.on_conflict_do_update(
                index_elements=["category", "ip", "date"],
                set_={"requests": stmt.excluded.requests},
            )
            await db.execute(stmt)
            print(f"  category_requests: {len(rows)} записей")

        await db.commit()

    conn.close()
    print("Миграция завершена.")


if __name__ == "__main__":
    asyncio.run(migrate())
