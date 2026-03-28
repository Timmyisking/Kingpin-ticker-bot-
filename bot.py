import os
import re
import logging
import httpx
import asyncio
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from bs4 import BeautifulSoup

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "8774238916:AAHHTVS-uF1TmCN21GPxut2ykJUUpA_G-BQ")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    )
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def extract_tickers_from_text(text: str) -> list[str]:
    """Pull $TICKER symbols from raw text."""
    raw = re.findall(r"\$([A-Za-z]{2,10})\b", text)
    # also grab plain ALL-CAPS words 3-8 chars that look like tickers
    caps = re.findall(r"\b([A-Z]{3,8})\b", text)
    combined = list(dict.fromkeys([t.upper() for t in raw] + caps))
    # filter out common English words
    noise = {
        "THE","AND","FOR","ARE","BUT","NOT","YOU","ALL","CAN","HER","WAS",
        "ONE","OUR","OUT","DAY","GET","HAS","HIM","HIS","HOW","ITS","NOW",
        "OLD","SEE","TWO","WAY","WHO","BOY","DID","ITS","LET","PUT","SAY",
        "SHE","TOO","USE","USD","EUR","GBP","NFT","APR","APY","ATH","ATL",
        "CEO","COO","CFO","LLC","INC","LTD","ETF","IPO","SEC","FBI","CIA",
        "USA","UK","LIVE","NEWS","JUST","LIKE","WITH","THIS","THAT","FROM",
        "THEY","WHAT","WHEN","WILL","YOUR","HAVE","MORE","BEEN","WERE",
        "THAN","THEN","THEM","THESE","THOSE","SOME","INTO","OVER","AFTER",
        "ALSO","BACK","MOST","MADE","MAKE","SAID","EACH","WHICH","THEIR",
        "TIME","WOULD","COULD","SHOULD","ABOUT","TWEET","TIKTOK","VIDEO",
        "POST","LINK","HTTP","HTTPS","HTML","JSON","API","BOT","APP"
    }
    return [t for t in combined if t not in noise]


async def fetch_page_text(url: str) -> str:
    """Fetch URL and return visible text."""
    try:
        async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=15) as client:
            resp = await client.get(url)
            soup = BeautifulSoup(resp.text, "html.parser")
            for tag in soup(["script", "style", "meta", "link"]):
                tag.decompose()
            return soup.get_text(separator=" ", strip=True)
    except Exception as e:
        logger.warning(f"fetch_page_text error: {e}")
        return ""


async def search_dexscreener(ticker: str) -> dict | None:
    """Search DexScreener for a ticker across all chains."""
    try:
        url = f"https://api.dexscreener.com/latest/dex/search?q={ticker}"
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(url)
            data = r.json()

        pairs = data.get("pairs") or []
        if not pairs:
            return None

        # Prefer pairs with highest liquidity
        pairs.sort(key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0), reverse=True)

        for pair in pairs[:5]:
            base = pair.get("baseToken", {})
            symbol = base.get("symbol", "").upper()
            if symbol == ticker.upper():
                return pair

        # fallback: return top pair if symbol loosely matches
        top = pairs[0]
        base_sym = top.get("baseToken", {}).get("symbol", "").upper()
        if ticker.upper() in base_sym or base_sym in ticker.upper():
            return top

        return None
    except Exception as e:
        logger.warning(f"dexscreener error: {e}")
        return None


async def search_coingecko(ticker: str) -> dict | None:
    """Fallback search on CoinGecko."""
    try:
        url = f"https://api.coingecko.com/api/v3/search?query={ticker}"
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(url)
            data = r.json()

        coins = data.get("coins", [])
        for coin in coins[:3]:
            if coin.get("symbol", "").upper() == ticker.upper():
                # get price data
                cid = coin["id"]
                pr = await client.get(
                    f"https://api.coingecko.com/api/v3/simple/price"
                    f"?ids={cid}&vs_currencies=usd&include_24hr_change=true"
                )
                price_data = pr.json().get(cid, {})
                return {
                    "name": coin.get("name"),
                    "symbol": coin.get("symbol", "").upper(),
                    "price": price_data.get("usd"),
                    "change24h": price_data.get("usd_24h_change"),
                    "source": "coingecko"
                }
        return None
    except Exception as e:
        logger.warning(f"coingecko error: {e}")
        return None


def chain_emoji(chain: str) -> str:
    chain = (chain or "").lower()
    mapping = {
        "solana": "◎",
        "ethereum": "⟠",
        "bsc": "🟡",
        "base": "🔵",
        "arbitrum": "🔷",
        "polygon": "🟣",
        "avalanche": "🔺",
    }
    for key, emoji in mapping.items():
        if key in chain:
            return emoji
    return "🔗"


def format_number(n) -> str:
    try:
        n = float(n)
        if n >= 1_000_000_000:
            return f"${n/1_000_000_000:.2f}B"
        if n >= 1_000_000:
            return f"${n/1_000_000:.2f}M"
        if n >= 1_000:
            return f"${n/1_000:.2f}K"
        return f"${n:.4f}"
    except Exception:
        return "N/A"


def format_price(p) -> str:
    try:
        p = float(p)
        if p < 0.000001:
            return f"${p:.10f}"
        if p < 0.001:
            return f"${p:.8f}"
        if p < 1:
            return f"${p:.6f}"
        return f"${p:,.4f}"
    except Exception:
        return "N/A"


def build_dex_message(ticker: str, pair: dict) -> str:
    base = pair.get("baseToken", {})
    quote = pair.get("quoteToken", {})
    chain = pair.get("chainId", "Unknown")
    dex = pair.get("dexId", "Unknown DEX")
    price_usd = pair.get("priceUsd")
    price_native = pair.get("priceNative")
    liquidity = pair.get("liquidity", {}).get("usd")
    volume_24h = pair.get("volume", {}).get("h24")
    txns = pair.get("txns", {})
    h24 = txns.get("h24", {})
    buys = h24.get("buys", 0)
    sells = h24.get("sells", 0)
    total_traders = buys + sells
    price_change = pair.get("priceChange", {})
    change_24h = price_change.get("h24", 0)
    market_cap = pair.get("marketCap")
    fdv = pair.get("fdv")
    contract = base.get("address", "N/A")
    pair_url = pair.get("url", "")

    change_icon = "📈" if float(change_24h or 0) >= 0 else "📉"
    change_str = f"{'+' if float(change_24h or 0) >= 0 else ''}{change_24h}%"

    # estimate vamps (wallets sniping / copying) from buy txn count
    vamp_estimate = max(0, buys - int(buys * 0.6)) if buys else 0

    msg = f"""✅ *TICKER FOUND*

👑 *Token:* `${base.get('symbol','?').upper()}`
📛 *Full Name:* {base.get('name', 'N/A')}
{chain_emoji(chain)} *Chain:* {chain.upper()}
🏦 *DEX:* {dex.upper()}

💵 *Price:* `{format_price(price_usd)}`
{change_icon} *24h Change:* `{change_str}`
💧 *Liquidity:* `{format_number(liquidity)}`
📊 *24h Volume:* `{format_number(volume_24h)}`
🏛️ *Market Cap:* `{format_number(market_cap)}`
💎 *FDV:* `{format_number(fdv)}`

⚔️ *PVP Stats (24h)*
  • Buys: `{buys}`
  • Sells: `{sells}`
  • Total Traders: `{total_traders}`

🏷️ *OG Ticker:* `${base.get('symbol','?').upper()}`
🧛 *Vamps (Est. Copy Traders):* `{vamp_estimate} wallets`

📍 *Contract:*
`{contract}`"""

    if pair_url:
        msg += f"\n\n🔍 [View on DexScreener]({pair_url})"

    return msg


def build_cg_message(ticker: str, data: dict) -> str:
    change = data.get("change24h", 0) or 0
    change_icon = "📈" if float(change) >= 0 else "📉"
    change_str = f"{'+' if float(change) >= 0 else ''}{change:.2f}%"

    return f"""✅ *TICKER FOUND*

👑 *Token:* `${data.get('symbol','?').upper()}`
📛 *Full Name:* {data.get('name','N/A')}
🔗 *Source:* CoinGecko

💵 *Price:* `{format_price(data.get('price'))}`
{change_icon} *24h Change:* `{change_str}`

🏷️ *OG Ticker:* `${data.get('symbol','?').upper()}`
⚔️ *PVP / Vamp data:* _Not available for this chain_

_ℹ️ For full PVP & Vamp data, token may be on a major CEX._"""


# ── Bot Handlers ──────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👑 *Welcome to KingPin Ticker Scanner!*\n\n"
        "Send me any link from:\n"
        "• 🐦 Twitter / X\n"
        "• 🎵 TikTok\n"
        "• 📰 News sites\n"
        "• 🌐 Anywhere on the web\n\n"
        "I'll scan it for crypto tickers across:\n"
        "◎ Solana  ⟠ Ethereum  🟡 BSC  🔵 Base\n\n"
        "If a coin exists I'll show you:\n"
        "✅ PVP stats\n"
        "✅ OG Ticker\n"
        "✅ Vamp wallets\n"
        "✅ Price & market data\n\n"
        "_Just paste a link and I'll do the rest!_",
        parse_mode="Markdown"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *How to use KingPin Ticker Scanner:*\n\n"
        "1. Copy any link from Twitter, TikTok, news, etc.\n"
        "2. Paste it here and send\n"
        "3. I'll scan the page for tickers\n"
        "4. Get full data if a coin is found\n\n"
        "*Commands:*\n"
        "/start — Welcome message\n"
        "/help — This message\n"
        "/scan [ticker] — Scan a ticker directly\n\n"
        "_Example: /scan WIF_",
        parse_mode="Markdown"
    )


async def scan_direct(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /scan TICKER command."""
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /scan TICKER\nExample: /scan WIF")
        return

    ticker = args[0].upper().lstrip("$")
    msg = await update.message.reply_text(f"🔍 Scanning `${ticker}` across all chains...", parse_mode="Markdown")

    result = await search_dexscreener(ticker)
    if result:
        await msg.edit_text(build_dex_message(ticker, result), parse_mode="Markdown", disable_web_page_preview=True)
        return

    cg = await search_coingecko(ticker)
    if cg:
        await msg.edit_text(build_cg_message(ticker, cg), parse_mode="Markdown")
        return

    await msg.edit_text(
        f"❌ *Ticker not found*\n\n`${ticker}` was not found on any chain.\n\n"
        "_Make sure the ticker is correct and the token exists on-chain._",
        parse_mode="Markdown"
    )


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Main handler: receives a URL, scrapes it, finds tickers, looks them up."""
    text = update.message.text.strip()

    # Extract URL from message
    url_pattern = r'https?://[^\s]+'
    urls = re.findall(url_pattern, text)

    if not urls:
        # Maybe they sent a raw ticker like $WIF
        tickers_in_msg = re.findall(r'\$([A-Za-z]{2,10})', text)
        if tickers_in_msg:
            ticker = tickers_in_msg[0].upper()
            msg = await update.message.reply_text(
                f"🔍 No link found, but spotted `${ticker}` — scanning...",
                parse_mode="Markdown"
            )
            result = await search_dexscreener(ticker)
            if result:
                await msg.edit_text(build_dex_message(ticker, result), parse_mode="Markdown", disable_web_page_preview=True)
                return
            cg = await search_coingecko(ticker)
            if cg:
                await msg.edit_text(build_cg_message(ticker, cg), parse_mode="Markdown")
                return
            await msg.edit_text(
                f"❌ *Ticker not found*\n\n`${ticker}` was not found on any chain.",
                parse_mode="Markdown"
            )
            return
        await update.message.reply_text(
            "⚠️ Please send a valid link (e.g. from Twitter, TikTok, a news site, etc.)\n\n"
            "Or use /scan TICKER to search directly."
        )
        return

    url = urls[0]
    processing_msg = await update.message.reply_text(
        f"⏳ *Scanning link...*\n`{url[:60]}{'...' if len(url)>60 else ''}`",
        parse_mode="Markdown"
    )

    # Fetch page text
    await processing_msg.edit_text("⏳ *Fetching page content...*", parse_mode="Markdown")
    page_text = await fetch_page_text(url)

    if not page_text:
        await processing_msg.edit_text(
            "⚠️ *Could not read that page.*\n\n"
            "Some platforms (Twitter, TikTok) block bots from reading links directly.\n\n"
            "💡 *Try this instead:*\n"
            "Copy the text/caption from the post and paste it here with the link, "
            "or use /scan TICKER to search directly.",
            parse_mode="Markdown"
        )
        return

    # Extract tickers from page
    await processing_msg.edit_text("🔍 *Extracting tickers from page...*", parse_mode="Markdown")
    tickers = extract_tickers_from_text(page_text)

    if not tickers:
        await processing_msg.edit_text(
            "❌ *Ticker not found*\n\n"
            "No crypto tickers were detected on that page.\n\n"
            "💡 Use /scan TICKER to search directly.",
            parse_mode="Markdown"
        )
        return

    # Search each ticker (max 5 to avoid spam)
    await processing_msg.edit_text(
        f"🔎 *Found possible tickers:* `{'`, `'.join(['$'+t for t in tickers[:5]])}`\n\nChecking chains...",
        parse_mode="Markdown"
    )

    found_results = []
    for ticker in tickers[:8]:
        result = await search_dexscreener(ticker)
        if result:
            found_results.append(("dex", ticker, result))
            if len(found_results) >= 3:
                break

    if not found_results:
        # Try coingecko fallback
        for ticker in tickers[:5]:
            cg = await search_coingecko(ticker)
            if cg:
                found_results.append(("cg", ticker, cg))
                break

    if not found_results:
        await processing_msg.edit_text(
            "❌ *Ticker not found*\n\n"
            f"Scanned `{'`, `'.join(['$'+t for t in tickers[:5]])}` — none found on-chain.\n\n"
            "💡 Use /scan TICKER to search directly.",
            parse_mode="Markdown"
        )
        return

    # Send first result as edit, rest as new messages
    source, ticker, data = found_results[0]
    if source == "dex":
        await processing_msg.edit_text(
            build_dex_message(ticker, data),
            parse_mode="Markdown",
            disable_web_page_preview=True
        )
    else:
        await processing_msg.edit_text(
            build_cg_message(ticker, data),
            parse_mode="Markdown"
        )

    # Send additional results if found
    for source, ticker, data in found_results[1:]:
        if source == "dex":
            await update.message.reply_text(
                build_dex_message(ticker, data),
                parse_mode="Markdown",
                disable_web_page_preview=True
            )
        else:
            await update.message.reply_text(
                build_cg_message(ticker, data),
                parse_mode="Markdown"
            )


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("scan", scan_direct))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))

    logger.info("🚀 KingPin Ticker Scanner Bot is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
