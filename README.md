# Vexora — Crypto & World News Radar (Telegram bot)

Vexora watches **114 sources** around the clock — fast Telegram news wires, crypto media, world and
regional news, US politics, markets, central banks, regulators, exchange announcements, on-chain
whale transfers, Google News and Reddit — and turns them into signals for crypto traders:

- **What matters now:** every item gets an importance score 1–10 with reasons, a bullish/bearish tone and topics.
- **Instant alerts:** important news pushed within seconds, at the level and categories each user chooses.
- **Memory:** BTC/ETH prices are recorded around every news item (at publication, +15 m, +1 h, +4 h, +24 h),
  including ~90 days of imported archive news, so the bot learns how the market reacted to each kind of event.
- **Learning from history:** for a new event it finds similar past events and shows how BTC moved after them —
  median move, how often it rose, range, how much bigger than usual, and confidence.
- **Market mood:** a transparent 0–100 index from Fear & Greed, news tone, price momentum, futures funding and positioning,
  plus open interest, stablecoin liquidity and Bitcoin network data.
- **Daily digest** at each user's local hour, and an **economic calendar** with reminders before CPI, payrolls, FOMC…
- **Exchanges:** prices, funding rates, order books and cross-exchange gaps on Binance, Bybit, OKX and Kraken.

> Signals, not financial advice. Past reactions don't guarantee future ones.

## Screens

New users get a 3-step setup on `/start`: tap the time on their clock (sets the time zone), choose how many
alerts, choose a morning-brief time. Everything can be changed later in ⚙️ Settings.

| Menu button | What it shows |
|---|---|
| 🏠 Home | Dashboard: BTC/ETH, market mood, Fear & Greed, alert status, next big release, top 3 stories, quick buttons |
| 🔥 Top | The most important stories of the last 24 h, one line per story (duplicates from other outlets merged) |
| 📰 News | Latest news by category (crypto, politics, elections, economy, regulation), search by word, pages |
| 📊 Sentiment | Market mood 0–100 with its components, Fear & Greed history, news tone, funding, long/short, open interest, liquidity, network |
| 💹 Markets | A coin across exchanges, funding rates, order book per exchange, buy/sell gap |
| 📅 Calendar | This/next week's economic releases in the user's time zone, impact filter, reminders toggle |
| 🧠 History | Biggest BTC moves after news; average moves by topic and by category |
| ⚙️ Settings | One place for everything: alerts (level, categories, test), 🌙 quiet hours (critical news can still come through), ⏸ pause 1 h / until morning, morning brief and time zone, release reminders, system status, run setup again |
| ❓ Help | How to use the bot |

Every alert has 📖 Details, 🧠 Similar past events and 🔕 pause buttons (1 h / until morning). Typing a word
such as `settings`, `calendar`, `mood` or `top` (also in Russian: `настройки`, `календарь`…) opens that screen.

Every story opens a detail view: importance and why, tone, topics, summary, BTC/ETH reaction table,
other outlets with the same story, and **🧠 Similar past events**. Type a coin (`sol`) to see its markets,
any word (`fed`) to search news, or `/similar <description>` to ask history about any event.

## How the analysis works

| Component | Method |
|---|---|
| Importance 1–10 | Source quality (central banks, wires, major outlets, exchanges) + type of event (rate decision, war, collapse, ETF decision, hack, CPI/jobs, regulator action, listing…) + money mentioned + crypto relevance + confirmation by other outlets; minus opinion, speculation ("could", "odds", "warns") and off-topic. Each point leaves a visible reason. |
| Tone | Bullish/bearish word rules from the crypto point of view (rate cut, inflows, ceasefire = bullish; hack, hike, tariffs, war = bearish), with negation handling ("not hacked") and macro context ("debt hits record" is bearish). |
| Similar events | TF-IDF headline similarity (55 %) + shared topics (35 %) + same category (10 %); the same moment (±6 h) excluded; one event per 30-minute window. |
| Signal | Bullish/bearish lean only when the 4 h median and the share of rises agree; confidence from sample size and consistency; volatility vs. the average news item. |
| Market mood | 35 % Fear & Greed, 25 % news tone (importance-weighted), 20 % momentum, 10 % funding, 10 % retail positioning — components always shown. |

## Data sources

| Group | Sources | Interval |
|---|---|---|
| ⚡ Telegram breaking news | Walter Bloomberg, FinancialJuice, Bloomberg, Kobeissi Letter, Watcher Guru, Wu Blockchain, CryptoQuant, Insider Paper, World News, Al Jazeera, CoinDesk, The Block, BRICS News, Disclose.tv | 20 s |
| 🏦 Exchange announcements | Binance, Bybit, OKX (listings, delistings) | 20 s |
| 🐋 On-chain | Whale Alert (transfers ≥ $50M), Glassnode, Unfolded | 20 s |
| Crypto media | CoinDesk, Cointelegraph, Decrypt, The Block, Bitcoin Magazine, Blockworks, DL News, The Defiant, CryptoSlate, Protos, Unchained and more (20) | 60 s |
| World & regions | BBC, Al Jazeera, Guardian, NYT, DW, France 24, Sky, CNN, CBS, ABC, NPR, Le Monde, Time, SCMP, Japan Times, Times of India, Moscow Times, Middle East Eye, Africanews, Latin Times | 60 s |
| US politics | Guardian, NYT, Politico, NPR, The Hill, Axios, Fox News, Washington Post, Bloomberg Politics | 60 s |
| Economy & markets | Bloomberg, WSJ, CNBC (3), MarketWatch, BBC Business, ForexLive, FXStreet, Investing.com, The Economist, ZeroHedge | 60 s |
| Central banks & regulators | Fed, ECB, Bank of England, Bank of Japan, BIS, SEC, CFTC, ESMA, FCA, US DOJ | 60 s |
| Google News | 16 topic searches (Bitcoin, ETFs, hacks, regulation, Fed, inflation, tariffs, elections, wars, Middle East, Russia/Ukraine, China, oil, yields…) — major outlets only | 5 min |
| Reddit | r/CryptoCurrency, r/Bitcoin, r/ethereum, r/CryptoMarkets, r/worldnews, r/Economics, r/geopolitics | 10 min |
| Archive | GDELT: ~90 days of headlines for 14 topics, imported once in the background | one-time |
| Market data | ccxt (4 exchanges), Binance & Bybit futures positioning, CoinGecko global, alternative.me Fear & Greed, DefiLlama stablecoins, mempool.space, blockchain.info | cached |
| Calendar | Forex Factory weekly calendar | 30 min |
| Optional (keys) | CryptoPanic (news + community votes), X/Twitter API v2 (curated accounts) | 2 min / 60 s |

Not included: Truth Social and Farside ETF flows block automated access; CryptoPanic and X need paid keys
(add them in `.env` and they switch on automatically).

## Getting started

```powershell
python -m venv .venv
.venv\Scripts\activate                 # Linux/macOS: source .venv/bin/activate
pip install -r requirements-dev.txt
copy .env.example .env                 # put your BOT_TOKEN in .env (all settings are documented there)
python -m bot.main                     # start (Ctrl+C to stop)
pytest                                 # tests
ruff check .                           # lint
```

Or with Docker: `docker compose up -d --build` (data and logs persist in the `bot_data` volume).

On first start the bot imports the news archive in the background (~20–40 min, progress in 📡 Status) and
fills in prices for it; patterns and similar-event signals get richer as it does.

Set `ADMIN_IDS` to your Telegram user id to use `/admin` (users, database size, alerts sent, job health,
failing sources, import progress).

## Project layout

```text
bot/
  main.py               entry point: services, scheduler (10 jobs), routers
  context.py            App (shared services) + market_view(): the full market snapshot
  scheduler.py          job runner with health tracking (last run, failures, duration)
  jobs.py               news, scoring, price memory, archive import, calendar, reminders, snapshots, digests, retention
  notifier.py           importance/tone scoring of new items and instant alerts
  handlers.py           news, top, markets, history, alerts, status, free-text search
  handlers_insights.py  sentiment, calendar, digest, similar events, admin
  handlers_settings.py  /start setup, home dashboard, settings, quiet hours, pause
  settings_ui.py        setup / settings / quiet hours / home screens
  screens.py            news / markets / history / alerts / status screens
  insights.py           sentiment / calendar / digest / similar / admin screens
  formatting.py         number and time formatting
collectors/
  news.py               114 sources: Telegram, RSS, Google News, Reddit, CryptoPanic, X
  history.py            GDELT archive import
  exchanges.py          ccxt: tickers, order books, candles, funding
  market_data.py        global market, futures positioning, stablecoins, network
  calendar.py           economic calendar
  fear_greed.py         Fear & Greed with 90-day history
analysis/
  importance.py         importance score 1–10 with reasons
  sentiment.py          headline tone, news sentiment index, market mood
  similar.py            similar past events → outlook and signal
  patterns.py           averages by topic, same-story detection
  reactions.py          BTC/ETH price around every item from 1-minute candles
storage/
  db.py                 SQLite (WAL) models, automatic column migrations, queries
tests/                  112 tests, no network needed
docs/ARCHITECTURE.md    data flow, jobs, design decisions
```

## Operations

- **Health:** 📡 Status and `/admin` show every background job (last success, failures); the Docker image has a HEALTHCHECK.
- **Resilience:** each source and API fails independently; rate-limited sources back off; the archive import resumes after
  restarts; missing price points are filled from exchange history after downtime.
- **Data retention:** news older than `RETENTION_DAYS` (default 365) is deleted with its price history, daily.
- **Logs:** console, plus an optional rotating file (`LOG_FILE`). API keys never appear in logs.
- **CI:** GitHub Actions runs lint, tests and a Docker build on every push.
