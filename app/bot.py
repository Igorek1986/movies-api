"""
Telegram-бот NUMParser (aiogram v3).

Привязка устройства — через deep link t.me/BOT?start=CODE.
Восстановление пароля — бот отправляет 6-значный код, пользователь вводит его на сайте.

Команды пользователя:
  /start [CODE]  — приветствие; если передан код — привязывает аккаунт
  /status        — роль и количество устройств

Команды администратора (telegram_id в TELEGRAM_ADMIN_IDS):
  /admin                       — список команд
  /info username               — информация об аккаунте
  /setpremium username         — роль premium
  /setsuper username           — роль super
  /setsimple username          — роль simple
  /broadcast текст             — всем привязанным пользователям
"""

import logging
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, Router, F, types
from aiogram.filters import Command, CommandObject
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    MenuButtonWebApp,
    MenuButtonDefault,
    WebAppInfo,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
)
from sqlalchemy import select, func

from app.db.database import async_session_maker
from app.db.models import TelegramUser, TelegramLinkCode, User, Device, SupportMessage
from app import settings_cache

logger = logging.getLogger(__name__)

_bot: Bot | None = None
_dp: Dispatcher | None = None
_router = Router()

# Cooldown для кнопки «Не работает»: telegram_id → timestamp последнего репорта
_report_cooldown: dict[int, datetime] = {}
_REPORT_COOLDOWN_MINUTES = 30


class AdminStates(StatesGroup):
    waiting_broadcast_text = State()


def _plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n)
    if 11 <= n % 100 <= 19:
        return many
    r = n % 10
    if r == 1:
        return one
    if 2 <= r <= 4:
        return few
    return many


def _main_keyboard() -> ReplyKeyboardMarkup:
    from app.config import get_settings

    donate_url = get_settings().DONATE_URL

    rows = []
    if donate_url:
        rows.append([KeyboardButton(text="💰 Донат")])
    rows.append(
        [KeyboardButton(text="📊 Статус"), KeyboardButton(text="🚨 Не работает")]
    )

    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True)


def _admin_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="👥 Пользователи")],
            [KeyboardButton(text="📢 Рассылка")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def get_bot() -> Bot | None:
    return _bot


def get_dp() -> Dispatcher | None:
    return _dp


async def _on_startup(bot: Bot) -> None:
    from app.config import get_settings

    settings = get_settings()
    try:
        if not settings.TELEGRAM_USE_POLLING:
            webhook_url = f"{settings.BASE_URL}/bot/webhook"
            secret = settings.TELEGRAM_BOT_TOKEN.split(":")[1]
            await bot.set_webhook(
                webhook_url,
                secret_token=secret,
                allowed_updates=["message", "callback_query"],
            )
            logger.info(f"Telegram webhook set: {webhook_url}")

        # Команды бота (видны в меню «/»)
        await bot.set_my_commands(
            [
                BotCommand(command="start", description="Главное меню"),
                BotCommand(command="status", description="Статус аккаунта"),
            ]
        )

        # Глобальная кнопка меню — открывает Mini App (для привязанных пользователей)
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(
                text="📱 Управление",
                web_app=WebAppInfo(url=f"{settings.BASE_URL}/tg-app"),
            )
        )
        logger.info("Bot commands and menu button set")
    except Exception as e:
        logger.warning(f"Telegram bot startup failed (API unavailable?): {e}")


async def _on_shutdown(bot: Bot) -> None:
    from app.config import get_settings

    if not get_settings().TELEGRAM_USE_POLLING:
        await bot.delete_webhook()


def init_bot(token: str) -> tuple[Bot, Dispatcher]:
    global _bot, _dp
    _bot = Bot(token=token, default=DefaultBotProperties(parse_mode="HTML"))
    _dp = Dispatcher()
    _dp.include_router(_router)
    _dp.startup.register(_on_startup)
    _dp.shutdown.register(_on_shutdown)
    return _bot, _dp


# ─── Хелперы ──────────────────────────────────────────────────────────────────


def _fmt_date(dt, user) -> str:
    """Format datetime in user's local timezone as dd.mm.YYYY."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    from datetime import timezone as _tz
    if dt is None:
        return "—"
    if not hasattr(dt, "hour"):
        return dt.strftime("%d.%m.%Y")
    tz_name = getattr(user, "timezone", None) or settings_cache.get("default_timezone") or "Europe/Moscow"
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, Exception):
        tz = ZoneInfo("Europe/Moscow")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_tz.utc)
    return dt.astimezone(tz).strftime("%d.%m.%Y")


def _is_admin(telegram_id: int) -> bool:
    from app.config import get_settings

    return telegram_id in get_settings().telegram_admin_id_list


async def _get_tg_user(db, telegram_id: int) -> TelegramUser | None:
    result = await db.execute(
        select(TelegramUser).where(TelegramUser.telegram_id == telegram_id)
    )
    return result.scalar_one_or_none()


async def _process_link_code(message: types.Message, code: str):
    """Привязывает Telegram-аккаунт к коду из БД."""
    logger.info(f"Link code attempt: tg_id={message.from_user.id} code={repr(code)}")
    async with async_session_maker() as db:
        now = datetime.now(timezone.utc)

        result = await db.execute(
            select(TelegramLinkCode).where(TelegramLinkCode.code == code)
        )
        link_code = result.scalar_one_or_none()

        if not link_code:
            all_codes = await db.execute(select(TelegramLinkCode))
            existing = [c.code for c in all_codes.scalars().all()]
            logger.warning(f"Link code not found: {repr(code)}, codes in DB: {existing}")
            await message.answer("Код не найден. Запросите новый на сайте.")
            return

        if link_code.expires_at.replace(tzinfo=timezone.utc) < now:
            await db.delete(link_code)
            await db.commit()
            await message.answer("Код истёк. Запросите новый на сайте.")
            return

        # Этот Telegram уже привязан к другому аккаунту?
        existing = await _get_tg_user(db, message.from_user.id)
        if existing and existing.user_id != link_code.user_id:
            await message.answer(
                "Этот Telegram уже привязан к другому аккаунту NUMParser.\n"
                "Сначала отвяжите его в настройках того аккаунта."
            )
            return

        # У целевого пользователя уже есть другой Telegram — обновляем
        result2 = await db.execute(
            select(TelegramUser).where(TelegramUser.user_id == link_code.user_id)
        )
        tg_user = result2.scalar_one_or_none()

        username = message.from_user.username
        if tg_user:
            tg_user.telegram_id = message.from_user.id
            tg_user.username = username
        else:
            db.add(
                TelegramUser(
                    user_id=link_code.user_id,
                    telegram_id=message.from_user.id,
                    username=username,
                )
            )

        await db.delete(link_code)
        await db.commit()

        user_result = await db.execute(select(User).where(User.id == link_code.user_id))
        user = user_result.scalar_one_or_none()

    await _send_start_menu(message)


# ─── Команды пользователя ─────────────────────────────────────────────────────


@_router.message(Command("start"))
async def cmd_start(message: types.Message, command: CommandObject):
    # Deep link: t.me/bot?start=CODE → command.args == "CODE"
    if command.args:
        await _process_link_code(message, command.args.strip())
        return

    await _send_start_menu(message)


@_router.message(Command("status"))
async def cmd_status(message: types.Message):
    async with async_session_maker() as db:
        tg = await _get_tg_user(db, message.from_user.id)
        if not tg:
            await message.answer(
                "Telegram не привязан ни к одному аккаунту NUMParser.",
                reply_markup=_main_keyboard(),
            )
            return

        user_result = await db.execute(select(User).where(User.id == tg.user_id))
        user = user_result.scalar_one_or_none()
        if not user:
            await message.answer("Аккаунт не найден.", reply_markup=_main_keyboard())
            return

        device_count = await db.scalar(
            select(func.count()).select_from(Device).where(Device.user_id == user.id)
        )

    role_labels = {"simple": "Базовый", "premium": "Премиум", "super": "Супер"}
    limit = settings_cache.get_role_limit(user.role, "device_limit") or 3
    limit_str = str(limit) if limit is not None else "∞"

    now = datetime.now(timezone.utc)

    # Строка подписки
    subscription_line = ""
    if user.timecode_grace_until:
        grace_until = user.timecode_grace_until
        if grace_until.tzinfo is None:
            grace_until = grace_until.replace(tzinfo=timezone.utc)
        if grace_until > now:
            days_left = (grace_until - now).days
            subscription_line = f"\n⚠️ <b>Grace-период:</b> ещё {days_left} {_plural(days_left, 'день', 'дня', 'дней')} (до {_fmt_date(grace_until, user)})"
        else:
            subscription_line = "\n❌ <b>Grace-период истёк</b>"
    elif user.premium_until:
        premium_until = user.premium_until
        if premium_until.tzinfo is None:
            premium_until = premium_until.replace(tzinfo=timezone.utc)
        if premium_until > now:
            days_left = (premium_until - now).days
            subscription_line = f"\n📅 <b>Подписка до:</b> {premium_until.strftime('%d.%m.%Y')} (осталось {days_left} {_plural(days_left, 'день', 'дня', 'дней')})"
        else:
            subscription_line = f"\n⏰ <b>Подписка истекла:</b> {premium_until.strftime('%d.%m.%Y')}"

    kb = _admin_keyboard() if _is_admin(message.from_user.id) else _main_keyboard()
    await message.answer(
        f"<b>Аккаунт:</b> {user.username}\n"
        f"<b>Роль:</b> {role_labels.get(user.role, user.role)}\n"
        f"<b>Устройств:</b> {device_count} / {limit_str}"
        f"{subscription_line}",
        reply_markup=kb,
    )


# ─── Хелпер: главное меню ────────────────────────────────────────────────────


async def _send_start_menu(message: types.Message):
    """Отправляет приветствие. Кнопка меню уже установлена глобально на боте."""
    from app.config import get_settings

    base_url = get_settings().BASE_URL
    is_admin = _is_admin(message.from_user.id)

    async with async_session_maker() as db:
        tg = await _get_tg_user(db, message.from_user.id)
        if tg:
            user_result = await db.execute(select(User).where(User.id == tg.user_id))
            user = user_result.scalar_one_or_none()
        else:
            user = None

    if is_admin:
        name = user.username if user else "—"
        text = (
            f"👋 Привет, <b>{name}</b>!\n\n"
            f"Вы администратор NUMParser.\n\n"
            f"<b>Команды:</b>\n"
            f"/status — статус аккаунта\n"
            f"/admin — управление пользователями\n\n"
            f"Нажмите кнопку <b>«📱 Управление»</b> рядом с полем ввода, "
            f"чтобы открыть панель администратора."
        )
    elif tg and user:
        text = (
            f"👋 Привет, <b>{user.username}</b>!\n\n"
            f"Нажмите кнопку <b>«📱 Управление»</b> рядом с полем ввода, "
            f"чтобы управлять устройствами.\n\n"
            f"<b>Команды:</b>\n"
            f"/status — статус аккаунта"
        )
    else:
        text = (
            f"👋 Привет! Я бот <b>NUMParser</b>.\n\n"
            f"Чтобы управлять устройствами через Telegram — "
            f"сначала привяжите аккаунт на сайте:\n"
            f'<a href="{base_url}/profiles">{base_url}/profiles</a>'
        )

    kb = _admin_keyboard() if is_admin else _main_keyboard()
    await message.answer(text, disable_web_page_preview=True, reply_markup=kb)


# ─── Команды администратора ───────────────────────────────────────────────────


@_router.message(Command("admin"))
async def cmd_admin(message: types.Message):
    if not _is_admin(message.from_user.id):
        return
    await message.answer(
        "<b>Команды администратора:</b>\n\n"
        "/info username — информация об аккаунте\n"
        "/setpremium username — роль premium\n"
        "/setsuper username — роль super\n"
        "/setsimple username — роль simple\n"
        "/broadcast текст — сообщение всем привязанным\n"
    )


@_router.message(Command("info"))
async def cmd_info(message: types.Message, command: CommandObject):
    if not _is_admin(message.from_user.id):
        return
    username = (command.args or "").strip().lstrip("@")
    if not username:
        await message.answer("Использование: /info username")
        return

    async with async_session_maker() as db:
        user_result = await db.execute(select(User).where(User.username == username))
        user = user_result.scalar_one_or_none()
        if not user:
            await message.answer(f"Пользователь <b>{username}</b> не найден.")
            return

        device_count = await db.scalar(
            select(func.count()).select_from(Device).where(Device.user_id == user.id)
        )
        tg_result = await db.execute(
            select(TelegramUser).where(TelegramUser.user_id == user.id)
        )
        tg = tg_result.scalar_one_or_none()

    role_labels = {"simple": "Базовый", "premium": "Премиум", "super": "Супер"}
    limit = settings_cache.get_role_limit(user.role, "device_limit") or 3
    limit_str = str(limit) if limit is not None else "∞"
    tg_str = (
        (f"@{tg.username}" if tg and tg.username else str(tg.telegram_id))
        if tg
        else "не привязан"
    )

    await message.answer(
        f"<b>Аккаунт:</b> {user.username}\n"
        f"<b>Роль:</b> {role_labels.get(user.role, user.role)}\n"
        f"<b>Устройств:</b> {device_count} / {limit_str}\n"
        f"<b>Telegram:</b> {tg_str}\n"
        f"<b>Регистрация:</b> {user.created_at.strftime('%d.%m.%Y') if user.created_at else '—'}"
    )


async def _set_role(message: types.Message, username: str, role: str):
    from app.db.models import USER_ROLES

    if role not in USER_ROLES:
        await message.answer(f"Неизвестная роль: {role}")
        return
    async with async_session_maker() as db:
        user_result = await db.execute(select(User).where(User.username == username))
        user = user_result.scalar_one_or_none()
        if not user:
            await message.answer(f"Пользователь <b>{username}</b> не найден.")
            return
        old_role = user.role
        user.role = role
        await db.commit()

    role_labels = {"simple": "Базовый", "premium": "Премиум", "super": "Супер"}
    await message.answer(
        f"Роль <b>{username}</b>: "
        f"{role_labels.get(old_role, old_role)} → {role_labels.get(role, role)}"
    )


@_router.message(Command("setpremium"))
async def cmd_setpremium(message: types.Message, command: CommandObject):
    if not _is_admin(message.from_user.id):
        return
    username = (command.args or "").strip().lstrip("@")
    if not username:
        await message.answer("Использование: /setpremium username")
        return
    await _set_role(message, username, "premium")


@_router.message(Command("setsuper"))
async def cmd_setsuper(message: types.Message, command: CommandObject):
    if not _is_admin(message.from_user.id):
        return
    username = (command.args or "").strip().lstrip("@")
    if not username:
        await message.answer("Использование: /setsuper username")
        return
    await _set_role(message, username, "super")


@_router.message(Command("setsimple"))
async def cmd_setsimple(message: types.Message, command: CommandObject):
    if not _is_admin(message.from_user.id):
        return
    username = (command.args or "").strip().lstrip("@")
    if not username:
        await message.answer("Использование: /setsimple username")
        return
    await _set_role(message, username, "simple")


@_router.message(Command("broadcast"))
async def cmd_broadcast(message: types.Message, command: CommandObject):
    if not _is_admin(message.from_user.id):
        return
    text = (command.args or "").strip()
    if not text:
        await message.answer("Использование: /broadcast текст сообщения")
        return

    async with async_session_maker() as db:
        result = await db.execute(select(TelegramUser))
        all_tg = result.scalars().all()

    sent, failed = 0, 0
    for tg in all_tg:
        if await send_message(tg.telegram_id, text):
            sent += 1
        else:
            failed += 1

    await message.answer(f"Отправлено: {sent}, ошибок: {failed}")


# ─── Кнопки клавиатуры ────────────────────────────────────────────────────────


@_router.message(F.text == "👥 Пользователи")
async def btn_users(message: types.Message):
    if not _is_admin(message.from_user.id):
        return
    async with async_session_maker() as db:
        total = await db.scalar(select(func.count()).select_from(User))
        by_role = await db.execute(select(User.role, func.count()).group_by(User.role))
        rows = by_role.all()

    role_labels = {"simple": "Базовый", "premium": "Премиум", "super": "Супер"}
    lines = "\n".join(f"  {role_labels.get(r, r)}: {cnt}" for r, cnt in sorted(rows))
    await message.answer(
        f"<b>Пользователи:</b> {total}\n\n{lines}\n\n"
        f"Для управления: /info username\n"
        f"/setpremium · /setsuper · /setsimple"
    )


@_router.message(F.text == "📢 Рассылка")
async def btn_broadcast_start(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    await state.set_state(AdminStates.waiting_broadcast_text)
    await message.answer(
        "Введите текст рассылки.\n\n<i>Отправьте /cancel для отмены.</i>",
        reply_markup=ReplyKeyboardRemove(),
    )


@_router.message(Command("cancel"), AdminStates.waiting_broadcast_text)
async def cmd_cancel_broadcast(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Рассылка отменена.", reply_markup=_admin_keyboard())


@_router.message(AdminStates.waiting_broadcast_text)
async def btn_broadcast_send(message: types.Message, state: FSMContext):
    await state.clear()
    text = message.text or ""
    if not text:
        await message.answer(
            "Пустой текст. Рассылка отменена.", reply_markup=_admin_keyboard()
        )
        return

    async with async_session_maker() as db:
        result = await db.execute(select(TelegramUser))
        all_tg = result.scalars().all()

    sent, failed = 0, 0
    for tg in all_tg:
        if await send_message(tg.telegram_id, text):
            sent += 1
        else:
            failed += 1

    await message.answer(
        f"✅ Рассылка завершена.\nОтправлено: {sent}, ошибок: {failed}",
        reply_markup=_admin_keyboard(),
    )


@_router.message(F.text == "💰 Донат")
async def btn_donate(message: types.Message):
    from app.config import get_settings

    url = get_settings().DONATE_URL
    if url:
        await message.answer(f"Нравится проект? Поддержи автора: {url}")


@_router.message(F.text == "📊 Статус")
async def btn_status(message: types.Message):
    await cmd_status(message)


@_router.message(F.text == "🚨 Не работает")
async def btn_not_working(message: types.Message):
    user_id = message.from_user.id
    now = datetime.now(timezone.utc)

    # Rate limit
    last = _report_cooldown.get(user_id)
    if last:
        from datetime import timedelta

        elapsed = (now - last).total_seconds() / 60
        if elapsed < _REPORT_COOLDOWN_MINUTES:
            wait = int(_REPORT_COOLDOWN_MINUTES - elapsed)
            await message.answer(
                f"Вы уже отправляли репорт. Следующий можно через {wait} {_plural(wait, 'минуту', 'минуты', 'минут')}."
            )
            return

    _report_cooldown[user_id] = now
    await _forward_to_support(message, "🚨 Сообщает, что сервис не работает")


# ─── Чат поддержки ────────────────────────────────────────────────────────────


async def _forward_to_support(message: types.Message, text: str):
    """Сохраняет сообщение в SupportMessage и пересылает администраторам с привязкой для ответа."""
    from app.config import get_settings

    admin_ids = get_settings().telegram_admin_id_list

    user_id = message.from_user.id
    username = message.from_user.username

    async with async_session_maker() as db:
        msg_obj = SupportMessage(
            user_telegram_id=user_id,
            user_username=username,
            direction="in",
            text=text,
            is_read=False,
        )
        db.add(msg_obj)
        await db.flush()

        if not admin_ids:
            await db.commit()
            await message.answer(
                "Ваше сообщение получено. Администратор ответит вам здесь."
            )
            return

        name = f"@{username}" if username else f"#{user_id}"
        forward_text = (
            f"📩 <b>Сообщение от {name}</b> (ID: <code>{user_id}</code>)\n\n"
            f"{text}\n\n"
            f"<i>Ответьте на это сообщение, чтобы написать пользователю.</i>"
        )

        for admin_id in admin_ids:
            try:
                sent = await _bot.send_message(
                    admin_id, forward_text, parse_mode="HTML"
                )
                db.add(
                    SupportMessage(
                        user_telegram_id=user_id,
                        user_username=username,
                        direction="in",
                        text=text,
                        admin_telegram_id=admin_id,
                        admin_msg_id=sent.message_id,
                        is_read=False,
                    )
                )
            except Exception as e:
                logger.warning(
                    f"Не удалось переслать сообщение поддержки admin {admin_id}: {e}"
                )

        await db.delete(msg_obj)
        await db.commit()

    await message.answer("✅ Сообщение отправлено администратору. Ожидайте ответа.")


@_router.message(F.text & ~F.text.startswith("/"))
async def handle_user_message(message: types.Message):
    """Любое текстовое сообщение не от команды — пересылается администраторам."""
    # Если это сообщение от администратора — обрабатываем как ответ поддержки
    if _is_admin(message.from_user.id):
        if message.reply_to_message:
            await _handle_admin_reply(message)
        return

    await _forward_to_support(message, message.text)


async def _handle_admin_reply(message: types.Message):
    """Обрабатывает ответ администратора на уведомление о сообщении пользователя."""
    reply_msg_id = message.reply_to_message.message_id
    admin_id = message.from_user.id

    async with async_session_maker() as db:
        result = await db.execute(
            select(SupportMessage).where(
                SupportMessage.admin_telegram_id == admin_id,
                SupportMessage.admin_msg_id == reply_msg_id,
                SupportMessage.direction == "in",
            )
        )
        original = result.scalar_one_or_none()

    if not original:
        # Не найдено — обычное сообщение от администратора, игнорируем
        return

    user_tg_id = original.user_telegram_id
    reply_text = message.text or message.caption or ""

    ok = await send_message(
        user_tg_id, f"💬 <b>Ответ от поддержки:</b>\n\n{reply_text}"
    )

    if ok:
        # Сохраняем ответ
        async with async_session_maker() as db:
            db.add(
                SupportMessage(
                    user_telegram_id=user_tg_id,
                    user_username=original.user_username,
                    direction="out",
                    text=reply_text,
                    admin_telegram_id=admin_id,
                    is_read=True,
                )
            )
            await db.commit()
        await message.reply("✅ Ответ отправлен пользователю.")
    else:
        await message.reply("❌ Не удалось отправить сообщение пользователю.")


# ─── Публичные функции отправки ───────────────────────────────────────────────


async def send_message(telegram_id: int, text: str) -> bool:
    if not _bot:
        return False
    try:
        await _bot.send_message(telegram_id, text, parse_mode="HTML")
        return True
    except Exception as e:
        logger.warning(f"Telegram send failed to {telegram_id}: {e}")
        return False


async def send_reset_code(telegram_id: int, username: str, code: str) -> bool:
    """Отправить 6-значный код для сброса пароля."""
    text = (
        f"Запрос на сброс пароля для аккаунта <b>{username}</b>.\n\n"
        f"Ваш код: <code>{code}</code>\n\n"
        "Введите его на странице восстановления пароля. "
        "Действует 15 минут.\n\n"
        "Если вы не запрашивали сброс — проигнорируйте."
    )
    return await send_message(telegram_id, text)


async def send_new_session_notification(
    telegram_id: int, ip: str, device: str, change_password_url: str, username: str = "", user_timezone: str = ""
) -> bool:
    """Уведомить пользователя о новом входе в аккаунт."""
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    tz_name = user_timezone or settings_cache.get("default_timezone") or "Europe/Moscow"
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, Exception):
        tz = ZoneInfo("Europe/Moscow")
    now = datetime.now(timezone.utc).astimezone(tz).strftime("%d.%m.%Y %H:%M")
    user_line = f"👤 Аккаунт: <b>{username}</b>\n" if username else ""
    text = (
        f"🔐 <b>Новый вход в аккаунт</b>\n\n"
        f"{user_line}"
        f"🌐 IP: <code>{ip}</code>\n"
        f"📱 Устройство: {device}\n"
        f"🕐 Время: {now} ({tz_name})\n\n"
        f'Если это были не вы — <a href="{change_password_url}">смените пароль</a>.'
    )
    return await send_message(telegram_id, text)
