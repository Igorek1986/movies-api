"""
Миграция: создаёт таблицу episodes и добавляет вспомогательные колонки
в media_cards (imdb_id, myshows_show_id, episodes_synced_at).

Идемпотентна — безопасно запускать повторно на уже обновлённой БД.

Запуск:
    # Локально:
    poetry run python migrations/migrate_episodes.py

    # Docker:
    docker compose exec app poetry run python migrations/migrate_episodes.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from sqlalchemy import text
from app.db.database import async_session_maker, init_db


async def main():
    await init_db()
    async with async_session_maker() as session:
        # 1. Таблица episodes
        await session.execute(text("""
            CREATE TABLE IF NOT EXISTS episodes (
                tmdb_show_id  INTEGER      NOT NULL,
                season        SMALLINT     NOT NULL,
                episode       SMALLINT     NOT NULL,
                title         VARCHAR(500) NULL,
                duration_sec  INTEGER      NULL,
                is_special    BOOLEAN      NOT NULL DEFAULT false,
                myshows_ep_id INTEGER      NULL,
                hash          VARCHAR(20)  NULL,
                air_date      DATE         NULL,
                PRIMARY KEY (tmdb_show_id, season, episode)
            );
        """))
        await session.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_episodes_tmdb_hash
            ON episodes (tmdb_show_id, hash);
        """))

        # 2. Колонки media_cards
        await session.execute(text("""
            ALTER TABLE media_cards
            ADD COLUMN IF NOT EXISTS imdb_id            VARCHAR(20)  NULL,
            ADD COLUMN IF NOT EXISTS kinopoisk_id       INTEGER      NULL,
            ADD COLUMN IF NOT EXISTS myshows_show_id    INTEGER      NULL,
            ADD COLUMN IF NOT EXISTS episodes_synced_at TIMESTAMPTZ  NULL;
        """))

        # 3. Апгрейд существующих установок: duration_sec SMALLINT → INTEGER
        await session.execute(text("""
            ALTER TABLE episodes
            ALTER COLUMN duration_sec TYPE INTEGER;
        """))

        # 4. Добавить hash и air_date если таблица уже существовала без них
        await session.execute(text("""
            ALTER TABLE episodes
            ADD COLUMN IF NOT EXISTS hash     VARCHAR(20) NULL,
            ADD COLUMN IF NOT EXISTS air_date DATE        NULL;
        """))

        await session.commit()
    print("Done: episodes table and media_cards columns ready")


if __name__ == "__main__":
    asyncio.run(main())
