
import os
import logging
import asyncio
import threading
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler

import httpx
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# -------------------- تنظیمات اصلی --------------------

TOKEN = os.getenv("TOKEN")
API_URL = "https://nerkhara.com/api/v1/latest.json"

CACHE_SECONDS = 45
MAX_DATA_AGE_MINUTES = 90

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# -------------------- شناسه دارایی‌ها --------------------

CURRENCIES = {
    "usd": "دلار آمریکا",
    "eur": "یورو",
    "gbp": "پوند انگلیس",
    "try": "لیر ترکیه",
    "cny": "یوان چین",
    "azn": "منات آذربایجان",
    "aed": "درهم امارات",
    "usdt": "تتر",
}

METALS = {
    "gold-18k": "طلای ۱۸ عیار",
    "gold-24k": "طلای ۲۴ عیار",
    "coin-emami": "سکه امامی",
    "coin-half": "نیم سکه",
    "coin-quarter": "ربع سکه",
}

CRYPTOS = {
    "btc": "بیت‌کوین",
    "eth": "اتریوم",
    "sol": "سولانا",
    "xrp": "ریپل",
    "bnb": "بایننس کوین",
    "doge": "دوج کوین",
    "trx": "ترون",
    "ton": "تون",
}

# -------------------- کش اطلاعات --------------------

market_cache = None
cache_time = 0
cache_lock = asyncio.Lock()


def to_float(value):
    try:
        return float(str(value).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def parse_datetime(value):
    if not value:
        return None

    try:
        value = str(value).replace("Z", "+00:00")
        result = datetime.fromisoformat(value)

        if result.tzinfo is None:
            result = result.replace(tzinfo=timezone.utc)

        return result
    except (ValueError, TypeError):
        return None


def validate_item(item, expected_unit=None):
    if not isinstance(item, dict):
        return False

    if item.get("stale") is True:
        return False

    updated = parse_datetime(item.get("updated_utc"))
    if updated is None:
        return False

    age = (datetime.now(timezone.utc) - updated).total_seconds() / 60

    if age < -5 or age > MAX_DATA_AGE_MINUTES:
        return False

    unit = str(item.get("unit", "")).upper()

    if expected_unit and unit != expected_unit:
        return False

    price = to_float(item.get("price"))

    if price is None or price <= 0:
        return False

    return True


async def fetch_market_data(force_refresh=False):
    global market_cache, cache_time

    async with cache_lock:
        now = asyncio.get_running_loop().time()

        if (
            not force_refresh
            and market_cache is not None
            and now - cache_time < CACHE_SECONDS
        ):
            return market_cache

        try:
            async with httpx.AsyncClient(
                timeout=20,
                follow_redirects=True,
            ) as client:
                response = await client.get(API_URL)
                response.raise_for_status()
                data = response.json()

            raw_items = data.get("items", [])

            if not isinstance(raw_items, list):
                raise ValueError("ساختار پاسخ API معتبر نیست.")

            result = {}

            for item in raw_items:
                if not isinstance(item, dict):
                    continue

                asset_id = item.get("id")

                if not asset_id:
                    continue

                expected_unit = None

                if asset_id in CURRENCIES or asset_id in METALS:
                    expected_unit = "IRT"
                elif asset_id in CRYPTOS:
                    expected_unit = "USDT"

                if validate_item(item, expected_unit):
                    result[asset_id] = item

            # برای نمایش نرخ اصلی، دلار و طلای ۱۸ عیار ضروری هستند.
            if "usd" not in result or "gold-18k" not in result:
                raise ValueError(
                    "اطلاعات معتبر دلار یا طلای ۱۸ عیار از API دریافت نشد."
                )

            market_cache = result
            cache_time = now

            return result

        except Exception as error:
            logger.exception("خطا در دریافت اطلاعات بازار: %s", error)

            if market_cache is not None:
                return market_cache

            raise


# -------------------- تبدیل و نمایش قیمت --------------------

def format_toman(price):
    return f"{int(round(price / 10)):,} تومان"


def get_item_price(item):
    return to_float(item.get("price"))


def get_crypto_price_irt(item, usdt_item):
    direct_price = to_float(item.get("price_irt"))

    if direct_price and direct_price > 0:
        return direct_price

    crypto_price = get_item_price(item)
    usdt_price = get_item_price(usdt_item)

    if not crypto_price or not usdt_price:
        return None

    return crypto_price * usdt_price


def get_latest_update(data):
    dates = []

    for item in data.values():
        updated = parse_datetime(item.get("updated_utc"))
        if updated:
            dates.append(updated)

    if not dates:
        return "نامشخص"

    latest = min(dates)
    return latest.astimezone().strftime("%Y/%m/%d - %H:%M:%S")


def build_price_message(data):
    lines = ["📊 **نرخ روز بازار**", ""]

    lines.append("💱 **ارزها**")

    for asset_id, name in CURRENCIES.items():
        item = data.get(asset_id)

        if item:
            price = get_item_price(item)
            lines.append(f"▪️ {name}: {format_toman(price)}")
        else:
            lines.append(f"▪️ {name}: اطلاعات در دسترس نیست")

    lines.append("")
    lines.append("🪙 **طلا و سکه**")

    for asset_id, name in METALS.items():
        item = data.get(asset_id)

        if item:
            price = get_item_price(item)
            lines.append(f"▪️ {name}: {format_toman(price)}")
        else:
            lines.append(f"▪️ {name}: اطلاعات در دسترس نیست")

    # محاسبه ارزش تقریبی طلای موجود در سکه امامی
    gold_18k = data.get("gold-18k")
    coin = data.get("coin-emami")

    if gold_18k and coin:
        gold_price = get_item_price(gold_18k)
        coin_price = get_item_price(coin)

        if gold_price and coin_price:
            gold_value = gold_price * (8.133 * 0.9 / 0.75)
            bubble = coin_price - gold_value

            lines.append("")
            lines.append(
                f"▪️ حباب تقریبی سکه امامی: {format_toman(bubble)}"
            )

    lines.append("")
    lines.append("₿ **ارزهای دیجیتال**")

    usdt_item = data.get("usdt")

    for asset_id, name in CRYPTOS.items():
        item = data.get(asset_id)

        if item and usdt_item:
            price = get_crypto_price_irt(item, usdt_item)

            if price:
                lines.append(f"▪️ {name}: {format_toman(price)}")
            else:
                lines.append(f"▪️ {name}: اطلاعات در دسترس نیست")
        else:
            lines.append(f"▪️ {name}: اطلاعات در دسترس نیست")

    lines.append("")
    lines.append(f"🕒 آخرین به‌روزرسانی: {get_latest_update(data)}")
    lines.append("")
    lines.append("منبع: NerkhAra")

    return "\n".join(lines)


async def send_all_prices(update, context, force_refresh=False):
    try:
        data = await fetch_market_data(force_refresh)
        message = build_price_message(data)

        await update.message.reply_text(
            message,
            parse_mode="Markdown",
        )

    except Exception:
        await update.message.reply_text(
            "❌ در حال حاضر دریافت نرخ‌ها امکان‌پذیر نیست. لطفاً کمی بعد دوباره تلاش کنید."
        )


# -------------------- دستور شروع --------------------

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [
            InlineKeyboardButton(
                "📊 نمایش نرخ روز",
                callback_data="show_rates",
            )
        ]
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "سلام به ACCB bot خوش اومدین 👋",
        reply_markup=reply_markup,
    )


# -------------------- دستور نرخ کامل --------------------

async def price_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_all_prices(update, context)


# -------------------- نمایش فقط دلار --------------------

async def dollar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        data = await fetch_market_data()
        item = data.get("usd")

        if not item:
            raise ValueError("قیمت دلار موجود نیست.")

        price = get_item_price(item)

        await update.message.reply_text(
            f"💵 **قیمت دلار آمریکا**\n\n"
            f"▪️ {format_toman(price)}\n\n"
            f"🕒 آخرین به‌روزرسانی: {get_latest_update({'usd': item})}",
            parse_mode="Markdown",
        )

    except Exception:
        await update.message.reply_text(
            "❌ دریافت قیمت دلار با مشکل مواجه شد. لطفاً بعداً تلاش کنید."
        )


# -------------------- نمایش فقط طلای ۱۸ عیار --------------------

async def gold_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        data = await fetch_market_data()
        item = data.get("gold-18k")

        if not item:
            raise ValueError("قیمت طلای ۱۸ عیار موجود نیست.")

        price = get_item_price(item)

        await update.message.reply_text(
            f"🪙 **قیمت طلای ۱۸ عیار**\n\n"
            f"▪️ هر گرم: {format_toman(price)}\n\n"
            f"🕒 آخرین به‌روزرسانی: {get_latest_update({'gold-18k': item})}",
            parse_mode="Markdown",
        )

    except Exception:
        await update.message.reply_text(
            "❌ دریافت قیمت طلای ۱۸ عیار با مشکل مواجه شد. لطفاً بعداً تلاش کنید."
        )


# -------------------- دریافت پیام‌های فارسی --------------------

async def text_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if text == "دلار":
        await dollar_command(update, context)

    elif text == "طلا":
        await gold_command(update, context)

    elif text == "نرخ":
        await price_command(update, context)


# -------------------- دکمه نمایش نرخ روز --------------------

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "show_rates":
        try:
            data = await fetch_market_data()
            message = build_price_message(data)

            await query.message.reply_text(
                message,
                parse_mode="Markdown",
            )

        except Exception:
            await query.message.reply_text(
                "❌ در حال حاضر دریافت نرخ‌ها امکان‌پذیر نیست. لطفاً کمی بعد دوباره تلاش کنید."
            )

    elif query.data == "refresh_rates":
        try:
            data = await fetch_market_data(force_refresh=True)
            message = build_price_message(data)

            await query.message.reply_text(
                message,
                parse_mode="Markdown",
            )

        except Exception:
            await query.message.reply_text(
                "❌ به‌روزرسانی نرخ‌ها ناموفق بود. لطفاً دوباره تلاش کنید."
            )


# -------------------- سرور سلامت Railway --------------------

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"ACCB Bot is running!")

    def log_message(self, format, *args):
        pass


def run_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    server.serve_forever()


# -------------------- اجرای ربات --------------------

def main():
    if not TOKEN:
        raise RuntimeError("متغیر محیطی TOKEN تنظیم نشده است.")

    threading.Thread(
        target=run_health_server,
        daemon=True,
    ).start()

    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("price", price_command))

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_command,
        )
    )

    application.add_handler(
        CallbackQueryHandler(button_callback)
    )

    logger.info("ACCB Bot started successfully.")

    application.run_polling()


if __name__ == "__main__":
    main()
