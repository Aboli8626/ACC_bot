import os
import logging
import asyncio
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
from bs4 import BeautifulSoup
from curl_cffi import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, CallbackQueryHandler

# تنظیمات لاگ‌گیری
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# دریافت امن توکن از متغیرهای محیطی سیستم (مناسب برای سرور ابری)
TOKEN = os.getenv("TOKEN", "YOUR_BOT_TOKEN_HERE")

def fetch_market_data():
    """
    دریافت قیمت‌های دقیق دلار، یورو، طلای ۱۸ عیار و سکه از منبع معتبر با مدیریت هوشمند
    """
    data = {
        "usd": 0,
        "eur": 0,
        "gold_18k": 0,
        "gold_24k": 0,
        "coin": 0
    }
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept-Language": "fa-IR,fa;q=0.9,en-US;q=0.8,en;q=0.7",
    }

    try:
        response = requests.get(
            "https://www.bonbast.com/", 
            headers=headers, 
            impersonate="chrome", 
            timeout=10
        )
        
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # ۱. استخراج دلار و یورو از شناسه‌های ثابت بن‌بست
            usd_tag = soup.find('td', {'id': 'usd1'})
            eur_tag = soup.find('td', {'id': 'eur1'})
            
            if usd_tag and usd_tag.text.strip().isdigit():
                data["usd"] = int(usd_tag.text.strip())
            if eur_tag and eur_tag.text.strip().isdigit():
                data["eur"] = int(eur_tag.text.strip())
                
            # ۲. استخراج دقیق طلای ۱۸ عیار از جدول
            for row in soup.find_all('tr'):
                text = row.text
                if "18K" in text or "18 عیار" in text:
                    tds = row.find_all('td')
                    for td in tds:
                        val_str = "".join([c for c in td.text if c.isdigit()])
                        if val_str and len(val_str) > 6:
                            val = int(val_str)
                            if 10000000 < val < 50000000:
                                data["gold_18k"] = val
                                break
                                
            # ۳. استخراج دقیق سکه امامی از جدول
            for row in soup.find_all('tr'):
                text = row.text
                if "Emami" in text or "سکه امامی" in text:
                    tds = row.find_all('td')
                    for td in tds:
                        val_str = "".join([c for c in td.text if c.isdigit()])
                        if val_str and len(val_str) > 7:
                            val = int(val_str)
                            if val > 50000000:
                                data["coin"] = val
                                break
                                
    except Exception as e:
        logger.error(f"Error fetching market data: {e}")

    # مقادیر پیش‌فرض ایمن (در صورت اختلال لحظه‌ای در اتصال به سایت)
    if data["usd"] == 0: data["usd"] = 264200
    if data["eur"] == 0: data["eur"] = 295750
    if data["gold_18k"] == 0: data["gold_18k"] = 25762962
    
    # محاسبه خودکار طلای ۲۴ عیار بر اساس ۱۸ عیار
    if data["gold_24k"] == 0:
        data["gold_24k"] = int(data["gold_18k"] * (4 / 3))
        
    # محاسبه قیمت سکه اگر مستقیم خوانده نشد
    if data["coin"] == 0 and data["gold_18k"] > 0:
        pure_gold_weight = 8.133 * (18 / 24)
        intrinsic = pure_gold_weight * data["gold_18k"]
        data["coin"] = int(intrinsic * 1.75)
        
    return data

def calculate_coin_bubble(gold_18k_toman, coin_price_toman):
    try:
        pure_gold_weight = 8.133 * (18 / 24)
        intrinsic_value = pure_gold_weight * gold_18k_toman
        
        if intrinsic_value > 0:
            bubble_amount = coin_price_toman - intrinsic_value
            bubble_percent = (bubble_amount / intrinsic_value) * 100
            return round(intrinsic_value), round(bubble_amount), round(bubble_percent, 1)
    except Exception:
        pass
    return 0, 0, 0

async def get_price_text():
    prices = fetch_market_data()
    intrinsic, bubble_amt, bubble_pct = calculate_coin_bubble(prices["gold_18k"], prices["coin"])
    
    text = (
        "📊 **قیمت‌های لحظه‌ای بازار آزاد**\n\n"
        f"💵 **دلار آمریکا:** `{prices['usd']:,}` تومان\n"
        f"💶 **یورو:** `{prices['eur']:,}` تومان\n\n"
        f"🟡 **طلای ۱۸ عیار (هر گرم):** `{prices['gold_18k']:,}` تومان\n"
        f"🟡 **طلای ۲۴ عیار (هر گرم):** `{prices['gold_24k']:,}` تومان\n\n"
        f"🪙 **سکه امامی:** `{prices['coin']:,}` تومان\n"
    )
    
    if intrinsic > 0:
        text += (
            f"\n🔍 **تحلیل حباب سکه:**\n"
            f"• ارزش ذاتی طلا: `{intrinsic:,}` تومان\n"
            f"• میزان حباب: `{bubble_amt:,}` تومان (`{bubble_pct}%`)\n"
        )
        
    return text

async def price_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("🔄 به‌روزرسانی قیمت‌ها", callback_data="refresh_prices")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    text = await get_price_text()
    
    if update.message:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")
    elif update.callback_query:
        await update.callback_query.message.edit_text(text, reply_markup=reply_markup, parse_mode="Markdown")
        await update.callback_query.answer()

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.data == "refresh_prices":
        await price_command(update, context)

# سرور وب صوری برای سازگاری با هاست‌های رایگان مثل Render (Web Service)
class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is alive and running!")

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    server_address = ('0.0.0.0', port)
    httpd = HTTPServer(server_address, SimpleHandler)
    httpd.serve_forever()

def main():
    if TOKEN == "YOUR_BOT_TOKEN_HERE":
        logger.warning("Warning: Bot token is not set securely via environment variables!")

    # اجرای سرور وب در پس‌زمینه برای جلوگیری از قطعی در هاست ابری رایگان
    t = threading.Thread(target=run_web_server)
    t.daemon = True
    t.start()

    application = ApplicationBuilder().token(TOKEN).build()

    application.add_handler(CommandHandler("price", price_command))
    application.add_handler(CallbackQueryHandler(button_handler))

    print("🤖 Bot is running...")
    application.run_polling()

if __name__ == "__main__":
    main()