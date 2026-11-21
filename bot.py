
import os
import time
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

# ==================================================
# تنظیمات اصلی
# ==================================================

TOKEN = os.getenv("TOKEN")
API_URL = "https://nerkhara.com/api/v1/latest.json"

CACHE_SECONDS = 45
MAX_DATA_AGE_MINUTES = 90

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)

# ==================================================
# شناسه دارایی‌ها
# ==================================================

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

# ==================================================
# کش اطلاعات
# ==================================================

price_cache = {
    "data": None,
    "time": 0,
}

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


def validate_item(item, expected_unit):
    if not isinstance(item, dict):
        return False

    if item.get("stale") is True:
        return False

    price = to_float(item.get("price"))

    if price is None or price <= 0:
        return False

    unit = str(item.get("unit", "")).upper()

    if unit != expected_unit:
        return False

    updated = parse_datetime(item.get("updated_utc"))

    if updated is None:
        return False

    age = (
        datetime.now(timezone.utc) - updated
    ).total_seconds() / 60

    if age < -5 or age > MAX_DATA_AGE_MINUTES:
        return False

    return True


async def fetch_market_data(force_refresh=False):
    async with cache_lock:

        now = time.monotonic()

        # استفاده از اطلاعات کش‌شده
        if (
            not force_refresh
            and price_cache["data"] is not None
            and now - price_cache["time"] < CACHE_SECONDS
        ):
            return price_cache["data"]

        try:
            async with httpx.AsyncClient(
                timeout=20,
                follow_redirects=True,
            ) as client:

                response = await client.get(API_URL)
                response.raise_for_status()

                api_data = response.json()

            raw_items = api_data.get("items", [])

            if not isinstance(raw_items, list):
                raise ValueError("ساختار پاسخ API نامعتبر است.")

            result = {}

            for item in raw_items:

                if not isinstance(item, dict):
                    continue

                asset_id = item.get("id")

                if not asset_id:
                    continue

                # واحد ارزها، طلا و تتر: تومان
                if (
                    asset_id in CURRENCIES
                    or asset_id in METALS
                ):
                    expected_unit = "IRT"

                # واحد رمزارزها: تتر
                elif asset_id in CRYPTOS:
                    expected_unit = "USDT"

                else:
                    continue

                if validate_item(item, expected_unit):
                    result[asset_id] = item

            # دلار، طلای ۱۸ عیار و تتر باید معتبر باشند.
            required_items = [
                "usd",
                "gold-18k",
                "usdt",
            ]

            for asset_id in required_items:
                if asset_id not in result:
                    raise ValueError(
                        f"اطلاعات معتبر {asset_id} دریافت نشد."
                    )

            price_cache["data"] = result
            price_cache["time"] = now

            return result

        except Exception as error:
            logger.exception(
                "خطا در دریافت اطلاعات بازار: %s",
                error,
            )
            raise


# ==================================================
# توابع نمایش قیمت
# ==================================================

def format_toman(price):
    # API نرخ‌اَرا قیمت IRT را از قبل به تومان می‌دهد.
    return f"{int(round(price)):,} تومان"


def get_item_price(item):
    return to_float(item.get("price"))


def get_crypto_price_irt(item, usdt_item):
    # اگر API قیمت مستقیم به تومان داشته باشد، همان را استفاده می‌کنیم.
    direct_price = to_float(item.get("price_irt"))

    if direct_price is not None and direct_price > 0:
        return direct_price

    # در غیر این صورت قیمت دلاری رمزارز را در قیمت تتر ضرب می‌کنیم.
    crypto_price = get_item_price(item)
    usdt_price = get_item_price(usdt_item)

    if crypto_price is None or usdt_price is None:
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

    # زمان قدیمی‌ترین داده در مجموعه
    latest = min(dates)

    return latest.astimezone().strftime(
        "%Y/%m/%d - %H:%M:%S"
    )


# ==================================================
# محاسبه حباب سکه
# ==================================================

def calculate_coin_bubble(data):
    gold = data.get("gold-18k")
    coin = data.get("coin-emami")

    if not gold or not coin:
        return None

    gold_price = get_item_price(gold)
    coin_price = get_item_price(coin)

    if gold_price is None or coin_price is None:
        return None

    pure_gold_weight = 8.133 * (18 / 24)

    intrinsic_value = pure_gold_weight * gold_price

    bubble = coin_price - intrinsic_value

    return bubble


# ==================================================
# ساخت متن نرخ کامل
# ==================================================

def get_price_text(data):

    lines = [
        "📊 **نرخ روز بازار**",
        "",
        "💱 **ارزها**",
    ]

    for asset_id, name in CURRENCIES.items():

        item = data.get(asset_id)

        if item:
            price = get_item_price(item)

            lines.append(
                f"▪️ {name}: {format_toman(price)}"
            )
        else:
            lines.append(
                f"▪️ {name}: اطلاعات در دسترس نیست"
            )

    lines.extend([
        "",
        "🪙 **طلا و سکه**",
    ])

    for asset_id, name in METALS.items():

        item = data.get(asset_id)

        if item:
            price = get_item_price(item)

            lines.append(
                f"▪️ {name}: {format_toman(price)}"
            )
        else:
            lines.append(
                f"▪️ {name}: اطلاعات در دسترس نیست"
            )

    bubble = calculate_coin_bubble(data)

    if bubble is not None:
        lines.append(
            f"▪️ حباب تقریبی سکه امامی: {format_toman(bubble)}"
        )

    lines.extend([
        "",
        "₿ **ارزهای دیجیتال**",
    ])

    usdt_item = data.get("usdt")

    for asset_id, name in CRYPTOS.items():

        item = data.get(asset_id)

        if item and usdt_item:

            price = get_crypto_price_irt(
                item,
                usdt_item,
            )

            if price is not None:
                lines.append(
                    f"▪️ {name}: {format_toman(price)}"
                )
            else:
                lines.append(
                    f"▪️ {name}: اطلاعات در دسترس نیست"
                )

        else:
            lines.append(
                f"▪️ {name}: اطلاعات در دسترس نیست"
            )

    lines.extend([
        "",
        f"🕒 آخرین به‌روزرسانی: {get_latest_update(data)}",
        "",
        "منبع: NerkhAra",
    ])

    return "\n".join(lines)


# ==================================================
# دکمه‌های ربات
# ==================================================

def get_start_keyboard():

    keyboard = [
        [
            InlineKeyboardButton(
                "📊 نمایش نرخ روز",
                callback_data="show_rates",
            )
        ]
    ]

    return InlineKeyboardMarkup(keyboard)


def get_keyboard():

    keyboard = [
        [
            InlineKeyboardButton(
                "🔄 به‌روزرسانی قیمت‌ها",
                callback_data="refresh_prices",
            )
        ]
    ]

    return InlineKeyboardMarkup(keyboard)


# ==================================================
# دستور /start
# ==================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await update.message.reply_text(
        "سلام به ACCB bot خوش اومدین 👋",
        reply_markup=get_start_keyboard(),
    )


# ==================================================
# دستور نرخ کامل
# ==================================================

async def price_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    force_refresh=False,
):

    try:

        data = await fetch_market_data(
            force_refresh=force_refresh
        )

        message = get_price_text(data)

        await update.effective_message.reply_text(
            message,
            parse_mode="Markdown",
            reply_markup=get_keyboard(),
            disable_web_page_preview=True,
        )

    except Exception:

        await update.effective_message.reply_text(
            "❌ دریافت نرخ‌ها با مشکل مواجه شد.\n"
            "لطفاً کمی بعد دوباره تلاش کنید."
        )


# ==================================================
# دستور دلار
# ==================================================

async def dollar_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    try:

        data = await fetch_market_data()

        item = data.get("usd")

        if not item:
            raise ValueError("قیمت دلار موجود نیست.")

        price = get_item_price(item)

        message = (
            "💵 **قیمت دلار آمریکا**\n\n"
            f"▪️ {format_toman(price)}\n\n"
            f"🕒 آخرین به‌روزرسانی: "
            f"{get_latest_update({'usd': item})}"
        )

        await update.effective_message.reply_text(
            message,
            parse_mode="Markdown",
            reply_markup=get_keyboard(),
        )

    except Exception:

        await update.effective_message.reply_text(
            "❌ دریافت قیمت دلار ناموفق بود."
        )


# ==================================================
# دستور طلای ۱۸ عیار
# ==================================================

async def gold_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    try:

        data = await fetch_market_data()

        item = data.get("gold-18k")

        if not item:
            raise ValueError("قیمت طلا موجود نیست.")

        price = get_item_price(item)

        message = (
            "🪙 **قیمت طلای ۱۸ عیار**\n\n"
            f"▪️ هر گرم: {format_toman(price)}\n\n"
            f"🕒 آخرین به‌روزرسانی: "
            f"{get_latest_update({'gold-18k': item})}"
        )

        await update.effective_message.reply_text(
            message,
            parse_mode="Markdown",
            reply_markup=get_keyboard(),
        )

    except Exception:

        await update.effective_message.reply_text(
            "❌ دریافت قیمت طلای ۱۸ عیار ناموفق بود."
        )


# ==================================================
# دستورات متنی فارسی
# ==================================================

async def text_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    text = update.message.text.strip()

    # یکسان‌سازی حروف عربی و فارسی
    text = text.replace("ي", "ی").replace("ك", "ک")

    if text == "دلار":
        await dollar_command(update, context)

    elif text == "طلا":
        await gold_command(update, context)

    elif text == "نرخ":
        await price_command(update, context)


# ==================================================
# مدیریت دکمه‌ها
# ==================================================

async def button_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    await query.answer()

    if query.data == "show_rates":

        try:

            data = await fetch_market_data()

            message = get_price_text(data)

            await query.message.reply_text(
                message,
                parse_mode="Markdown",
                reply_markup=get_keyboard(),
                disable_web_page_preview=True,
            )

        except Exception:

            await query.message.reply_text(
                "❌ دریافت نرخ‌ها ناموفق بود. لطفاً دوباره تلاش کنید."
            )

    elif query.data == "refresh_prices":

        await query.answer(
            "در حال دریافت قیمت‌های جدید...",
            show_alert=False,
        )

        try:

            data = await fetch_market_data(
                force_refresh=True
            )

            message = get_price_text(data)

            await query.message.reply_text(
                message,
                parse_mode="Markdown",
                reply_markup=get_keyboard(),
                disable_web_page_preview=True,
            )

        except Exception:

            await query.message.reply_text(
                "❌ به‌روزرسانی قیمت‌ها ناموفق بود."
            )


# ==================================================
# سرور سلامت Railway
# ==================================================

class HealthHandler(BaseHTTPRequestHandler):

    def do_GET(self):

        self.send_response(200)

        self.send_header(
            "Content-type",
            "text/plain; charset=utf-8",
        )

        self.end_headers()

        self.wfile.write(
            b"ACCB Bot is running!"
        )

    def log_message(self, format, *args):
        pass


def run_health_server():

    port = int(
        os.environ.get("PORT", 10000)
    )

    server = HTTPServer(
        ("0.0.0.0", port),
        HealthHandler,
    )

    server.serve_forever()


# ==================================================
# اجرای ربات
# ==================================================

def main():

    if not TOKEN:
        raise RuntimeError(
            "متغیر محیطی TOKEN تنظیم نشده است."
        )

    threading.Thread(
        target=run_health_server,
        daemon=True,
    ).start()

    application = (
        Application.builder()
        .token(TOKEN)
        .build()
    )

    # دستورات اسلش‌دار
    application.add_handler(
        CommandHandler("start", start_command)
    )

    application.add_handler(
        CommandHandler("price", price_command)
    )

    # دکمه‌های شیشه‌ای
    application.add_handler(
        CallbackQueryHandler(button_handler)
    )

    # دستورات متنی فارسی
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_handler,
        )
    )

    logger.info("ACCB Bot started successfully.")

    application.run_polling()


if __name__ == "__main__":
    main()
