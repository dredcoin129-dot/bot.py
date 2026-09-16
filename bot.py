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
    python bot.py
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
