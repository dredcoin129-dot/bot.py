"""
DexScreener new-memecoin alert bot
===================================

Scans for very-new tokens on a given chain and pushes the Telegram
invite link for any that pass your filters:
  - market cap between MIN_MC and MAX_MC
  - has both a Telegram group and a Twitter/X account
  - has NO website
  - pair created within AGE_HOURS

READ THIS FIRST -- one gap you'll need to fill in:
DexScreener's public API (api.dexscreener.com, free, no key needed) is
built for looking up pairs/tokens you already have an address for --
search, or by address. There is no documented endpoint that streams
"every new pair created on chain X." So `discover_candidate_addresses()`
below is a stub: wire in your existing scraper's output, an Apify
DexScreener actor, or an on-chain "pool created" listener. Everything
downstream of that (enrichment, filtering, chain switching, dedup,
scheduling, commands, link-only output) is fully implemented against
the real DexScreener response schema.

Setup:
    pip install -r requirements.txt
    export TELEGRAM_TOKEN="123456:your-botfather-token"
    python memecoin_alert_bot.py
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("memecoin-bot")

DEXSCREENER_TOKENS_URL = "https://api.dexscreener.com/tokens/v1/{chain}/{address}"

DEFAULT_CHAIN = os.environ.get("DEFAULT_CHAIN", "solana")
DEFAULT_MIN_MC = float(os.environ.get("MIN_MC", 5_000))
DEFAULT_MAX_MC = float(os.environ.get("MAX_MC", 500_000))
DEFAULT_AGE_HOURS = float(os.environ.get("AGE_HOURS", 24))
DEFAULT_INTERVAL_SECONDS = int(os.environ.get("CHECK_INTERVAL", 300))


@dataclass
class ChatState:
    """Per-chat settings and in-memory 'already sent' tracking.
    Resets on restart -- that's an accepted tradeoff, not a bug."""
    chain: str = DEFAULT_CHAIN
    min_mc: float = DEFAULT_MIN_MC
    max_mc: float = DEFAULT_MAX_MC
    age_hours: float = DEFAULT_AGE_HOURS
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS
    seen: set[str] = field(default_factory=set)


STATE: dict[int, ChatState] = {}


def get_state(chat_id: int) -> ChatState:
    return STATE.setdefault(chat_id, ChatState())


async def discover_candidate_addresses(chain: str) -> list[str]:
    """*** PLUG YOUR NEW-PAIR SOURCE IN HERE ***
    Return a list of token addresses on `chain` worth checking this cycle.
    Returning [] just means "nothing new to check" this pass. Wire in your
    existing scraper, an Apify DexScreener actor, or an on-chain
    "pool created" listener here."""
    return []


async def fetch_pair(client: httpx.AsyncClient, chain: str, address: str) -> Optional[dict]:
    url = DEXSCREENER_TOKENS_URL.format(chain=chain, address=address)
    try:
        resp = await client.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        # Handles both the older {"pairs": [...]} shape and the newer
        # v1 endpoints' bare-array responses -- verify against a live
        # call and simplify once you've seen the real shape.
        pairs = data.get("pairs", []) if isinstance(data, dict) else data
        return pairs[0] if pairs else None
    except (httpx.HTTPError, ValueError, IndexError, KeyError) as exc:
        log.warning("DexScreener lookup failed for %s: %s", address, exc)
        return None


def passes_filters(pair: dict, state: ChatState) -> bool:
    info = pair.get("info") or {}
    websites = info.get("websites") or []
    socials = info.get("socials") or []
    # NOTE: verify this against a live response -- DexScreener's exact
    # platform label for X/Twitter isn't documented; checking both covers it.
    platforms = {(s.get("platform") or "").lower() for s in socials}

    market_cap = pair.get("marketCap") or pair.get("fdv") or 0
    created_at_ms = pair.get("pairCreatedAt")  # verify this field name against a live response

    if not (state.min_mc <= market_cap <= state.max_mc):
        return False
    if websites:  # must NOT have a website
        return False
    if "telegram" not in platforms:
        return False
    if not ({"twitter", "x"} & platforms):
        return False
    if created_at_ms is None:
        return False
    age_hours = (time.time() * 1000 - created_at_ms) / 3_600_000
    return age_hours <= state.age_hours


def extract_telegram_link(pair: dict) -> Optional[str]:
    for s in (pair.get("info") or {}).get("socials", []):
        if (s.get("platform") or "").lower() != "telegram":
            continue
        handle = (s.get("handle") or "").strip().lstrip("@")
        if not handle:
            continue
        if handle.startswith("http"):
            return handle
        if handle.startswith("t.me/"):
            return f"https://{handle}"
        return f"https://t.me/{handle}"
    return None


async def scan_once(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> int:
    state = get_state(chat_id)
    candidates = await discover_candidate_addresses(state.chain)
    sent = 0
    async with httpx.AsyncClient() as client:
        for address in candidates:
            if address in state.seen:
                continue
            state.seen.add(address)
            pair = await fetch_pair(client, state.chain, address)
            if not pair or not passes_filters(pair, state):
                continue
            link = extract_telegram_link(pair)
            if link:
                await context.bot.send_message(chat_id=chat_id, text=link)
                sent += 1
    return sent


async def scheduled_scan(context: ContextTypes.DEFAULT_TYPE) -> None:
    await scan_once(context.job.chat_id, context)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    state = get_state(chat_id)
    for job in context.job_queue.get_jobs_by_name(f"scan_{chat_id}"):
        job.schedule_removal()
    context.job_queue.run_repeating(
        scheduled_scan, interval=state.interval_seconds, first=5,
        chat_id=chat_id, name=f"scan_{chat_id}",
    )
    await update.message.reply_text(
        f"Watching {state.chain}. MC ${state.min_mc:,.0f}-${state.max_mc:,.0f}, "
        f"under {state.age_hours:g}h old, TG+X required, no website.\n"
        f"/status  /scannow  /setchain <chain>"
    )


async def setchain(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = get_state(update.effective_chat.id)
    if not context.args:
        await update.message.reply_text("Usage: /setchain <chain>  e.g. /setchain base")
        return
    state.chain = context.args[0].lower()
    state.seen.clear()
    await update.message.reply_text(f"Chain set to {state.chain}. Seen-list cleared.")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = get_state(update.effective_chat.id)
    await update.message.reply_text(
        f"Chain: {state.chain}\n"
        f"MC range: ${state.min_mc:,.0f}-${state.max_mc:,.0f}\n"
        f"Max age: {state.age_hours:g}h\n"
        f"Interval: {state.interval_seconds}s\n"
        f"Seen this session: {len(state.seen)}"
    )


async def scannow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Scanning now...")
    sent = await scan_once(update.effective_chat.id, context)
    if sent == 0:
        await update.message.reply_text("No new matches.")


def main() -> None:
    token = os.environ["TELEGRAM_TOKEN"]
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("setchain", setchain))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("scannow", scannow))
    log.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
