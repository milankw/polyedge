"""
PolyEdge Market Scanner
Continuously scans Polymarket markets and enriches with external data + model analysis.
"""

import asyncio
import json
import re

import httpx

from api_server import get_db, GAMMA_API, fetch_markets, analyze_market_edge
from models import ModelAggregator, ProbabilityEstimate
from data_feeds import (
    get_full_crypto_context,
    get_full_sports_context,
    get_news_signals,
    fetch_todays_matches,
    CRYPTO_IDS,
)

aggregator = ModelAggregator()

# ── Market Categorization ───────────────────────────────────────

# Keyword patterns for categorization
CATEGORY_PATTERNS = {
    "sports_football": [
        r"\b(premier league|champions league|la liga|serie a|bundesliga|ligue 1)\b",
        r"\b(football|soccer|match|goal|epl|ucl)\b",
        r"\b(manchester|liverpool|arsenal|chelsea|barcelona|real madrid|bayern|psg|juventus|inter)\b",
    ],
    "sports_basketball": [
        r"\b(nba|basketball|lakers|celtics|warriors|nets|bucks)\b",
    ],
    "sports_other": [
        r"\b(nfl|mlb|tennis|formula 1|f1|ufc|mma|boxing|cricket|rugby)\b",
    ],
    "crypto_btc": [
        r"\b(bitcoin|btc)\b",
    ],
    "crypto_eth": [
        r"\b(ethereum|eth)\b",
    ],
    "crypto_sol": [
        r"\b(solana|sol)\b",
    ],
    "crypto_other": [
        r"\b(crypto|token|defi|nft|blockchain|altcoin|dogecoin|cardano|xrp)\b",
    ],
    "politics_us": [
        r"\b(trump|biden|harris|congress|senate|house|republican|democrat|gop|dnc|presidential|us election)\b",
    ],
    "politics_world": [
        r"\b(ukraine|russia|china|nato|eu|un|sanctions|ceasefire|war)\b",
    ],
    "entertainment": [
        r"\b(oscars|grammy|emmy|movie|tv show|super bowl halftime|celebrity)\b",
    ],
    "tech": [
        r"\b(apple|google|meta|microsoft|ai|artificial intelligence|openai|tesla)\b",
    ],
    "weather": [
        r"\b(hurricane|tornado|earthquake|temperature|climate|weather|storm)\b",
    ],
}


def categorize_market(market: dict) -> str:
    """
    Classify a market into a category based on question text and tags.
    """
    question = (market.get("question", "") or "").lower()
    tags = " ".join(t.lower() for t in (market.get("tags", []) or []) if isinstance(t, str))
    group_title = (market.get("groupItemTitle", "") or "").lower()
    text = f"{question} {tags} {group_title}"

    for category, patterns in CATEGORY_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, text, re.IGNORECASE):
                return category

    return "other"


# ── Scanner Engine ──────────────────────────────────────────────

class MarketScanner:
    """
    Scans Polymarket markets and runs them through probability models
    with external data enrichment.
    """

    def __init__(self):
        self.aggregator = ModelAggregator()

    async def full_scan(
        self,
        limit: int = 100,
        min_edge: float = 0.03,
        min_volume: float = 500,
    ) -> dict:
        """
        Full scan pipeline:
        1. Fetch active markets
        2. Categorize each market
        3. Fetch relevant external data
        4. Run through probability models
        5. Filter by edge threshold
        6. Rank by edge * confidence
        """
        markets = await fetch_markets(limit=limit)
        if not markets:
            return {"opportunities": [], "scanned": 0, "errors": []}

        # Pre-fetch shared external data
        news_signals = await get_news_signals()

        # Pre-fetch crypto data for common coins
        crypto_data = {}
        try:
            btc, eth, sol = await asyncio.gather(
                get_full_crypto_context("bitcoin"),
                get_full_crypto_context("ethereum"),
                get_full_crypto_context("solana"),
                return_exceptions=True,
            )
            if not isinstance(btc, Exception):
                crypto_data["bitcoin"] = btc
            if not isinstance(eth, Exception):
                crypto_data["ethereum"] = eth
            if not isinstance(sol, Exception):
                crypto_data["solana"] = sol
        except Exception:
            pass

        opportunities = []
        errors = []

        for m in markets:
            try:
                opp = await self._analyze_single_market(
                    m, news_signals, crypto_data, min_edge, min_volume
                )
                if opp:
                    opportunities.append(opp)
            except Exception as e:
                errors.append({"market_id": m.get("id", ""), "error": str(e)})

        # Sort by edge * confidence
        opportunities.sort(
            key=lambda x: abs(x["edge"]) * x["confidence"], reverse=True
        )

        # Store opportunities in DB
        self._cache_opportunities(opportunities)

        return {
            "opportunities": opportunities,
            "scanned": len(markets),
            "matched": len(opportunities),
            "errors": errors,
        }

    async def sports_scan(self, limit: int = 100) -> dict:
        """Sports-specific scan with team data enrichment."""
        markets = await fetch_markets(limit=limit)
        matches = await fetch_todays_matches()

        sports_markets = [
            m for m in markets
            if categorize_market(m).startswith("sports")
        ]

        opportunities = []
        for m in sports_markets:
            analysis = analyze_market_edge(m)
            category = categorize_market(m)

            # Try to match market to a live match for richer data
            match_context = self._match_to_football(m, matches)

            opp = {
                "market_id": m.get("id", ""),
                "question": m.get("question", ""),
                "category": category,
                "yes_price": analysis.get("yes_price", 0),
                "no_price": analysis.get("no_price", 0),
                "volume": float(m.get("volume", 0) or 0),
                "edge": analysis["edge"],
                "confidence": analysis["confidence"],
                "signals": analysis["signals"],
                "match_context": match_context,
            }
            if analysis["edge"] > 0:
                opportunities.append(opp)

        opportunities.sort(key=lambda x: x["edge"], reverse=True)
        return {
            "opportunities": opportunities,
            "scanned": len(sports_markets),
            "total_markets": len(markets),
            "todays_matches": len(matches),
        }

    async def crypto_scan(self) -> dict:
        """Crypto latency arbitrage scan."""
        markets = await fetch_markets(limit=100)

        # Fetch all crypto data
        crypto_data = {}
        for coin_id in CRYPTO_IDS:
            try:
                crypto_data[coin_id] = await get_full_crypto_context(coin_id)
            except Exception:
                pass

        crypto_markets = [
            m for m in markets
            if categorize_market(m).startswith("crypto")
        ]

        opportunities = []
        for m in crypto_markets:
            category = categorize_market(m)
            coin_key = {
                "crypto_btc": "bitcoin",
                "crypto_eth": "ethereum",
                "crypto_sol": "solana",
            }.get(category)

            if coin_key and coin_key in crypto_data:
                analysis = analyze_market_edge(m)
                yes_price = analysis.get("yes_price", 0.5)
                estimate = self.aggregator.crypto_model.detect_arbitrage(
                    yes_price, crypto_data[coin_key]
                )

                if abs(estimate.edge) > 0.02:
                    opportunities.append({
                        "market_id": m.get("id", ""),
                        "question": m.get("question", ""),
                        "category": category,
                        "coin": coin_key,
                        "yes_price": yes_price,
                        "our_probability": estimate.our_probability,
                        "edge": estimate.edge,
                        "confidence": estimate.confidence,
                        "reasoning": estimate.reasoning,
                        "crypto_data": {
                            "binance_price": crypto_data[coin_key].get("binance_price", 0),
                            "change_5m": crypto_data[coin_key].get("momentum_5m", {}).get("change_pct", 0),
                            "change_1h": crypto_data[coin_key].get("momentum_1h", {}).get("change_pct", 0),
                            "change_24h": crypto_data[coin_key].get("change_24h_pct", 0),
                        },
                    })

        opportunities.sort(key=lambda x: abs(x["edge"]), reverse=True)
        return {
            "opportunities": opportunities,
            "scanned": len(crypto_markets),
            "crypto_prices": {
                coin: {"price": data.get("binance_price", 0)}
                for coin, data in crypto_data.items()
            },
        }

    async def arbitrage_scan(self, limit: int = 200) -> dict:
        """Cross-market arbitrage scan."""
        markets = await fetch_markets(limit=limit)

        parsed = []
        for m in markets:
            analysis = analyze_market_edge(m)
            parsed.append({
                "id": m.get("id", ""),
                "question": m.get("question", ""),
                "yes_price": analysis.get("yes_price", 0),
                "no_price": analysis.get("no_price", 0),
                "volume": float(m.get("volume", 0) or 0),
                "category": categorize_market(m),
            })

        arb_opps = self.aggregator.find_arbitrage(parsed)
        return {
            "opportunities": arb_opps,
            "scanned": len(parsed),
        }

    # ── Helpers ──────────────────────────────────────────────────

    async def _analyze_single_market(
        self,
        market: dict,
        news_signals: dict,
        crypto_data: dict,
        min_edge: float,
        min_volume: float,
    ) -> dict | None:
        """Analyze a single market with the model aggregator."""
        volume = float(market.get("volume", 0) or 0)
        if volume < min_volume:
            return None

        category = categorize_market(market)
        analysis = analyze_market_edge(market)
        yes_price = analysis.get("yes_price", 0)
        no_price = analysis.get("no_price", 0)

        if yes_price <= 0:
            return None

        # Build external data context
        external_data = {"news_signals": news_signals}

        if category.startswith("crypto"):
            coin_key = {
                "crypto_btc": "bitcoin",
                "crypto_eth": "ethereum",
                "crypto_sol": "solana",
            }.get(category)
            if coin_key and coin_key in crypto_data:
                external_data["crypto_context"] = crypto_data[coin_key]

        # Run through model
        market_data = {
            "id": market.get("id", ""),
            "question": market.get("question", ""),
            "yes_price": yes_price,
            "no_price": no_price,
            "volume": volume,
            "category": category,
        }
        estimate = self.aggregator.get_edge(market_data, category, external_data)

        # Combine model estimate with basic signal analysis
        combined_edge = max(abs(estimate.edge), analysis["edge"])
        if combined_edge < min_edge:
            return None

        # Determine recommended side
        if estimate.edge > 0:
            side = "YES"
        elif estimate.edge < 0:
            side = "NO"
        else:
            side = "YES" if yes_price < 0.5 else "NO"

        return {
            "market_id": market.get("id", ""),
            "question": market.get("question", ""),
            "category": category,
            "yes_price": yes_price,
            "no_price": no_price,
            "volume": volume,
            "volume_24h": float(market.get("volume24hr", 0) or 0),
            "liquidity": float(market.get("liquidityClob", 0) or 0),
            "our_probability": estimate.our_probability,
            "market_probability": estimate.market_probability,
            "edge": round(estimate.edge, 4),
            "confidence": estimate.confidence,
            "model": estimate.model_name,
            "reasoning": estimate.reasoning,
            "basic_signals": analysis["signals"],
            "recommended_side": side,
            "recommended_size": min(5.0, max(1.0, combined_edge * 50)),
            "token_ids": market.get("clobTokenIds", []),
        }

    def _match_to_football(self, market: dict, matches: list[dict]) -> dict | None:
        """Try to match a Polymarket sports market to a football-data.org match."""
        question = (market.get("question", "") or "").lower()
        for match in matches:
            home = (match.get("homeTeam", {}).get("name", "") or "").lower()
            away = (match.get("awayTeam", {}).get("name", "") or "").lower()
            if home and away and (home in question or away in question):
                return {
                    "match_id": match.get("id"),
                    "home_team": match.get("homeTeam", {}),
                    "away_team": match.get("awayTeam", {}),
                    "competition": match.get("competition", {}).get("name", ""),
                    "utc_date": match.get("utcDate", ""),
                    "status": match.get("status", ""),
                }
        return None

    def _cache_opportunities(self, opportunities: list[dict]):
        """Store discovered opportunities in the database."""
        if not opportunities:
            return
        db = get_db()
        for opp in opportunities[:50]:  # cap at 50
            try:
                db.execute(
                    """INSERT INTO opportunities
                       (market_id, signal_type, our_probability, market_probability,
                        edge, confidence, reasoning, category)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        opp.get("market_id", ""),
                        opp.get("model", "basic"),
                        opp.get("our_probability", 0),
                        opp.get("market_probability", 0),
                        opp.get("edge", 0),
                        str(opp.get("confidence", 0)),
                        opp.get("reasoning", ""),
                        opp.get("category", ""),
                    ),
                )
            except Exception:
                pass
        db.commit()
        db.close()
