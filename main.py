import os
import asyncio
import time
import json
import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)
from telegram.error import BadRequest

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
BITCOIN_ADDRESS = os.getenv("BITCOIN_ADDRESS", "")

# Товары хранятся в JSON-файле
PRODUCTS_FILE = "products.json"
PENDING_FILE = "pending_orders.json"

def load_json(filename, default):
    if os.path.exists(filename):
        with open(filename, "r") as f:
            return json.load(f)
    return default

def save_json(filename, data):
    with open(filename, "w") as f:
        json.dump(data, f)

PRODUCTS = load_json(PRODUCTS_FILE, [
    {"id": "1", "name": "Стикерпак", "price": 300, "delivery_text": "Спасибо за покупку! Вот ваш стикерпак.", "delivery_photo": None},
    {"id": "2", "name": "Гайд по Python", "price": 500, "delivery_text": "Спасибо! Вот ваш гайд по Python.", "delivery_photo": None},
    {"id": "3", "name": "Премиум доступ", "price": 1000, "delivery_text": "Добро пожаловать в премиум!", "delivery_photo": None},
])

pending_orders = load_json(PENDING_FILE, {})

user_carts = {}

(
    ADD_NAME, ADD_PRICE, ADD_DELIVERY_TEXT, ADD_DELIVERY_PHOTO,
    EDIT_SELECT, EDIT_FIELD, EDIT_VALUE_TEXT, EDIT_VALUE_PHOTO,
) = range(8)

SATOSHI = 100_000_000
ORDER_TIMEOUT = 3600


def get_next_id():
    if not PRODUCTS:
        return "1"
    return str(max(int(p["id"]) for p in PRODUCTS) + 1)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def delete_extra_msgs(context, chat_id: int):
    for msg_id in context.user_data.pop("extra_msgs", []):
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except BadRequest:
            pass


async def fetch_btc_rate() -> float:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get("https://api.coingecko.com/api/v3/simple/price", params={"ids": "bitcoin", "vs_currencies": "rub"})
            return float(r.json()["bitcoin"]["rub"])
    except Exception:
        return 0.0


def rub_to_btc(rub: int, rate: float) -> float:
    return rub / rate if rate > 0 else 0.0


def btc_to_satoshi(btc: float) -> int:
    return int(btc * SATOSHI)


async def get_received_satoshi(address: str) -> int:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"https://blockstream.info/api/address/{address}")
            if r.status_code != 200:
                return -1
            data = r.json()
            chain = data.get("chain_stats", {})
            mempool = data.get("mempool_stats", {})
            funded = chain.get("funded_txo_sum", 0) + mempool.get("funded_txo_sum", 0)
            spent = chain.get("spent_txo_sum", 0) + mempool.get("spent_txo_sum", 0)
            return funded - spent
    except Exception:
        return -1


# ─── ГЛАВНОЕ МЕНЮ ──────────────────────────────────────────────────────────

def main_menu_keyboard(user_id: int) -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("🛍 Каталог", callback_data="catalog"), InlineKeyboardButton("🛒 Корзина", callback_data="view_cart")],
        [InlineKeyboardButton("📋 Мои заказы", callback_data="my_orders"), InlineKeyboardButton("🎁 Пробники", callback_data="samples")],
        [InlineKeyboardButton("🆘 Поддержка", callback_data="support")],
    ]
    if is_admin(user_id):
        keyboard.append([InlineKeyboardButton("⚙️ Админ-панель", callback_data="admin_panel")])
    return InlineKeyboardMarkup(keyboard)


HOME_BTN = InlineKeyboardButton("🏠  Главное меню", callback_data="back")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await delete_extra_msgs(context, update.effective_chat.id)
    msg = await update.message.reply_text("👋 Добро пожаловать в магазин!\n\nВыберите раздел:", reply_markup=main_menu_keyboard(user_id))
    context.user_data["nav_msg"] = msg.message_id


async def back_to_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await delete_extra_msgs(context, query.message.chat_id)
    await query.edit_message_text("👋 Добро пожаловать в магазин!\n\nВыберите раздел:", reply_markup=main_menu_keyboard(query.from_user.id))


# ─── КАТАЛОГ ───────────────────────────────────────────────────────────────

async def show_catalog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not PRODUCTS:
        await query.edit_message_text("😔 Товаров пока нет.", reply_markup=InlineKeyboardMarkup([[HOME_BTN]]))
        return
    text = "🛍  *Наши товары:*\n"
    keyboard = []
    for p in PRODUCTS:
        text += f"\n▫ *{p['name']}* — {p['price']} руб."
        keyboard.append([InlineKeyboardButton(f"+ {p['name']} ({p['price']} ₽)", callback_data=f"add_{p['id']}")])
    keyboard.append([HOME_BTN])
    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))


async def add_to_cart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    prod_id = query.data.split("_", 1)[1]
    product = next((p for p in PRODUCTS if p["id"] == prod_id), None)
    if not product:
        await query.answer("Товар не найден!")
        return
    user_carts.setdefault(user_id, []).append(product)
    await query.answer(f"✅ {product['name']} добавлен в корзину!")


# ─── КОРЗИНА ───────────────────────────────────────────────────────────────

async def view_cart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    cart = user_carts.get(user_id, [])
    if not cart:
        await query.edit_message_text("🛒 Корзина пуста.\n\nДобавьте товары из каталога.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🛍  Перейти в каталог", callback_data="catalog")], [HOME_BTN]]))
        return
    text = "🛒 *Ваша корзина:*\n"
    total = 0
    for p in cart:
        text += f"\n▫ {p['name']} — {p['price']} руб."
        total += p["price"]
    text += f"\n\n💰 *Итого: {total} руб.*"
    keyboard = [
        [InlineKeyboardButton("🛍  Продолжить покупки", callback_data="catalog")],
        [InlineKeyboardButton("₿  Оплатить Bitcoin", callback_data="order_btc")],
        [InlineKeyboardButton("🗑  Очистить корзину", callback_data="clear_cart")],
        [HOME_BTN],
    ]
    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))


async def clear_cart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Корзина очищена")
    user_carts[query.from_user.id] = []
    await query.edit_message_text("🛒 Корзина очищена.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🛍  В каталог", callback_data="catalog")], [HOME_BTN]]))


# ─── BITCOIN ОПЛАТА ────────────────────────────────────────────────────────

async def make_order_btc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    cart = user_carts.get(user_id, [])

    if not cart:
        await query.edit_message_text("🛒 Корзина пуста.", reply_markup=InlineKeyboardMarkup([[HOME_BTN]]))
        return

    if user_id in pending_orders:
        order = pending_orders[user_id]
        await query.edit_message_text(
            f"⏳ *У вас уже есть активный заказ*\n\nСумма: `{order['amount_btc']:.8f}` BTC\n\nАдрес кошелька отправлен отдельным сообщением выше.\nОжидаю поступления средств...",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отменить заказ", callback_data="cancel_order")], [HOME_BTN]]),
        )
        return

    await query.edit_message_text("⏳ Загружаю текущий курс Bitcoin...")

    rate = await fetch_btc_rate()
    if rate <= 0:
        await query.edit_message_text("❌ Не удалось получить курс BTC. Попробуйте позже.", reply_markup=InlineKeyboardMarkup([[HOME_BTN]]))
        return

    total_rub = sum(p["price"] for p in cart)
    btc_amount = rub_to_btc(total_rub, rate)
    btc_amount = round(btc_amount, 5)
    if btc_amount < 0.00001:
        btc_amount = 0.00001
    expected_satoshi = btc_to_satoshi(btc_amount)
    baseline = await get_received_satoshi(BITCOIN_ADDRESS)

    pending_orders[str(user_id)] = {
        "amount_btc": btc_amount,
        "amount_rub": total_rub,
        "cart": list(cart),
        "created_at": time.time(),
        "expected_satoshi": expected_satoshi,
        "baseline_satoshi": baseline if baseline >= 0 else 0,
    }
    save_json(PENDING_FILE, pending_orders)
    user_carts[user_id] = []

    await query.edit_message_text(
        f"₿ *Оплата Bitcoin*\n\nСумма к оплате:\n`{btc_amount:.8f}` BTC\n\n💱 Курс: 1 BTC ≈ {rate:,.0f} ₽\n🛒 Итого: {total_rub} ₽\n\n👇 Адрес кошелька — в следующем сообщении\n\n⏳ Бот проверяет оплату каждые 30 секунд.\nТовар будет отправлен после подтверждения.\nВремя ожидания: до 60 минут.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отменить заказ", callback_data="cancel_order")], [HOME_BTN]]),
    )

    addr_msg = await context.bot.send_message(chat_id=user_id, text=BITCOIN_ADDRESS)
    context.user_data.setdefault("extra_msgs", []).append(addr_msg.message_id)

    instr_msg = await context.bot.send_message(chat_id=user_id, text=f"ℹ️ Отправьте точно {btc_amount:.8f} BTC на адрес выше.\nКомиссия сети — за ваш счёт.\nБот проверит платёж автоматически.")
    context.user_data.setdefault("extra_msgs", []).append(instr_msg.message_id)

    asyncio.create_task(check_payment_loop(user_id, context.application))


async def cancel_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = str(query.from_user.id)
    pending_orders.pop(user_id, None)
    save_json(PENDING_FILE, pending_orders)
    await delete_extra_msgs(context, query.message.chat_id)
    await query.edit_message_text("❌ Заказ отменён.\n\nВы можете начать заново.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🛍  Каталог", callback_data="catalog")], [HOME_BTN]]))


async def check_payment_loop(user_id: int, application):
    uid = str(user_id)
    while True:
        await asyncio.sleep(30)
        order = pending_orders.get(uid)
        if not order:
            break
        if time.time() - order["created_at"] > ORDER_TIMEOUT:
            pending_orders.pop(uid, None)
            save_json(PENDING_FILE, pending_orders)
            await application.bot.send_message(chat_id=user_id, text="⌛ Время ожидания оплаты истекло (1 час).\nЗаказ отменён. Нажмите /start чтобы начать заново.")
            break
        received = await get_received_satoshi(BITCOIN_ADDRESS)
        if received < 0:
            continue
        baseline = order.get("baseline_satoshi", 0)
        if received - baseline >= order["expected_satoshi"]:
            pending_orders.pop(uid, None)
            save_json(PENDING_FILE, pending_orders)
            await application.bot.send_message(chat_id=user_id, text="✅ *Оплата получена!* Отправляю ваши товары...", parse_mode="Markdown")
            for product in order["cart"]:
                text = product.get("delivery_text") or f"Спасибо за покупку товара «{product['name']}»!"
                photo = product.get("delivery_photo")
                if photo:
                    await application.bot.send_photo(chat_id=user_id, photo=photo, caption=text)
                else:
                    await application.bot.send_message(chat_id=user_id, text=text)
            cart_text = "\n".join(f"▫ {p['name']} — {p['price']} руб." for p in order["cart"])
            for admin_id in ADMIN_IDS:
                try:
                    await application.bot.send_message(chat_id=admin_id, text=f"💰 *Новая оплата Bitcoin!*\n\n👤 Покупатель ID: `{user_id}`\n🛒 Товары:\n{cart_text}\n💵 Сумма: {order['amount_rub']} ₽ / `{order['amount_btc']:.8f}` BTC", parse_mode="Markdown")
                except Exception:
                    pass
            break


# ─── МОИ ЗАКАЗЫ ───────────────────────────────────────────────────────────

async def show_my_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = str(query.from_user.id)
    if uid not in pending_orders:
        await query.edit_message_text("📋 У вас нет активных заказов.", reply_markup=InlineKeyboardMarkup([[HOME_BTN]]))
        return
    order = pending_orders[uid]
    cart_items = "\n".join(f"▫ {p['name']} — {p['price']} руб." for p in order["cart"])
    remaining = max(0, ORDER_TIMEOUT - int(time.time() - order["created_at"]))
    await query.edit_message_text(
        f"📋 *Ваш активный заказ*\n\n🛒 Товары:\n{cart_items}\n\n💰 Сумма: {order['amount_rub']} руб.\n₿ К оплате: `{order['amount_btc']:.8f}` BTC\n\n👛 *Адрес:*\n`{BITCOIN_ADDRESS}`\n\n⏳ Осталось: {remaining // 60} мин.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отменить заказ", callback_data="cancel_order")], [HOME_BTN]]),
    )


# ─── ПОДДЕРЖКА ─────────────────────────────────────────────────────────────

async def show_support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "samples":
        text = "🎁 *Пробники*\n\nЧтобы получить бесплатный пробник, напишите администратору.\nОн лично отправит вам подарок!"
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("✉️ Написать админу", url=f"tg://user?id={os.getenv('ADMIN_CONTACT_ID')}")], [HOME_BTN]])
    else:
        text = "🆘 *Техподдержка*\n\nЕсли у вас возникли вопросы или проблемы с заказом, пишите:\n@IchikavaAdmin"
        keyboard = InlineKeyboardMarkup([[HOME_BTN]])
    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=keyboard)


# ─── АДМИН-ПАНЕЛЬ ──────────────────────────────────────────────────────────

def admin_main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Добавить товар", callback_data="adm_add")],
        [InlineKeyboardButton("✏️ Редактировать товар", callback_data="adm_edit")],
        [InlineKeyboardButton("📋 Список товаров", callback_data="adm_list")],
        [HOME_BTN],
    ])


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.answer("Нет доступа!", show_alert=True)
        return
    await query.edit_message_text("⚙️ *Админ-панель*\n\nВыберите действие:", parse_mode="Markdown", reply_markup=admin_main_keyboard())


async def admin_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    if not PRODUCTS:
        text = "Товаров нет."
    else:
        text = "📋 *Товары:*\n"
        for p in PRODUCTS:
            has_photo = "✅" if p.get("delivery_photo") else "❌"
            text += f"\n*{p['name']}* — {p['price']} руб.\nФото: {has_photo}\nТекст: {p.get('delivery_text', '—')}\n"
    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="admin_panel")]]))


# ─── ДОБАВЛЕНИЕ ТОВАРА ────────────────────────────────────────────────────

async def adm_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    context.user_data["new_product"] = {}
    await query.edit_message_text("➕ *Добавление товара*\n\nШаг 1/4 — Введите название товара:", parse_mode="Markdown")
    return ADD_NAME

async def adm_add_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_product"]["name"] = update.message.text.strip()
    await update.message.reply_text("Шаг 2/4 — Введите цену в рублях (только число):")
    return ADD_PRICE

async def adm_add_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        price = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("⚠️ Цена должна быть числом. Введите ещё раз:")
        return ADD_PRICE
    context.user_data["new_product"]["price"] = price
    await update.message.reply_text("Шаг 3/4 — Введите текст, который получит покупатель после оплаты:")
    return ADD_DELIVERY_TEXT

async def adm_add_delivery_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_product"]["delivery_text"] = update.message.text.strip()
    await update.message.reply_text("Шаг 4/4 — Отправьте фото для покупателя или нажмите «Пропустить»:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⏭  Пропустить фото", callback_data="adm_skip_photo")]]))
    return ADD_DELIVERY_PHOTO

async def adm_add_delivery_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_product"]["delivery_photo"] = update.message.photo[-1].file_id
    return await _save_new_product(update, context)

async def adm_skip_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["new_product"]["delivery_photo"] = None
    return await _save_new_product(update, context, query=query)

async def _save_new_product(update, context, query=None):
    global PRODUCTS
    np = context.user_data.pop("new_product")
    np["id"] = get_next_id()
    PRODUCTS.append(np)
    save_json(PRODUCTS_FILE, PRODUCTS)
    text = f"✅ Товар *{np['name']}* успешно добавлен!"
    if query:
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=admin_main_keyboard())
    else:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=admin_main_keyboard())
    return ConversationHandler.END

async def adm_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("new_product", None)
    context.user_data.pop("edit_product_id", None)
    context.user_data.pop("edit_field", None)
    await update.message.reply_text("Отменено.", reply_markup=admin_main_keyboard())
    return ConversationHandler.END


# ─── РЕДАКТИРОВАНИЕ ТОВАРА ────────────────────────────────────────────────

async def adm_edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    if not PRODUCTS:
        await query.edit_message_text("Товаров нет.", reply_markup=admin_main_keyboard())
        return ConversationHandler.END
    keyboard = [[InlineKeyboardButton(p["name"], callback_data=f"esel_{p['id']}")] for p in PRODUCTS]
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="adm_cancel_cb")])
    await query.edit_message_text("✏️ Выберите товар:", reply_markup=InlineKeyboardMarkup(keyboard))
    return EDIT_SELECT

async def adm_edit_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    prod_id = query.data.split("_", 1)[1]
    product = next((p for p in PRODUCTS if p["id"] == prod_id), None)
    if not product:
        await query.answer("Товар не найден!")
        return ConversationHandler.END
    context.user_data["edit_product_id"] = prod_id
    keyboard = [
        [InlineKeyboardButton("📝 Название", callback_data="ef_name")],
        [InlineKeyboardButton("💰 Цена", callback_data="ef_price")],
        [InlineKeyboardButton("📄 Текст после покупки", callback_data="ef_delivery_text")],
        [InlineKeyboardButton("🖼 Фото после покупки", callback_data="ef_delivery_photo")],
        [InlineKeyboardButton("🗑 Удалить товар", callback_data="ef_delete")],
        [InlineKeyboardButton("🔙 Назад", callback_data="adm_cancel_cb")],
    ]
    await query.edit_message_text(f"✏️ *{product['name']}* — {product['price']} руб.\n\nЧто изменить?", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
    return EDIT_FIELD

async def adm_edit_field(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global PRODUCTS
    query = update.callback_query
    await query.answer()
    field = query.data.split("_", 1)[1]
    prod_id = context.user_data.get("edit_product_id")
    if field == "delete":
        PRODUCTS = [p for p in PRODUCTS if p["id"] != prod_id]
        save_json(PRODUCTS_FILE, PRODUCTS)
        await query.edit_message_text("🗑 Товар удалён.", reply_markup=admin_main_keyboard())
        return ConversationHandler.END
    context.user_data["edit_field"] = field
    prompts = {"name": "Введите новое название:", "price": "Введите новую цену (число):", "delivery_text": "Введите новый текст после покупки:", "delivery_photo": "Отправьте новое фото:"}
    await query.edit_message_text(prompts.get(field, "Введите значение:"))
    return EDIT_VALUE_PHOTO if field == "delivery_photo" else EDIT_VALUE_TEXT

async def adm_edit_value_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global PRODUCTS
    field = context.user_data.get("edit_field")
    prod_id = context.user_data.get("edit_product_id")
    product = next((p for p in PRODUCTS if p["id"] == prod_id), None)
    if not product:
        await update.message.reply_text("Товар не найден.")
        return ConversationHandler.END
    value = update.message.text.strip()
    if field == "price":
        try:
            value = int(value)
        except ValueError:
            await update.message.reply_text("⚠️ Цена должна быть числом. Введите ещё раз:")
            return EDIT_VALUE_TEXT
    product[field] = value
    save_json(PRODUCTS_FILE, PRODUCTS)
    await update.message.reply_text("✅ Сохранено!", reply_markup=admin_main_keyboard())
    return ConversationHandler.END

async def adm_edit_value_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global PRODUCTS
    prod_id = context.user_data.get("edit_product_id")
    product = next((p for p in PRODUCTS if p["id"] == prod_id), None)
    if not product:
        await update.message.reply_text("Товар не найден.")
        return ConversationHandler.END
    if update.message.photo:
        product["delivery_photo"] = update.message.photo[-1].file_id
        save_json(PRODUCTS_FILE, PRODUCTS)
        await update.message.reply_text("✅ Фото обновлено!", reply_markup=admin_main_keyboard())
    else:
        await update.message.reply_text("Пожалуйста, отправьте фото.")
        return EDIT_VALUE_PHOTO
    return ConversationHandler.END

async def adm_cancel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.pop("new_product", None)
    context.user_data.pop("edit_product_id", None)
    context.user_data.pop("edit_field", None)
    await query.edit_message_text("⚙️ *Админ-панель*\n\nВыберите действие:", parse_mode="Markdown", reply_markup=admin_main_keyboard())
    return ConversationHandler.END


# ─── ЗАПУСК ────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    add_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(adm_add_start, pattern="^adm_add$")],
        states={
            ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_add_name)],
            ADD_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_add_price)],
            ADD_DELIVERY_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_add_delivery_text)],
            ADD_DELIVERY_PHOTO: [MessageHandler(filters.PHOTO, adm_add_delivery_photo), CallbackQueryHandler(adm_skip_photo, pattern="^adm_skip_photo$")],
        },
        fallbacks=[MessageHandler(filters.COMMAND, adm_cancel), CallbackQueryHandler(adm_cancel_cb, pattern="^adm_cancel_cb$")],
        per_message=False,
    )

    edit_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(adm_edit_start, pattern="^adm_edit$")],
        states={
            EDIT_SELECT: [CallbackQueryHandler(adm_edit_select, pattern="^esel_")],
            EDIT_FIELD: [CallbackQueryHandler(adm_edit_field, pattern="^ef_"), CallbackQueryHandler(adm_cancel_cb, pattern="^adm_cancel_cb$")],
            EDIT_VALUE_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_edit_value_text)],
            EDIT_VALUE_PHOTO: [MessageHandler(filters.PHOTO, adm_edit_value_photo)],
        },
        fallbacks=[MessageHandler(filters.COMMAND, adm_cancel), CallbackQueryHandler(adm_cancel_cb, pattern="^adm_cancel_cb$")],
        per_message=False,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(add_conv)
    app.add_handler(edit_conv)
    app.add_handler(CallbackQueryHandler(admin_panel, pattern="^admin_panel$"))
    app.add_handler(CallbackQueryHandler(admin_list, pattern="^adm_list$"))
    app.add_handler(CallbackQueryHandler(show_catalog, pattern="^catalog$"))
    app.add_handler(CallbackQueryHandler(add_to_cart, pattern="^add_"))
    app.add_handler(CallbackQueryHandler(view_cart, pattern="^view_cart$"))
    app.add_handler(CallbackQueryHandler(clear_cart, pattern="^clear_cart$"))
    app.add_handler(CallbackQueryHandler(make_order_btc, pattern="^order_btc$"))
    app.add_handler(CallbackQueryHandler(cancel_order, pattern="^cancel_order$"))
    app.add_handler(CallbackQueryHandler(show_support, pattern="^support$"))
    app.add_handler(CallbackQueryHandler(show_support, pattern="^samples$"))
    app.add_handler(CallbackQueryHandler(show_my_orders, pattern="^my_orders$"))
    app.add_handler(CallbackQueryHandler(back_to_start, pattern="^back$"))
async def error_handler(update, context):
        try:
            if update and update.callback_query:
                await update.callback_query.answer()
        except:
            pass

    app.add_error_handler(error_handler)
    print("Бот запущен!")
    app.run_polling()


if __name__ == "__main__":
    main()
