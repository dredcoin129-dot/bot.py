import os
import asyncio
import logging
import requests
from datetime import datetime, timezone
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ---------- CONFIG ----------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
if not TELEGRAM_TOKEN:
    raise ValueError("Set TELEGRAM_TOKEN environment variable!")

CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", 300))

# Add/remove any chain IDs supported by DexScreener here
CHAINS = os.environ.get("CHAINS", "solana,robinhood,arc,ethereum,bsc,base,arbitrum,polygon,avalanche,optimism").split(",")

# Filter thresholds (your original rules)
MIN_LIQ = 1000
MAX_LIQ = 3000
MAX_MC = 10000
MAX_FDV = 10000
MAX_AGE_HOURS = 336
MAX_VOL_H24 = 1000
MAX_BUYS_H24 = 300
MAX_SELLS_H24 = 500

API_BASE = "https://api.dexscreener.com"
HEADERS = {"User-Agent": "MemeHunterBot/1.0"}

logging.basicConfig(level=logging.INFO)

# ---------- API FETCH ----------
def fetch_new_pairs(chain: str):
    try:
        resp = requests.get(f"{API_BASE}/token-profiles/latest/v1", headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        logging.error(f"Profile fetch failed: {e}")
        return []

    profiles = resp.json()
    found = []

    for profile in profiles:
        if profile.get("chainId") != chain:
            continue

        token_address = profile.get("tokenAddress")
        if not token_address:
            continue

        try:
            pair_resp = requests.get(f"{API_BASE}/token-pairs/v1/{chain}/{token_address}", headers=HEADERS, timeout=15)
            pair_resp.raise_for_status()
        except Exception:
            continue

        pairs = pair_resp.json()
        if not pairs:
            continue

        for pair in pairs:
            result = evaluate_pair(pair)
            if result:
                found.append(result)

    return found

# ---------- FILTER ENGINE ----------
def evaluate_pair(pair: dict):
    liq = pair.get("liquidity", {}).get("usd")
    if liq is None or not (MIN_LIQ <= liq <= MAX_LIQ):
        return None

    mc = pair.get("marketCap")
    if mc is None or mc > MAX_MC:
        return None

    fdv = pair.get("fdv")
    if fdv is None or fdv > MAX_FDV:
        return None

    created_ms = pair.get("pairCreatedAt")
    if not created_ms:
        return None
    age_hours = (datetime.now(timezone.utc).timestamp() * 1000 - created_ms) / (1000 * 3600)
    if age_hours > MAX_AGE_HOURS:
        return None

    vol_h24 = pair.get("volume", {}).get("h24")
    if vol_h24 is None or vol_h24 > MAX_VOL_H24:
        return None

    txns = pair.get("txns", {}).get("h24", {})
    buys = txns.get("buys")
    sells = txns.get("sells")
    if buys is None or buys > MAX_BUYS_H24 or sells is None or sells > MAX_SELLS_H24:
        return None

    info = pair.get("info", {})
    websites = info.get("websites", [])
    if websites:   # This is the "no website" filter you requested
        return None

    socials = info.get("socials", [])
    tg_link = None
    for s in socials:
        if s.get("platform", "").lower() == "telegram":
            tg_link = s.get("url") or s.get("handle")
            break

    if not tg_link:
        return None

    return {"tg": tg_link, "name": pair.get("baseToken", {}).get("symbol", "?")}

# ---------- BOT COMMANDS ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.bot_data["seen"] = context.bot_data.get("seen", set())
    context.bot_data["chat_id"] = update.effective_chat.id
    await update.message.reply_text(
        "🤖 Meme Hunter active!\n\n"
        f"Chains: {', '.join(CHAINS)}\n"
        f"Liquidity: ${MIN_LIQ}–${MAX_LIQ}\n"
        f"MC max: ${MAX_MC}\n"
        f"FDV max: ${MAX_FDV}\n"
        f"Age max: {MAX_AGE_HOURS}h\n"
        f"Vol max: ${MAX_VOL_H24}\n"
        f"Buys max: ${MAX_BUYS_H24}\n"
        f"Sells max: ${MAX_SELLS_H24}\n"
        "No-website filter: ON ✅\n\n"
        "Commands:\n"
        "/scannow – manual scan\n"
        "/status – show filters"
    )

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"📊 Filters\n"
        f"Chains: {', '.join(CHAINS)}\n"
        f"Liquidity: ${MIN_LIQ}–${MAX_LIQ}\n"
        f"MC max: ${MAX_MC}\n"
        f"FDV max: ${MAX_FDV}\n"
        f"Age max: {MAX_AGE_HOURS}h\n"
        f"Vol max: ${MAX_VOL_H24}\n"
        f"Buys max: ${MAX_BUYS_H24}\n"
        f"Sells max: ${MAX_SELLS_H24}\n"
        "No-website: ON"
    )

async def scan_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Scanning now...")
    await scan_and_send(context)

# ---------- CORE SCAN JOB ----------
async def scan_and_send(context: ContextTypes.DEFAULT_TYPE):
    seen = context.bot_data.get("seen", set())
    chat_id = context.bot_data.get("chat_id")
    if not chat_id:
        return

    for chain in CHAINS:
        pairs = fetch_new_pairs(chain.strip())
        for pair in pairs:
            if pair["tg"] in seen:
                continue
            await context.bot.send_message(chat_id=chat_id, text=pair["tg"])
            seen.add(pair["tg"])
            await asyncio.sleep(0.5)

    context.bot_data["seen"] = seen

# ---------- MAIN ----------
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("scannow", scan_now))

    app.job_queue.run_repeating(scan_and_send, interval=CHECK_INTERVAL, first=10)
    app.run_polling()

if __name__ == "__main__":
    main()
