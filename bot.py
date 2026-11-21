
import os
import logging
import asyncio
import threading
import time

from datetime import datetime, timezone, timedelta
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

import httpx

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters
)


# ==========================================
# 1. SETTINGS
# ==========================================

TOKEN = os.getenv("TOKEN")

API_URL = "https://nerkhara.com/api/v1/latest.json"

CACHE_SECONDS = 45
MAX_PRICE_AGE_MINUTES = 90

TEHRAN_TIMEZONE = timezone(timedelta(hours=3, minutes=30))

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

logger = logging.getLogger(__name__)


# ==========================================
# 2. ASSETS
# ==========================================

CURRENCIES = {
    "usd": "🇺🇸 دلار آمریکا",
    "eur": "🇪🇺 یورو",
    "gbp": "🇬🇧 پوند انگلیس",
    "try": "🇹🇷 لیر ترکیه",
    "cny": "🇨🇳 یوان چین",
    "azn": "🇦🇿 منات آذربایجان",
    "aed": "🇦🇪 درهم امارات",
    "usdt": "💲 تتر"
}

METALS = {
    "gold-18k": "طلای ۱۸ عیار",
    "gold-24k": "طلای ۲۴ عیار",
    "coin-emami": "سکه امامی",
    "coin-half": "نیم سکه",
    "coin-quarter": "ربع سکه"
}

CRYPTO = {
    "btc": ("₿", "بیت‌کوین"),
    "eth": ("Ξ", "اتریوم"),
    "sol": ("◎", "سولانا"),
    "xrp": ("✕", "ریپل"),
    "bnb": ("◆", "بایننس‌کوین"),
    "doge": ("Ð", "دوج‌کوین"),
    "trx": ("◈", "ترون"),
    "ton": ("💎", "تون")
}


# ==========================================
# 3. CACHE
# ==========================================

price_cache = {
    "data": None,
    "time": 0
}

cache_lock = asyncio.Lock()


# ==========================================
# 4. TIME VALIDATION
# ==========================================

def parse_api_time(value):

    if not value:
        raise ValueError("API timestamp is missing")

    dt = datetime.fromisoformat(
        value.replace("Z", "+00:00")
    )

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt.astimezone(timezone.utc)


def validate_item(item, asset_id):

    if not item:
        raise ValueError(f"Missing asset: {asset_id}")

    if item.get("stale") is True:
        raise ValueError(f"Stale price: {asset_id}")

    price = item.get("price")

    if price is None:
        raise ValueError(f"Missing price: {asset_id}")

    price = float(price)

    if not price > 0:
        raise ValueError(f"Invalid price: {asset_id}")

    updated = parse_api_time(item.get("updated_utc"))

    now = datetime.now(timezone.utc)

    age = (now - updated).total_seconds() / 60

    if age < -5:
        raise ValueError(f"Future timestamp: {asset_id}")

    if age > MAX_PRICE_AGE_MINUTES:
        raise ValueError(
            f"Price is too old: {asset_id}, age={age:.1f} minutes"
        )

    return updated, price


# ==========================================
# 5. FETCH MARKET DATA
# ==========================================

async def fetch_market_data(force_refresh=False):

    async with cache_lock:

        now = time.monotonic()

        if (
            not force_refresh
            and price_cache["data"] is not None
            and now - price_cache["time"] < CACHE_SECONDS
        ):
            return price_cache["data"]

        logger.info("Fetching market prices...")

        async with httpx.AsyncClient(
            timeout=20.0,
            follow_redirects=True,
            headers={
                "User-Agent": "IranTelegramPriceBot/1.0"
            }
        ) as client:

            response = await client.get(API_URL)
            response.raise_for_status()

            result = response.json()

        if not isinstance(result, dict):
            raise ValueError("Invalid API response")

        if not isinstance(result.get("items"), list):
            raise ValueError("API items are missing")

        items = {
            item["id"]: item
            for item in result["items"]
            if isinstance(item, dict) and item.get("id")
        }

        data = {
            "currencies": {},
            "metals": {},
            "crypto": {},
            "updated": None
        }

        timestamps = []

        # ----------------------------------
        # CURRENCIES
        # ----------------------------------

        for asset_id, name in CURRENCIES.items():

            item = items.get(asset_id)

            updated, price = validate_item(item, asset_id)

            if item.get("unit") != "IRT":
                raise ValueError(
                    f"Unexpected currency unit: {asset_id}"
                )

            data["currencies"][asset_id] = {
                "name": name,
                "price": int(price)
            }

            timestamps.append(updated)

        # ----------------------------------
        # GOLD AND COINS
        # ----------------------------------

        for asset_id, name in METALS.items():

            item = items.get(asset_id)

            updated, price = validate_item(item, asset_id)

            if item.get("unit") != "IRT":
                raise ValueError(
                    f"Unexpected metal unit: {asset_id}"
                )

            data["metals"][asset_id] = {
                "name": name,
                "price": int(price)
            }

            timestamps.append(updated)

        # ----------------------------------
        # USDT PRICE
        # ----------------------------------

        usdt_price = data["currencies"]["usdt"]["price"]

        # ----------------------------------
        # CRYPTOCURRENCIES
        # ----------------------------------

        for asset_id, info in CRYPTO.items():

            item = items.get(asset_id)

            if not item:
                logger.warning(
                    "Crypto unavailable: %s", asset_id
                )
                continue

            try:

                updated, price_usdt = validate_item(
                    item, asset_id
                )

                if item.get("unit") != "USDT":
                    raise ValueError(
                        f"Unexpected crypto unit: {asset_id}"
                    )

                direct_irt = item.get("price_irt")

                if direct_irt is not None and float(direct_irt) > 0:

                    price_toman = int(float(direct_irt))
                    estimated = False

                else:

                    price_toman = int(price_usdt * usdt_price)
                    estimated = True

                data["crypto"][asset_id] = {
                    "symbol": info[0],
                    "name": info[1],
                    "price_usdt": price_usdt,
                    "price_toman": price_toman,
                    "estimated": estimated
                }

                timestamps.append(updated)

            except (ValueError, TypeError) as error:

                logger.warning(
                    "Skipping crypto %s: %s",
                    asset_id,
                    error
                )

        if not timestamps:
            raise ValueError("No valid prices received")

        data["updated"] = min(timestamps)

        # Cache only completely validated main market data
        price_cache["data"] = data
        price_cache["time"] = time.monotonic()

        logger.info("Market data updated successfully")

        return data


# ==========================================
# 6. COIN BUBBLE
# ==========================================

def calculate_coin_bubble(gold_18k, coin_price):

    # Emami coin:
    # 8.133 grams, 900/1000 gold purity.
    # Convert pure gold weight to 18K equivalent.

    gold_equivalent_weight = 8.133 * 0.9 / 0.75

    intrinsic_value = gold_equivalent_weight * gold_18k

    if intrinsic_value <= 0:
        return 0, 0, 0

    bubble_amount = coin_price - intrinsic_value

    bubble_percent = (
        bubble_amount / intrinsic_value
    ) * 100

    return (
        round(intrinsic_value),
        round(bubble_amount),
        round(bubble_percent, 1)
    )


# ==========================================
# 7. PRICE FORMAT
# ==========================================

def format_number(number):
    return f"{number:,}"


def get_price_text(prices):

    text = (
        "📊 *قیمت لحظه‌ای بازار آزاد ایران*\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
    )

    # CURRENCIES

    text += "💱 *ارزهای خارجی*\n\n"

    for item in prices["currencies"].values():

        text += (
            f"{item['name']}: "
            f"`{format_number(item['price'])}` تومان\n"
        )

    # GOLD AND COINS

    text += "\n━━━━━━━━━━━━━━━━━━\n"
    text += "🟡 *طلا و سکه*\n\n"

    for item in prices["metals"].values():

        text += (
            f"{item['name']}: "
            f"`{format_number(item['price'])}` تومان\n"
        )

    # COIN BUBBLE

    gold_price = prices["metals"]["gold-18k"]["price"]

    coin_price = prices["metals"]["coin-emami"]["price"]

    intrinsic, bubble, percentage = calculate_coin_bubble(
        gold_price,
        coin_price
    )

    text += "\n🔍 *تحلیل حباب سکه امامی*\n\n"

    text += (
        f"ارزش ذاتی: `{format_number(intrinsic)}` تومان\n"
        f"میزان حباب: `{format_number(bubble)}` تومان\n"
        f"درصد حباب: `{percentage}%`\n"
    )

    # CRYPTO

    text += "\n━━━━━━━━━━━━━━━━━━\n"
    text += "🪙 *ارزهای دیجیتال*\n\n"

    if prices["crypto"]:

        for item in prices["crypto"].values():

            text += (
                f"{item['symbol']} *{item['name']}*\n"
                f"قیمت دلاری: "
                f"`{item['price_usdt']:,.4f}` USDT\n"
                f"قیمت تومانی: "
                f"`{format_number(item['price_toman'])}` تومان"
            )

            if item["estimated"]:
                text += " (تخمینی با نرخ تتر)"

            text += "\n\n"

    else:

        text += "اطلاعات رمزارزها در دسترس نیست.\n"

    # LAST UPDATE

    updated = prices["updated"].astimezone(
        TEHRAN_TIMEZONE
    )

    updated_text = updated.strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    text += "\n━━━━━━━━━━━━━━━━━━\n"

    text += (
        f"🕒 قدیمی‌ترین زمان ثبت نرخ‌های اصلی: "
        f"`{updated_text}`\n"
    )

    text += (
        "🌐 منبع: [NerkhAra](https://nerkhara.com/)\n"
    )

    text += "\n⚠️ نرخ‌ها مربوط به بازار آزاد هستند."

    return text


# ==========================================
# 8. KEYBOARD
# ==========================================

def get_keyboard():

    keyboard = [
        [
            InlineKeyboardButton(
                "🔄 به‌روزرسانی قیمت‌ها",
                callback_data="refresh_prices"
            )
        ]
    ]

    return InlineKeyboardMarkup(keyboard)


# ==========================================
# 9. PRICE COMMAND
# ==========================================

async def price_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    force_refresh=False
):

    try:

        prices = await fetch_market_data(
            force_refresh=force_refresh
        )

        text = get_price_text(prices)

        if update.callback_query:

            await update.callback_query.message.edit_text(
                text,
                reply_markup=get_keyboard(),
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True
            )

        elif update.message:

            await update.message.reply_text(
                text,
                reply_markup=get_keyboard(),
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True
            )

    except Exception:

        logger.exception("Unable to fetch market prices")

        error_text = (
            "⚠️ *دریافت قیمت‌ها ناموفق بود.*\n\n"
            "اطلاعات معتبر از منبع قیمت دریافت نشد "
            "یا داده‌های دریافتی قدیمی بودند.\n\n"
            "لطفاً چند دقیقه دیگر دوباره تلاش کن."
        )

        try:

            if update.callback_query:

                await update.callback_query.message.edit_text(
                    error_text,
                    reply_markup=get_keyboard(),
                    parse_mode=ParseMode.MARKDOWN
                )

            elif update.message:

                await update.message.reply_text(
                    error_text,
                    parse_mode=ParseMode.MARKDOWN
                )

        except Exception:

            logger.exception("Unable to send error message")


# ==========================================
# 10. REFRESH BUTTON
# ==========================================

async def button_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    if query.data == "refresh_prices":

        await query.answer(
            "در حال دریافت قیمت‌های جدید..."
        )

        await price_command(
            update,
            context,
            force_refresh=True
        )


# ==========================================
# 11. NORMAL MESSAGES
# ==========================================

async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user_text = update.message.text.strip().lower()

    keywords = [
        "قیمت",
        "دلار",
        "طلا",
        "سکه",
        "ارز",
        "قیمتها",
        "نرخ",
        "یورو",
        "پوند",
        "تتر"
    ]

    if any(word in user_text for word in keywords):

        await price_command(update, context)

    else:

        await update.message.reply_text(
            "سلام! 👋\n\n"
            "برای مشاهده قیمت‌های بازار دستور زیر را ارسال کن:\n\n"
            "/price"
        )


# ==========================================
# 12. RAILWAY WEB SERVER
# ==========================================

class SimpleHandler(BaseHTTPRequestHandler):

    def do_GET(self):

        self.send_response(200)

        self.send_header(
            "Content-Type",
            "text/plain; charset=utf-8"
        )

        self.end_headers()

        self.wfile.write(
            b"Telegram Price Bot is running!"
        )

    def log_message(self, format, *args):
        pass


def run_web_server():

    port = int(os.environ.get("PORT", 10000))

    server = ThreadingHTTPServer(
        ("0.0.0.0", port),
        SimpleHandler
    )

    logger.info(
        "Web server started on port %s",
        port
    )

    server.serve_forever()


# ==========================================
# 13. MAIN
# ==========================================

def main():

    if not TOKEN:

        raise RuntimeError(
            "TOKEN environment variable is missing!"
        )

    server_thread = threading.Thread(
        target=run_web_server,
        daemon=True
    )

    server_thread.start()

    application = (
        ApplicationBuilder()
        .token(TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler("start", price_command)
    )

    application.add_handler(
        CommandHandler("price", price_command)
    )

    application.add_handler(
        CallbackQueryHandler(button_handler)
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_message
        )
    )

    logger.info("Telegram Price Bot is starting...")

    application.run_polling(
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()
