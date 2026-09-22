"""
Meme Hunter Bot — Official DexScreener REST API edition
-------------------------------------------------------
Filters:
- Liquidity:  $1,000 – $3,000
- Market Cap: max $10,000
- FDV:        max $10,000
- Pair Age:   max 336 hours
- 24h Volume: max $1,000
- 24h Buys:   max $300
- 24h Sells:  max $500
- Must have Telegram link

Output: only the Telegram invite link.
"""

import os
import asyncio
import logging
from datetime import datetime, timezone

import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# --------------------------------------------------
# CONFIG
# --------------------------------------------------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN environment variable is required!")

CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", 300))
DEFAULT_CHAINS = os.environ.get(
    "CHAINS",
    "solana,ethereum,bsc,base,arbitrum,polygon,optimism"
)
INITIAL_CHAINS = [c.strip().lower() for c in DEFAULT_CHAINS.split(",") if c.strip()]

# Filter thresholds
MIN_LIQ = 1_000
MAX_LIQ = 3_000
MAX_MC = 10_000
MAX_FDV = 10_000
MAX_AGE_HOURS = 336
MAX_VOL_H24 = 1_000
MAX_BUYS_H24 = 300
MAX_SELLS_H24 = 500

API_BASE = "https://api.dexscreener.com"
HEADERS = {
    "User-Agent": "MemeHunterBot/1.0",
    "Accept": "application/json",
}

# --------------------------------------------------
# LOGGING
# --------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("MemeHunter")


# --------------------------------------------------
# API HELPERS
# --------------------------------------------------
def fetch_latest_profiles():
    try:
        r = requests.get(f"{API_BASE}/token-profiles/latest/v1", headers=HEADERS, timeout=20)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.error(f"fetch_latest_profiles error: {e}")
        return []


def fetch_token_pairs(chain: str, token_address: str):
    try:
        r = requests.get(
            f"{API_BASE}/token-pairs/v1/{chain}/{token_address}",
            headers=HEADERS,
            timeout=20,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.error(f"fetch_token_pairs error ({chain}/{token_address}): {e}")
        return []


# --------------------------------------------------
# FILTER ENGINE
# --------------------------------------------------
def evaluate_pair(pair: dict):
    """Return a dict if the pair passes all filters, else None."""

    # Liquidity
    liq = (pair.get("liquidity") or {}).get("usd")
    if liq is None or not (MIN_LIQ <= liq <= MAX_LIQ):
        return None

    # Market Cap
    mc = pair.get("marketCap")
    if mc is None or mc > MAX_MC:
        return None

    # FDV
    fdv = pair.get("fdv")
    if fdv is None or fdv > MAX_FDV:
        return None

    # Age
    created_ms = pair.get("pairCreatedAt")
    if not created_ms:
        return None
    age_hours = (datetime.now(timezone.utc).timestamp() * 1000 - created_ms) / 3_600_000
    if age_hours > MAX_AGE_HOURS:
        return None

    # Volume
    vol_h24 = (pair.get("volume") or {}).get("h24")
    if vol_h24 is None or vol_h24 > MAX_VOL_H24:
        return None

    # Txns
    txns = (pair.get("txns") or {}).get("h24") or {}
    buys = txns.get("buys")
    sells = txns.get("sells")
    if buys is None or buys > MAX_BUYS_H24:
        return None
    if sells is None or sells > MAX_SELLS_H24:
        return None

    # NOTE: "no website" filter removed as requested.

    # Telegram
    info = pair.get("info") or {}
    socials = info.get("socials") or []
    tg_link = None
    for s in socials:
        platform = (s.get("platform") or "").lower()
        if platform == "telegram":
            tg_link = s.get("url") or s.get("handle")
            break

    if not tg_link:
        return None

    base = pair.get("baseToken") or {}
    return {
        "tg": tg_link,
        "symbol": base.get("symbol", "?"),
        "chain": pair.get("chainId", "?"),
        "mc": mc,
        "liq": liq,
    }


# --------------------------------------------------
# SCAN LOGIC
# --------------------------------------------------
def scan_chain(chain: str, seen: set) -> list:
    results = []
    profiles = fetch_latest_profiles()
    if not profiles:
        return results

    chain_profiles = [p for p in profiles if (p.get("chainId") or "").lower() == chain.lower()]
    log.info(f"{chain}: {len(chain_profiles)} profiles for this chain")

    for profile in chain_profiles:
        token_addr = profile.get("tokenAddress")
        if not token_addr:
            continue

        pairs = fetch_token_pairs(chain, token_addr)
        if not pairs:
            continue

        for p in pairs:
            evaluated = evaluate_pair(p)
            if not evaluated:
                continue
            if evaluated["tg"] in seen:
                continue
            results.append(evaluated)
            seen.add(evaluated["tg"])

    log.info(f"{chain}: {len(results)} tokens passed filters")
    return results


# --------------------------------------------------
# COMMANDS
# --------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.bot_data.setdefault("seen", set())
    context.bot_data.setdefault("chains", INITIAL_CHAINS.copy())
    context.bot_data["chat_id"] = update.effective_chat.id

    chains = ", ".join(context.bot_data["chains"])
    await update.message.reply_text(
        "🤖 *Meme Hunter* is live.\n\n"
        f"Chains: `{chains}`\n\n"
        "*Filters:*\n"
        f"• Liquidity: ${MIN_LIQ:,}–${MAX_LIQ:,}\n"
        f"• Market Cap: max ${MAX_MC:,}\n"
        f"• FDV: max ${MAX_FDV:,}\n"
        f"• Age: max {MAX_AGE_HOURS}h\n"
        f"• 24h Volume: max ${MAX_VOL_H24:,}\n"
        f"• 24h Buys: max {MAX_BUYS_H24}\n"
        f"• 24h Sells: max {MAX_SELLS_H24}\n"
        "• Must have Telegram\n\n"
        "*Commands:*\n"
        "/status – show current settings\n"
        "/scannow – force a manual scan\n"
        "/chains – list active chains\n"
        "/setchains <c1,c2,...> – replace the chain list\n"
        "/addchain <c> – add one chain\n"
        "/removechain <c> – remove one chain\n"
        "/why <chain> – diagnose why tokens fail",
        parse_mode="Markdown",
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chains = context.bot_data.get("chains", INITIAL_CHAINS)
    await update.message.reply_text(
        "*Current settings*\n"
        f"Chains: `{', '.join(chains)}`\n"
        f"Interval: {CHECK_INTERVAL}s\n"
        f"Liquidity: ${MIN_LIQ:,}–${MAX_LIQ:,}\n"
        f"MC max: ${MAX_MC:,}\n"
        f"FDV max: ${MAX_FDV:,}\n"
        f"Age max: {MAX_AGE_HOURS}h\n"
        f"Vol max: ${MAX_VOL_H24:,}\n"
        f"Buys max: {MAX_BUYS_H24}\n"
        f"Sells max: {MAX_SELLS_H24}",
        parse_mode="Markdown",
    )


async def cmd_chains(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chains = context.bot_data.get("chains", INITIAL_CHAINS)
    await update.message.reply_text(
        f"Active chains ({len(chains)}):\n`{', '.join(chains)}`",
        parse_mode="Markdown",
    )


async def cmd_setchains(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /setchains solana,base,bsc")
        return
    raw = " ".join(context.args)
    chains = [c.strip().lower() for c in raw.split(",") if c.strip()]
    context.bot_data["chains"] = chains
    context.bot_data["seen"] = set()
    await update.message.reply_text(
        f"✅ Chains set to: `{', '.join(chains)}`", parse_mode="Markdown"
    )


async def cmd_addchain(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /addchain base")
        return
    new_chain = context.args[0].strip().lower()
    chains = context.bot_data.setdefault("chains", INITIAL_CHAINS.copy())
    if new_chain in chains:
        await update.message.reply_text(f"`{new_chain}` is already active.", parse_mode="Markdown")
        return
    chains.append(new_chain)
    await update.message.reply_text(f"✅ Added `{new_chain}`", parse_mode="Markdown")


async def cmd_removechain(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /removechain base")
        return
    target = context.args[0].strip().lower()
    chains = context.bot_data.setdefault("chains", INITIAL_CHAINS.copy())
    if target not in chains:
        await update.message.reply_text(f"`{target}` is not active.", parse_mode="Markdown")
        return
    chains.remove(target)
    await update.message.reply_text(f"✅ Removed `{target}`", parse_mode="Markdown")


async def cmd_scannow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Scanning now...")
    await scan_and_send(context)


async def cmd_why(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Diagnose why tokens fail filters."""
    await update.message.reply_text("🔍 Running diagnostic...")

    chain = context.args[0].lower() if context.args else "solana"
    profiles = fetch_latest_profiles()
    chain_profiles = [p for p in profiles if (p.get("chainId") or "").lower() == chain]

    if not chain_profiles:
        await update.message.reply_text(f"No profiles for {chain}")
        return

    lines = [f"*Chain: {chain}* — {len(chain_profiles)} profiles\n"]

    for profile in chain_profiles[:8]:
        addr = profile.get("tokenAddress", "?")
        pairs = fetch_token_pairs(chain, addr)
        if not pairs:
            lines.append(f"`{addr[:8]}` — no pairs")
            continue

        p = pairs[0]
        base = p.get("baseToken") or {}
        sym = base.get("symbol", "?")

        liq = (p.get("liquidity") or {}).get("usd")
        mc = p.get("marketCap")
        fdv = p.get("fdv")
        created = p.get("pairCreatedAt")
        vol = (p.get("volume") or {}).get("h24")
        txns = (p.get("txns") or {}).get("h24") or {}
        buys = txns.get("buys")
        sells = txns.get("sells")
        socials = (p.get("info") or {}).get("socials") or []
        has_tg = any((s.get("platform") or "").lower() == "telegram" for s in socials)

        reasons = []
        if liq is None or not (MIN_LIQ <= liq <= MAX_LIQ):
            reasons.append(f"liq={liq}")
        if mc is None or mc > MAX_MC:
            reasons.append(f"mc={mc}")
        if fdv is None or fdv > MAX_FDV:
            reasons.append(f"fdv={fdv}")
        if not created:
            reasons.append("no age")
        if vol is None or vol > MAX_VOL_H24:
            reasons.append(f"vol={vol}")
        if buys is None or buys > MAX_BUYS_H24:
            reasons.append(f"buys={buys}")
        if sells is None or sells > MAX_SELLS_H24:
            reasons.append(f"sells={sells}")
        if not has_tg:
            reasons.append("no telegram")

        if reasons:
            lines.append(f"`{sym}` ❌ {', '.join(reasons)}")
        else:
            lines.append(f"`{sym}` ✅ PASSES")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# --------------------------------------------------
# CORE SCAN LOOP
# --------------------------------------------------
async def scan_and_send(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.bot_data.get("chat_id")
    if not chat_id:
        log.warning("No chat_id yet – waiting for /start.")
        return

    chains = context.bot_data.get("chains", INITIAL_CHAINS)
    seen = context.bot_data.setdefault("seen", set())

    log.info(f"Scanning chains: {chains}")

    for chain in chains:
        try:
            matches = scan_chain(chain, seen)
        except Exception as e:
            log.error(f"scan_chain({chain}) failed: {e}")
            continue

        for m in matches:
            try:
                await context.bot.send_message(chat_id=chat_id, text=m["tg"])
                log.info(f"Sent {m['chain']}/{m['symbol']} -> {m['tg']}")
            except Exception as e:
                log.error(f"send_message failed: {e}")
            await asyncio.sleep(0.5)


# --------------------------------------------------
# MAIN
# --------------------------------------------------
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("chains", cmd_chains))
    app.add_handler(CommandHandler("setchains", cmd_setchains))
    app.add_handler(CommandHandler("addchain", cmd_addchain))
    app.add_handler(CommandHandler("removechain", cmd_removechain))
    app.add_handler(CommandHandler("scannow", cmd_scannow))
    app.add_handler(CommandHandler("why", cmd_why))

    if app.job_queue is not None:
        app.job_queue.run_repeating(scan_and_send, interval=CHECK_INTERVAL, first=10)
        log.info("JobQueue scheduled (interval=%ss).", CHECK_INTERVAL)
    else:
        log.warning("JobQueue unavailable – falling back to asyncio loop.")

        async def loop_scan():
            await asyncio.sleep(10)
            while True:
                try:
                    await scan_and_send(app)
                except Exception as e:
                    log.error(f"loop_scan error: {e}")
                await asyncio.sleep(CHECK_INTERVAL)

        try:
            asyncio.get_event_loop().create_task(loop_scan())
        except RuntimeError:
            asyncio.new_event_loop().create_task(loop_scan())

    log.info("Bot starting polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
