"""
Background tasks.

premium_expiry_check — runs daily at 05:00 server local time:
  1. Finds users with expired premium_until → demotes to simple
  2. If user's timecodes exceed simple_timecode_limit → sets grace period
  3. Sends Telegram notification (deferred if within quiet hours)
  4. Sends deferred notifications when quiet hours end
  5. Cleans up timecodes after grace_period expires
"""
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

_task: asyncio.Task | None = None


# ─── Timezone helpers ──────────────────────────────────────────────────────────

def _get_tz(tz_str: str | None) -> ZoneInfo:
    from app import settings_cache
    name = tz_str or settings_cache.get("default_timezone") or "Europe/Moscow"
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, Exception):
        return ZoneInfo("Europe/Moscow")


def _is_quiet_hour(user_tz: str | None, quiet_start: int, quiet_end: int) -> bool:
    """True if current local time is within quiet hours [quiet_start, midnight) ∪ [0, quiet_end)."""
    hour = datetime.now(_get_tz(user_tz)).hour
    if quiet_start > quiet_end:   # wraps midnight, e.g. 22–9
        return hour >= quiet_start or hour < quiet_end
    return quiet_start <= hour < quiet_end


def _next_morning_utc(user_tz: str | None, quiet_end: int) -> datetime:
    """Return next quiet_end:00 in user timezone as UTC-aware datetime."""
    tz = _get_tz(user_tz)
    now_local = datetime.now(tz)
    candidate = now_local.replace(hour=quiet_end, minute=0, second=0, microsecond=0)
    if now_local >= candidate:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc)


def _seconds_until_next_5am() -> float:
    """Seconds until next 05:00 server local time."""
    now = datetime.now()
    target = now.replace(hour=5, minute=0, second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    return (target - now).total_seconds()


# ─── Core check ───────────────────────────────────────────────────────────────

async def run_premium_expiry_check(_now: datetime | None = None) -> None:
    from app.db.database import async_session_maker
    from app.db.models import User, Device, Timecode, TelegramUser
    from app import settings_cache
    from sqlalchemy import select, func, delete, and_

    logger.info("Running premium expiry check...")

    async with async_session_maker() as db:
        now = _now or datetime.now(timezone.utc)
        quiet_start      = settings_cache.get_int("quiet_hours_start")
        quiet_end        = settings_cache.get_int("quiet_hours_end")
        simple_tc_limit  = settings_cache.get_int("simple_timecode_limit")
        grace_days       = settings_cache.get_int("timecode_grace_days")

        # ── 1. Demote expired premium users ───────────────────────────────────
        result = await db.execute(
            select(User).where(
                and_(
                    User.role == "premium",
                    User.premium_until.isnot(None),
                    User.premium_until <= now,
                )
            )
        )
        expired = result.scalars().all()

        for user in expired:
            user.role = "simple"
            user.premium_until = None

            # Устанавливаем grace-период для всей очистки (устройства, профили, таймкоды).
            # Если пользователь продлит premium до истечения grace — данные не удаляются.
            if grace_days == 0:
                user.timecode_grace_until = now  # немедленная очистка в шаге 5
            else:
                user.timecode_grace_until = now + timedelta(days=grace_days)

            # Telegram notification
            tg = (await db.execute(
                select(TelegramUser).where(TelegramUser.user_id == user.id)
            )).scalar_one_or_none()

            if tg:
                if _is_quiet_hour(user.timezone, quiet_start, quiet_end):
                    user.notify_premium_after = _next_morning_utc(user.timezone, quiet_end)
                    logger.info(f"User {user.username}: notification deferred (quiet hours)")
                else:
                    await _send_premium_expired(tg.telegram_id, user)

            logger.info(f"User {user.username}: premium expired → simple")

        await db.commit()

        # ── 2. Advance warning: 3 days before expiry ──────────────────────────
        warn_days    = 3
        warn_horizon = now + timedelta(days=warn_days)

        result = await db.execute(
            select(User).where(
                and_(
                    User.role == "premium",
                    User.premium_until.isnot(None),
                    User.premium_until > now,
                    User.premium_until <= warn_horizon,
                    User.premium_warned == False,  # noqa: E712 — ещё не предупреждали
                )
            )
        )
        for user in result.scalars().all():
            tg = (await db.execute(
                select(TelegramUser).where(TelegramUser.user_id == user.id)
            )).scalar_one_or_none()
            if tg:
                if _is_quiet_hour(user.timezone, quiet_start, quiet_end):
                    user.notify_premium_after = _next_morning_utc(user.timezone, quiet_end)
                else:
                    await _send_premium_warning(tg.telegram_id, user)
            user.premium_warned = True  # не отправлять повторно

        await db.commit()

        # ── 4. Send deferred notifications ────────────────────────────────────
        result = await db.execute(
            select(User).where(
                and_(
                    User.notify_premium_after.isnot(None),
                    User.notify_premium_after <= now,
                )
            )
        )
        for user in result.scalars().all():
            tg = (await db.execute(
                select(TelegramUser).where(TelegramUser.user_id == user.id)
            )).scalar_one_or_none()
            if tg:
                await _send_premium_expired(tg.telegram_id, user)
            user.notify_premium_after = None

        await db.commit()

        # ── 5. Clean up devices / profiles / timecodes after grace period ────────
        result = await db.execute(
            select(User).where(
                and_(
                    User.timecode_grace_until.isnot(None),
                    User.timecode_grace_until <= now,
                )
            )
        )
        for user in result.scalars().all():
            dev_limit  = settings_cache.get_role_limit(user.role, "device_limit")
            prof_limit = settings_cache.get_role_limit(user.role, "profile_limit")
            tc_limit   = settings_cache.get_role_limit(user.role, "timecode_limit")
            if dev_limit is not None:
                await _cleanup_devices(db, user.id, dev_limit, user.username)
            if prof_limit is not None:
                await _cleanup_profiles(db, user.id, prof_limit, user.username)
            if tc_limit is not None:
                await _cleanup_timecodes(db, user.id, tc_limit, user.username)
            user.timecode_grace_until = None

        await db.commit()

    logger.info("Premium expiry check complete.")


async def _send_premium_expired(telegram_id: int, user) -> None:
    from app.bot import get_bot
    from app import settings_cache

    bot = get_bot()
    if not bot:
        return

    s_dev  = settings_cache.get_int("simple_device_limit")
    s_prof = settings_cache.get_int("simple_profile_limit")
    s_tc   = settings_cache.get_int("simple_timecode_limit")
    grace_days = settings_cache.get_int("timecode_grace_days")

    grace_note = ""
    if user.timecode_grace_until:
        deadline = user.timecode_grace_until.strftime("%d.%m.%Y")
        grace_note = (
            f"\n\n⏳ Лишние данные будут удалены <b>{deadline}</b>.\n"
            f"Продлите подписку до этой даты, чтобы сохранить историю."
        )

    try:
        await bot.send_message(
            telegram_id,
            f"❌ <b>Подписка Premium истекла.</b>\n\n"
            f"Аккаунт переведён на <b>Simple</b>:\n"
            f"• Синхронизация с MyShows — недоступна\n"
            f"• Лимит устройств: <b>{s_dev}</b>\n"
            f"• Лимит профилей на устройство: <b>{s_prof}</b>\n"
            f"• Лимит таймкодов на профиль: <b>{s_tc}</b>"
            f"{grace_note}",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.warning(f"Failed to send premium expiry notification to {telegram_id}: {e}")


async def _send_premium_renewed(telegram_id: int, user, was_grace: bool) -> None:
    """Уведомление об успешном продлении / восстановлении Premium."""
    from app.bot import get_bot

    bot = get_bot()
    if not bot:
        return

    expires_str = user.premium_until.strftime("%d.%m.%Y") if user.premium_until else "—"

    if was_grace:
        text = (
            f"✅ <b>Подписка Premium восстановлена!</b>\n\n"
            f"Аккаунт снова <b>Premium</b> до {expires_str}.\n"
            f"Все данные сохранены — история, устройства, профили."
        )
    else:
        text = (
            f"✅ <b>Подписка Premium продлена.</b>\n\n"
            f"Аккаунт <b>Premium</b> до {expires_str}."
        )

    try:
        await bot.send_message(telegram_id, text, parse_mode="HTML")
    except Exception as e:
        logger.warning(f"Failed to send premium renewed notification to {telegram_id}: {e}")


async def _send_premium_warning(telegram_id: int, user) -> None:
    from app.bot import get_bot
    from app import settings_cache

    bot = get_bot()
    if not bot:
        return

    # Показываем дату как в админке — UTC-дата (premium_until = 23:59:59 UTC нужного дня)
    expires_str = user.premium_until.strftime("%d.%m.%Y")

    s_dev  = settings_cache.get_int("simple_device_limit")
    s_prof = settings_cache.get_int("simple_profile_limit")
    s_tc   = settings_cache.get_int("simple_timecode_limit")

    try:
        await bot.send_message(
            telegram_id,
            f"⏰ <b>Подписка Premium истекает {expires_str}.</b>\n\n"
            f"После истечения аккаунт будет переведён на <b>Simple</b>:\n"
            f"• Синхронизация с MyShows — недоступна\n"
            f"• Лишние устройства будут удалены (оставлено {s_dev} старейших)\n"
            f"• Лишние профили на устройство будут удалены (оставлено {s_prof})\n"
            f"• Лишние таймкоды на профиль будут удалены (лимит {s_tc})\n\n"
            f"Обратитесь к администратору для продления.",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.warning(f"Failed to send premium warning to {telegram_id}: {e}")


async def _cleanup_devices(db, user_id: int, device_limit: int, username: str) -> int:
    """Удаляет самые новые устройства сверх лимита. Таймкоды удаляются каскадно.
    Возвращает количество удалённых устройств."""
    from app.db.models import Device
    from sqlalchemy import select, delete

    all_ids = (await db.execute(
        select(Device.id)
        .where(Device.user_id == user_id)
        .order_by(Device.created_at.asc())
    )).scalars().all()

    if len(all_ids) <= device_limit:
        return 0

    delete_ids = all_ids[device_limit:]
    await db.execute(delete(Device).where(Device.id.in_(delete_ids)))
    logger.info(f"User {username}: deleted {len(delete_ids)} devices (limit={device_limit})")
    return len(delete_ids)


async def _cleanup_profiles(db, user_id: int, profile_limit: int, username: str) -> int:
    """Для каждого устройства удаляет самые новые профили сверх лимита + их таймкоды.
    Возвращает суммарное количество удалённых профилей."""
    from app.db.models import Device, LampaProfile, Timecode
    from sqlalchemy import select, delete

    dev_ids = (await db.execute(
        select(Device.id).where(Device.user_id == user_id)
    )).scalars().all()

    total_deleted = 0
    for device_id in dev_ids:
        profiles = (await db.execute(
            select(LampaProfile)
            .where(LampaProfile.device_id == device_id)
            .order_by(LampaProfile.id.asc())
        )).scalars().all()

        if len(profiles) <= profile_limit:
            continue

        to_delete = profiles[profile_limit:]
        del_profile_ids = [lp.lampa_profile_id for lp in to_delete]
        del_lp_ids      = [lp.id for lp in to_delete]

        await db.execute(
            delete(Timecode).where(
                Timecode.device_id == device_id,
                Timecode.lampa_profile_id.in_(del_profile_ids),
            )
        )
        await db.execute(delete(LampaProfile).where(LampaProfile.id.in_(del_lp_ids)))
        total_deleted += len(to_delete)

    if total_deleted:
        logger.info(f"User {username}: deleted {total_deleted} profiles (limit={profile_limit}/device)")
    return total_deleted


async def _cleanup_timecodes(db, user_id: int, limit: int, username: str) -> None:
    """Удаляет старейшие таймкоды по каждому профилю отдельно до лимита per-profile."""
    from app.db.models import Device, Timecode
    from sqlalchemy import select, func, delete

    dev_ids = (await db.execute(
        select(Device.id).where(Device.user_id == user_id)
    )).scalars().all()

    if not dev_ids:
        return

    total_deleted = 0
    for device_id in dev_ids:
        profiles = (await db.execute(
            select(Timecode.lampa_profile_id)
            .where(Timecode.device_id == device_id)
            .distinct()
        )).scalars().all()

        for profile_id in profiles:
            count = (await db.execute(
                select(func.count()).select_from(Timecode).where(
                    Timecode.device_id == device_id,
                    Timecode.lampa_profile_id == profile_id,
                )
            )).scalar() or 0

            excess = count - limit
            if excess <= 0:
                continue

            oldest_ids = (await db.execute(
                select(Timecode.id)
                .where(
                    Timecode.device_id == device_id,
                    Timecode.lampa_profile_id == profile_id,
                )
                .order_by(Timecode.updated_at.asc())
                .limit(excess)
            )).scalars().all()

            if oldest_ids:
                await db.execute(delete(Timecode).where(Timecode.id.in_(oldest_ids)))
                total_deleted += len(oldest_ids)

    if total_deleted:
        logger.info(f"User {username}: deleted {total_deleted} old timecodes (limit={limit}/profile)")


# ─── Task lifecycle ───────────────────────────────────────────────────────────

async def _task_loop() -> None:
    while True:
        wait = _seconds_until_next_5am()
        logger.info(f"Next premium check in {wait / 3600:.1f}h (at 05:00 server time)")
        await asyncio.sleep(wait)
        try:
            await run_premium_expiry_check()
        except Exception as e:
            logger.error(f"Premium expiry check failed: {e}", exc_info=True)


def start_tasks() -> None:
    global _task
    _task = asyncio.create_task(_task_loop())
    logger.info("Background tasks started")


def stop_tasks() -> None:
    global _task
    if _task:
        _task.cancel()
        _task = None
