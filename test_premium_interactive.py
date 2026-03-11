"""
test_premium_interactive.py — интерактивный тест flow Premium с реальными Telegram-уведомлениями.

  1. Заполняет БД тестовыми данными (если ещё нет)
  2. Устанавливает premium_until = сейчас + 2 дня
  3. Эмулирует проверку в день предупреждения → Telegram-уведомление
  4. Ждёт подтверждения от вас
  5. Эмулирует проверку после истечения → роль → simple, grace установлен
  6. Ждёт подтверждения
  7. Эмулирует проверку после grace → очистка данных
  8. Показывает итог

Запуск:
    poetry run python test_premium_interactive.py --username igorek1986
"""

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

FMT     = "%d.%m.%Y %H:%M UTC"
N_DEV   = 8
N_PROF  = 8
N_TC    = 500   # меньше чем в seed_test_data, чтобы быстро наполнить


def ts(dt: datetime) -> str:
    return dt.strftime(FMT)


def sep(title: str = "", char: str = "─"):
    w = 64
    print(f"\n{char * w}")
    if title:
        print(f"  {title}")
        print(char * w)


def ask(prompt: str) -> bool:
    """Возвращает True = продолжать, False = прервать."""
    while True:
        ans = input(f"\n  {prompt}\n  [Enter = продолжить / n = прервать]: ").strip().lower()
        if ans == "":
            return True
        if ans in ("n", "no", "н", "нет"):
            return False


async def get_state(db, user_id: int) -> dict:
    from app.db.models import Device, Timecode, LampaProfile
    from sqlalchemy import select, func

    devs = (await db.execute(
        select(Device.id, Device.name)
        .where(Device.user_id == user_id)
        .order_by(Device.created_at)
    )).all()

    dev_ids = [r.id for r in devs]

    tc_total = (await db.execute(
        select(func.count()).select_from(Timecode).where(Timecode.device_id.in_(dev_ids))
    )).scalar() or 0 if dev_ids else 0

    prof_total = (await db.execute(
        select(func.count()).select_from(LampaProfile).where(LampaProfile.device_id.in_(dev_ids))
    )).scalar() or 0 if dev_ids else 0

    return {
        "devices": [(r.id, r.name) for r in devs],
        "dev_count": len(devs),
        "prof_total": prof_total,
        "tc_total": tc_total,
    }


def print_state(label: str, user, state: dict, limits: dict | None = None):
    grace  = user.timecode_grace_until.strftime("%d.%m.%Y") if user.timecode_grace_until else "—"
    puntil = user.premium_until.strftime("%d.%m.%Y") if user.premium_until else "—"
    names  = [n for _, n in state["devices"]]
    print(f"\n  ┌─ {label}")
    print(f"  │  Роль:       {user.role}")
    print(f"  │  Premium до: {puntil}  │  Grace до: {grace}")
    print(f"  │  Устройств:  {state['dev_count']}", end="")
    if limits:
        print(f"  (лимит: {limits['dev']})", end="")
    print(f"  → {names}")
    print(f"  │  Профилей:  {state['prof_total']}", end="")
    if limits:
        print(f"  (лимит: {limits['prof']} / устр.)", end="")
    print()
    print(f"  │  Таймкодов: {state['tc_total']:,}", end="")
    if limits:
        print(f"  (лимит: {limits['tc']:,} / профиль)", end="")
    print(f"\n  └{'─' * 50}")


async def ensure_test_data(db, user, n_dev: int, n_prof: int, n_tc: int) -> int:
    """Возвращает количество тестовых устройств (создаёт если нет)."""
    from app.db.models import Device, LampaProfile, Timecode
    from app.utils import generate_profile_api_key
    from sqlalchemy import select, insert

    existing = (await db.execute(
        select(Device).where(Device.user_id == user.id, Device.name.like("Test%"))
    )).scalars().all()

    if existing:
        print(f"  ✅ Тестовые данные уже есть: {len(existing)} устройств.")
        return len(existing)

    print(f"  Создаём {n_dev} устройств × {n_prof} профилей × {n_tc:,} таймкодов...", end="", flush=True)
    t0 = time.monotonic()

    for di in range(1, n_dev + 1):
        device = Device(user_id=user.id, name=f"Test{di}", token=generate_profile_api_key())
        db.add(device)
        await db.flush()

        for pi in range(1, n_prof + 1):
            pid = f"testprof-{device.id}-{pi}"
            db.add(LampaProfile(device_id=device.id, lampa_profile_id=pid, name=f"Test{pi}"))
            await db.flush()

            for b in range(0, n_tc, 1000):
                end = min(b + 1000, n_tc)
                rows = [
                    {
                        "device_id": device.id,
                        "lampa_profile_id": pid,
                        "card_id": f"{b + j + 1}_movie",
                        "item": "0",
                        "data": json.dumps({"duration": 7200, "time": (b + j) * 2 % 7200, "percent": (b + j) % 100}),
                    }
                    for j in range(end - b)
                ]
                await db.execute(insert(Timecode).values(rows))

        await db.commit()

    elapsed = time.monotonic() - t0
    total = n_dev * n_prof * n_tc
    print(f" готово за {elapsed:.1f}с ({total:,} тк).")
    return n_dev


async def run(username: str):
    import app.db.models  # noqa: F401
    from app.db.database import async_session_maker
    from app.db.models import User, TelegramUser
    from app.config import get_settings
    from app.bot import init_bot, get_bot
    from app import settings_cache
    from app.tasks import run_premium_expiry_check
    from sqlalchemy import select

    # Инициализируем бот для отправки реальных уведомлений
    settings = get_settings()
    bot = None
    if settings.TELEGRAM_BOT_TOKEN:
        bot, _ = init_bot(settings.TELEGRAM_BOT_TOKEN)
        print("  🤖 Telegram бот инициализирован.")
    else:
        print("  ⚠️  TELEGRAM_BOT_TOKEN не задан — уведомления не будут отправлены.")

    grace_days = settings_cache.get_int("timecode_grace_days")
    s_dev  = settings_cache.get_int("simple_device_limit")
    s_prof = settings_cache.get_int("simple_profile_limit")
    s_tc   = settings_cache.get_int("simple_timecode_limit")
    limits = {"dev": s_dev, "prof": s_prof, "tc": s_tc}

    # ── Найти пользователя ────────────────────────────────────────────────────
    async with async_session_maker() as db:
        user = (await db.execute(select(User).where(User.username == username))).scalar_one_or_none()
        if not user:
            print(f"Пользователь '{username}' не найден.")
            sys.exit(1)

        tg = (await db.execute(
            select(TelegramUser).where(TelegramUser.user_id == user.id)
        )).scalar_one_or_none()

    sep("ИНТЕРАКТИВНЫЙ ТЕСТ PREMIUM EXPIRY", "═")
    print(f"  Пользователь:  {username}")
    print(f"  Telegram:      {'@' + tg.username if tg and tg.username else 'привязан' if tg else '❌ не привязан'}")
    print(f"  Grace days:    {grace_days}")
    print(f"  Лимиты Simple: {s_dev} устр. / {s_prof} проф. / {s_tc:,} тк")

    if not tg:
        print("\n  ⚠️  Telegram не привязан — уведомления не придут.")
        if not ask("Продолжить без уведомлений?"):
            return

    # ── Временная шкала ───────────────────────────────────────────────────────
    now_real      = datetime.now(timezone.utc)
    premium_until = (now_real + timedelta(days=2)).replace(hour=23, minute=59, second=59, microsecond=0)
    t_warn        = premium_until - timedelta(days=2)
    t_expired     = premium_until + timedelta(seconds=1)
    t_grace_end   = premium_until + timedelta(days=grace_days, seconds=1)

    sep("Временная шкала")
    print(f"  premium_until = {ts(premium_until)}")
    print(f"  ШАГ 1 (warn)  = {ts(t_warn)}")
    print(f"  ШАГ 2 (exp.)  = {ts(t_expired)}")
    print(f"  ШАГ 3 (grace) = {ts(t_grace_end)}")

    # ── Подготовка: заполнить БД + сбросить состояние ────────────────────────
    sep("Подготовка")
    async with async_session_maker() as db:
        user = (await db.execute(select(User).where(User.username == username))).scalar_one()
        await ensure_test_data(db, user, N_DEV, N_PROF, N_TC)

        user.role = "premium"
        user.premium_until = premium_until
        user.premium_warned = False
        user.timecode_grace_until = None
        user.notify_premium_after = None
        await db.commit()
        await db.refresh(user)
        state0 = await get_state(db, user.id)

    print_state("Начальное состояние", user, state0, limits)

    # ══════════════════════════════════════════════════════════════════════════
    sep(f"ШАГ 1 / Предупреждение  [{ts(t_warn)}]", "─")
    print("  Ожидаем уведомление: «Premium истекает DD.MM»")
    if not ask("Запустить проверку?"):
        return

    await run_premium_expiry_check(_now=t_warn)

    async with async_session_maker() as db:
        user = (await db.execute(select(User).where(User.username == username))).scalar_one()
        state1 = await get_state(db, user.id)

    print_state("После шага 1", user, state1)
    if user.premium_warned:
        print("  ✅ premium_warned = True → уведомление отправлено")
    else:
        print("  ❌ Уведомление НЕ было отправлено (premium_warned = False)")

    if not ask("Telegram-уведомление получено? Продолжить?"):
        print("\n  ❌ Тест прерван.")
        return

    # ══════════════════════════════════════════════════════════════════════════
    sep(f"ШАГ 2 / Premium истёк  [{ts(t_expired)}]", "─")
    print("  Ожидаем: роль → simple, grace установлен, уведомление «истёк»")
    if not ask("Запустить проверку?"):
        return

    await run_premium_expiry_check(_now=t_expired)

    async with async_session_maker() as db:
        user = (await db.execute(select(User).where(User.username == username))).scalar_one()
        state2 = await get_state(db, user.id)

    print_state("После шага 2", user, state2, limits)

    ok = user.role == "simple" and user.timecode_grace_until is not None
    print(f"  {'✅' if user.role == 'simple' else '❌'} Роль: {user.role}")
    print(f"  {'✅' if user.timecode_grace_until else '❌'} Grace: {ts(user.timecode_grace_until) if user.timecode_grace_until else '—'}")
    print(f"  📊 Данные НЕ тронуты: {state2['dev_count']} устр., {state2['tc_total']:,} тк")

    if not ok:
        print("\n  ❌ Что-то пошло не так — тест прерван.")
        return

    if not ask("Telegram-уведомление получено? Запустить очистку?"):
        print("\n  ❌ Тест прерван.")
        return

    # ══════════════════════════════════════════════════════════════════════════
    sep(f"ШАГ 3 / Grace истёк — очистка  [{ts(t_grace_end)}]", "─")
    print(f"  Ожидаем: удаление устройств > {s_dev}, профилей > {s_prof}/устр., тк > {s_tc:,}/профиль")

    await run_premium_expiry_check(_now=t_grace_end)

    async with async_session_maker() as db:
        user = (await db.execute(select(User).where(User.username == username))).scalar_one()
        state3 = await get_state(db, user.id)

    print_state("После шага 3", user, state3, limits)

    d_dev  = state2["dev_count"]  - state3["dev_count"]
    d_prof = state2["prof_total"] - state3["prof_total"]
    d_tc   = state2["tc_total"]   - state3["tc_total"]

    sep("Итог очистки")
    print(f"  {'✅' if d_dev  > 0 else '➖'} Удалено устройств:  {d_dev}   (осталось {state3['dev_count']} из {state2['dev_count']})")
    print(f"  {'✅' if d_prof > 0 else '➖'} Удалено профилей:   {d_prof}   (осталось {state3['prof_total']} из {state2['prof_total']})")
    print(f"  {'✅' if d_tc   > 0 else '➖'} Удалено таймкодов:  {d_tc:,}  (осталось {state3['tc_total']:,} из {state2['tc_total']:,})")
    print(f"  {'✅' if user.timecode_grace_until is None else '❌'} grace_until сброшен")

    sep("Тест завершён — очистка тестовых данных?", "═")
    if ask("Удалить тестовые устройства (Test1–Test8)?"):
        from app.db.models import Device
        from sqlalchemy import delete
        async with async_session_maker() as db:
            user = (await db.execute(select(User).where(User.username == username))).scalar_one()
            result = await db.execute(
                delete(Device).where(Device.user_id == user.id, Device.name.like("Test%"))
            )
            user.role = "premium"
            user.premium_until = None
            user.premium_warned = False
            await db.commit()
        print(f"  🗑  Тестовые данные удалены. Роль сброшена на premium.")
    else:
        print("  Тестовые данные оставлены.")

    if bot:
        await bot.session.close()


def main():
    parser = argparse.ArgumentParser(description="Интерактивный тест Premium expiry flow")
    parser.add_argument("--username", required=True)
    args = parser.parse_args()
    asyncio.run(run(args.username))


if __name__ == "__main__":
    main()
