import asyncio
import logging
import os
import re
import aiohttp
import aiosqlite
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command, CommandStart
from aiohttp import web

# Токен берем из переменных окружения Render или вставляем строку
BOT_TOKEN = os.getenv("9099946480:AAFrIsXyad0GMLVoYfx_EXhgjeFEuBLRrDQ", "9099946480:AAFrIsXyad0GMLVoYfx_EXhgjeFEuBLRrDQ")
PORT = int(os.getenv("PORT", 8080))
CHECK_INTERVAL_SECONDS = 1800

logging.basicConfig(level=logging.INFO)
bot = Bot(token=9099946480:AAFrIsXyad0GMLVoYfx_EXhgjeFEuBLRrDQ)
dp = Dispatcher()
DB_FILE = "tracker.db"

# --- Фиктивный веб-сервер для прохождения проверки Render ---
async def dummy_web_handler(request):
    return web.Response(text="Bot is running!")

async def start_dummy_server():
    app = web.Application()
    app.router.add_get("/", dummy_web_handler)
    app.router.add_get("/healthz", dummy_web_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logging.info(f"Dummy health-check server started on port {PORT}")

# --- Инициализация БД ---
async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tracks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                item_id TEXT,
                target_price REAL,
                last_price REAL
            )
        """)
        await db.commit()

# --- Извлечение артикула WB ---
def extract_wb_id(text: str) -> str | None:
    match = re.search(r"catalog/(\d+)/detail", text)
    if match:
        return match.group(1)
    if text.strip().isdigit():
        return text.strip()
    return None

# --- Парсер цены Wildberries ---
async def get_wb_price(item_id: str) -> float | None:
    url = f"https://card.wb.ru/cards/v1/detail?appType=1&curr=rub&dest=-1257786&nm={item_id}"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    products = data.get("data", {}).get("products", [])
                    if products:
                        price_kopecks = products[0].get("salePriceU") or products[0].get("priceU")
                        return float(price_kopecks) / 100
    except Exception as e:
        logging.error(f"Ошибка WB {item_id}: {e}")
    return None

# --- Фоновый цикл проверки цен ---
async def price_checker_loop():
    while True:
        try:
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)
            async with aiosqlite.connect(DB_FILE) as db:
                async with db.execute("SELECT id, user_id, item_id, target_price, last_price FROM tracks") as cursor:
                    rows = await cursor.fetchall()

                for row_id, user_id, item_id, target_price, last_price in rows:
                    current_price = await get_wb_price(item_id)
                    if not current_price:
                        continue

                    if current_price <= target_price and (last_price is None or current_price < last_price):
                        text = (
                            f"🔔 **Цена снизилась!**\n\n"
                            f"Артикул: `{item_id}`\n"
                            f"Текущая цена: **{current_price:,.2f} ₽**\n"
                            f"Ваша цель: {target_price:,.2f} ₽\n"
                            f"Ссылка: https://www.wildberries.ru/catalog/{item_id}/detail.aspx"
                        )
                        try:
                            await bot.send_message(chat_id=user_id, text=text, parse_mode="Markdown")
                        except Exception as e:
                            logging.error(f"Ошибка отправки сообщения: {e}")

                    await db.execute("UPDATE tracks SET last_price = ? WHERE id = ?", (current_price, row_id))
                    await db.commit()
                    await asyncio.sleep(1)
        except Exception as e:
            logging.error(f"Сбой цикла проверки цен: {e}")

# --- Обработчики команд ---
@dp.message(CommandStart())
async def start_handler(message: types.Message):
    await message.answer(
        "Привет! Отправь мне ссылку на товар WB (или артикул) и желаемую цену через пробел.\n\n"
        "Пример:\n`https://www.wildberries.ru/catalog/12345678/detail.aspx 1500`\n"
        "или:\n`12345678 1500`",
        parse_mode="Markdown"
    )

@dp.message(Command("list"))
async def list_handler(message: types.Message):
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT item_id, target_price, last_price FROM tracks WHERE user_id = ?", (message.from_user.id,)) as cur:
            rows = await cur.fetchall()

    if not rows:
        await message.answer("У вас пока нет отслеживаемых товаров.")
        return

    text = "📋 **Ваши товары:**\n\n"
    for item_id, target, last in rows:
        last_str = f"{last:,.2f} ₽" if last else "еще не проверено"
        text += f"• `{item_id}` — Цель: {target:,.2f} ₽ (Текущая: {last_str})\n"
    await message.answer(text, parse_mode="Markdown")

@dp.message()
async def add_track_handler(message: types.Message):
    parts = message.text.strip().split()
    if len(parts) < 2:
        await message.answer("Введите ссылку/артикул и цену через пробел. Пример: `12345678 1500`")
        return

    raw_item, raw_price = parts[0], parts[1].replace(",", ".")
    item_id = extract_wb_id(raw_item)

    if not item_id:
        await message.answer("Не удалось распознать артикул товара.")
        return

    try:
        target_price = float(raw_price)
    except ValueError:
        await message.answer("Некорректная цена. Укажите число.")
        return

    initial_price = await get_wb_price(item_id)
    if initial_price is None:
        await message.answer("Не удалось найти товар по этому артикулу на WB.")
        return

    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT INTO tracks (user_id, item_id, target_price, last_price) VALUES (?, ?, ?, ?)",
            (message.from_user.id, item_id, target_price, initial_price)
        )
        await db.commit()

    await message.answer(
        f"✅ Добавлено!\n"
        f"Артикул: `{item_id}`\n"
        f"Текущая цена: **{initial_price:,.2f} ₽**\n"
        f"Цель: **{target_price:,.2f} ₽**",
        parse_mode="Markdown"
    )

# --- Точка входа ---
async def main():
    await init_db()
    # 1. Запуск заглушки сервера для Render
    await start_dummy_server()
    # 2. Запуск фоновой проверки цен
    asyncio.create_task(price_checker_loop())
    # 3. Запуск Telegram бота
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
