"""
Kingpintickerscannerbot — Memecoin Ticker Scanner
Paste any TikTok or X post text and the bot scans for tickers,
ranks them (OG / Runner / Mid / Late / Dead) and alerts you
when a previously "not found" post later gets a ticker.

Requirements:
    pip install python-telegram-bot==20.7 requests

Setup:
    1. Get a bot token from @BotFather on Telegram
    2. Replace BOT_TOKEN below with your token
    3. Run:  python ticker_bot.py
"""

import re
import json
import os
import asyncio
import logging
from datetime import datetime, timezone
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
import requests

# ─────────────────────────────────────────────
#  CONFIG — replace with your actual bot token
# ─────────────────────────────────────────────
BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"

# How often (in seconds) to recheck "not found" posts for new tickers
RECHECK_INTERVAL = 3600  # 1 hour

# File where pending (no ticker yet) posts are stored
PENDING_FILE = "pending_posts.json"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
#  TICKER EXTRACTION
# ─────────────────────────────────────────────

TICKER_PATTERN = re.compile(r"\$([A-Z]{2,10})\b")

# Common words that look like tickers but aren't
IGNORE_LIST = {
    "THE", "FOR", "AND", "NOT", "BUT", "ARE", "YOU", "ALL",
    "CAN", "HAS", "ITS", "NOW", "NEW", "GET", "USE", "ONE",
    "TOP", "BIG", "OUT", "OFF", "USD", "ETH", "BTC", "SOL",
    "USDT", "USDC", "NFT", "DEX", "CEO", "API", "DM", "RT",
}


def extract_tickers(text: str) -> list[str]:
    """Pull all $TICKER symbols from raw text."""
    found = TICKER_PATTERN.findall(text.upper())
    return [t for t in found if t not in IGNORE_LIST]


def also_scan_hashtags(text: str) -> list[str]:
    """
    Some memecoin posts use #TICKER instead of $TICKER.
    Grab those too and merge with dollar-sign finds.
    """
    hashtag_pattern = re.compile(r"#([A-Z]{2,10})\b")
    found = hashtag_pattern.findall(text.upper())
    return [t for t in found if t not in IGNORE_LIST]


# ─────────────────────────────────────────────
#  TICKER RANKING  (via DexScreener — free API)
# ─────────────────────────────────────────────

def fetch_token_data(ticker: str) -> dict | None:
    """
    Query DexScreener for the token.
    Returns the best matching pair or None.
    """
    try:
        url = f"https://api.dexscreener.com/latest/dex/search?q={ticker}"
        resp = requests.get(url, timeout=10)
        data = resp.json()
        pairs = data.get("pairs") or []
        # Filter to Solana pairs only (most memecoins live on Solana)
        sol_pairs = [p for p in pairs if p.get("chainId") == "solana"]
        if sol_pairs:
            # Return the pair with the highest liquidity
            return max(sol_pairs, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0))
        # Fall back to any chain if no Solana pair
        if pairs:
            return max(pairs, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0))
        return None
    except Exception as e:
        logger.warning(f"DexScreener fetch failed for {ticker}: {e}")
        return None


def rank_ticker(pair: dict) -> str:
    """
    Rank a token based on its age and price change.

    OG    — launched > 30 days ago, still has volume
    Runner — < 30 days old, strong 24h gain (> +50%)
    Mid   — < 30 days old, moderate gain (+10% to +50%)
    Late  — price change negative or minimal, still active
    Dead  — very low liquidity or volume (< $1,000)
    """
    try:
        liquidity_usd = float(pair.get("liquidity", {}).get("usd", 0) or 0)
        volume_24h = float(pair.get("volume", {}).get("h24", 0) or 0)
        price_change_24h = float(pair.get("priceChange", {}).get("h24", 0) or 0)

        created_at = pair.get("pairCreatedAt")  # epoch ms
        age_days = None
        if created_at:
            age_days = (datetime.now(timezone.utc).timestamp() * 1000 - created_at) / (1000 * 86400)

        if liquidity_usd < 1000 or volume_24h < 1000:
            return "💀 DEAD"

        if age_days and age_days > 30:
            return "🏆 OG"

        if price_change_24h >= 50:
            return "🚀 RUNNER"

        if 10 <= price_change_24h < 50:
            return "⚡ MID"

        if price_change_24h < 10:
            return "🐢 LATE"

        return "❓ UNKNOWN"
    except Exception:
        return "❓ UNKNOWN"


def format_ticker_result(ticker: str, pair: dict) -> str:
    """Build a clean response message for a found ticker."""
    rank = rank_ticker(pair)
    name = pair.get("baseToken", {}).get("name", ticker)
    symbol = pair.get("baseToken", {}).get("symbol", ticker)
    price = pair.get("priceUsd", "N/A")
    change_24h = pair.get("priceChange", {}).get("h24", "N/A")
    liquidity = pair.get("liquidity", {}).get("usd", "N/A")
    volume_24h = pair.get("volume", {}).get("h24", "N/A")
    dex_url = pair.get("url", "")

    try:
        liquidity = f"${float(liquidity):,.0f}"
    except Exception:
        pass
    try:
        volume_24h = f"${float(volume_24h):,.0f}"
    except Exception:
        pass

    lines = [
        f"🔎 *${symbol}* — {name}",
        f"📊 Rank: *{rank}*",
        f"💵 Price: `${price}`",
        f"📈 24h Change: `{change_24h}%`",
        f"💧 Liquidity: `{liquidity}`",
        f"📦 24h Volume: `{volume_24h}`",
    ]
    if dex_url:
        lines.append(f"🔗 [View on DexScreener]({dex_url})")
    return "\n".join(lines)


# ─────────────────────────────────────────────
#  PENDING POSTS STORAGE
# ─────────────────────────────────────────────

def load_pending() -> list:
    if os.path.exists(PENDING_FILE):
        with open(PENDING_FILE, "r") as f:
            return json.load(f)
    return []


def save_pending(pending: list):
    with open(PENDING_FILE, "w") as f:
        json.dump(pending, f, indent=2)


def add_pending(chat_id: int, tickers: list[str], original_text: str):
    pending = load_pending()
    pending.append({
        "chat_id": chat_id,
        "tickers": tickers,
        "text": original_text[:300],
        "added_at": datetime.now(timezone.utc).isoformat(),
    })
    save_pending(pending)


# ─────────────────────────────────────────────
#  BOT HANDLERS
# ─────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome to *Kingpintickerscannerbot!*\n\n"
        "I scan TikTok and X posts for memecoin tickers and rank them.\n\n"
        "📌 *How to use:*\n"
        "1️⃣ Copy the full text of any TikTok or X post\n"
        "2️⃣ Paste it here and send\n"
        "3️⃣ I'll find any tickers and rank them as:\n"
        "   🏆 OG | 🚀 Runner | ⚡ Mid | 🐢 Late | 💀 Dead\n\n"
        "If no ticker is found yet, I'll monitor and alert you when one appears! 🔔",
        parse_mode="Markdown",
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *Kingpintickerscannerbot Help*\n\n"
        "*How to use:*\n"
        "Just paste the text of any TikTok or X (Twitter) post and send it to me.\n\n"
        "*Ranking explained:*\n"
        "🏆 *OG* — Token is 30+ days old and still active\n"
        "🚀 *Runner* — Less than 30 days old, pumping hard (+50% in 24h)\n"
        "⚡ *Mid* — Moderate gains (+10% to +50% in 24h)\n"
        "🐢 *Late* — Low or negative price movement\n"
        "💀 *Dead* — Very low liquidity or volume\n\n"
        "*Commands:*\n"
        "/start — Welcome message\n"
        "/help — This help message\n"
        "/pending — See posts I'm still monitoring",
        parse_mode="Markdown",
    )


async def pending_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    pending = load_pending()
    user_pending = [p for p in pending if p["chat_id"] == chat_id]
    if not user_pending:
        await update.message.reply_text("✅ You have no posts pending. All tickers were found!")
        return
    msg = f"⏳ *{len(user_pending)} post(s) still being monitored:*\n\n"
    for i, p in enumerate(user_pending[:5], 1):
        preview = p["text"][:80].replace("\n", " ")
        tickers = ", ".join([f"${t}" for t in p["tickers"]]) if p["tickers"] else "scanning..."
        msg += f"{i}. `{preview}...`\n   Tickers: {tickers}\n\n"
    await update.message.reply_text(msg, parse_mode="Markdown")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    chat_id = update.message.chat_id

    if len(text) < 5:
        await update.message.reply_text("Please paste the full post text — it looks a bit short!")
        return

    await update.message.reply_text("🔍 Scanning for tickers...")

    # Extract tickers from $ signs and # hashtags
    dollar_tickers = extract_tickers(text)
    hash_tickers = also_scan_hashtags(text)
    all_tickers = list(dict.fromkeys(dollar_tickers + hash_tickers))  # dedupe, preserve order

    if not all_tickers:
        await update.message.reply_text(
            "❌ *Ticker not found* in this post.\n\n"
            "I'll keep monitoring and alert you if a ticker appears later! 🔔\n\n"
            "_(Make sure the post actually mentions a $TICKER or #TICKER symbol)_",
            parse_mode="Markdown",
        )
        add_pending(chat_id, [], text)
        return

    results = []
    not_found = []

    for ticker in all_tickers:
        pair = fetch_token_data(ticker)
        if pair:
            results.append(format_ticker_result(ticker, pair))
        else:
            not_found.append(ticker)

    # Send found results
    if results:
        header = f"✅ Found *{len(results)}* ticker(s):\n\n"
        await update.message.reply_text(
            header + "\n\n─────────────\n\n".join(results),
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )

    # Handle tickers found in text but not yet on DEX
    if not_found:
        nf_list = ", ".join([f"${t}" for t in not_found])
        await update.message.reply_text(
            f"⏳ These tickers were mentioned but *not yet live* on any DEX:\n{nf_list}\n\n"
            f"I'll monitor and alert you when they go live! 🔔",
            parse_mode="Markdown",
        )
        add_pending(chat_id, not_found, text)


# ─────────────────────────────────────────────
#  BACKGROUND RECHECK JOB
# ─────────────────────────────────────────────

async def recheck_pending(context: ContextTypes.DEFAULT_TYPE):
    """Runs on a schedule. Rechecks all pending tickers."""
    pending = load_pending()
    if not pending:
        return

    still_pending = []

    for entry in pending:
        chat_id = entry["chat_id"]
        tickers = entry.get("tickers", [])

        if not tickers:
            # No tickers found before — rescan original text
            dollar_tickers = extract_tickers(entry.get("text", ""))
            hash_tickers = also_scan_hashtags(entry.get("text", ""))
            tickers = list(dict.fromkeys(dollar_tickers + hash_tickers))

        if not tickers:
            still_pending.append(entry)
            continue

        resolved = []
        unresolved = []

        for ticker in tickers:
            pair = fetch_token_data(ticker)
            if pair:
                resolved.append((ticker, pair))
            else:
                unresolved.append(ticker)

        if resolved:
            msg = "🔔 *Ticker Alert!* A ticker from a post you shared is now live:\n\n"
            for ticker, pair in resolved:
                msg += format_ticker_result(ticker, pair) + "\n\n─────────────\n\n"
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=msg,
                    parse_mode="Markdown",
                    disable_web_page_preview=True,
                )
            except Exception as e:
                logger.warning(f"Could not alert chat {chat_id}: {e}")

        if unresolved:
            entry["tickers"] = unresolved
            still_pending.append(entry)

    save_pending(still_pending)


# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("pending", pending_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Schedule the recheck job
    app.job_queue.run_repeating(recheck_pending, interval=RECHECK_INTERVAL, first=60)

    logger.info("Kingpintickerscannerbot is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
