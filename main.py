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
from flask import Flask
from threading import Thread

app_web = Flask(__name__)

@app_web.route('/')
def home():
    return "Bot is running!"

def run_web():
    port = int(os.environ.get("PORT", 10000))
    app_web.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run_web)
    t.start()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
BITCOIN_ADDRESS = os.getenv("BITCOIN_ADDRESS", "")

PRODUCTS_FILE = "products.json"
PENDING_FILE = "pending_orders.json"
REVIEWS_FILE = "reviews.json"

def storage(filename, data=None):
    if data is not None:
        with open(filename, "w") as f:
            json.dump(data, f)
    elif os.path.exists(filename):
        with open(filename, "r") as f:
            return json.load(f)
    return None

PRODUCTS = storage(PRODUCTS_FILE) or [
    {"id": "1", "name": "Стикерпак", "price": 300, "items": [{"text": "Стикерпак №1", "photo": None}, {"text": "Стикерпак №2", "photo": None}]},
    {"id": "2", "name": "Гайд по Python", "price": 500, "items": [{"text": "Гайд по Python (полный)", "photo": None}]},
    {"id": "3", "name": "Премиум доступ", "price": 1000, "items": [{"text": "Доступ на месяц", "photo": None}, {"text": "Доступ на год", "photo": None}]},
    {"id": "test", "name": "Тестовый товар", "price": 0, "items": [{"text": "Тестовый экземпляр 1", "photo": None}, {"text": "Тестовый экземпляр 2", "photo": None}]},
]

pending_orders = storage(PENDING_FILE) or {}
REVIEWS = storage(REVIEWS_FILE) or []
user_carts = {}

(ADD_NAME, ADD_PRICE, ADD_DELIVERY_TEXT, ADD_DELIVERY_PHOTO,
 EDIT_SELECT, EDIT_FIELD, EDIT_VALUE_TEXT, EDIT_VALUE_PHOTO,
 REVIEW_STAR, REVIEW_TEXT) = range(10)

SATOSHI, ORDER_TIMEOUT = 100_000_000, 3600

def get_next_id():
    return str(max(int(p["id"]) for p in PRODUCTS) + 1) if PRODUCTS else "1"

def is_admin(uid): return uid in ADMIN_IDS

async def delete_extra_msgs(context, chat_id):
    for msg_id in context.user_data.pop("extra_msgs", []):
        try: await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except BadRequest: pass

async def fetch_btc_rate():
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            return float((await c.get("https://api.coinbase.com/v2/prices/BTC-RUB/spot")).json()["data"]["amount"])
    except: return 0.0

def rub_to_btc(rub, rate): return rub / rate if rate > 0 else 0.0
def btc_to_satoshi(btc): return int(btc * SATOSHI)

async def get_received_satoshi(address):
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"https://blockstream.info/api/address/{address}")
            if r.status_code != 200: return -1
            d = r.json()
            return (d["chain_stats"]["funded_txo_sum"] + d["mempool_stats"]["funded_txo_sum"]) - (d["chain_stats"]["spent_txo_sum"] + d["mempool_stats"]["spent_txo_sum"])
    except: return -1

def main_menu_keyboard(uid):
    k = [
        [InlineKeyboardButton("Каталог", callback_data="catalog"), InlineKeyboardButton("Корзина", callback_data="view_cart")],
        [InlineKeyboardButton("Мои заказы", callback_data="my_orders"), InlineKeyboardButton("Пробники", callback_data="samples")],
        [InlineKeyboardButton("Отзывы", callback_data="show_reviews"), InlineKeyboardButton("Поддержка", callback_data="support")],
    ]
    if is_admin(uid): k.append([InlineKeyboardButton("Админ-панель", callback_data="admin_panel")])
    return InlineKeyboardMarkup(k)

HOME_BTN = InlineKeyboardButton("Главное меню", callback_data="back")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await delete_extra_msgs(context, update.effective_chat.id)
    await update.message.reply_text("Добро пожаловать в магазин!", reply_markup=main_menu_keyboard(update.effective_user.id))

async def back_to_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    await delete_extra_msgs(context, q.message.chat_id)
    await q.edit_message_text("Добро пожаловать в магазин!", reply_markup=main_menu_keyboard(q.from_user.id))

async def show_catalog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if not PRODUCTS:
        await q.edit_message_text("Товаров пока нет.", reply_markup=InlineKeyboardMarkup([[HOME_BTN]])); return
    text, kb = "Товары:\n", []
    for p in PRODUCTS:
        n = len(p.get("items", []))
        text += f"\n{p['name']} — {p['price']} руб. (в наличии: {n})"
        kb.append([InlineKeyboardButton(f"+ {p['name']} ({p['price']} руб.)", callback_data=f"add_{p['id']}")])
    kb.append([HOME_BTN])
    await q.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

async def add_to_cart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; p = next((p for p in PRODUCTS if p["id"] == q.data.split("_", 1)[1]), None)
    if not p: await q.answer("Товар не найден!"); return
    user_carts.setdefault(q.from_user.id, []).append(p)
    await q.answer(f"Добавлен {p['name']}!")

async def view_cart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    cart = user_carts.get(q.from_user.id, [])
    if not cart:
        await q.edit_message_text("Корзина пуста.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("В каталог", callback_data="catalog")], [HOME_BTN]])); return
    total = sum(p["price"] for p in cart)
    text = "Корзина:\n" + "\n".join(f"{p['name']} — {p['price']} руб." for p in cart) + f"\n\nИтого: {total} руб."
    kb = [[InlineKeyboardButton("Продолжить покупки", callback_data="catalog")],
          [InlineKeyboardButton("Оплатить Bitcoin", callback_data="order_btc")],
          [InlineKeyboardButton("Очистить корзину", callback_data="clear_cart")], [HOME_BTN]]
    await q.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

async def clear_cart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer("Очищено"); user_carts[q.from_user.id] = []
    await q.edit_message_text("Корзина очищена.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("В каталог", callback_data="catalog")], [HOME_BTN]]))

async def deliver(uid, product, context):
    if product.get("items"):
        item = product["items"].pop(0); text, photo = item.get("text") or f"Товар «{product['name']}»!", item.get("photo")
        if not product["items"]: PRODUCTS.remove(product)
        storage(PRODUCTS_FILE, PRODUCTS)
    else: text, photo = f"Товар «{product['name']}»!", None
    if photo: await context.bot.send_photo(chat_id=uid, photo=photo, caption=text)
    else: await context.bot.send_message(chat_id=uid, text=text)

async def make_order_btc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    uid, cart = q.from_user.id, user_carts.get(q.from_user.id, [])
    if not cart: await q.edit_message_text("Корзина пуста.", reply_markup=InlineKeyboardMarkup([[HOME_BTN]])); return
    if str(uid) in pending_orders:
        o = pending_orders[str(uid)]
        await q.edit_message_text(f"Уже есть заказ на {o['amount_btc']:.8f} BTC. Ожидайте.", parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Отменить", callback_data="cancel_order")], [HOME_BTN]])); return
    total = sum(p["price"] for p in cart)
    if total == 0:
        user_carts[uid] = []
        for p in cart: await deliver(uid, p, context)
        await q.edit_message_text("Бесплатный товар отправлен!", reply_markup=InlineKeyboardMarkup([[HOME_BTN]])); return
    await q.edit_message_text("Загружаю курс...")
    rate = await fetch_btc_rate()
    if rate <= 0: await q.edit_message_text("Не удалось получить курс.", reply_markup=InlineKeyboardMarkup([[HOME_BTN]])); return
    btc = round(rub_to_btc(total, rate), 5)
    if 0 < btc < 0.00001: btc = 0.00001
    sat = btc_to_satoshi(btc); base = await get_received_satoshi(BITCOIN_ADDRESS)
    pending_orders[str(uid)] = {"amount_btc": btc, "amount_rub": total, "cart": list(cart), "created_at": time.time(), "expected_satoshi": sat, "baseline_satoshi": base if base >= 0 else 0}
    storage(PENDING_FILE, pending_orders); user_carts[uid] = []
    await q.edit_message_text(f"Оплата Bitcoin\n\nСумма: {btc:.8f} BTC\nКурс: 1 BTC ≈ {rate:,.0f} руб.\nИтого: {total} руб.\n\nАдрес в следующем сообщении\n\nБот проверяет каждые 30 сек.\nТовар после подтверждения.\nДо 60 минут.",
        parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Отменить", callback_data="cancel_order")], [HOME_BTN]]))
    await context.bot.send_message(chat_id=uid, text=BITCOIN_ADDRESS)
    await context.bot.send_message(chat_id=uid, text=f"Отправьте {btc:.8f} BTC на адрес выше.\nКомиссия сети — за ваш счёт.\nПроверка каждые 30 сек.\nТовар сразу после транзакции.")
    asyncio.create_task(check_payment_loop(uid, context.application))

async def cancel_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    pending_orders.pop(str(q.from_user.id), None); storage(PENDING_FILE, pending_orders)
    await delete_extra_msgs(context, q.message.chat_id)
    await q.edit_message_text("Заказ отменён.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Каталог", callback_data="catalog")], [HOME_BTN]]))

async def check_payment_loop(uid, app):
    while True:
        await asyncio.sleep(30)
        o = pending_orders.get(str(uid))
        if not o: break
        if time.time() - o["created_at"] > ORDER_TIMEOUT:
            pending_orders.pop(str(uid), None); storage(PENDING_FILE, pending_orders)
            await app.bot.send_message(chat_id=uid, text="Время истекло. Заказ отменён."); break
        r = await get_received_satoshi(BITCOIN_ADDRESS)
        if r < 0: continue
        if r - o.get("baseline_satoshi", 0) >= o["expected_satoshi"] - int(o["expected_satoshi"] * 0.05):
            pending_orders.pop(str(uid), None); storage(PENDING_FILE, pending_orders)
            await app.bot.send_message(chat_id=uid, text="Оплата получена! Отправляю...")
            for p in o["cart"]: await deliver(uid, p, app)
            cart_text = "\n".join(f"{p['name']} — {p['price']} руб." for p in o["cart"])
            for aid in ADMIN_IDS:
                try: await app.bot.send_message(chat_id=aid, text=f"Новая оплата!\nПокупатель: {uid}\n{cart_text}\nСумма: {o['amount_rub']} руб. / {o['amount_btc']:.8f} BTC")
                except: pass
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("Оставить отзыв", callback_data="review_shop")]])
            await app.bot.send_message(chat_id=uid, text="Понравился магазин? Оставьте отзыв!", reply_markup=kb)
            break

async def show_my_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    o = pending_orders.get(str(q.from_user.id))
    if not o: await q.edit_message_text("Нет активных заказов.", reply_markup=InlineKeyboardMarkup([[HOME_BTN]])); return
    items = "\n".join(f"{p['name']} — {p['price']} руб." for p in o["cart"])
    await q.edit_message_text(f"Активный заказ\n\n{items}\n\nСумма: {o['amount_rub']} руб.\nК оплате: {o['amount_btc']:.8f} BTC\n\nАдрес: {BITCOIN_ADDRESS}\nОсталось: {max(0, ORDER_TIMEOUT - int(time.time() - o['created_at'])) // 60} мин.",
        parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Отменить", callback_data="cancel_order")], [HOME_BTN]]))

async def show_reviews(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if not REVIEWS: await q.edit_message_text("Пока нет отзывов.", reply_markup=InlineKeyboardMarkup([[HOME_BTN]])); return
    text = "Отзывы:\n" + "\n".join(f"{'⭐'*r['stars']} {r['username']} ({r['date']})\n{r['text']}\n" for r in REVIEWS[-10:])
    await q.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[HOME_BTN]]))

async def review_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    kb = [[InlineKeyboardButton(str(i), callback_data=f"rstars_{i}") for i in range(1, 4)],
          [InlineKeyboardButton(str(i), callback_data=f"rstars_{i}") for i in range(4, 6)]]
    await q.edit_message_text("Оцените магазин:", reply_markup=InlineKeyboardMarkup(kb))
    return REVIEW_STAR

async def review_star(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    context.user_data["stars"] = int(q.data.split("_")[1])
    await q.edit_message_text("Напишите отзыв (или /skip):")
    return REVIEW_TEXT

async def review_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    REVIEWS.append({"user_id": update.effective_user.id, "username": update.effective_user.username or update.effective_user.full_name,
                    "stars": context.user_data.get("stars", 5), "text": update.message.text, "date": time.strftime("%Y-%m-%d %H:%M")})
    storage(REVIEWS_FILE, REVIEWS)
    await update.message.reply_text("Спасибо за отзыв!")
    return ConversationHandler.END

async def review_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await review_text(update, context)

async def show_support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if q.data == "samples":
        await q.edit_message_text("Пробники\n\nНапишите администратору.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Написать", url=f"tg://user?id={os.getenv('ADMIN_CONTACT_ID')}")], [HOME_BTN]]))
    else:
        await q.edit_message_text("Техподдержка\n\nПишите: @IchikavaAdmin", reply_markup=InlineKeyboardMarkup([[HOME_BTN]]))

def admin_main_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("Добавить товар", callback_data="adm_add")],
                                 [InlineKeyboardButton("Редактировать", callback_data="adm_edit")],
                                 [InlineKeyboardButton("Список", callback_data="adm_list")], [HOME_BTN]])

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if not is_admin(q.from_user.id): await q.answer("Нет доступа!"); return
    await q.edit_message_text("Админ-панель", reply_markup=admin_main_keyboard())

async def admin_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if not is_admin(q.from_user.id): return
    if not PRODUCTS: await q.edit_message_text("Товаров нет.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data="admin_panel")]])); return
    text = "Товары:\n" + "\n".join(f"{p['name']} — {p['price']} руб. ({len(p.get('items',[]))} шт.)" for p in PRODUCTS)
    await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data="admin_panel")]]))

async def adm_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if not is_admin(q.from_user.id): return
    context.user_data["new"] = {"items": []}
    await q.edit_message_text("Шаг 1/3 — Название:"); return ADD_NAME

async def adm_add_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new"]["name"] = update.message.text.strip()
    await update.message.reply_text("Шаг 2/3 — Цена:"); return ADD_PRICE

async def adm_add_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try: context.user_data["new"]["price"] = int(update.message.text.strip())
    except: await update.message.reply_text("Число!"); return ADD_PRICE
    await update.message.reply_text("Шаг 3/3 — Текст:"); return ADD_DELIVERY_TEXT

async def adm_add_delivery_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new"]["items"].append({"text": update.message.text.strip(), "photo": None})
    kb = [[InlineKeyboardButton("Пропустить фото", callback_data="adm_skip_photo")], [InlineKeyboardButton("Завершить", callback_data="adm_finish")]]
    await update.message.reply_text("Фото или Пропустить:", reply_markup=InlineKeyboardMarkup(kb))
    return ADD_DELIVERY_PHOTO

async def adm_add_delivery_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new"]["items"][-1]["photo"] = update.message.photo[-1].file_id
    kb = [[InlineKeyboardButton("Ещё", callback_data="adm_add_more")], [InlineKeyboardButton("Завершить", callback_data="adm_finish")]]
    await update.message.reply_text("Ещё или Завершить?", reply_markup=InlineKeyboardMarkup(kb))
    return ADD_DELIVERY_PHOTO

async def adm_skip_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    kb = [[InlineKeyboardButton("Ещё", callback_data="adm_add_more")], [InlineKeyboardButton("Завершить", callback_data="adm_finish")]]
    await q.edit_message_text("Ещё или Завершить?", reply_markup=InlineKeyboardMarkup(kb))
    return ADD_DELIVERY_PHOTO

async def adm_add_more(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    await q.edit_message_text("Текст следующего:"); return ADD_DELIVERY_TEXT

async def adm_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    return await _save_new_product(update, context, query=q)

async def _save_new_product(update, context, query=None):
    global PRODUCTS
    np = context.user_data.pop("new"); np["id"] = get_next_id(); PRODUCTS.append(np)
    storage(PRODUCTS_FILE, PRODUCTS)
    text = f"Товар {np['name']} добавлен ({len(np.get('items',[]))} шт.)"
    if query: await query.edit_message_text(text, reply_markup=admin_main_keyboard())
    else: await update.message.reply_text(text, reply_markup=admin_main_keyboard())
    return ConversationHandler.END

async def adm_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for k in ("new", "edit_product_id", "edit_field"): context.user_data.pop(k, None)
    await update.message.reply_text("Отменено.", reply_markup=admin_main_keyboard())
    return ConversationHandler.END

async def adm_edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if not is_admin(q.from_user.id): return
    if not PRODUCTS: await q.edit_message_text("Нет товаров.", reply_markup=admin_main_keyboard()); return ConversationHandler.END
    kb = [[InlineKeyboardButton(f"{p['name']} ({len(p.get('items',[]))} шт.)", callback_data=f"esel_{p['id']}")] for p in PRODUCTS]
    kb.append([InlineKeyboardButton("Назад", callback_data="adm_cancel_cb")])
    await q.edit_message_text("Выберите:", reply_markup=InlineKeyboardMarkup(kb))
    return EDIT_SELECT

async def adm_edit_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    p = next((p for p in PRODUCTS if p["id"] == q.data.split("_", 1)[1]), None)
    if not p: await q.answer("Не найден!"); return ConversationHandler.END
    context.user_data["edit_id"] = p["id"]
    kb = [[InlineKeyboardButton("Название", callback_data="ef_name")], [InlineKeyboardButton("Цена", callback_data="ef_price")],
          [InlineKeyboardButton("Удалить", callback_data="ef_delete")], [InlineKeyboardButton("Назад", callback_data="adm_cancel_cb")]]
    await q.edit_message_text(f"{p['name']} — {p['price']} руб.\nЧто изменить?", reply_markup=InlineKeyboardMarkup(kb))
    return EDIT_FIELD

async def adm_edit_field(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global PRODUCTS
    q = update.callback_query; await q.answer()
    f, pid = q.data.split("_", 1)[1], context.user_data.get("edit_id")
    if f == "delete":
        PRODUCTS = [p for p in PRODUCTS if p["id"] != pid]; storage(PRODUCTS_FILE, PRODUCTS)
        await q.edit_message_text("Удалён.", reply_markup=admin_main_keyboard()); return ConversationHandler.END
    context.user_data["edit_field"] = f
    await q.edit_message_text("Введите новое значение:" if f != "price" else "Цена (число):")
    return EDIT_VALUE_TEXT

async def adm_edit_value_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global PRODUCTS
    p = next((p for p in PRODUCTS if p["id"] == context.user_data.get("edit_id")), None)
    if not p: await update.message.reply_text("Не найден."); return ConversationHandler.END
    v = update.message.text.strip()
    if context.user_data.get("edit_field") == "price":
        try: v = int(v)
        except: await update.message.reply_text("Число!"); return EDIT_VALUE_TEXT
    p[context.user_data["edit_field"]] = v
    storage(PRODUCTS_FILE, PRODUCTS)
    await update.message.reply_text("Сохранено!", reply_markup=admin_main_keyboard())
    return ConversationHandler.END

async def adm_cancel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    for k in ("new", "edit_id", "edit_field"): context.user_data.pop(k, None)
    await q.edit_message_text("Админ-панель", reply_markup=admin_main_keyboard())
    return ConversationHandler.END

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    add_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(adm_add_start, pattern="^adm_add$")],
        states={ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_add_name)],
                ADD_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_add_price)],
                ADD_DELIVERY_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_add_delivery_text)],
                ADD_DELIVERY_PHOTO: [MessageHandler(filters.PHOTO, adm_add_delivery_photo),
                                     CallbackQueryHandler(adm_skip_photo, pattern="^adm_skip_photo$"),
                                     CallbackQueryHandler(adm_add_more, pattern="^adm_add_more$"),
                                     CallbackQueryHandler(adm_finish, pattern="^adm_finish$")]},
        fallbacks=[MessageHandler(filters.COMMAND, adm_cancel), CallbackQueryHandler(adm_cancel_cb, pattern="^adm_cancel_cb$")], per_message=False)

    edit_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(adm_edit_start, pattern="^adm_edit$")],
        states={EDIT_SELECT: [CallbackQueryHandler(adm_edit_select, pattern="^esel_")],
                EDIT_FIELD: [CallbackQueryHandler(adm_edit_field, pattern="^ef_"), CallbackQueryHandler(adm_cancel_cb, pattern="^adm_cancel_cb$")],
                EDIT_VALUE_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_edit_value_text)]},
        fallbacks=[MessageHandler(filters.COMMAND, adm_cancel), CallbackQueryHandler(adm_cancel_cb, pattern="^adm_cancel_cb$")], per_message=False)

    review_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(review_start, pattern="^review_shop$")],
        states={REVIEW_STAR: [CallbackQueryHandler(review_star, pattern="^rstars_")],
                REVIEW_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, review_text), CommandHandler("skip", review_skip)]},
        fallbacks=[], per_message=True)

    handlers = [
        CommandHandler("start", start), add_conv, edit_conv, review_conv,
        CallbackQueryHandler(admin_panel, pattern="^admin_panel$"), CallbackQueryHandler(admin_list, pattern="^adm_list$"),
        CallbackQueryHandler(show_catalog, pattern="^catalog$"), CallbackQueryHandler(add_to_cart, pattern="^add_"),
        CallbackQueryHandler(view_cart, pattern="^view_cart$"), CallbackQueryHandler(clear_cart, pattern="^clear_cart$"),
        CallbackQueryHandler(make_order_btc, pattern="^order_btc$"), CallbackQueryHandler(cancel_order, pattern="^cancel_order$"),
        CallbackQueryHandler(show_reviews, pattern="^show_reviews$"), CallbackQueryHandler(show_support, pattern="^support$"),
        CallbackQueryHandler(show_support, pattern="^samples$"), CallbackQueryHandler(show_my_orders, pattern="^my_orders$"),
        CallbackQueryHandler(back_to_start, pattern="^back$")
    ]
    for h in handlers: app.add_handler(h)

    for uid in list(pending_orders.keys()):
        asyncio.create_task(check_payment_loop(int(uid), app))

    async def error_handler(update, context):
        try:
            if update and update.callback_query: await update.callback_query.answer()
        except: pass

    app.add_error_handler(error_handler)
    keep_alive()
    print("Бот запущен!")
    app.run_polling()

if __name__ == "__main__":
    main()
