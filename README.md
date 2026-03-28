# 👑 KingPin Ticker Scanner Bot

A Telegram bot that scans any link (Twitter, TikTok, news, web) for crypto tickers and returns full on-chain data across Solana, Ethereum, BSC and Base.

## Features
- Paste any link → bot scans for tickers
- Returns PVP stats, OG ticker, vamp wallets, price, liquidity
- Supports all chains via DexScreener + CoinGecko
- /scan TICKER for direct lookups

## Deploy on Railway (Free)

1. Go to railway.app and sign up
2. Click "New Project" → "Deploy from GitHub repo"
3. Upload these files to a GitHub repo
4. Set environment variable: BOT_TOKEN=your_token
5. Deploy!

## Files
- bot.py — main bot code
- requirements.txt — dependencies
- Procfile — tells Railway how to run the bot
