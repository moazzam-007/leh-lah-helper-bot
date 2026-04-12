import asyncio
import base64
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta
from typing import Any

import requests
from flask import Flask, jsonify, request
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.request import HTTPXRequest

# Setup Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Environment Variables
TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip().rstrip("/")
ADMIN_ID_RAW = os.getenv("ADMIN_ID", "").strip()
LEHLAH_COOKIE = os.getenv("LEHLAH_COOKIE", "").strip()
PORT = int(os.getenv("PORT", "10000"))

LEHLAH_API_URL = "https://creator.lehlah.club/api/campaign-url-builder"
LEHLAH_ORIGIN = "https://creator.lehlah.club"
LEHLAH_REFERER = "https://creator.lehlah.club/link-genie"
TOKEN_MAX_AGE_SECONDS = 5184000
MAX_BULK_URLS = 20

# ==========================================
# BOT STATE TRACKING
# ==========================================
telegram_app = None
event_loop = None
bot_ready = False          # True sirf tab jab webhook successfully set ho jaye
bot_start_time = None      # Bot start time track karne ke liye

# ==========================================
# ADMIN CHECK LOGIC
# ==========================================
def get_admin_id() -> int | None:
    if not ADMIN_ID_RAW:
        return None
    try:
        return int(ADMIN_ID_RAW)
    except ValueError:
        logger.warning("ADMIN_ID numeric format mein nahi hai.")
        return None

async def check_admin(update: Update) -> bool:
    admin_id = get_admin_id()
    if admin_id is None:
        return True
    user = update.effective_user
    if user and user.id == admin_id:
        return True

    if update.message:
        await update.message.reply_text("Unauthorized! Aap is bot ke admin nahi hain. Access Denied.")
    return False

# ==========================================
# TOKEN & API HELPERS
# ==========================================
def build_lehlah_headers() -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Origin": LEHLAH_ORIGIN,
        "Referer": LEHLAH_REFERER,
        "Cookie": LEHLAH_COOKIE,
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
        ),
    }

def extract_auth_token(cookie_string: str) -> str | None:
    match = re.search(r"authToken=([^;]+)", cookie_string)
    if not match:
        return None
    return match.group(1)

def decode_token_payload(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) < 2:
        raise ValueError("JWT token format galat hai")
    payload_b64 = parts[1]
    padding = "=" * (-len(payload_b64) % 4)
    decoded = base64.urlsafe_b64decode(payload_b64 + padding)
    return json.loads(decoded.decode("utf-8"))

def get_token_status_text() -> str:
    if not LEHLAH_COOKIE:
        return "LEHLAH_COOKIE env variable missing hai."
    token = extract_auth_token(LEHLAH_COOKIE)
    if not token:
        return "Cookie ke andar authToken nahi mila."
    try:
        payload = decode_token_payload(token)
        iat = payload.get("iat")
        if not iat:
            return "Token payload mila, lekin iat field nahi mili."
        issued_at = datetime.fromtimestamp(iat)
        expires_at = issued_at + timedelta(seconds=TOKEN_MAX_AGE_SECONDS)
        remaining = expires_at - datetime.now()
        days_left = remaining.days

        if remaining.total_seconds() <= 0:
            state = "Expired"
        elif days_left < 7:
            state = "Expiring soon"
        else:
            state = "Valid"

        return (
            f"Token status: {state}\n"
            f"Expires: {expires_at.strftime('%d %b %Y %I:%M %p')}\n"
            f"Days left: {days_left}"
        )
    except Exception as exc:
        return f"Token parse error: {exc}"

def extract_urls(text: str) -> list[str]:
    return re.findall(r"https?://[^\s]+", text or "")

# ==========================================
# CORE FUNCTIONS
# ==========================================
def create_lehlah_affiliate(product_url: str) -> dict[str, Any]:
    if not LEHLAH_COOKIE:
        return {"ok": False, "error": "LEHLAH_COOKIE env variable missing hai."}

    payload = {
        "title": "",
        "full_page_url": product_url,
        "page_no": 1,
        "DEVICE_TYPE": "web",
        "from": "LinkGenie",
    }

    try:
        response = requests.post(
            LEHLAH_API_URL,
            headers=build_lehlah_headers(),
            json=payload,
            timeout=20
        )
        response.raise_for_status()
        data = response.json()

        candidates = data.get("data", {}).get("data", {}).get("data", [])
        if isinstance(candidates, list) and candidates:
            return {"ok": True, "item": candidates[0]}

        return {"ok": False, "error": "API response mein affiliate item nahi mila."}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

def extract_original_link(lehlah_url: str) -> dict[str, Any]:
    try:
        match = re.search(r'/s/([a-zA-Z0-9]+)', lehlah_url)
        if not match:
            return {"ok": False, "error": "Short code URL mein nahi mila."}

        short_code = match.group(1)

        api_url = "https://web.lehlah.club/api/redirection/generate-redirect-url-in-app-redirection"
        payload = {
            "short_code": short_code,
            "referrer": "https://creator.lehlah.club/link-genie",
            "is_in_app": False,
            "is_telegram": False,
            "is_youtube": False,
            "is_instagram": False,
            "is_ios": False,
            "is_android": False,
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://web.lehlah.club",
            "Referer": "https://web.lehlah.club/",
            "Cookie": LEHLAH_COOKIE,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/123.0.0.0 Safari/537.36",
        }

        res = requests.post(api_url, headers=headers, json=payload, timeout=15)
        data = res.json()
        redirect_url = data.get("redirect_url")

        if redirect_url:
            return {"ok": True, "original": redirect_url}

        return {"ok": False, "error": "redirect_url API response mein nahi mila."}

    except Exception as e:
        return {"ok": False, "error": str(e)}

# ==========================================
# TELEGRAM HANDLERS
# ==========================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_admin(update):
        return

    welcome_text = (
        "<b>Budget Looks (LehLah) Bot mein Swagat hai!</b>\n\n"
        "Main aapka personal Affiliate Assistant hoon. Aap mujhe seedha koi bhi link bhej sakte hain:\n\n"
        "<b>Auto-Detect System:</b>\n"
        "<b>LehLah Link Bhejein:</b> Main uska original product nikal kar dunga.\n"
        "<b>Flipkart/Myntra Link Bhejein:</b> Main aapka LehLah Affiliate link bana kar dunga.\n\n"
        "<b>Commands:</b>\n"
        "/bulk - Ek sath multiple URLs process karne ke liye\n"
        "/check_token - LehLah cookie ki expiry check karne ke liye\n"
        "/status - Bot health check karne ke liye"
    )
    await update.message.reply_text(welcome_text, parse_mode="HTML")

async def cmd_bulk(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_admin(update):
        return
    context.user_data["state"] = "bulk"
    await update.message.reply_text(
        "<b>Bulk mode on.</b>\n\nEk line mein ek URL bhejo (Max 20 URLs).",
        parse_mode="HTML"
    )

async def cmd_check_token(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_admin(update):
        return
    await update.message.reply_text(get_token_status_text())

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_admin(update):
        return
    uptime = ""
    if bot_start_time:
        delta = datetime.now() - bot_start_time
        hours, rem = divmod(int(delta.total_seconds()), 3600)
        minutes = rem // 60
        uptime = f"\nUptime: {hours}h {minutes}m"
    await update.message.reply_text(
        f"Bot status: {'✅ Running' if bot_ready else '⚠️ Starting...'}{uptime}\n\n{get_token_status_text()}"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_admin(update):
        return

    message = update.message
    if not message:
        return

    text = message.text or message.caption or ""
    urls = extract_urls(text)

    if not urls:
        await update.message.reply_text("Kripya koi valid URL bhejein.")
        return

    if context.user_data.get("state") == "bulk":
        await process_bulk(update, urls)
        context.user_data["state"] = None
        return

    url = urls[0]
    loop = asyncio.get_running_loop()

    if "lehlah.club" in url:
        await update.message.reply_text("LehLah link detect hua. Original link extract kar raha hoon...")
        result = await loop.run_in_executor(None, extract_original_link, url)

        if result["ok"]:
            await update.message.reply_text(
                f"<b>Original Link:</b>\n\n{result['original']}",
                parse_mode="HTML"
            )
        else:
            await update.message.reply_text(f"Extraction Fail: {result['error']}")
    else:
        await update.message.reply_text("Product link detect hua. Affiliate link bana raha hoon...")
        result = await loop.run_in_executor(None, create_lehlah_affiliate, url)

        if result["ok"]:
            item = result["item"]
            title = item.get("title") or item.get("product_title") or "Product"
            generated = item.get("generated_url") or "Link missing"
            price = item.get("price") or item.get("product_price") or ""

            reply = f"<b>Affiliate Link Ready!</b>\n\n{title}\n"
            if price:
                reply += f"Price: {price}\n"
            reply += f"\n{generated}"

            await update.message.reply_text(reply, parse_mode="HTML")
        else:
            await update.message.reply_text(f"Link generate nahi hua.\nReason: {result['error']}")

async def process_bulk(update: Update, urls: list[str]) -> None:
    if len(urls) > MAX_BULK_URLS:
        urls = urls[:MAX_BULK_URLS]
        await update.message.reply_text(f"Sirf pehli {MAX_BULK_URLS} URLs process kar raha hoon.")

    await update.message.reply_text(f"{len(urls)} URLs bulk process kar raha hoon...")
    loop = asyncio.get_running_loop()
    responses = []

    for index, url in enumerate(urls, start=1):
        if "lehlah.club" in url:
            res = await loop.run_in_executor(None, extract_original_link, url)
            if res["ok"]:
                responses.append(f"{index}. Extracted:\n{res['original']}")
            else:
                responses.append(f"{index}. Extract Fail: {url}\nReason: {res['error']}")
        else:
            res = await loop.run_in_executor(None, create_lehlah_affiliate, url)
            if res["ok"]:
                generated = res["item"].get("generated_url") or "Link missing"
                responses.append(f"{index}. Affiliate:\n{generated}")
            else:
                responses.append(f"{index}. Gen Fail: {url}\nReason: {res['error']}")

    chunk = "<b>Bulk Results:</b>\n\n"
    for line in responses:
        msg = line + "\n\n"
        if len(chunk) + len(msg) > 3800:
            await update.message.reply_text(chunk, parse_mode="HTML")
            chunk = ""
        chunk += msg

    if chunk:
        await update.message.reply_text(chunk, parse_mode="HTML")

# ==========================================
# FLASK WEBHOOK & SERVER
# ==========================================
def process_update_in_thread(update_dict: dict[str, Any]) -> None:
    global telegram_app, event_loop
    if telegram_app and event_loop and bot_ready:
        try:
            update = Update.de_json(update_dict, telegram_app.bot)
            asyncio.run_coroutine_threadsafe(telegram_app.process_update(update), event_loop)
        except Exception:
            logger.exception("Update processing error")

app = Flask(__name__)

@app.route("/")
def home() -> str:
    return "LehLah Telegram Bot Active!"

@app.route("/health")
def health():
    """
    Render ka health check endpoint.
    Bot ready nahi hua tab bhi 200 return karta hai taaki service crash na ho,
    lekin status clearly deta hai.
    """
    status = {
        "status": "ok" if bot_ready else "starting",
        "bot_ready": bot_ready,
        "uptime_seconds": int((datetime.now() - bot_start_time).total_seconds()) if bot_start_time else 0,
    }
    return jsonify(status), 200

@app.route(f"/{TOKEN}", methods=["POST"])
def webhook() -> Any:
    if not bot_ready:
        return jsonify({"status": "bot not ready yet"}), 503
    try:
        update_dict = request.get_json(silent=True)
        if update_dict:
            threading.Thread(target=process_update_in_thread, args=(update_dict,), daemon=True).start()
        return jsonify({"status": "ok"})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

# ==========================================
# BOT SETUP WITH RETRY
# ==========================================
def run_event_loop_in_background(loop: asyncio.AbstractEventLoop) -> None:
    asyncio.set_event_loop(loop)
    loop.run_forever()

async def setup_webhook_with_retry(app_instance, webhook_full_url: str, max_retries: int = 10) -> bool:
    """
    Webhook setup karta hai retry logic ke saath.
    Exponential backoff use karta hai taaki Telegram network issues pe crash na ho.
    """
    await app_instance.initialize()
    await app_instance.start()

    for attempt in range(1, max_retries + 1):
        try:
            logger.info(f"Webhook set karne ki koshish ({attempt}/{max_retries})...")
            await app_instance.bot.set_webhook(
                url=webhook_full_url,
                allowed_updates=["message", "callback_query"],
                drop_pending_updates=True,   # Purane queued updates ignore karo fresh start ke liye
            )
            logger.info(f"✅ Webhook successfully set: {webhook_full_url}")
            return True
        except Exception as exc:
            wait = min(2 ** attempt, 60)  # max 60 seconds wait
            logger.warning(f"❌ Webhook attempt {attempt} failed: {exc}. Retry in {wait}s...")
            await asyncio.sleep(wait)

    logger.error("❌ Webhook set karna fail hua after all retries!")
    return False

def initialize_bot_in_background() -> None:
    """
    Bot ko background thread mein initialize karta hai.
    Flask server ko block nahi karta - Render health check fail nahi hogi.
    """
    global telegram_app, event_loop, bot_ready, bot_start_time

    bot_start_time = datetime.now()

    # Custom timeouts set karo - Render ke slow network ke liye zaroori hain
    trequest = HTTPXRequest(
        connection_pool_size=8,
        connect_timeout=30.0,    # Connection establish karne ka time
        read_timeout=30.0,       # Response read karne ka time
        write_timeout=30.0,      # Data send karne ka time
        pool_timeout=15.0,       # Pool se connection lene ka wait time
    )

    telegram_app = (
        ApplicationBuilder()
        .token(TOKEN)
        .request(trequest)
        .build()
    )

    telegram_app.add_handler(CommandHandler("start", cmd_start))
    telegram_app.add_handler(CommandHandler("bulk", cmd_bulk))
    telegram_app.add_handler(CommandHandler("check_token", cmd_check_token))
    telegram_app.add_handler(CommandHandler("status", cmd_status))
    telegram_app.add_handler(MessageHandler(filters.TEXT | filters.CAPTION, handle_message))

    event_loop = asyncio.new_event_loop()

    # Event loop ko background thread mein chalao
    loop_thread = threading.Thread(target=run_event_loop_in_background, args=(event_loop,), daemon=True)
    loop_thread.start()

    webhook_full_url = f"{WEBHOOK_URL}/{TOKEN}"

    # Webhook setup ka future create karo - timeout ke saath
    future = asyncio.run_coroutine_threadsafe(
        setup_webhook_with_retry(telegram_app, webhook_full_url),
        event_loop,
    )

    try:
        success = future.result(timeout=300)  # Max 5 min wait
        if success:
            bot_ready = True
            logger.info("🚀 Bot fully initialized and ready!")
        else:
            logger.error("Bot initialization failed after all retries.")
    except Exception as exc:
        logger.exception(f"Bot initialization thread mein error: {exc}")

def main() -> None:
    if not TOKEN or not WEBHOOK_URL or not LEHLAH_COOKIE:
        raise RuntimeError("Missing required env vars: BOT_TOKEN, WEBHOOK_URL, LEHLAH_COOKIE")

    logger.info("Flask server start ho raha hai...")

    # Bot ko background mein initialize karo — Flask block nahi hoga
    init_thread = threading.Thread(target=initialize_bot_in_background, daemon=True)
    init_thread.start()

    # Flask immediately start karo taaki Render ka health check pass ho
    app.run(host="0.0.0.0", port=PORT, debug=False)

if __name__ == "__main__":
    main()
