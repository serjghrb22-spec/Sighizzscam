import asyncio
import html
import logging
import os
from datetime import datetime, timezone

import aiosqlite
from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiocryptopay import AioCryptoPay, Networks

# =========================
# ВСЁ БЕРЁТСЯ ИЗ ПЕРЕМЕННЫХ BOTHOST
# =========================

BOT_TOKEN = os.getenv("BOT_TOKEN")
CRYPTO_PAY_TOKEN = os.getenv("CRYPTO_PAY_TOKEN")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "@support")
CHANNEL_ID = os.getenv("CHANNEL_ID", "")
MIN_TOPUP = float(os.getenv("MIN_TOPUP", "1"))
NOTIFY_CHAT_ID = os.getenv("NOTIFY_CHAT_ID", "")

# =========================
# ДАННЫЕ ТОВАРА
# =========================

ITEM_LOGIN = "CardIx"
ITEM_PASSWORD = "Pass123"
ITEM_PHONE = "+79091234568"

NO_CODE_PRODUCT_ID = 1

# =========================
# ДАЛЬШЕ НИЧЕГО НЕ ТРОГАЙ
# =========================

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shop.db")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger(__name__)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


async def db() -> aiosqlite.Connection:
    con = await aiosqlite.connect(DB_PATH)
    con.row_factory = aiosqlite.Row
    await con.execute("PRAGMA busy_timeout=5000")
    return con


async def init_db() -> None:
    con = await db()
    try:
        await con.execute("PRAGMA journal_mode=WAL")
        await con.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id    INTEGER PRIMARY KEY,
                username   TEXT,
                balance    REAL DEFAULT 0,
                created_at TEXT
            )
        """)
        await con.execute("""
            CREATE TABLE IF NOT EXISTS products (
                id    INTEGER PRIMARY KEY AUTOINCREMENT,
                name  TEXT NOT NULL,
                price REAL NOT NULL
            )
        """)
        await con.execute("""
            CREATE TABLE IF NOT EXISTS inventory (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id INTEGER NOT NULL,
                login      TEXT,
                password   TEXT,
                phone      TEXT,
                sold       INTEGER DEFAULT 0,
                sold_to    INTEGER,
                sold_at    TEXT,
                FOREIGN KEY (product_id) REFERENCES products (id)
            )
        """)
        await con.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      INTEGER NOT NULL,
                product_id   INTEGER NOT NULL,
                inventory_id INTEGER NOT NULL,
                price        REAL NOT NULL,
                created_at   TEXT NOT NULL
            )
        """)

        cur = await con.execute("SELECT COUNT(*) AS c FROM products")
        cnt = (await cur.fetchone())["c"]
        if cnt == 0:
            await con.executemany(
                "INSERT INTO products (name, price) VALUES (?, ?)",
                [
                    ("Альфа", 70.0),
                    ("Сбер", 60.0),
                    ("Газ", 50.0),
                    ("Т-Банк", 60.0),
                ],
            )
            log.info("Products seeded")

        cur = await con.execute("SELECT COUNT(*) AS c FROM inventory")
        inv_cnt = (await cur.fetchone())["c"]
        if inv_cnt == 0:
            for product_id in (1, 2, 3, 4):
                for _ in range(5):
                    await con.execute(
                        """
                        INSERT INTO inventory (product_id, login, password, phone)
                        VALUES (?, ?, ?, ?)
                        """,
                        (product_id, ITEM_LOGIN, ITEM_PASSWORD, ITEM_PHONE),
                    )
            log.info("Inventory seeded (5 pcs each)")

        await con.commit()
        log.info("DB initialized")
    finally:
        await con.close()


# =========================
# KEYBOARDS
# =========================

def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛒 Товары", callback_data="products")],
        [InlineKeyboardButton(text="💳 Пополнить", callback_data="topup")],
        [InlineKeyboardButton(text="🧾 Покупки", callback_data="purchases")],
        [InlineKeyboardButton(text="💰 Баланс", callback_data="balance")],
        [InlineKeyboardButton(text="🆘 Поддержка", callback_data="support")],
    ])


def back_keyboard(target: str = "home") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data=target)],
    ])


def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton(text="📦 Товары", callback_data="admin_products")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="home")],
    ])


def products_keyboard(products: list) -> InlineKeyboardMarkup:
    buttons = []
    for pid, name, price in products:
        buttons.append([
            InlineKeyboardButton(
                text=f"{name} — {price:.2f} USDT",
                callback_data=f"buy_{pid}",
            )
        ])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="home")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def topup_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="5 USDT", callback_data="pay_5"),
            InlineKeyboardButton(text="10 USDT", callback_data="pay_10"),
            InlineKeyboardButton(text="20 USDT", callback_data="pay_20"),
        ],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="home")],
    ])


# =========================
# HELPERS
# =========================

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def notify_admin(text: str) -> None:
    if not NOTIFY_CHAT_ID:
        return
    try:
        await bot.send_message(int(NOTIFY_CHAT_ID), text)
    except Exception:
        log.exception("notify_admin error")


async def is_subscribed(user_id: int) -> bool:
    if not CHANNEL_ID:
        return True
    try:
        member = await bot.get_chat_member(CHANNEL_ID, user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception:
        log.exception("check subscription failed")
        return True


async def get_or_create_user(user_id: int, username: str | None) -> None:
    con = await db()
    try:
        await con.execute(
            """
            INSERT INTO users (user_id, username, balance, created_at)
            VALUES (?, ?, 0, ?)
            ON CONFLICT(user_id) DO UPDATE SET username = excluded.username
            """,
            (user_id, username, utc_now()),
        )
        await con.commit()
    finally:
        await con.close()


async def give_product_to_user(user_id: int, product_id: int) -> dict | None:
    con = await db()
    try:
        await con.execute("BEGIN IMMEDIATE")

        cur = await con.execute(
            "SELECT id, name, price FROM products WHERE id = ?",
            (product_id,),
        )
        product_row = await cur.fetchone()
        if not product_row:
            await con.rollback()
            return None

        product_name = product_row["name"]
        price = float(product_row["price"])

        cur = await con.execute(
            """
            SELECT id, login, password, phone
            FROM inventory
            WHERE product_id = ? AND sold = 0
            ORDER BY id LIMIT 1
            """,
            (product_id,),
        )
        item = await cur.fetchone()
        if not item:
            await con.rollback()
            return None

        inventory_id = item["id"]
        login = item["login"]
        password = item["password"]
        phone = item["phone"]

        cur = await con.execute(
            """
            UPDATE inventory
            SET sold = 1, sold_to = ?, sold_at = ?
            WHERE id = ? AND sold = 0
            """,
            (user_id, utc_now(), inventory_id),
        )
        if cur.rowcount != 1:
            await con.rollback()
            return None

        await con.execute(
            """
            INSERT INTO orders (user_id, product_id, inventory_id, price, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, product_id, inventory_id, price, utc_now()),
        )

        await con.commit()
        return {
            "name": product_name,
            "price": price,
            "login": login,
            "password": password,
            "phone": phone,
        }
    except Exception:
        await con.rollback()
        log.exception("give_product_to_user error")
        return None
    finally:
        await con.close()


# =========================
# BOT + DISPATCHER + ROUTER
# =========================

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
crypto = AioCryptoPay(token=CRYPTO_PAY_TOKEN, network=Networks.MAIN_NET)

dp = Dispatcher()
router = Router()
dp.include_router(router)


# =========================
# START
# =========================

@router.message(Command("start"))
async def cmd_start(message: Message):
    await get_or_create_user(message.from_user.id, message.from_user.username)
    await message.answer(
        "👋 <b>Добро пожаловать!</b>\n\nВыбери раздел:",
        reply_markup=main_keyboard(),
    )


@router.callback_query(F.data == "home")
async def go_home(callback: CallbackQuery):
    await callback.answer()
    await callback.message.edit_text(
        "🏠 <b>Главное меню</b>\n\nВыбери раздел:",
        reply_markup=main_keyboard(),
    )


# =========================
# BALANCE
# =========================

@router.callback_query(F.data == "balance")
async def show_balance(callback: CallbackQuery):
    await callback.answer()
    con = await db()
    try:
        cur = await con.execute(
            "SELECT balance FROM users WHERE user_id = ?",
            (callback.from_user.id,),
        )
        row = await cur.fetchone()
    finally:
        await con.close()

    balance = float(row["balance"]) if row else 0.0

    await callback.message.edit_text(
        f"💰 <b>Твой баланс:</b> {balance:.2f} USDT",
        reply_markup=back_keyboard(),
    )


# =========================
# TOPUP
# =========================

@router.callback_query(F.data == "topup")
async def topup(callback: CallbackQuery):
    await callback.answer()
    await callback.message.edit_text(
        f"💳 <b>Пополнение баланса</b>\n\n"
        f"Минимум: <b>{MIN_TOPUP:.2f} USDT</b>\n\n"
        f"Выбери сумму:",
        reply_markup=topup_keyboard(),
    )


@router.callback_query(F.data.startswith("pay_"))
async def pay_invoice(callback: CallbackQuery):
    await callback.answer()
    try:
        amount = float(callback.data.split("_")[1])
    except (IndexError, ValueError):
        await callback.message.answer("❌ Некорректная сумма.")
        return

    if amount < MIN_TOPUP:
        await callback.message.answer(
            f"❌ Минимальная сумма пополнения — {MIN_TOPUP:.2f} USDT."
        )
        return

    try:
        invoice = await crypto.create_invoice(
            asset="USDT",
            amount=amount,
            description=f"Пополнение баланса на {amount:.2f} USDT",
        )
        pay_url = invoice.bot_invoice_url
    except Exception:
        log.exception("CryptoPay invoice error")
        await callback.message.answer("❌ Ошибка при создании счёта. Попробуй позже.")
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Оплатить", url=pay_url)],
        [
            InlineKeyboardButton(
                text="🔄 Проверить оплату",
                callback_data=f"check_topup_{invoice.invoice_id}_{amount}",
            )
        ],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="topup")],
    ])
    await callback.message.edit_text(
        f"💳 <b>Счёт на {amount:.2f} USDT создан</b>\n\n"
        f"Нажми «Оплатить», чтобы перейти к оплате.",
        reply_markup=keyboard,
    )


@router.callback_query(F.data.startswith("check_topup_"))
async def check_topup(callback: CallbackQuery):
    await callback.answer()
    try:
        _, _, invoice_id, amount = callback.data.split("_")
        invoice_id = int(invoice_id)
        amount = float(amount)
    except (ValueError, IndexError):
        return

    try:
        invoices = await crypto.get_invoices(invoice_ids=invoice_id)
    except Exception:
        log.exception("CryptoPay get_invoices error")
        await callback.message.answer("❌ Ошибка проверки. Попробуй позже.")
        return

    if not invoices:
        await callback.message.answer("⏳ Счёт не найден.")
        return

    inv = invoices[0]
    if inv.status == "paid":
        con = await db()
        try:
            await con.execute(
                "UPDATE users SET balance = balance + ? WHERE user_id = ?",
                (amount, callback.from_user.id),
            )
            await con.commit()
        finally:
            await con.close()

        await callback.message.edit_text(
            f"✅ <b>Оплата прошла!</b>\n\n"
            f"💰 Баланс пополнен на <b>{amount:.2f} USDT</b>",
            reply_markup=main_keyboard(),
        )

        await notify_admin(
            "💰 <b>Пополнение баланса</b>\n\n"
            f"👤 Пользователь: <code>{callback.from_user.id}</code>\n"
            f"🔗 Юзернейм: @{callback.from_user.username or '—'}\n"
            f"💵 Сумма: <b>{amount:.2f} USDT</b>"
        )
    else:
        await callback.message.answer("⏳ Оплата ещё не поступила. Попробуй позже.")


# =========================
# PRODUCTS
# =========================

@router.callback_query(F.data == "products")
async def show_products(callback: CallbackQuery):
    await callback.answer()
    con = await db()
    try:
        cur = await con.execute(
            """
            SELECT p.id, p.name, p.price
            FROM products p
            WHERE EXISTS (
                SELECT 1 FROM inventory i
                WHERE i.product_id = p.id AND i.sold = 0
            )
            ORDER BY p.id
            """
        )
        rows = await cur.fetchall()
    finally:
        await con.close()

    if not rows:
        await callback.message.edit_text(
            "📦 <b>Товары</b>\n\nПока ничего нет в наличии.",
            reply_markup=back_keyboard(),
        )
        return

    products = [(r["id"], r["name"], r["price"]) for r in rows]
    await callback.message.edit_text(
        "📦 <b>Выбери товар:</b>",
        reply_markup=products_keyboard(products),
    )


# =========================
# BUY
# =========================

@router.callback_query(F.data.startswith("buy_"))
async def buy_product(callback: CallbackQuery):
    await callback.answer()
    user_id = callback.from_user.id

    try:
        product_id = int(callback.data.split("_", 1)[1])
    except (IndexError, ValueError):
        await callback.message.answer("❌ Некорректный товар.")
        return

    con = await db()
    try:
        cur = await con.execute(
            "SELECT id, name, price FROM products WHERE id = ?",
            (product_id,),
        )
        product_row = await cur.fetchone()
    finally:
        await con.close()

    if not product_row:
        await callback.message.answer("❌ Товар не найден.")
        return

    product_name = product_row["name"]
    price = float(product_row["price"])

    try:
        invoice = await crypto.create_invoice(
            asset="USDT",
            amount=price,
            description=f"Покупка: {product_name}",
        )
        pay_url = invoice.bot_invoice_url
    except Exception:
        log.exception("CryptoPay buy invoice error")
        await callback.message.answer("❌ Ошибка при создании счёта. Попробуй позже.")
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Оплатить", url=pay_url)],
        [
            InlineKeyboardButton(
                text="🔄 Проверить оплату",
                callback_data=f"check_buy_{invoice.invoice_id}_{product_id}",
            )
        ],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="products")],
    ])

    await callback.message.edit_text(
        f"🛒 <b>Товар:</b> {html.escape(product_name)}\n"
        f"💵 <b>Цена:</b> {price:.2f} USDT\n\n"
        f"Нажми «Оплатить», чтобы перейти к оплате.",
        reply_markup=keyboard,
    )


@router.callback_query(F.data.startswith("check_buy_"))
async def check_buy_payment(callback: CallbackQuery):
    await callback.answer()
    try:
        _, _, invoice_id, product_id = callback.data.split("_")
        invoice_id = int(invoice_id)
        product_id = int(product_id)
    except (ValueError, IndexError):
        return

    try:
        invoices = await crypto.get_invoices(invoice_ids=invoice_id)
    except Exception:
        log.exception("CryptoPay get_invoices error")
        await callback.message.answer("❌ Ошибка проверки. Попробуй позже.")
        return

    if not invoices:
        await callback.message.answer("⏳ Счёт не найден.")
        return

    inv = invoices[0]
    if inv.status != "paid":
        await callback.message.answer("⏳ Оплата ещё не поступила. Попробуй позже.")
        return

    result = await give_product_to_user(callback.from_user.id, product_id)
    if not result:
        await callback.message.answer(
            "❌ Оплата прошла, но товар закончился. Напиши в поддержку."
        )
        return

    text = (
        "✅ <b>Покупка успешно совершена!</b>\n\n"
        f"🛒 Товар: <b>{html.escape(result['name'])}</b>\n"
        f"💵 Цена: <b>{result['price']:.2f} USDT</b>\n\n"
        "🔐 <b>Данные товара:</b>\n\n"
        f"👤 Логин: <code>{html.escape(result['login'] or '')}</code>\n"
        f"🔑 Пароль: <code>{html.escape(result['password'] or '')}</code>"
    )

    if product_id == NO_CODE_PRODUCT_ID:
        if result["phone"]:
            text += f"\n📱 Телефон: <code>{html.escape(result['phone'])}</code>"
        keyboard = back_keyboard("products")
    else:
        if result["phone"]:
            text += f"\n📱 Телефон: <code>{html.escape(result['phone'])}</code>"
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=f"📱 {result['phone']}",
                callback_data=f"getcode_{product_id}",
            )],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="products")],
        ])

    await callback.message.edit_text(text, reply_markup=keyboard)

    await notify_admin(
        "🛒 <b>Новая покупка</b>\n\n"
        f"👤 Пользователь: <code>{callback.from_user.id}</code>\n"
        f"🔗 Юзернейм: @{callback.from_user.username or '—'}\n"
        f"📦 Товар: <b>{html.escape(result['name'])}</b>\n"
        f"💵 Цена: <b>{result['price']:.2f} USDT</b>"
    )


# =========================
# REQUEST CODE
# =========================

@router.callback_query(F.data.startswith("getcode_"))
async def request_code(callback: CallbackQuery):
    await callback.answer()
    try:
        product_id = int(callback.data.split("_")[1])
    except (IndexError, ValueError):
        return

    await callback.message.answer(
        "⏳ <b>Ожидание кода...</b>\n\n"
            "Код будет отправлен в этот чат в течение нескольких минут.\n"
        "Пожалуйста, не закрывай бота."
    )

    await notify_admin(
        "📩 <b>Запрос кода</b>\n\n"
        f"👤 Пользователь: <code>{callback.from_user.id}</code>\n"
        f"🔗 Юзернейм: @{callback.from_user.username or '—'}\n"
        f"📦 Товар ID: <b>{product_id}</b>\n\n"
        "Отправь код пользователю вручную."
    )


# =========================
# PURCHASES
# =========================

@router.callback_query(F.data == "purchases")
async def purchases(callback: CallbackQuery):
    await callback.answer()
    con = await db()
    try:
        cur = await con.execute(
            """
            SELECT o.id, p.name, o.price, o.created_at
            FROM orders o
            JOIN products p ON p.id = o.product_id
            WHERE o.user_id = ?
            ORDER BY o.id DESC
            LIMIT 20
            """,
            (callback.from_user.id,),
        )
        rows = await cur.fetchall()
    finally:
        await con.close()

    if not rows:
        await callback.message.edit_text(
            "🧾 <b>Покупки</b>\n\nУ тебя пока нет покупок.",
            reply_markup=back_keyboard(),
        )
        return

    text = "🧾 <b>Последние покупки</b>\n\n"
    for r in rows:
        text += (
            f"#{r['id']} — <b>{html.escape(r['name'])}</b>\n"
            f"💵 {float(r['price']):.2f} USDT\n"
            f"🕐 {html.escape(str(r['created_at'])[:19])}\n\n"
        )

    await callback.message.edit_text(text, reply_markup=back_keyboard())


# =========================
# SUPPORT
# =========================

@router.callback_query(F.data == "support")
async def support(callback: CallbackQuery):
    await callback.answer()
    username = SUPPORT_USERNAME.lstrip("@")

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🆘 Написать в поддержку",
            url=f"https://t.me/{username}",
        )],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="home")],
    ])

    await callback.message.edit_text(
        "🆘 <b>Поддержка</b>\n\n"
        "Если возникла проблема — напиши нам.",
        reply_markup=keyboard,
    )


# =========================
# CHECK SUB
# =========================

@router.callback_query(F.data == "check_sub")
async def check_sub(callback: CallbackQuery):
    await callback.answer()
    if await is_subscribed(callback.from_user.id):
        await callback.message.edit_text(
            "✅ Подписка подтверждена!",
            reply_markup=main_keyboard(),
        )
    else:
        await callback.message.answer("❌ Ты ещё не подписался на канал.")


# =========================
# ADMIN
# =========================

@router.message(Command("admin"))
async def admin_command(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("❌ Доступ запрещён.")
        return
    await message.answer(
        "🛠 <b>Админ-панель</b>",
        reply_markup=admin_keyboard(),
    )


@router.callback_query(F.data == "admin")
async def admin_menu(callback: CallbackQuery):
    await callback.answer()
    if not is_admin(callback.from_user.id):
        return
    await callback.message.edit_text(
        "🛠 <b>Админ-панель</b>",
        reply_markup=admin_keyboard(),
    )


@router.callback_query(F.data == "admin_stats")
async def admin_stats(callback: CallbackQuery):
    await callback.answer()
    if not is_admin(callback.from_user.id):
        return

    con = await db()
    try:
        cur = await con.execute("SELECT COUNT(*) AS c FROM users")
        users_count = (await cur.fetchone())["c"]

        cur = await con.execute("SELECT COUNT(*) AS c FROM orders")
        orders_count = (await cur.fetchone())["c"]

        cur = await con.execute("SELECT COALESCE(SUM(price), 0) AS s FROM orders")
        revenue = (await cur.fetchone())["s"]

        cur = await con.execute("SELECT COALESCE(SUM(balance), 0) AS s FROM users")
        balances = (await cur.fetchone())["s"]
    finally:
        await con.close()

    await callback.message.edit_text(
        "📊 <b>Статистика</b>\n\n"
        f"👥 Пользователей: <b>{users_count}</b>\n"
        f"🛒 Покупок: <b>{orders_count}</b>\n"
        f"💰 Продаж: <b>{float(revenue):.2f} USDT</b>\n"
        f"💳 Балансы: <b>{float(balances):.2f} USDT</b>",
        reply_markup=back_keyboard("admin"),
    )


@router.callback_query(F.data == "admin_products")
async def admin_products(callback: CallbackQuery):
    await callback.answer()
    if not is_admin(callback.from_user.id):
        return

    con = await db()
    try:
        cur = await con.execute(
            """
            SELECT p.id, p.name, p.price, COUNT(i.id) AS stock
            FROM products p
            LEFT JOIN inventory i
                ON i.product_id = p.id AND i.sold = 0
            GROUP BY p.id
            ORDER BY p.id
            """
        )
        rows = await cur.fetchall()
    finally:
        await con.close()

    if not rows:
        await callback.message.edit_text(
            "📦 <b>Товары</b>\n\nПусто.",
            reply_markup=back_keyboard("admin"),
        )
        return

    text = "📦 <b>Товары</b>\n\n"
    for r in rows:
        text += (
            f"#{r['id']} <b>{html.escape(r['name'])}</b>\n"
            f"💵 {float(r['price']):.2f} USDT\n"
            f"📦 В наличии: {r['stock']}\n\n"
        )

    await callback.message.edit_text(text, reply_markup=back_keyboard("admin"))


# =========================
# ERROR HANDLER
# =========================

@router.error()
async def error_handler(event):
    log.exception("Unhandled error", exc_info=event.exception)


# =========================
# MAIN
# =========================

async def main():
    await init_db()
    log.info("Bot starting...")
    await bot.delete_webhook(drop_pending_updates=True)
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        await crypto.close()


if __name__ == "__main__":
    asyncio.run(main())
