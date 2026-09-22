import os
import re
import asyncio
import logging
import requests
from datetime import datetime, timezone
from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ---------- CONFIG ----------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN environment variable is required!")

CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", 300))

# Add/remove chains here (these are DexScreener chain slugs)
CHAINS = os.environ.get(
    "CHAINS",
    "solana,ethereum,bsc,base,arbitrum,polygon,avalanche,optimism"
).split(",")

# Filter thresholds (your original rules)
MIN_LIQ = 1_000
MAX_LIQ = 3_000
MAX_MC = 10_000
MAX_FDV = 10_000
MAX_AGE_HOURS = 336
MAX_VOL_H24 = 1_000
MAX_BUYS_H24 = 300
MAX_SELLS_H24 = 500

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36"
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("MemeHunter")


# ---------- SCRAPER ----------
def fetch_new_pairs(chain: str):
    """Scrape DexScreener's new-pairs page for a given chain."""
    url = f"https://dexscreener.com/new-pairs?chain={chain}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        log.error(f"Request failed for {chain}: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    rows = soup.select("table tbody tr")
    found = []

    for row in rows:
        cols = row.find_all("td")
        if len(cols) < 8:
            continue

        # Extract token name & address
        name_cell = cols[0]
        token_link = name_cell.find("a")
        if not token_link:
            continue
        token_name = token_link.text.strip()
        token_addr = token_link.get("href", "").split("/")[-1]

        # Market Cap (parse "$5.2k" → 5200)
        mc_text = cols[3].text.strip().replace("$", "").replace(",", "")
        try:
            if "k" in mc_text.lower():
                mc = float(mc_text.lower().replace("k", "")) * 1000
            elif "m" in mc_text.lower():
                mc = float(mc_text.lower().replace("m", "")) * 1_000_000
            else:
                mc = float(mc_text)
        except:
            continue

        if not (5_000 <= mc <= MAX_MC):
            continue

        # Social links
        social_cell = cols[5]
        social_links = social_cell.find_all("a", href=True)
        has_twitter = any("twitter.com" in a["href"] or "x.com" in a["href"] for a in social_links)
        has_telegram = any("t.me" in a["href"] for a in social_links)
        has_website = any(
            not ("twitter.com" in a["href"] or "x.com" in a["href"] or "t.me" in a["href"])
            for a in social_links
        )

        if not (has_twitter and has_telegram) or has_website:
            continue

        # Age – parse "X mins ago", "Y hours ago"
        time_cell = cols[1]
        time_text = time_cell.text.strip()
        age_seconds = parse_age(time_text)
        if age_seconds is None or age_seconds > MAX_AGE_HOURS * 3600:
            continue

        # Extract Telegram link
        tg_link = None
        for a in social_links:
            if "t.me" in a["href"]:
                tg_link = a["href"]
                break

        if tg_link:
            found.append({"tg": tg_link, "name": token_name, "mc": mc})

    return found


def parse_age(text):
    """Convert '5 mins ago', '2 hours ago' to seconds."""
    text = text.lower()
    if "now" in text or "just" in text:
        return 0
    match = re.search(r"(\d+)\s*(min|hour|day)s?\s*ago", text)
    if not match:
        return None
    num = int(match.group(1))
    unit = match.group(2)
    if unit.startswith("min"):
        return num * 60
    elif unit.startswith("hour"):
        return num * 3600
    elif unit.startswith("day"):
        return num * 86400
    return None


# ---------- COMMANDS ----------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.bot_data.setdefault("seen", set())
    context.bot_data["chat_id"] = update.effective_chat.id
    await update.message.reply_text(
        "🤖 Meme Hunter active!\n\n"
        f"Chains: {', '.join(CHAINS)}\n"
        f"Liquidity: ${MIN_LIQ}–${MAX_LIQ}\n"
        f"MC max: ${MAX_MC}\n"
        f"FDV max: ${MAX_FDV}\n"
        f"Age max: {MAX_AGE_HOURS}h\n"
        f"Vol max: ${MAX_VOL_H24}\n"
        f"Buys max: {MAX_BUYS_H24}\n"
        f"Sells max: ${MAX_SELLS_H24}\n"
        "No-website: ON ✅\n\n"
        "/scannow – manual scan\n"
        "/status – show filters"
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"📊 Filters\n"
        f"Chains: {', '.join(CHAINS)}\n"
        f"Liquidity: ${MIN_LIQ}–${MAX_LIQ}\n"
        f"MC max: ${MAX_MC}\n"
        f"FDV max: ${MAX_FDV}\n"
        f"Age max: {MAX_AGE_HOURS}h\n"
        f"Vol max: ${MAX_VOL_H24}\n"
        f"Buys max: {MAX_BUYS_H24}\n"
        f"Sells max: ${MAX_SELLS_H24}\n"
        "No-website: ON"
    )


async def cmd_scannow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Scanning now...")
    await scan_and_send(context)


# ---------- SCAN LOOP ----------
async def scan_and_send(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.bot_data.get("chat_id")
    if not chat_id:
        log.warning("No chat_id yet – waiting for /start.")
        return

    seen = context.bot_data.setdefault("seen", set())

    for chain in CHAINS:
        chain = chain.strip()
        log.info(f"Scanning {chain}...")
        pairs = fetch_new_pairs(chain)
        for pair in pairs:
            if pair["tg"] in seen:
                continue
            await context.bot.send_message(chat_id=chat_id, text=pair["tg"])
            log.info(f"Sent {pair['name']} -> {pair['tg']}")
            seen.add(pair["tg"])
            await asyncio.sleep(0.5)

    context.bot_data["seen"] = seen


# ---------- MAIN ----------
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("scannow", cmd_scannow))

    if app.job_queue is not None:
        app.job_queue.run_repeating(scan_and_send, interval=CHECK_INTERVAL, first=10)
        log.info("JobQueue scheduled.")
    else:
        log.warning("JobQueue unavailable – using asyncio fallback.")

        async def loop_scan():
            await asyncio.sleep(10)
            while True:
                try:
                    await scan_and_send(app)
                except Exception as e:
                    log.error(f"Scan error: {e}")
                await asyncio.sleep(CHECK_INTERVAL)

        asyncio.get_event_loop().create_task(loop_scan())

    log.info("Bot starting polling...")
    app.run_polling()


if __name__ == "__main__":
    main()
