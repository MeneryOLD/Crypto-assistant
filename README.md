# Crypto Intelligence

An AI-powered cryptocurrency market intelligence and trading research assistant, accessible through Telegram.

## Overview

Crypto Intelligence collects, processes and analyzes information from cryptocurrency markets and external sources to help experienced traders and investors make more informed decisions.

The application combines market data, financial news, economic developments, regulatory updates and AI-powered research into one interface.

It does not automatically execute trades. Instead, it provides structured analysis, evidence, potential scenarios, risks and alerts so that users can make their own trading decisions.

## Core objectives

- Collect relevant cryptocurrency and financial information from multiple sources.
- Monitor BTC, ETH and the top 20 cryptocurrencies by market capitalization.
- Identify significant market movements and potentially relevant events.
- Connect news and external developments with market data.
- Provide AI-assisted research and market explanations.
- Generate evidence-based trading theses and scenario analysis.
- Monitor user watchlists and notify users about relevant developments.

## Main features

### 1. Market Data Collection

Collect and store:

- Current prices and historical prices.
- Trading volume.
- Market capitalization.
- Price changes and volatility.
- Available exchange market data.
- Order book information, where available.

### 2. News and Information Collection

Monitor information from:

- Cryptocurrency news publications.
- Official cryptocurrency project announcements.
- Exchange announcements.
- Regulatory authorities.
- Central banks and financial institutions.
- Economic data providers.
- Geopolitical and political developments.
- On-chain data providers.

Every collected item should retain its source, publication time, retrieval time and original URL.

### 3. AI Research Assistant

Users can ask questions such as:

- Why is Bitcoin falling today?
- What happened in the crypto market over the last 24 hours?
- What are the latest developments concerning Ethereum?
- What events could affect the market this week?
- What are the arguments for and against a bullish BTC scenario?

The assistant should:

- Retrieve relevant information.
- Separate facts from interpretations.
- Connect related events and market movements.
- Cite its sources.
- Explain uncertainty and conflicting evidence.
- Avoid presenting speculation as fact.

### 4. Trading Research

Generate structured research reports containing:

- Market context.
- Relevant developments.
- Bullish and bearish arguments.
- Potential scenarios.
- Supporting evidence.
- Risks and invalidation conditions.
- Important events to monitor.

The system should not present AI-generated analysis as guaranteed predictions or personalized financial advice.

### 5. Alerts and Watchlists

Users should be able to:

- Follow selected cryptocurrencies.
- Set price and percentage-change alerts.
- Receive important news notifications.
- Receive alerts about significant market events.
- Follow selected research topics.

### 6. Telegram Interface

Initial commands:

| Command | Description |
|---|---|
| `/start` | Start the bot |
| `/help` | Display available commands |
| `/market` | Display market overview |
| `/price` | Check cryptocurrency prices |
| `/news` | Latest relevant news |
| `/research` | Request AI-powered research |
| `/watchlist` | Manage followed assets |
| `/alerts` | Manage notifications |

The interface will also use inline keyboards and interactive buttons.

## Technology Stack

| Component | Technology |
|---|---|
| Language | TypeScript |
| Runtime | Node.js |
| Telegram framework | grammY |
| Database | PostgreSQL |
| ORM | Prisma |
| HTTP server | Fastify |
| AI integration | LLM API |
| Market data | CoinGecko and exchange APIs |
| Background jobs | BullMQ (later stage) |
| Queue/cache | Redis (later stage) |
| Containerization | Docker |
| Testing | Vitest |
| Version control | Git and GitHub |

## Architecture

The application follows a modular monolith architecture.

```text
Telegram User
     |
     v
Telegram Bot (grammY)
     |
     v
Application Layer
     |
     +-------------------+
     |                   |
     v                   v
Market Module       Research Module
     |                   |
     v                   v
Market Providers     News Providers
     |                   |
     +---------+---------+
               |
               v
        Data Processing
               |
               v
          PostgreSQL
               |
               v
          AI Analysis
               |
               v
       Research Reports
               |
               v
       Telegram Alerts
```

The Telegram interface must remain separate from the business logic.

## Data Sources

Potential integrations:

- CoinGecko: market prices, volume and market capitalization.
- Binance and Kraken: exchange market data.
- Official project websites and announcement feeds.
- Regulatory authority publications.
- Financial news and RSS feeds.
- Economic data providers.
- On-chain data providers.

API availability, pricing, rate limits and usage permissions must be checked before implementation.

## Initial MVP Scope

The first version should focus on:

1. Telegram bot setup.
2. Cryptocurrency market data collection.
3. News collection from a limited number of reliable sources.
4. PostgreSQL storage.
5. Basic market overview.
6. AI-powered research with source citations.
7. Simple watchlists and notifications.

## Explicitly Out of Scope for the MVP

- Automatic trade execution.
- Exchange account management.
- Leverage and derivatives execution.
- Advanced on-chain analytics.
- Predictive machine-learning models.
- A separate web or mobile application.

These features can be evaluated after the core research product works reliably.

## Development Principles

- Prefer reliable data over large amounts of unverified information.
- Store source provenance for every research item.
- Separate data collection, processing and analysis.
- Make AI outputs traceable to their supporting evidence.
- Handle API failures and rate limits.
- Avoid duplicate events and notifications.
- Keep user data secure.
- Test business logic independently from Telegram handlers.

## Development Roadmap

### Phase 1 — Foundation
- Initialize TypeScript project.
- Configure grammY.
- Configure PostgreSQL and Prisma.
- Implement basic commands.
- Set up Docker.

### Phase 2 — Market Data
- Integrate a market data provider.
- Collect prices and market statistics.
- Store historical snapshots.
- Build market overview commands.

### Phase 3 — Information Collection
- Integrate RSS feeds and selected APIs.
- Store articles and announcements.
- Normalize and deduplicate information.
- Add source tracking.

### Phase 4 — AI Research
- Integrate an LLM API.
- Retrieve relevant market data and articles.
- Generate structured research.
- Include citations, uncertainty and risk analysis.

### Phase 5 — Automation
- Add watchlists.
- Implement scheduled market updates.
- Add price and news alerts.
- Improve reliability and monitoring.

## Project Vision

Build a reliable research environment that helps traders understand not only what is happening in cryptocurrency markets, but also why it may be happening, what evidence supports different interpretations, and what developments deserve attention next.

The goal is not to predict every market movement. The goal is to make collecting, understanding and evaluating market information significantly more efficient.