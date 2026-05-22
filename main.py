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
    return "🟢 Бот работает!"

def run_web():
    port = int(os.environ.get("PORT", 10000))
    app_web.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run_web)
    t.start()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
BITCOIN_ADDRESS = os.getenv("BITCOIN_ADDRESS", "")
BITCOIN_ADDRESS_2 = os.getenv("BITCOIN_ADDRESS_2", "")
REVIEWS_LINK = "https://t.me/yamadarew?direct"

JSONBIN_API_KEY = os.getenv("JSONBIN_API_KEY", "$2a$10$uxDB6hgHmuCDGDQ335QkLuzjj4epmf/h2vgV/CX6tcQ70RTKXEf3O")
JSONBIN_PRODUCTS_ID = os.getenv("JSONBIN_PRODUCTS_ID", "6a103150ee5a733b12ff50e1")
JSONBIN_PENDING_ID = os.getenv("JSONBIN_PENDING_ID", "6a1032766610dd3ae888aeb5")
JSONBIN_SALES_ID = os.getenv("JSONBIN_SALES_ID", "6a1032236610dd3ae888acd4")

JSONBIN_HEADERS = {
    "X-Master-Key": JSONBIN_API_KEY,
    "Content-Type": "application/json",
}

async def jsonbin_read(bin_id):
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"https://api.jsonbin.io/v3/b/{bin_id}/latest", headers=JSONBIN_HEADERS)
            return r.json()["record"]
    except:
        return None

async def jsonbin_write(bin_id, data):
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.put(f"https://api.jsonbin.io/v3/b/{bin_id}", headers=JSONBIN_HEADERS, json=data)
    except:
        pass

PRODUCTS = []
pending_orders = {}
SALES = []
user_carts = {}

(ADD_NAME, ADD_PRICE, ADD_DELIVERY_TEXT, ADD_DELIVERY_PHOTO,
 EDIT_SELECT, EDIT_FIELD, EDIT_VALUE_TEXT, EDIT_VALUE_PHOTO) = range(8)

SATOSHI, ORDER_TIMEOUT = 100_000_000, 3600

def get_next_id():
    return str(max(int(p["id"]) for p in PRODUCTS) + 1) if PRODUCTS else "1"

def is_admin(uid): return uid in ADMIN_IDS

def get_address_for_order():
    if len(pending_orders) == 0:
        return BITCOIN_ADDRESS
    elif BITCOIN_ADDRESS_2:
        return BITCOIN_ADDRESS_2
    else:
        return BITCOIN_ADDRESS

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
        [InlineKeyboardButton("🛍 Каталог", callback_data="catalog"), InlineKeyboardButton("🛒 Корзина", callback_data="view_cart")],
        [InlineKeyboardButton("📋 Мои заказы", callback_data="my_orders"), InlineKeyboardButton("🎁 Пробники", callback_data="samples")],
        [InlineKeyboardButton("💬 Отзывы", url="https://t.me/yamadarew?direct"), InlineKeyboardButton("🆘 Поддержка", callback_data="support")],
    ]
    if is_admin(uid): k.append([InlineKeyboardButton("⚙️ Админ-панель", callback_data="admin_panel")])
    return InlineKeyboardMarkup(k)

HOME_BTN = InlineKeyboardButton("🏠 Главное меню", callback_data="back")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await delete_extra_msgs(context, update.effective_chat.id)
    await update.message.reply_text("👋 Добро пожаловать в магазин!\n\nВыберите раздел:", reply_markup=main_menu_keyboard(update.effective_user.id))

async def back_to_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    await delete_extra_msgs(context, q.message.chat_id)
    await q.edit_message_text("👋 Добро пожаловать в магазин!\n\nВыберите раздел:", reply_markup=main_menu_keyboard(q.from_user.id))

async def show_catalog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if not PRODUCTS:
        await q.edit_message_text("😔 Товаров пока нет.", reply_markup=InlineKeyboardMarkup([[HOME_BTN]])); return
    text, kb = "🛍 *Наши товары:*\n", []
    for p in PRODUCTS:
        n = len(p.get("items", []))
        text += f"\n▫ *{p['name']}* — {p['price']} руб. (в наличии: {n})"
        kb.append([InlineKeyboardButton(f"➕ {p['name']} ({p['price']} ₽)", callback_data=f"add_{p['id']}")])
    kb.append([HOME_BTN])
    await q.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

async def add_to_cart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; p = next((p for p in PRODUCTS if p["id"] == q.data.split("_", 1)[1]), None)
    if not p: await q.answer("❌ Товар не найден!"); return
    user_carts.setdefault(q.from_user.id, []).append(p)
    await q.answer(f"✅ {p['name']} добавлен в корзину!")

async def view_cart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    cart = user_carts.get(q.from_user.id, [])
    if not cart:
        await q.edit_message_text("🛒 Корзина пуста.\n\nДобавьте товары из каталога.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🛍 В каталог", callback_data="catalog")], [HOME_BTN]])); return
    total = sum(p["price"] for p in cart)
    text = "🛒 *Ваша корзина:*\n" + "\n".join(f"▫ {p['name']} — {p['price']} руб." for p in cart) + f"\n\n💰 *Итого: {total} руб.*"
    kb = [[InlineKeyboardButton("🛍 Продолжить покупки", callback_data="catalog")],
          [InlineKeyboardButton("₿ Оплатить Bitcoin", callback_data="order_btc")],
          [InlineKeyboardButton("🗑 Очистить корзину", callback_data="clear_cart")], [HOME_BTN]]
    await q.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

async def clear_cart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer("🗑 Корзина очищена"); user_carts[q.from_user.id] = []
    await q.edit_message_text("🛒 Корзина очищена.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🛍 В каталог", callback_data="catalog")], [HOME_BTN]]))

async def deliver(uid, product, context):
    if product.get("items"):
        item = product["items"].pop(0); text, photo = item.get("text") or f"🎁 Товар «{product['name']}»!", item.get("photo")
        if not product["items"]: PRODUCTS.remove(product)
        await jsonbin_write(JSONBIN_PRODUCTS_ID, PRODUCTS)
    else: text, photo = f"🎁 Товар «{product['name']}»!", None
    if photo: await context.bot.send_photo(chat_id=uid, photo=photo, caption=text)
    else: await context.bot.send_message(chat_id=uid, text=text)

async def make_order_btc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    uid, cart = q.from_user.id, user_carts.get(q.from_user.id, [])
    if not cart: await q.edit_message_text("🛒 Корзина пуста.", reply_markup=InlineKeyboardMarkup([[HOME_BTN]])); return
    if str(uid) in pending_orders:
        o = pending_orders[str(uid)]
        await q.edit_message_text(f"⏳ *У вас уже есть активный заказ*\n\nСумма: `{o['amount_btc']:.8f}` BTC\n\nОжидаю поступления средств...", parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отменить", callback_data="cancel_order")], [HOME_BTN]])); return
    total = sum(p["price"] for p in cart)
    if total == 0:
        user_carts[uid] = []
        for p in cart: await deliver(uid, p, context)
        for aid in ADMIN_IDS:
            try:
                cart_text = "\n".join(f"▫ {p['name']} — {p['price']} руб." for p in cart)
                await context.bot.send_message(chat_id=aid, text=f"🎁 *Бесплатный товар выдан!*\n\n👤 Покупатель: `{uid}`\n🛒 Товары:\n{cart_text}", parse_mode="Markdown")
            except: pass
        await q.edit_message_text("🎁 Бесплатный товар отправлен!", reply_markup=InlineKeyboardMarkup([[HOME_BTN]]))
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("💬 Оставить отзыв", url=REVIEWS_LINK)]])
        await context.bot.send_message(chat_id=uid, text="💬 Понравился магазин? Оставьте отзыв в нашем канале!", reply_markup=kb)
        return
    await q.edit_message_text("⏳ Загружаю курс Bitcoin...")
    rate = await fetch_btc_rate()
    if rate <= 0: await q.edit_message_text("❌ Не удалось получить курс.", reply_markup=InlineKeyboardMarkup([[HOME_BTN]])); return
    btc = round(rub_to_btc(total, rate), 5)
    if 0 < btc < 0.00001: btc = 0.00001
    order_address = get_address_for_order()
    sat = btc_to_satoshi(btc); base = await get_received_satoshi(order_address)
    pending_orders[str(uid)] = {"amount_btc": btc, "amount_rub": total, "cart": list(cart), "created_at": time.time(), "expected_satoshi": sat, "baseline_satoshi": base if base >= 0 else 0, "address": order_address}
    await jsonbin_write(JSONBIN_PENDING_ID, pending_orders); user_carts[uid] = []
    await q.edit_message_text(
        f"₿ *Оплата Bitcoin*\n\n"
        f"Сумма к оплате:\n`{btc:.8f}` BTC\n\n"
        f"💱 Курс: 1 BTC ≈ {rate:,.0f} ₽\n"
        f"🛒 Итого: {total} ₽\n\n"
        f"👇 Адрес кошелька — в следующем сообщении\n\n"
        f"⏳ Бот проверяет оплату каждые 30 сек.\n"
        f"Товар будет отправлен после подтверждения.\n"
        f"Время ожидания: до 60 минут.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отменить", callback_data="cancel_order")], [HOME_BTN]]))
    await context.bot.send_message(chat_id=uid, text=order_address)
    await context.bot.send_message(chat_id=uid, text=f"ℹ️ *Инструкция по оплате:*\n\n1️⃣ Отправьте точно `{btc:.8f}` BTC на адрес выше\n2️⃣ Бот проверяет каждые 30 сек.\n3️⃣ Товар будет отправлен *сразу после обнаружения транзакции*\n\n⚡ *Скорость: до 1 минуты*", parse_mode="Markdown")
    asyncio.create_task(check_payment_loop(uid, context.application))

async def cancel_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    pending_orders.pop(str(q.from_user.id), None); await jsonbin_write(JSONBIN_PENDING_ID, pending_orders)
    await delete_extra_msgs(context, q.message.chat_id)
    await q.edit_message_text("❌ Заказ отменён.\n\nВы можете начать заново.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🛍 Каталог", callback_data="catalog")], [HOME_BTN]]))

async def check_payment_loop(uid, app):
    while True:
        await asyncio.sleep(30)
        o = pending_orders.get(str(uid))
        if not o: break
        if time.time() - o["created_at"] > ORDER_TIMEOUT:
            pending_orders.pop(str(uid), None); await jsonbin_write(JSONBIN_PENDING_ID, pending_orders)
            await app.bot.send_message(chat_id=uid, text="⌛ Время ожидания истекло (1 час).\nЗаказ отменён."); break
        r = await get_received_satoshi(o.get("address", BITCOIN_ADDRESS))
        if r < 0: continue
        if r - o.get("baseline_satoshi", 0) >= o["expected_satoshi"] - int(o["expected_satoshi"] * 0.05):
            pending_orders.pop(str(uid), None); await jsonbin_write(JSONBIN_PENDING_ID, pending_orders)
            await app.bot.send_message(chat_id=uid, text="✅ *Оплата получена!* Отправляю ваши товары...", parse_mode="Markdown")
            for p in o["cart"]: await deliver(uid, p, app)
            
            sale = {
                "user_id": uid,
                "cart": [{"name": p["name"], "price": p["price"]} for p in o["cart"]],
                "total_rub": o["amount_rub"],
                "total_btc": o["amount_btc"],
                "date": time.strftime("%Y-%m-%d %H:%M")
            }
            SALES.append(sale)
            await jsonbin_write(JSONBIN_SALES_ID, SALES)
            
            cart_text = "\n".join(f"▫ {p['name']} — {p['price']} руб." for p in o["cart"])
            for aid in ADMIN_IDS:
                try: await app.bot.send_message(chat_id=aid, text=f"💰 *Новая оплата!*\n\n👤 Покупатель: `{uid}`\n🛒 Товары:\n{cart_text}\n💵 Сумма: {o['amount_rub']} ₽ / `{o['amount_btc']:.8f}` BTC", parse_mode="Markdown")
                except: pass
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("💬 Оставить отзыв", url=REVIEWS_LINK)]])
            await app.bot.send_message(chat_id=uid, text="💬 Понравился магазин? Оставьте отзыв в нашем канале!", reply_markup=kb)
            break

async def show_my_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    o = pending_orders.get(str(q.from_user.id))
    if not o: await q.edit_message_text("📋 У вас нет активных заказов.", reply_markup=InlineKeyboardMarkup([[HOME_BTN]])); return
    items = "\n".join(f"▫ {p['name']} — {p['price']} руб." for p in o["cart"])
    await q.edit_message_text(
        f"📋 *Ваш активный заказ*\n\n🛒 Товары:\n{items}\n\n"
        f"💰 Сумма: {o['amount_rub']} руб.\n"
        f"₿ К оплате: `{o['amount_btc']:.8f}` BTC\n\n"
        f"👛 *Адрес:*\n`{o.get('address', BITCOIN_ADDRESS)}`\n\n"
        f"⏳ Осталось: {max(0, ORDER_TIMEOUT - int(time.time() - o['created_at'])) // 60} мин.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отменить", callback_data="cancel_order")], [HOME_BTN]]))

async def show_support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if q.data == "samples":
        await q.edit_message_text("🎁 *Пробники*\n\nЧтобы получить бесплатный пробник, напишите администратору.\nОн лично отправит вам подарок!", parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✉️ Написать админу", url=f"tg://user?id={os.getenv('ADMIN_CONTACT_ID')}")], [HOME_BTN]]))
    else:
        await q.edit_message_text("🆘 *Техподдержка*\n\nЕсли у вас возникли вопросы или проблемы с заказом, пишите:\n@IchikavaAdmin", parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[HOME_BTN]]))

def admin_main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Добавить товар", callback_data="adm_add")],
        [InlineKeyboardButton("✏️ Редактировать", callback_data="adm_edit")],
        [InlineKeyboardButton("📋 Список", callback_data="adm_list")],
        [InlineKeyboardButton("📊 Статистика", callback_data="adm_stats")],
        [HOME_BTN],
    ])

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if not is_admin(q.from_user.id): await q.answer("⛔ Нет доступа!"); return
    await q.edit_message_text("⚙️ *Админ-панель*\n\nВыберите действие:", parse_mode="Markdown", reply_markup=admin_main_keyboard())

async def admin_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if not is_admin(q.from_user.id): return
    if not PRODUCTS: await q.edit_message_text("📋 Товаров нет.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="admin_panel")]])); return
    text = "📋 *Товары:*\n" + "\n".join(f"▫ *{p['name']}* — {p['price']} руб. ({len(p.get('items',[]))} шт.)" for p in PRODUCTS)
    await q.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="admin_panel")]]))

async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if not is_admin(q.from_user.id): return
    
    active = len(pending_orders)
    total_sales = len(SALES)
    total_rub = sum(s["total_rub"] for s in SALES)
    
    text = f"📊 *Статистика магазина*\n\n"
    text += f"💰 Всего продаж: {total_sales} на {total_rub} руб.\n"
    text += f"🛒 Активных заказов: {active}\n"
    text += f"📦 Товаров в каталоге: {len(PRODUCTS)}\n"
    
    await q.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="admin_panel")]]))

async def adm_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if not is_admin(q.from_user.id): return
    context.user_data["new"] = {"items": []}
    await q.edit_message_text("➕ *Добавление товара*\n\nШаг 1/3 — Введите название товара:", parse_mode="Markdown"); return ADD_NAME

async def adm_add_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new"]["name"] = update.message.text.strip()
    await update.message.reply_text("Шаг 2/3 — Введите цену в рублях (только число):"); return ADD_PRICE

async def adm_add_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try: context.user_data["new"]["price"] = int(update.message.text.strip())
    except: await update.message.reply_text("⚠️ Цена должна быть числом. Введите ещё раз:"); return ADD_PRICE
    await update.message.reply_text("Шаг 3/3 — Введите текст, который получит покупатель:"); return ADD_DELIVERY_TEXT

async def adm_add_delivery_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if "new" in context.user_data and context.user_data["new"] and "name" in context.user_data["new"]:
        context.user_data["new"]["items"].append({"text": text, "photo": None})
    else:
        prod_id = context.user_data.get("edit_id")
        product = next((p for p in PRODUCTS if p["id"] == prod_id), None)
        if product:
            product.setdefault("items", []).append({"text": text, "photo": None})
            await jsonbin_write(JSONBIN_PRODUCTS_ID, PRODUCTS)
    kb = [[InlineKeyboardButton("⏭ Пропустить фото", callback_data="adm_skip_photo")], [InlineKeyboardButton("✅ Завершить", callback_data="adm_finish")]]
    await update.message.reply_text("🖼 Отправьте фото или нажмите «Пропустить»:", reply_markup=InlineKeyboardMarkup(kb))
    return ADD_DELIVERY_PHOTO

async def adm_add_delivery_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "new" in context.user_data and context.user_data["new"] and "name" in context.user_data["new"]:
        context.user_data["new"]["items"][-1]["photo"] = update.message.photo[-1].file_id
    else:
        prod_id = context.user_data.get("edit_id")
        product = next((p for p in PRODUCTS if p["id"] == prod_id), None)
        if product and product.get("items"):
            product["items"][-1]["photo"] = update.message.photo[-1].file_id
            await jsonbin_write(JSONBIN_PRODUCTS_ID, PRODUCTS)
    kb = [[InlineKeyboardButton("➕ Ещё экземпляр", callback_data="adm_add_more")], [InlineKeyboardButton("✅ Завершить", callback_data="adm_finish")]]
    await update.message.reply_text("✅ Фото добавлено! Добавить ещё экземпляр или завершить?", reply_markup=InlineKeyboardMarkup(kb))
    return ADD_DELIVERY_PHOTO

async def adm_skip_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    kb = [[InlineKeyboardButton("➕ Ещё экземпляр", callback_data="adm_add_more")], [InlineKeyboardButton("✅ Завершить", callback_data="adm_finish")]]
    await q.edit_message_text("Добавить ещё экземпляр или завершить?", reply_markup=InlineKeyboardMarkup(kb))
    return ADD_DELIVERY_PHOTO

async def adm_add_more(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    await q.edit_message_text("📝 Введите текст для следующего экземпляра:"); return ADD_DELIVERY_TEXT

async def adm_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if "new" in context.user_data and context.user_data["new"] and "name" in context.user_data["new"]:
        return await _save_new_product(update, context, query=q)
    else:
        await q.edit_message_text("✅ Экземпляры добавлены!", reply_markup=admin_main_keyboard())
        return ConversationHandler.END

async def _save_new_product(update, context, query=None):
    global PRODUCTS
    np = context.user_data.pop("new"); np["id"] = get_next_id(); PRODUCTS.append(np)
    await jsonbin_write(JSONBIN_PRODUCTS_ID, PRODUCTS)
    text = f"✅ Товар *{np['name']}* успешно добавлен! (экземпляров: {len(np.get('items',[]))})"
    if query: await query.edit_message_text(text, parse_mode="Markdown", reply_markup=admin_main_keyboard())
    else: await update.message.reply_text(text, parse_mode="Markdown", reply_markup=admin_main_keyboard())
    return ConversationHandler.END

async def adm_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for k in ("new", "edit_product_id", "edit_field"): context.user_data.pop(k, None)
    await update.message.reply_text("❌ Отменено.", reply_markup=admin_main_keyboard())
    return ConversationHandler.END

async def adm_edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if not is_admin(q.from_user.id): return
    if not PRODUCTS: await q.edit_message_text("📋 Нет товаров.", reply_markup=admin_main_keyboard()); return ConversationHandler.END
    kb = [[InlineKeyboardButton(f"{p['name']} ({len(p.get('items',[]))} шт.)", callback_data=f"esel_{p['id']}")] for p in PRODUCTS]
    kb.append([InlineKeyboardButton("🔙 Назад", callback_data="adm_cancel_cb")])
    await q.edit_message_text("✏️ Выберите товар для редактирования:", reply_markup=InlineKeyboardMarkup(kb))
    return EDIT_SELECT

async def adm_edit_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    p = next((p for p in PRODUCTS if p["id"] == q.data.split("_", 1)[1]), None)
    if not p: await q.answer("❌ Товар не найден!"); return ConversationHandler.END
    context.user_data["edit_id"] = p["id"]
    kb = [
        [InlineKeyboardButton("📝 Название", callback_data="ef_name")],
        [InlineKeyboardButton("💰 Цена", callback_data="ef_price")],
        [InlineKeyboardButton("➕ Добавить экземпляр", callback_data="ef_add_item")],
        [InlineKeyboardButton("🗑 Удалить", callback_data="ef_delete")],
        [InlineKeyboardButton("🔙 Назад", callback_data="adm_cancel_cb")],
    ]
    await q.edit_message_text(f"✏️ *{p['name']}* — {p['price']} руб. ({len(p.get('items',[]))} шт.)\n\nЧто изменить?", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))
    return EDIT_FIELD

async def adm_edit_field(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global PRODUCTS
    q = update.callback_query; await q.answer()
    f, pid = q.data.split("_", 1)[1], context.user_data.get("edit_id")
    if f == "delete":
        PRODUCTS = [p for p in PRODUCTS if p["id"] != pid]; await jsonbin_write(JSONBIN_PRODUCTS_ID, PRODUCTS)
        await q.edit_message_text("🗑 Товар удалён.", reply_markup=admin_main_keyboard()); return ConversationHandler.END
    if f == "add_item":
        context.user_data.pop("new", None)  # убираем флаг нового товара
        await q.edit_message_text("📝 Введите текст для нового экземпляра:")
        return ADD_DELIVERY_TEXT
    context.user_data["edit_field"] = f
    await q.edit_message_text("📝 Введите новое значение:" if f != "price" else "💰 Введите новую цену (число):")
    return EDIT_VALUE_TEXT

async def adm_edit_value_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global PRODUCTS
    p = next((p for p in PRODUCTS if p["id"] == context.user_data.get("edit_id")), None)
    if not p: await update.message.reply_text("❌ Товар не найден."); return ConversationHandler.END
    v = update.message.text.strip()
    if context.user_data.get("edit_field") == "price":
        try: v = int(v)
        except: await update.message.reply_text("⚠️ Цена должна быть числом!"); return EDIT_VALUE_TEXT
    p[context.user_data["edit_field"]] = v
    await jsonbin_write(JSONBIN_PRODUCTS_ID, PRODUCTS)
    await update.message.reply_text("✅ Сохранено!", reply_markup=admin_main_keyboard())
    return ConversationHandler.END

async def adm_cancel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    for k in ("new", "edit_id", "edit_field"): context.user_data.pop(k, None)
    await q.edit_message_text("⚙️ *Админ-панель*\n\nВыберите действие:", parse_mode="Markdown", reply_markup=admin_main_keyboard())
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
                EDIT_VALUE_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_edit_value_text)],
                ADD_DELIVERY_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_add_delivery_text)],
                ADD_DELIVERY_PHOTO: [MessageHandler(filters.PHOTO, adm_add_delivery_photo),
                                     CallbackQueryHandler(adm_skip_photo, pattern="^adm_skip_photo$"),
                                     CallbackQueryHandler(adm_add_more, pattern="^adm_add_more$"),
                                     CallbackQueryHandler(adm_finish, pattern="^adm_finish$")]},
        fallbacks=[MessageHandler(filters.COMMAND, adm_cancel), CallbackQueryHandler(adm_cancel_cb, pattern="^adm_cancel_cb$")], per_message=False)

    handlers = [
        CommandHandler("start", start), add_conv, edit_conv,
        CallbackQueryHandler(admin_panel, pattern="^admin_panel$"), CallbackQueryHandler(admin_list, pattern="^adm_list$"),
        CallbackQueryHandler(admin_stats, pattern="^adm_stats$"),
        CallbackQueryHandler(show_catalog, pattern="^catalog$"), CallbackQueryHandler(add_to_cart, pattern="^add_"),
        CallbackQueryHandler(view_cart, pattern="^view_cart$"), CallbackQueryHandler(clear_cart, pattern="^clear_cart$"),
        CallbackQueryHandler(make_order_btc, pattern="^order_btc$"), CallbackQueryHandler(cancel_order, pattern="^cancel_order$"),
        CallbackQueryHandler(show_support, pattern="^support$"), CallbackQueryHandler(show_support, pattern="^samples$"),
        CallbackQueryHandler(show_my_orders, pattern="^my_orders$"), CallbackQueryHandler(back_to_start, pattern="^back$")
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
    print("✅ Бот запущен!")
    app.run_polling()

if __name__ == "__main__":
    main()
