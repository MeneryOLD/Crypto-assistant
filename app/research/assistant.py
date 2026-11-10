"""AI research assistant backed by Claude.

The model only sees evidence we collected (market snapshots + stored news),
each item tagged with an ID, and must cite those IDs. We then append the
sources we actually hold for every cited ID, so answers stay traceable.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

import anthropic

from app.formatting import fmt_price
from app.market.coingecko import CoinMarket, GlobalMarket
from app.models import NewsItem

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are the research assistant of "Crypto Intelligence", a Telegram bot for experienced crypto traders and investors.

You receive an EVIDENCE block with current market data (IDs like [M1]) and recent news items (IDs like [N1]), \
followed by the user's question. Answer using that evidence.

Rules:
- Base factual claims on the evidence and cite IDs inline, e.g. "ETF outflows continued [N3]". Cite only IDs that exist.
- Clearly separate FACTS (what the evidence says) from INTERPRETATION (your analysis). Label interpretation as such.
- Point out uncertainty, missing information and conflicting evidence. If the evidence does not answer the question, say so plainly instead of guessing.
- Never present speculation as fact, never promise outcomes, and do not give personalized financial advice.
- For thesis or "bullish/bearish" questions, structure the answer as: Market context, Key developments, Bullish arguments, \
Bearish arguments, Scenarios, Risks and invalidation conditions, What to watch next.
- For other questions, use short sections that fit the question.

Formatting: this is shown in Telegram as plain text. Do not use Markdown (no **, #, tables or links). \
Use short section titles in CAPS, "- " bullets and blank lines between sections. Keep it under 3000 characters. \
Do not add a source list at the end; it is appended automatically.
"""

DISCLAIMER = "AI-generated research based on the sources above. Not financial advice."

_CITATION_RE = re.compile(r"\[([MN]\d+)\]")


class ResearchUnavailable(RuntimeError):
    pass


@dataclass(slots=True)
class Evidence:
    markets: list[CoinMarket]
    news: list[NewsItem]
    global_market: GlobalMarket | None = None
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def render(self) -> str:
        lines = [f"Current time (UTC): {self.now:%Y-%m-%d %H:%M}", ""]
        if self.global_market:
            g = self.global_market
            lines += [
                "GLOBAL MARKET [M0]",
                f"Total market cap ${g.total_market_cap_usd:,.0f} ({g.market_cap_change_24h:+.2f}% 24h), "
                f"24h volume ${g.total_volume_usd:,.0f}, BTC dominance {g.btc_dominance:.1f}%, "
                f"ETH dominance {g.eth_dominance:.1f}%. Source: CoinGecko.",
                "",
            ]
        lines.append("MARKET DATA (source: CoinGecko)")
        for i, m in enumerate(self.markets, 1):
            lines.append(
                f"[M{i}] {m.name} ({m.symbol}) rank {m.rank or '?'}: {fmt_price(m.price_usd)}, "
                f"1h {_pct(m.change_1h)}, 24h {_pct(m.change_24h)}, 7d {_pct(m.change_7d)}, "
                f"mcap ${m.market_cap or 0:,.0f}, 24h vol ${m.volume_24h or 0:,.0f}"
            )
        lines += ["", "NEWS"]
        if not self.news:
            lines.append("(no relevant news collected in the lookback window)")
        for i, n in enumerate(self.news, 1):
            when = f"{n.published_at:%Y-%m-%d %H:%M} UTC" if n.published_at else "time unknown"
            lines.append(f"[N{i}] {n.source}, {when}: {n.title}")
            if n.summary:
                lines.append(f"     {n.summary}")
        return "\n".join(lines)

    def source_for(self, ref: str) -> str | None:
        kind, idx = ref[0], int(ref[1:])
        if kind == "M":
            if idx == 0 and self.global_market:
                return "CoinGecko global market data"
            if 1 <= idx <= len(self.markets):
                return f"CoinGecko market data: {self.markets[idx - 1].name}"
        elif kind == "N" and 1 <= idx <= len(self.news):
            n = self.news[idx - 1]
            return f"{n.source}: {n.title} {n.url}"
        return None


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+.2f}%"


def cited_sources(text: str, evidence: Evidence) -> list[tuple[str, str]]:
    """Ordered, de-duplicated (ref, source) pairs for every valid citation in text."""
    seen: dict[str, str] = {}
    for ref in _CITATION_RE.findall(text):
        if ref not in seen and (src := evidence.source_for(ref)):
            seen[ref] = src
    return list(seen.items())


def format_report(answer: str, evidence: Evidence) -> str:
    sources = cited_sources(answer, evidence)
    parts = [answer.strip()]
    if sources:
        parts.append("SOURCES\n" + "\n".join(f"[{ref}] {src}" for ref, src in sources))
    else:
        parts.append("SOURCES\nNo collected sources were cited; treat this answer with extra caution.")
    parts.append(DISCLAIMER)
    return "\n\n".join(parts)


class ResearchAssistant:
    def __init__(self, api_key: str | None, *, model: str = "claude-opus-5-5", effort: str = "medium"):
        self._client = anthropic.AsyncAnthropic(api_key=api_key) if api_key else None
        self._model = model
        self._effort = effort

    @property
    def enabled(self) -> bool:
        return self._client is not None

    async def answer(self, question: str, evidence: Evidence) -> str:
        if self._client is None:
            raise ResearchUnavailable("AI research is not configured (set ANTHROPIC_API_KEY).")

        user_content = f"<evidence>\n{evidence.render()}\n</evidence>\n\n<question>\n{question}\n</question>"
        try:
            response = await self._client.beta.messages.create(
                model=self._model,
                max_tokens=16000,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_content}],
                output_config={"effort": self._effort},
                # Server-side fallback: if a safety classifier declines, the API
                # retries on a suitable fallback model inside the same call.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except anthropic.AuthenticationError as exc:
            raise ResearchUnavailable("The AI provider rejected the API key.") from exc
        except anthropic.RateLimitError as exc:
            raise ResearchUnavailable("The AI provider is rate limiting requests. Try again in a minute.") from exc
        except anthropic.APIStatusError as exc:
            log.error("Claude API error %s: %s", exc.status_code, exc.message)
            raise ResearchUnavailable("The AI provider returned an error. Try again later.") from exc
        except anthropic.APIConnectionError as exc:
            raise ResearchUnavailable("Could not reach the AI provider.") from exc

        if response.stop_reason == "refusal":
            raise ResearchUnavailable("The AI declined to answer this request. Try rephrasing the question.")

        text = "".join(block.text for block in response.content if block.type == "text").strip()
        if not text:
            raise ResearchUnavailable("The AI returned an empty answer. Try again.")
        if response.stop_reason == "max_tokens":
            text += "\n\n[Answer was cut off.]"
        return format_report(text, evidence)
