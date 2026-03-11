"""
seed_test_data.py — заполняет аккаунт тестовыми устройствами, профилями и таймкодами.

Запуск:
    poetry run python seed_test_data.py --username igorek1986
    poetry run python seed_test_data.py --username igorek1986 --devices 8 --profiles 8 --timecodes 10000
    poetry run python seed_test_data.py --username igorek1986 --clean   # удалить тестовые данные
"""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))


async def seed(username: str, n_devices: int, n_profiles: int, n_timecodes: int, clean: bool):
    import app.db.models  # noqa: F401 — регистрирует модели в Base.metadata
    from app.db.database import async_session_maker
    from app.db.models import User, Device, LampaProfile, Timecode
    from app.utils import generate_profile_api_key
    from sqlalchemy import select, insert, delete

    async with async_session_maker() as db:
        # Найти пользователя
        user = (await db.execute(select(User).where(User.username == username))).scalar_one_or_none()
        if not user:
            print(f"Пользователь '{username}' не найден.")
            sys.exit(1)
        print(f"Пользователь: {user.username} (id={user.id}, role={user.role})")

        # Найти существующие тестовые устройства
        existing = (await db.execute(
            select(Device).where(
                Device.user_id == user.id,
                Device.name.like("Test%"),
            )
        )).scalars().all()

        if clean:
            if not existing:
                print("Тестовых устройств не найдено, нечего удалять.")
                return
            ids = [d.id for d in existing]
            await db.execute(delete(Device).where(Device.id.in_(ids)))
            await db.commit()
            print(f"Удалено {len(ids)} тестовых устройств (каскадно: профили, таймкоды).")
            return

        if existing:
            print(f"Уже существует {len(existing)} тестовых устройств. Используйте --clean для удаления.")
            sys.exit(1)

        total = n_devices * n_profiles * n_timecodes
        print(f"Устройств: {n_devices}  ·  Профилей на устройство: {n_profiles}  ·  Таймкодов на профиль: {n_timecodes:,}")
        print(f"Итого таймкодов: {total:,}")
        print()

        t_start = time.monotonic()

        for di in range(1, n_devices + 1):
            device = Device(user_id=user.id, name=f"Test{di}", token=generate_profile_api_key())
            db.add(device)
            await db.flush()  # получить device.id

            for pi in range(1, n_profiles + 1):
                profile_id = f"testprof-{device.id}-{pi}"
                db.add(LampaProfile(device_id=device.id, lampa_profile_id=profile_id, name=f"Test{pi}"))
                await db.flush()

                # Вставляем таймкоды батчами по 1000 строк
                batch_size = 1000
                for batch_start in range(0, n_timecodes, batch_size):
                    batch_end = min(batch_start + batch_size, n_timecodes)
                    rows = [
                        {
                            "device_id": device.id,
                            "lampa_profile_id": profile_id,
                            "card_id": f"{batch_start + j + 1}_movie",
                            "item": "0",
                            "data": json.dumps({
                                "duration": 7200,
                                "time": (batch_start + j) * 2 % 7200,
                                "percent": (batch_start + j) % 100,
                            }),
                        }
                        for j in range(batch_end - batch_start)
                    ]
                    await db.execute(insert(Timecode).values(rows))

                done = (di - 1) * n_profiles * n_timecodes + (pi - 1) * n_timecodes + n_timecodes
                elapsed = time.monotonic() - t_start
                speed = done / elapsed if elapsed > 0 else 0
                eta = (total - done) / speed if speed > 0 else 0
                print(
                    f"  Устройство {di}/{n_devices}  Профиль {pi}/{n_profiles}  "
                    f"[{done:,}/{total:,}]  {speed:,.0f} тк/с  ETA {eta:.0f}с      ",
                    end="\r",
                )

            await db.commit()  # коммитим после каждого устройства

        elapsed = time.monotonic() - t_start
        print(f"\n\nГотово за {elapsed:.1f}с. Вставлено {total:,} таймкодов.")


def main():
    parser = argparse.ArgumentParser(description="Заполнить БД тестовыми данными")
    parser.add_argument("--username",  required=True, help="Имя пользователя")
    parser.add_argument("--devices",   type=int, default=8,     help="Количество устройств (default: 8)")
    parser.add_argument("--profiles",  type=int, default=8,     help="Профилей на устройство (default: 8)")
    parser.add_argument("--timecodes", type=int, default=10000, help="Таймкодов на профиль (default: 10000)")
    parser.add_argument("--clean",     action="store_true",     help="Удалить тестовые данные (Test*) и выйти")
    args = parser.parse_args()

    asyncio.run(seed(args.username, args.devices, args.profiles, args.timecodes, args.clean))


if __name__ == "__main__":
    main()
