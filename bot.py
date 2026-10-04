import asyncio
import html
import logging
import os
import uuid
from urllib.parse import quote

import aiosqlite
from aiogram import Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.filters import CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from dotenv import load_dotenv
from yoomoney import Client, Quickpay

load_dotenv()

# ==================== НАСТРОЙКИ ====================
# Секреты читаются из файла .env
BOT_TOKEN = os.getenv("BOT_TOKEN")

# Настройки ЮMoney
YOOMONEY_WALLET = os.getenv("YOOMONEY_WALLET")  # Номер кошелька ЮMoney
YOOMONEY_TOKEN = os.getenv("YOOMONEY_TOKEN")  # Токен ЮMoney

_missing = [
    name
    for name, value in (
        ("BOT_TOKEN", BOT_TOKEN),
        ("YOOMONEY_WALLET", YOOMONEY_WALLET),
        ("YOOMONEY_TOKEN", YOOMONEY_TOKEN),
    )
    if not value
]
if _missing:
    raise SystemExit(f"В файле .env не заданы переменные: {', '.join(_missing)}")

# Параметры турнира
ENTRY_FEE = 300  # Стоимость взноса (в рублях)
ADMIN_ID = 1463122914  # ID судьи/организатора в Telegram
CAPTAINS_CHAT_LINK = "https://t.me/+AbCdEfGhIjK12345"  # Ссылка на беседу капитанов

# Проверка подписки (только один турнирный канал)
TOURNAMENT_CHANNEL = {
    "name": "SALQSIAN",
    "id": "@SALQSIAN",
    "link": "https://t.me/SALQSIAN",
}

DB_NAME = "cs2_bot.db"
MAX_ANKETA_LEN = 3500  # Чтобы карточка админу уместилась в лимит Telegram (4096)
# ===================================================

logging.basicConfig(level=logging.INFO)
ym_client = Client(YOOMONEY_TOKEN)


def get_proxy_url() -> str | None:
    proxy_ip = os.getenv("PROXY_IP")
    proxy_port = os.getenv("PROXY_PORT")
    if not proxy_ip or not proxy_port:
        return None

    proxy_user = os.getenv("PROXY_USER")
    proxy_pass = os.getenv("PROXY_PASS")
    auth = ""
    if proxy_user:
        auth = quote(proxy_user, safe="")
        if proxy_pass:
            auth += f":{quote(proxy_pass, safe='')}"
        auth += "@"
    return f"socks5://{auth}{proxy_ip}:{proxy_port}"


# ----------------- FSM СОСТОЯНИЯ -----------------
class TeamRegistration(StatesGroup):
    waiting_for_application = State()


class PlayerProfile(StatesGroup):
    waiting_for_nickname = State()
    waiting_for_age = State()
    waiting_for_role = State()
    waiting_for_faceit = State()
    waiting_for_steam = State()
    waiting_for_about = State()


# ----------------- БАЗА ДАННЫХ (SQLite) -----------------
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        # Таблица игроков для Тиндера
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS players (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                nickname TEXT,
                age INTEGER,
                role TEXT,
                faceit_lvl TEXT,
                steam_link TEXT,
                about TEXT,
                is_active INTEGER DEFAULT 1
            )
            """
        )
        # Таблицы лайков и дизлайков
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS likes (
                from_user_id INTEGER,
                to_user_id INTEGER,
                PRIMARY KEY (from_user_id, to_user_id)
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS dislikes (
                from_user_id INTEGER,
                to_user_id INTEGER,
                PRIMARY KEY (from_user_id, to_user_id)
            )
            """
        )
        # Заявки команд (чтобы анкета не терялась при перезапуске бота)
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS applications (
                order_id TEXT PRIMARY KEY,
                user_id INTEGER,
                username TEXT,
                anketa TEXT,
                status TEXT DEFAULT 'pending'
            )
            """
        )
        await db.commit()


# ----------------- ПРОВЕРКА ПОДПИСКИ -----------------
async def is_subscribed(bot: Bot, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(
            chat_id=TOURNAMENT_CHANNEL["id"], user_id=user_id
        )
        return member.status not in [ChatMemberStatus.LEFT, ChatMemberStatus.KICKED]
    except Exception as e:
        logging.error(f"Ошибка проверки подписки: {e}")
        return False


def get_sub_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"📢 Подписаться: {TOURNAMENT_CHANNEL['name']}",
                    url=TOURNAMENT_CHANNEL["link"],
                )
            ],
            [
                InlineKeyboardButton(
                    text="✅ Я подписался", callback_data="check_subscription"
                )
            ],
        ]
    )


# ----------------- КЛАВИАТУРЫ -----------------
def get_main_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🏆 Зарегистрировать команду")],
            [
                KeyboardButton(text="🔍 Искать тиммейтов"),
                KeyboardButton(text="👤 Моя анкета игрока"),
            ],
        ],
        resize_keyboard=True,
    )


def get_payment_keyboard(pay_url: str, order_id: str):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 Оплатить взнос (ЮMoney/Карта)", url=pay_url)],
            [
                InlineKeyboardButton(
                    text="🔄 Проверить оплату", callback_data=f"verify_{order_id}"
                )
            ],
        ]
    )


def get_admin_verdict_keyboard(user_id: int):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ В сетку", callback_data=f"admin_approve_{user_id}"
                ),
                InlineKeyboardButton(
                    text="❌ Отклонить", callback_data=f"admin_reject_{user_id}"
                ),
            ]
        ]
    )


def get_swipe_keyboard(target_id: int):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="👎 Пропустить", callback_data=f"swipe_dislike_{target_id}"),
                InlineKeyboardButton(text="👍 Позвать в стак", callback_data=f"swipe_like_{target_id}"),
            ],
            [InlineKeyboardButton(text="🚪 Закончить просмотр", callback_data="swipe_stop")],
        ]
    )


async def safe_delete(message: types.Message):
    # Telegram не даёт удалять сообщения старше 48 часов
    try:
        await message.delete()
    except Exception as e:
        logging.warning(f"Не удалось удалить сообщение: {e}")


# ----------------- СТАРТ И ГЛАВНОЕ МЕНЮ -----------------
async def cmd_start(message: types.Message, bot: Bot, state: FSMContext):
    await state.clear()
    if not await is_subscribed(bot, message.from_user.id):
        await message.answer(
            "👋 Привет! Чтобы участвовать в турнире или искать команду, подпишись на наш канал:",
            reply_markup=get_sub_keyboard(),
        )
        return

    await message.answer(
        "👋 <b>Добро пожаловать на платформу турнира по CS2!</b>\n\n"
        "Выбери действие в меню снизу:\n"
        "• <b>🏆 Зарегистрировать команду</b> — подача заявки готового состава и оплата слота.\n"
        "• <b>🔍 Искать тиммейтов</b> — тиндер для поиска +1/+2 или вступления в стак.\n"
        "• <b>👤 Моя анкета игрока</b> — создание или редактирование своего профиля.",
        reply_markup=get_main_menu(),
    )


async def check_sub_callback(callback: types.CallbackQuery, bot: Bot):
    if await is_subscribed(bot, callback.from_user.id):
        await safe_delete(callback.message)
        await callback.message.answer(
            "✅ Подписка подтверждена!", reply_markup=get_main_menu()
        )
        await callback.answer()
    else:
        await callback.answer("❌ Ты ещё не подписался на канал!", show_alert=True)


async def not_text_fallback(message: types.Message):
    await message.answer("Пожалуйста, пришлите ответ обычным текстовым сообщением.")


# ----------------- ВЕТКА 1: РЕГИСТРАЦИЯ КОМАНДЫ -----------------
SAMPLE_TEXT = (
    "📋 <b>Пример заполнения заявки на турнир по CS2:</b>\n\n"
    "1. <b>Название команды:</b> Cloud9 Academy\n"
    "2. <b>Тег:</b> C9A\n"
    "3. <b>Капитан:</b> @username (Steam: steamcommunity.com/id/...)\n"
    "4. <b>Состав (Ники + FACEIT):</b>\n"
    "   • Игрок 1 (lvl 10, faceit.com/...)\n"
    "   • Игрок 2 (lvl 10, faceit.com/...)\n"
    "   • Игрок 3 (lvl 9, faceit.com/...)\n"
    "   • Игрок 4 (lvl 9, faceit.com/...)\n"
    "   • Игрок 5 (lvl 8, faceit.com/...)\n"
    "5. <b>Запасной:</b> Игрок 6\n\n"
    "⚠️ <b>Скопируй структуру выше, заполни данные своей команды и пришли одним сообщением:</b>"
)


async def start_team_registration(message: types.Message, bot: Bot, state: FSMContext):
    await state.clear()
    if not await is_subscribed(bot, message.from_user.id):
        await message.answer("Для регистрации команды подпишись на канал:", reply_markup=get_sub_keyboard())
        return

    await state.set_state(TeamRegistration.waiting_for_application)
    await message.answer(SAMPLE_TEXT, disable_web_page_preview=True)


def create_quickpay_url(order_id: str) -> str:
    # Quickpay делает синхронный HTTP-запрос в конструкторе — вызывать через to_thread
    quickpay = Quickpay(
        receiver=YOOMONEY_WALLET,
        quickpay_form="shop",
        targets="Взнос за турнир CS2",
        paymentType="AC",
        sum=ENTRY_FEE,
        label=order_id,
    )
    return quickpay.redirected_url


async def receive_team_application(message: types.Message, state: FSMContext):
    if len(message.text) > MAX_ANKETA_LEN:
        await message.answer(
            f"Заявка слишком длинная. Сократите её до {MAX_ANKETA_LEN} символов и пришлите снова:"
        )
        return

    order_id = f"cs2_{message.from_user.id}_{uuid.uuid4().hex[:6]}"
    username = f"@{message.from_user.username}" if message.from_user.username else "Нет"

    # Формируем ссылку на оплату через ЮMoney Quickpay
    try:
        pay_url = await asyncio.to_thread(create_quickpay_url, order_id)
    except Exception as e:
        logging.error(f"Ошибка создания ссылки на оплату: {e}")
        await message.answer(
            "⚠️ Не удалось сформировать ссылку на оплату. Пришлите заявку ещё раз через минуту."
        )
        return

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT INTO applications (order_id, user_id, username, anketa) VALUES (?, ?, ?, ?)",
            (order_id, message.from_user.id, username, message.text),
        )
        await db.commit()

    await state.clear()

    await message.answer(
        f"📄 <b>Анкета принята!</b>\n\n"
        f"Для подтверждения бронирования слота оплатите взнос: <b>{ENTRY_FEE} ₽</b>.\n\n"
        f"1. Нажмите <b>«Оплатить взнос»</b> (картой РФ или ЮMoney).\n"
        f"2. Подтвердите перевод.\n"
        f"3. Вернитесь и нажмите <b>«Проверить оплату»</b>.",
        reply_markup=get_payment_keyboard(pay_url, order_id),
    )


def check_payment_sync(order_id: str) -> bool:
    try:
        history = ym_client.operation_history(label=order_id)
        for op in history.operations:
            if op.label == order_id and op.status == "success":
                return True
    except Exception as e:
        logging.error(f"Ошибка проверки платежа: {e}")
    return False


async def verify_team_payment(callback: types.CallbackQuery, bot: Bot):
    order_id = callback.data.removeprefix("verify_")
    user_id = callback.from_user.id

    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT user_id, username, anketa, status FROM applications WHERE order_id = ?",
            (order_id,),
        ) as cursor:
            application = await cursor.fetchone()

    if not application or application[0] != user_id:
        await callback.answer("⚠️ Заявка не найдена. Подайте её заново через меню.", show_alert=True)
        return

    _, username, anketa, status = application
    if status != "pending":
        await callback.answer("Оплата по этой заявке уже подтверждена.", show_alert=True)
        return

    paid = await asyncio.to_thread(check_payment_sync, order_id)

    # Для тестовых прогонов без реальной оплаты можно подставить: if True:
    if not paid:
        await callback.answer("⏳ Платёж пока не найден. Подождите 15-20 сек и попробуйте снова.", show_alert=True)
        return

    # Защита от повторного нажатия: статус меняет только первый запрос
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "UPDATE applications SET status = 'paid' WHERE order_id = ? AND status = 'pending'",
            (order_id,),
        )
        await db.commit()
        if cursor.rowcount == 0:
            await callback.answer("Оплата по этой заявке уже подтверждена.", show_alert=True)
            return

    await callback.message.edit_text(
        f"🎉 <b>Оплата успешно подтверждена!</b>\n\n"
        f"Слот для вашей команды забронирован.\n"
        f"🔗 Вступайте в беседу капитанов турнира: {CAPTAINS_CHAT_LINK}",
    )

    # Отправка заявки администратору
    admin_card = (
        f"🟢 <b>НОВАЯ ОПЛАЧЕННАЯ КОМАНДА!</b>\n\n"
        f"💰 Взнос: <code>{ENTRY_FEE} ₽</code> (Оплачено)\n"
        f"🧾 Label заказа: <code>{order_id}</code>\n"
        f"👤 Капитан: {html.escape(username)} (ID: <code>{user_id}</code>)\n\n"
        f"📋 <b>Анкета:</b>\n{html.escape(anketa)}"
    )
    try:
        await bot.send_message(
            chat_id=ADMIN_ID,
            text=admin_card,
            reply_markup=get_admin_verdict_keyboard(user_id),
            disable_web_page_preview=True,
        )
    except Exception as e:
        logging.error(f"Не удалось отправить заявку {order_id} администратору: {e}")
    await callback.answer("Оплата зафиксирована!")


async def admin_approve_cmd(callback: types.CallbackQuery, bot: Bot):
    user_id = int(callback.data.split("_")[2])
    try:
        await bot.send_message(
            chat_id=user_id,
            text="✅ Судьи проверили вашу анкету. Команда официально допущена в турнирную сетку!",
        )
    except Exception as e:
        logging.warning(f"Не удалось уведомить капитана {user_id}: {e}")
    await callback.message.edit_text(
        f"{callback.message.html_text}\n\n━━━━━━━━━━━━━━━━━━━━\n🟢 <b>СТАТУС: В СЕТКЕ</b>",
        reply_markup=None,
        disable_web_page_preview=True,
    )
    await callback.answer("Команда утверждена!")


async def admin_reject_cmd(callback: types.CallbackQuery, bot: Bot):
    user_id = int(callback.data.split("_")[2])
    try:
        await bot.send_message(
            chat_id=user_id,
            text="⚠️ Ваша заявка отклонена судьями турнира. Свяжитесь с поддержкой для разъяснений и возврата взноса.",
        )
    except Exception as e:
        logging.warning(f"Не удалось уведомить капитана {user_id}: {e}")
    await callback.message.edit_text(
        f"{callback.message.html_text}\n\n━━━━━━━━━━━━━━━━━━━━\n🔴 <b>СТАТУС: ОТКЛОНЕНО</b>",
        reply_markup=None,
        disable_web_page_preview=True,
    )
    await callback.answer("Заявка отклонена.")


# ----------------- ВЕТКА 2: АНКЕТА ИГРОКА -----------------
async def my_profile_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT nickname, age, role, faceit_lvl, steam_link, about FROM players WHERE user_id = ?",
            (message.from_user.id,),
        ) as cursor:
            row = await cursor.fetchone()

    if row:
        nick, age, role, lvl, steam, about = (html.escape(str(value)) for value in row)
        await message.answer(
            f"👤 <b>Ваша анкета игрока:</b>\n\n"
            f"• <b>Ник:</b> {nick} ({age} лет)\n"
            f"• <b>Роль:</b> {role}\n"
            f"• <b>Уровень FACEIT:</b> {lvl}\n"
            f"• <b>Steam:</b> {steam}\n"
            f"• <b>О себе / Прайм-тайм:</b> {about}\n\n"
            f"Хотите перезаполнить? Просто напишите новый никнейм:",
            disable_web_page_preview=True,
        )
    else:
        await message.answer(
            "📝 У вас ещё нет анкеты в поиске тиммейтов.\nДавайте создадим её за минуту!\n\nВведите ваш <b>игровой никнейм</b>:"
        )

    await state.set_state(PlayerProfile.waiting_for_nickname)


async def profile_step_nick(message: types.Message, state: FSMContext):
    await state.update_data(nickname=message.text)
    await state.set_state(PlayerProfile.waiting_for_age)
    await message.answer("Сколько вам лет? (напишите числом, например: 16):")


async def profile_step_age(message: types.Message, state: FSMContext):
    if not message.text.isdigit() or not 10 <= int(message.text) <= 99:
        await message.answer("Пожалуйста, введите возраст числом (от 10 до 99):")
        return
    await state.update_data(age=int(message.text))
    await state.set_state(PlayerProfile.waiting_for_role)
    await message.answer(
        "Ваша основная роль в CS2?\n(Например: Снайпер / Рифлер / IGL / Опорник / Энтри)"
    )


async def profile_step_role(message: types.Message, state: FSMContext):
    await state.update_data(role=message.text)
    await state.set_state(PlayerProfile.waiting_for_faceit)
    await message.answer("Ваш уровень/ELO на FACEIT (или ранг Premier):")


async def profile_step_faceit(message: types.Message, state: FSMContext):
    await state.update_data(faceit_lvl=message.text)
    await state.set_state(PlayerProfile.waiting_for_steam)
    await message.answer("Ссылка на ваш профиль Steam (или FACEIT):")


async def profile_step_steam(message: types.Message, state: FSMContext):
    await state.update_data(steam_link=message.text)
    await state.set_state(PlayerProfile.waiting_for_about)
    await message.answer(
        "Расскажите немного о себе и своём прайм-тайме:\n"
        "(Например: Играю с 18:00 по МСК, ищу стак на турнир, без тильта, 3000 часов)"
    )


async def profile_step_about(message: types.Message, state: FSMContext):
    data = await state.get_data()
    username = f"@{message.from_user.username}" if message.from_user.username else ""

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            """
            INSERT OR REPLACE INTO players (user_id, username, nickname, age, role, faceit_lvl, steam_link, about, is_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                message.from_user.id,
                username,
                data["nickname"],
                data["age"],
                data["role"],
                data["faceit_lvl"],
                data["steam_link"],
                message.text,
            ),
        )
        await db.commit()

    await state.clear()
    await message.answer(
        "🎉 <b>Анкета сохранена!</b> Теперь вы видны в поиске тиммейтов.",
        reply_markup=get_main_menu(),
    )


# ----------------- ВЕТКА 3: ПОИСК ТИММЕЙТОВ (СВАЙПЫ) -----------------
async def show_next_candidate(user_id: int, message: types.Message):
    async with aiosqlite.connect(DB_NAME) as db:
        # Проверяем, есть ли у самого юзера анкета
        async with db.execute("SELECT 1 FROM players WHERE user_id = ?", (user_id,)) as cur:
            has_profile = await cur.fetchone()

        if not has_profile:
            await message.answer("⚠️ Чтобы просматривать других игроков, сначала заполните свою анкету во вкладке «👤 Моя анкета игрока».")
            return

        # Ищем случайную анкету, исключая себя, лайкнутых и дизлайкнутых
        query = """
            SELECT user_id, nickname, age, role, faceit_lvl, steam_link, about
            FROM players
            WHERE user_id != ?
              AND is_active = 1
              AND user_id NOT IN (SELECT to_user_id FROM likes WHERE from_user_id = ?)
              AND user_id NOT IN (SELECT to_user_id FROM dislikes WHERE from_user_id = ?)
            ORDER BY RANDOM() LIMIT 1
        """
        async with db.execute(query, (user_id, user_id, user_id)) as cursor:
            candidate = await cursor.fetchone()

    if not candidate:
        await message.answer(
            "😔 Новых анкет пока нет. Вы просмотрели всех доступных игроков!\nЗагляните чуть позже или пригласите друзей в бота."
        )
        return

    c_id = candidate[0]
    nick, age, role, lvl, steam, about = (html.escape(str(value)) for value in candidate[1:])
    card_text = (
        f"🎮 <b>Игрок:</b> {nick} ({age} лет)\n"
        f"🎯 <b>Роль:</b> {role}\n"
        f"🎖 <b>Уровень:</b> {lvl}\n"
        f"🔗 <b>Ссылка:</b> {steam}\n\n"
        f"💬 <b>О себе:</b> {about}"
    )
    await message.answer(card_text, reply_markup=get_swipe_keyboard(c_id), disable_web_page_preview=True)


async def start_search_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    await show_next_candidate(message.from_user.id, message)


async def swipe_like_callback(callback: types.CallbackQuery, bot: Bot):
    await callback.answer()
    target_id = int(callback.data.split("_")[2])
    my_id = callback.from_user.id

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT OR IGNORE INTO likes (from_user_id, to_user_id) VALUES (?, ?)", (my_id, target_id))
        await db.commit()

        # Проверка взаимности
        async with db.execute("SELECT 1 FROM likes WHERE from_user_id = ? AND to_user_id = ?", (target_id, my_id)) as cur:
            is_match = await cur.fetchone()

    await safe_delete(callback.message)

    if is_match:
        # Взаимный мэтч!
        my_user = f"@{callback.from_user.username}" if callback.from_user.username else callback.from_user.full_name
        try:
            target_chat = await bot.get_chat(target_id)
            target_user = f"@{target_chat.username}" if target_chat.username else target_chat.full_name
        except Exception:
            target_user = "пользователем"

        # Сообщение текущему пользователю
        await callback.message.answer(
            f"🔥 <b>У вас взаимный мэтч с {html.escape(target_user)}!</b>\n"
            f"Свяжитесь, собирайте состав и регистрируйтесь на турнир!",
        )

        # Сообщение второму пользователю
        try:
            await bot.send_message(
                chat_id=target_id,
                text=(
                    f"🔥 <b>У вас взаимный мэтч с игроком {html.escape(my_user)}!</b>\n"
                    f"Напишите тиммейту для совместной игры на турнире."
                ),
            )
        except Exception as e:
            logging.warning(f"Не удалось уведомить {target_id} о мэтче: {e}")
    else:
        # Уведомляем о новом лайке
        try:
            await bot.send_message(
                chat_id=target_id,
                text="🔔 Кто-то заинтересовался вашей анкетой в поиске тиммейтов! Откройте поиск, чтобы найти его.",
            )
        except Exception as e:
            logging.warning(f"Не удалось уведомить {target_id} о лайке: {e}")

    # Показываем следующую анкету
    await show_next_candidate(my_id, callback.message)


async def swipe_dislike_callback(callback: types.CallbackQuery):
    await callback.answer()
    target_id = int(callback.data.split("_")[2])
    my_id = callback.from_user.id

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT OR IGNORE INTO dislikes (from_user_id, to_user_id) VALUES (?, ?)", (my_id, target_id))
        await db.commit()

    await safe_delete(callback.message)
    await show_next_candidate(my_id, callback.message)


async def swipe_stop_callback(callback: types.CallbackQuery):
    await callback.answer()
    await safe_delete(callback.message)
    await callback.message.answer("Поиск приостановлен. Вы всегда можете вернуться к нему через меню.", reply_markup=get_main_menu())


# ----------------- ТОЧКА ВХОДА -----------------
async def main():
    await init_db()

    # Прокси из файла .env (если не задан — подключаемся напрямую)
    proxy_url = get_proxy_url()
    session = AiohttpSession(proxy=proxy_url) if proxy_url else None

    bot = Bot(
        token=BOT_TOKEN,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())

    # Старт и кнопки меню — регистрируются первыми, чтобы работать из любого состояния
    dp.message.register(cmd_start, CommandStart())
    dp.message.register(start_team_registration, F.text == "🏆 Зарегистрировать команду")
    dp.message.register(my_profile_cmd, F.text == "👤 Моя анкета игрока")
    dp.message.register(start_search_cmd, F.text == "🔍 Искать тиммейтов")
    dp.callback_query.register(check_sub_callback, F.data == "check_subscription")

    # Регистрация команды
    dp.message.register(receive_team_application, TeamRegistration.waiting_for_application, F.text)
    dp.callback_query.register(verify_team_payment, F.data.startswith("verify_"))
    dp.callback_query.register(admin_approve_cmd, F.data.startswith("admin_approve_"), F.from_user.id == ADMIN_ID)
    dp.callback_query.register(admin_reject_cmd, F.data.startswith("admin_reject_"), F.from_user.id == ADMIN_ID)

    # Анкета игрока
    dp.message.register(profile_step_nick, PlayerProfile.waiting_for_nickname, F.text)
    dp.message.register(profile_step_age, PlayerProfile.waiting_for_age, F.text)
    dp.message.register(profile_step_role, PlayerProfile.waiting_for_role, F.text)
    dp.message.register(profile_step_faceit, PlayerProfile.waiting_for_faceit, F.text)
    dp.message.register(profile_step_steam, PlayerProfile.waiting_for_steam, F.text)
    dp.message.register(profile_step_about, PlayerProfile.waiting_for_about, F.text)

    # Не-текстовые сообщения (фото, стикеры) во время заполнения анкет
    dp.message.register(not_text_fallback, StateFilter(TeamRegistration, PlayerProfile))

    # Поиск тиммейтов (Свайпы)
    dp.callback_query.register(swipe_like_callback, F.data.startswith("swipe_like_"))
    dp.callback_query.register(swipe_dislike_callback, F.data.startswith("swipe_dislike_"))
    dp.callback_query.register(swipe_stop_callback, F.data == "swipe_stop")

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        print("Бот успешно запущен! База данных подключена.")
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
