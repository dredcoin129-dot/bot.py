"""
Meme Hunter Bot — dexscraper edition
-------------------------------------
Scans DexScreener new/trending pairs across any supported chain using the
`dexscraper` SDK (with Cloudflare bypass).

Filters:
- Liquidity:  $1,000 – $3,000
- Market Cap: max $10,000
- FDV:        max $10,000
- Pair Age:   max 336 hours
- 24h Volume: max $1,000
- 24h Buys:   max $300
- 24h Sells:  max $500
- Must have Telegram link
- Must NOT have a website

Output: only the Telegram invite link.
"""

import os
import asyncio
import logging
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from dexscraper import DexScraper, ScrapingConfig, Filters, Chain, RankBy, Timeframe

# --------------------------------------------------
# CONFIG
# --------------------------------------------------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN environment variable is required!")

CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", 300))
DEBUG_MODE = os.environ.get("DEBUG_MODE", "false").lower() == "true"

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

# --------------------------------------------------
# LOGGING
# --------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("MemeHunter")

# --------------------------------------------------
# CHAIN MAPPING
# --------------------------------------------------
CHAIN_MAP = {
    "solana": Chain.SOLANA,
    "ethereum": Chain.ETHEREUM,
    "bsc": Chain.BSC,
    "base": Chain.BASE,
    "arbitrum": Chain.ARBITRUM,
    "polygon": Chain.POLYGON,
    "optimism": Chain.OPTIMISM,
}


def get_chain_enum(chain_name: str):
    """Map a string chain name to the dexscraper Chain enum."""
    return CHAIN_MAP.get(chain_name.lower())


# --------------------------------------------------
# SAFE ATTRIBUTE ACCESS
# --------------------------------------------------
def get_attr(obj, *names, default=None):
    """Return the first attribute found on obj from the given names."""
    for name in names:
        if hasattr(obj, name):
            val = getattr(obj, name)
            if val is not None:
                return val
    return default


def get_nested_attr(obj, path: str, default=None):
    """Access nested attributes using dot notation."""
    current = obj
    for part in path.split("."):
        if current is None:
            return default
        current = getattr(current, part, None)
    return current if current is not None else default


# --------------------------------------------------
# TOKEN EXTRACTION
# --------------------------------------------------
def extract_token_data(token) -> dict:
    """Extract the fields we need from a dexscraper token object."""

    # Symbol / name
    symbol = get_attr(token, "symbol", "base_token_symbol", "baseTokenSymbol", default="?")

    # Market cap
    mc = get_attr(token, "market_cap", "marketCap", "mc", default=None)
    if mc is None:
        mc = get_nested_attr(token, "profile.market_cap")

    # FDV
    fdv = get_attr(token, "fdv", "fully_diluted_valuation", default=None)

    # Liquidity
    liq = get_attr(token, "liquidity", "liquidity_usd", default=None)
    if isinstance(liq, dict):
        liq = liq.get("usd")
    if liq is None:
        liq = get_nested_attr(token, "liquidity.usd")

    # Pair creation time
    created = get_attr(token, "pair_created_at", "pairCreatedAt", "created_at", default=None)
    if created is None:
        created = get_nested_attr(token, "profile.pair_created_at")

    # 24h volume
    vol = get_attr(token, "volume_24h", "volume_h24", default=None)
    if isinstance(vol, dict):
        vol = vol.get("h24")
    if vol is None:
        vol = get_nested_attr(token, "volume.h24")

    # 24h txns
    buys = get_attr(token, "buys_24h", "buys_h24", default=None)
    sells = get_attr(token, "sells_24h", "sells_h24", default=None)
    if buys is None:
        buys = get_nested_attr(token, "txns.h24.buys")
    if sells is None:
        sells = get_nested_attr(token, "txns.h24.sells")

    # Socials & websites
    socials = get_attr(token, "socials", default=None)
    if socials is None:
        socials = get_nested_attr(token, "profile.socials", default=[])

    websites = get_attr(token, "websites", default=None)
    if websites is None:
        websites = get_nested_attr(token, "profile.websites", default=[])

    # Telegram link
    tg_link = None
    if socials:
        for s in socials:
            platform = (get_attr(s, "platform", "type", default="") or "").lower()
            if platform == "telegram":
                tg_link = get_attr(s, "url", "handle", default=None)
                break

    # Age in hours
    age_hours = None
    if created:
        try:
            if isinstance(created, (int, float)):
                created_ms = created if created > 1e12 else created * 1000
                age_hours = (datetime.now(timezone.utc).timestamp() * 1000 - created_ms) / 3_600_000
            else:
                # Assume ISO string
                dt = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
                age_hours = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
        except Exception:
            age_hours = None

    return {
        "symbol": symbol,
        "mc": mc,
        "fdv": fdv,
        "liq": liq,
        "created": created,
        "age_hours": age_hours,
        "vol_h24": vol,
        "buys": buys,
        "sells": sells,
        "tg": tg_link,
        "has_website": bool(websites),
        "raw": token,
    }


# --------------------------------------------------
# FILTER ENGINE
# --------------------------------------------------
def passes_filters(data: dict) -> bool:
    """Return True if the token data passes all filters."""

    # Liquidity
    liq = data["liq"]
    if liq is None or not (MIN_LIQ <= liq <= MAX_LIQ):
        return False

    # Market cap
    mc = data["mc"]
    if mc is None or mc > MAX_MC:
        return False

    # FDV
    fdv = data["fdv"]
    if fdv is None or fdv > MAX_FDV:
        return False

    # Age
    age = data["age_hours"]
    if age is None or age > MAX_AGE_HOURS:
        return False

    # 24h volume
    vol = data["vol_h24"]
    if vol is None or vol > MAX_VOL_H24:
        return False

    # 24h buys / sells
    buys = data["buys"]
    sells = data["sells"]
    if buys is None or buys > MAX_BUYS_H24:
        return False
    if sells is None or sells > MAX_SELLS_H24:
        return False

    # No website
    if data["has_website"]:
        return False

    # Must have Telegram
    if not data["tg"]:
        return False

    return True


# --------------------------------------------------
# SCAN LOGIC
# --------------------------------------------------
async def scan_chain(chain_name: str, seen: set) -> list:
    """Scan a single chain and return a list of matching token data dicts."""
    chain_enum = get_chain_enum(chain_name)
    if chain_enum is None:
        log.warning(f"Unknown chain: {chain_name}")
        return []

    config = ScrapingConfig(
        timeframe=Timeframe.H24,
        rank_by=RankBy.VOLUME,
        filters=Filters(
            chain_ids=[chain_enum],
            liquidity_min=MIN_LIQ,
        ),
    )

    results = []
    try:
        scraper = DexScraper(config=config, use_cloudflare_bypass=True)
        batch = await scraper.extract_token_data()
    except Exception as e:
        log.error(f"dexscraper failed for {chain_name}: {e}")
        return []

    tokens = batch.get_top_tokens(500) if hasattr(batch, "get_top_tokens") else []
    if not tokens:
        tokens = getattr(batch, "tokens", []) or []

    log.info(f"{chain_name}: {len(tokens)} tokens fetched from dexscraper")

    for token in tokens:
        try:
            data = extract_token_data(token)
        except Exception as e:
            log.error(f"extract_token_data error: {e}")
            continue

        if not passes_filters(data):
            continue
        if data["tg"] in seen:
            continue

        results.append(data)
        seen.add(data["tg"])

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
        "• Must have Telegram, must NOT have website\n\n"
        "*Commands:*\n"
        "/status – show current settings\n"
        "/scannow – force a manual scan\n"
        "/chains – list active chains\n"
        "/setchains <c1,c2,...> – replace the chain list\n"
        "/addchain <c> – add one chain\n"
        "/removechain <c> – remove one chain\n"
        "/debug – inspect one token's raw attributes",
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
        f"Sells max: {MAX_SELLS_H24}\n"
        "No website: ON ✅",
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


async def cmd_debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fetch one batch and print the raw attributes of the first token."""
    await update.message.reply_text("🔍 Fetching one token for inspection...")
    chain = context.bot_data.get("chains", INITIAL_CHAINS)[0]
    chain_enum = get_chain_enum(chain)
    if chain_enum is None:
        await update.message.reply_text(f"Unknown chain: {chain}")
        return

    try:
        config = ScrapingConfig(
            timeframe=Timeframe.H24,
            rank_by=RankBy.VOLUME,
            filters=Filters(chain_ids=[chain_enum], liquidity_min=0),
        )
        scraper = DexScraper(config=config, use_cloudflare_bypass=True)
        batch = await scraper.extract_token_data()
        tokens = batch.get_top_tokens(1) if hasattr(batch, "get_top_tokens") else []
        if not tokens:
            tokens = getattr(batch, "tokens", []) or []
        if not tokens:
            await update.message.reply_text("No tokens returned.")
            return

        token = tokens[0]
        lines = ["*Token attributes:*"]
        for attr in dir(token):
            if attr.startswith("_"):
                continue
            try:
                val = getattr(token, attr)
            except Exception:
                continue
            if callable(val):
                continue
            lines.append(f"`{attr}` = `{val}`")
        text = "\n".join(lines[:60])
        await update.message.reply_text(text, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"Debug error: {e}")


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
            matches = await scan_chain(chain, seen)
        except Exception as e:
            log.error(f"scan_chain({chain}) failed: {e}")
            continue

        for m in matches:
            try:
                await context.bot.send_message(chat_id=chat_id, text=m["tg"])
                log.info(f"Sent {chain}/{m['symbol']} -> {m['tg']}")
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
    app.add_handler(CommandHandler("debug", cmd_debug))

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
