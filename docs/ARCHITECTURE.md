# Architecture

## Data flow

```text
 114 sources ──► collectors/news.py ──► storage (dedupe by URL + headline) ──► notifier.score_items
 (Telegram 20s,     per-source interval,       │                                 importance 1–10 + tone
  RSS 60s,          ETag/If-Modified-Since,    │                                        │
  Google 5m,        back-off on 429            │                                        ▼
  Reddit 10m)                                  │                          Notifier: users whose level &
                                               │                          categories match, no repeated
 GDELT archive ──► collectors/history.py ──────┤                          stories, ≤12/hour  ──► Telegram
 (90 days, once)                               │
                                               ▼
 Binance 1-minute candles ──► analysis/reactions.py: BTC/ETH at 0, +15m, +1h, +4h, +24h for every item
                                               │
                                               ▼
                         analysis/similar.py (index of past events with known reactions, refreshed every 10 min)
                         analysis/patterns.py (averages by topic / category)
                                               │
                                               ▼
           screens: article view, alerts, /similar, 🧠 History, ☀️ digest ("what history says")

 Exchanges, futures APIs, CoinGecko, Fear & Greed, DefiLlama, mempool ──► bot/context.market_view()
                                               ──► 📊 Sentiment, ☀️ digest, hourly mood snapshots (trend)

 Forex Factory calendar ──► calendar_events ──► 📅 Calendar, reminders 30 min before, digest,
                                                and released events become memory events
```

## Background jobs (bot/main.py)

| Job | Every | Does |
|---|---|---|
| news-fast | 3 s | Telegram channels whose 20 s interval passed |
| news | 5 s | RSS / Google News / Reddit / CryptoPanic / X whose interval passed |
| scoring | 20 s | importance + tone for anything not yet scored (startup backfill, archive, calendar) |
| price-memory | 60 s | due BTC/ETH price points from 1-minute candles (fills gaps after downtime) |
| history-import | 8 s | one GDELT archive request (GDELT allows 1 per 5 s); progress saved in `kv` |
| calendar | 30 min | this/next week's calendar; records past high-impact releases as events |
| reminders | 60 s | 30 min before high-impact US releases, once per user and event |
| mood-snapshot | 1 h | market mood + components, for trends |
| digest | 60 s | sends each user's brief at their local hour, once per day |
| retention | 24 h | deletes data older than `RETENTION_DAYS` |

The scheduler records last success, failures and duration per job; a job is unhealthy after 3 consecutive
failures or when it hasn't succeeded for 3 intervals (min 10 min). Status and `/admin` show this.

## Design decisions

- **SQLite in WAL mode** — one file, no server; readers never block the collectors. Columns added in later
  versions are migrated automatically at startup (`storage/db.py: MIGRATIONS`).
- **Prices from candles, not live ticks** — reactions are computed from historical 1-minute candles, so
  downtime loses nothing and archive news gets reactions too.
- **Archive on a fixed 10-day grid** — window boundaries never move, so each finished window is imported once.
- **Events counted once per 30 minutes** — dozens of outlets report the same moment; counting each would
  multiply one price move.
- **Rules, not a model, for importance and tone** — transparent (every point has a reason), free, instant,
  testable. An LLM can replace them later behind the same interfaces (`analysis/importance.score`,
  `analysis/sentiment.headline_tone`).
- **Every external call can fail independently** — screens render with whatever data is available.
- **Secrets never in logs** — HTTP errors are logged by type/status only (URLs may contain API keys).
