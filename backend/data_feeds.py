"""
PolyEdge Data Ingestion Layer
Async data collection from sports, crypto, and news sources.
"""

import asyncio
import json
import sqlite3
import time
from datetime import datetime, timezone, timedelta

import httpx

from api_server import DB_PATH, get_db

# ── Rate Limiter ────────────────────────────────────────────────

class RateLimiter:
    """Simple token-bucket rate limiter."""

    def __init__(self, calls_per_minute: int):
        self.interval = 60.0 / calls_per_minute
        self._last_call = 0.0

    async def wait(self):
        now = time.monotonic()
        elapsed = now - self._last_call
        if elapsed < self.interval:
            await asyncio.sleep(self.interval - elapsed)
        self._last_call = time.monotonic()


# Per-source rate limiters
_football_limiter = RateLimiter(10)   # 10 req/min
_coingecko_limiter = RateLimiter(10)  # 10 req/min (free tier)
_binance_limiter = RateLimiter(60)    # generous limit


# ── Cache Helpers ───────────────────────────────────────────────

def _init_cache_table():
    db = get_db()
    db.execute("""
        CREATE TABLE IF NOT EXISTS api_cache (
            cache_key TEXT PRIMARY KEY,
            data TEXT,
            fetched_at TEXT DEFAULT (datetime('now'))
        )
    """)
    db.commit()
    db.close()

_init_cache_table()


def _get_cached(key: str, max_age_seconds: int) -> dict | None:
    db = get_db()
    row = db.execute(
        "SELECT data, fetched_at FROM api_cache WHERE cache_key = ?", (key,)
    ).fetchone()
    db.close()
    if not row:
        return None
    fetched = datetime.fromisoformat(row["fetched_at"])
    if (datetime.utcnow() - fetched).total_seconds() > max_age_seconds:
        return None
    return json.loads(row["data"])


def _set_cached(key: str, data):
    db = get_db()
    db.execute(
        "INSERT OR REPLACE INTO api_cache (cache_key, data, fetched_at) VALUES (?, ?, datetime('now'))",
        (key, json.dumps(data)),
    )
    db.commit()
    db.close()


# ── Sports Data (football-data.org) ────────────────────────────

FOOTBALL_API = "https://api.football-data.org/v4"
FOOTBALL_COMPETITIONS = ["PL", "CL", "BL1", "SA", "PD", "FL1"]


async def fetch_todays_matches() -> list[dict]:
    """Fetch today's scheduled matches across major competitions."""
    cached = _get_cached("football_today", 3600)  # 1 hour cache
    if cached is not None:
        return cached

    await _football_limiter.wait()
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                f"{FOOTBALL_API}/matches",
                params={"dateFrom": _today(), "dateTo": _today()},
            )
            if resp.status_code == 200:
                data = resp.json().get("matches", [])
                _set_cached("football_today", data)
                return data
    except (httpx.HTTPError, httpx.TimeoutException):
        pass
    return []


async def fetch_team_matches(team_id: int, limit: int = 20) -> list[dict]:
    """Fetch recent matches for a team (for form analysis)."""
    cache_key = f"team_matches_{team_id}"
    cached = _get_cached(cache_key, 3600)
    if cached is not None:
        return cached

    await _football_limiter.wait()
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                f"{FOOTBALL_API}/teams/{team_id}/matches",
                params={"status": "FINISHED", "limit": limit},
            )
            if resp.status_code == 200:
                data = resp.json().get("matches", [])
                _set_cached(cache_key, data)
                return data
    except (httpx.HTTPError, httpx.TimeoutException):
        pass
    return []


async def fetch_standings(competition: str) -> list[dict]:
    """Fetch league standings for a competition code."""
    cache_key = f"standings_{competition}"
    cached = _get_cached(cache_key, 3600)
    if cached is not None:
        return cached

    await _football_limiter.wait()
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                f"{FOOTBALL_API}/competitions/{competition}/standings"
            )
            if resp.status_code == 200:
                data = resp.json().get("standings", [])
                _set_cached(cache_key, data)
                return data
    except (httpx.HTTPError, httpx.TimeoutException):
        pass
    return []


def compute_team_form(matches: list[dict], team_id: int, last_n: int = 5) -> dict:
    """
    Compute form stats from recent matches.
    Returns wins, draws, losses, goals_for, goals_against for last N games.
    """
    wins, draws, losses, gf, ga = 0, 0, 0, 0, 0
    home_wins, home_played, away_wins, away_played = 0, 0, 0, 0
    counted = 0

    for m in matches:
        if counted >= last_n:
            break
        score = m.get("score", {})
        ft = score.get("fullTime", {})
        home_goals = ft.get("home")
        away_goals = ft.get("away")
        if home_goals is None or away_goals is None:
            continue

        home_team_id = m.get("homeTeam", {}).get("id")
        is_home = home_team_id == team_id

        if is_home:
            home_played += 1
            gf += home_goals
            ga += away_goals
            if home_goals > away_goals:
                wins += 1
                home_wins += 1
            elif home_goals == away_goals:
                draws += 1
            else:
                losses += 1
        else:
            away_played += 1
            gf += away_goals
            ga += home_goals
            if away_goals > home_goals:
                wins += 1
                away_wins += 1
            elif home_goals == away_goals:
                draws += 1
            else:
                losses += 1
        counted += 1

    total = max(counted, 1)
    return {
        "matches_analyzed": counted,
        "wins": wins,
        "draws": draws,
        "losses": losses,
        "win_rate": wins / total,
        "goals_for_avg": gf / total,
        "goals_against_avg": ga / total,
        "home_win_rate": home_wins / max(home_played, 1),
        "away_win_rate": away_wins / max(away_played, 1),
        "points_per_game": (wins * 3 + draws) / total,
    }


def compute_head_to_head(matches: list[dict], team_a_id: int, team_b_id: int) -> dict:
    """Compute head-to-head record between two teams from match lists."""
    a_wins, b_wins, draws = 0, 0, 0
    for m in matches:
        home_id = m.get("homeTeam", {}).get("id")
        away_id = m.get("awayTeam", {}).get("id")
        if not ({home_id, away_id} == {team_a_id, team_b_id}):
            continue
        ft = m.get("score", {}).get("fullTime", {})
        hg, ag = ft.get("home"), ft.get("away")
        if hg is None or ag is None:
            continue
        if hg > ag:
            if home_id == team_a_id:
                a_wins += 1
            else:
                b_wins += 1
        elif ag > hg:
            if away_id == team_a_id:
                a_wins += 1
            else:
                b_wins += 1
        else:
            draws += 1

    total = a_wins + b_wins + draws
    return {
        "total_meetings": total,
        "team_a_wins": a_wins,
        "team_b_wins": b_wins,
        "draws": draws,
        "team_a_win_rate": a_wins / max(total, 1),
    }


async def get_full_sports_context(team_a_id: int, team_b_id: int, competition: str = "PL") -> dict:
    """Gather all sports data needed for the model."""
    team_a_matches, team_b_matches, standings = await asyncio.gather(
        fetch_team_matches(team_a_id, 20),
        fetch_team_matches(team_b_id, 20),
        fetch_standings(competition),
    )

    form_a_5 = compute_team_form(team_a_matches, team_a_id, 5)
    form_a_20 = compute_team_form(team_a_matches, team_a_id, 20)
    form_b_5 = compute_team_form(team_b_matches, team_b_id, 5)
    form_b_20 = compute_team_form(team_b_matches, team_b_id, 20)
    h2h = compute_head_to_head(team_a_matches + team_b_matches, team_a_id, team_b_id)

    # Extract league positions from standings
    pos_a, pos_b = 0, 0
    if standings:
        table = standings[0].get("table", []) if standings else []
        for entry in table:
            tid = entry.get("team", {}).get("id")
            if tid == team_a_id:
                pos_a = entry.get("position", 0)
            elif tid == team_b_id:
                pos_b = entry.get("position", 0)

    return {
        "team_a": {
            "id": team_a_id,
            "form_5": form_a_5,
            "form_20": form_a_20,
            "league_position": pos_a,
        },
        "team_b": {
            "id": team_b_id,
            "form_5": form_b_5,
            "form_20": form_b_20,
            "league_position": pos_b,
        },
        "head_to_head": h2h,
    }


# ── Crypto Price Feeds ──────────────────────────────────────────

COINGECKO_API = "https://api.coingecko.com/api/v3"
BINANCE_API = "https://api.binance.com/api/v3"

CRYPTO_IDS = {
    "bitcoin": "BTCUSDT",
    "ethereum": "ETHUSDT",
    "solana": "SOLUSDT",
}


async def fetch_coingecko_prices() -> dict:
    """Fetch current prices + 24h change from CoinGecko."""
    cached = _get_cached("coingecko_prices", 60)  # 1 min cache
    if cached is not None:
        return cached

    await _coingecko_limiter.wait()
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{COINGECKO_API}/simple/price",
                params={
                    "ids": "bitcoin,ethereum,solana",
                    "vs_currencies": "usd",
                    "include_24hr_change": "true",
                    "include_last_updated_at": "true",
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                _set_cached("coingecko_prices", data)
                return data
    except (httpx.HTTPError, httpx.TimeoutException):
        pass
    return {}


async def fetch_coingecko_chart(coin_id: str, days: int = 1) -> list:
    """Fetch price chart for momentum detection."""
    cache_key = f"coingecko_chart_{coin_id}_{days}"
    cached = _get_cached(cache_key, 60)
    if cached is not None:
        return cached

    await _coingecko_limiter.wait()
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{COINGECKO_API}/coins/{coin_id}/market_chart",
                params={"vs_currency": "usd", "days": days},
            )
            if resp.status_code == 200:
                data = resp.json().get("prices", [])
                _set_cached(cache_key, data)
                return data
    except (httpx.HTTPError, httpx.TimeoutException):
        pass
    return []


async def fetch_binance_price(symbol: str = "BTCUSDT") -> dict:
    """Fetch real-time price from Binance."""
    await _binance_limiter.wait()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{BINANCE_API}/ticker/price", params={"symbol": symbol}
            )
            if resp.status_code == 200:
                return resp.json()
    except (httpx.HTTPError, httpx.TimeoutException):
        pass
    return {}


async def fetch_binance_klines(symbol: str = "BTCUSDT", interval: str = "1m", limit: int = 60) -> list:
    """Fetch recent klines for short-term momentum."""
    await _binance_limiter.wait()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{BINANCE_API}/klines",
                params={"symbol": symbol, "interval": interval, "limit": limit},
            )
            if resp.status_code == 200:
                return resp.json()
    except (httpx.HTTPError, httpx.TimeoutException):
        pass
    return []


def compute_crypto_momentum(klines: list, window_minutes: int = 5) -> dict:
    """
    Compute short-term momentum from Binance klines.
    Each kline: [open_time, open, high, low, close, volume, ...]
    """
    if not klines or len(klines) < 2:
        return {"change_pct": 0.0, "direction": "flat", "volume_trend": 0.0}

    recent = klines[-window_minutes:] if len(klines) >= window_minutes else klines
    start_price = float(recent[0][1])  # open of first candle
    end_price = float(recent[-1][4])    # close of last candle

    if start_price == 0:
        return {"change_pct": 0.0, "direction": "flat", "volume_trend": 0.0}

    change_pct = ((end_price - start_price) / start_price) * 100

    # Volume trend: compare recent volume to earlier volume
    total_vol = sum(float(k[5]) for k in klines)
    recent_vol = sum(float(k[5]) for k in recent)
    earlier_vol = total_vol - recent_vol
    avg_earlier = earlier_vol / max(len(klines) - len(recent), 1)
    avg_recent = recent_vol / max(len(recent), 1)
    volume_trend = (avg_recent / max(avg_earlier, 0.01)) - 1.0

    direction = "up" if change_pct > 0.1 else ("down" if change_pct < -0.1 else "flat")

    return {
        "change_pct": round(change_pct, 4),
        "direction": direction,
        "volume_trend": round(volume_trend, 2),
        "start_price": start_price,
        "end_price": end_price,
    }


async def get_full_crypto_context(coin_id: str = "bitcoin") -> dict:
    """Gather all crypto data for a coin."""
    binance_symbol = CRYPTO_IDS.get(coin_id, "BTCUSDT")

    cg_prices, cg_chart, bn_price, bn_klines = await asyncio.gather(
        fetch_coingecko_prices(),
        fetch_coingecko_chart(coin_id, 1),
        fetch_binance_price(binance_symbol),
        fetch_binance_klines(binance_symbol, "1m", 60),
    )

    coin_data = cg_prices.get(coin_id, {})
    momentum = compute_crypto_momentum(bn_klines, 5)

    # 1-hour momentum from chart data
    hour_momentum = {"change_pct": 0.0}
    if len(cg_chart) >= 2:
        hour_ago = [p for p in cg_chart if p[0] >= (time.time() - 3600) * 1000]
        if len(hour_ago) >= 2:
            start = hour_ago[0][1]
            end = hour_ago[-1][1]
            if start > 0:
                hour_momentum["change_pct"] = round(((end - start) / start) * 100, 4)

    return {
        "coin_id": coin_id,
        "binance_symbol": binance_symbol,
        "coingecko_price": coin_data.get("usd", 0),
        "change_24h_pct": coin_data.get("usd_24h_change", 0),
        "binance_price": float(bn_price.get("price", 0)) if bn_price else 0,
        "momentum_5m": momentum,
        "momentum_1h": hour_momentum,
    }


# ── News Signals ────────────────────────────────────────────────

# Using RSS feeds from public sources (no API key needed)
RSS_FEEDS = {
    "reuters_world": "https://feeds.reuters.com/reuters/worldNews",
    "bbc_world": "http://feeds.bbci.co.uk/news/world/rss.xml",
}

# Keywords that signal movement in prediction markets
SIGNAL_KEYWORDS = {
    "geopolitical": ["war", "ceasefire", "invasion", "sanctions", "troops", "missile", "nuclear"],
    "election": ["election", "poll", "vote", "ballot", "candidate", "primary", "debate"],
    "crypto_regulation": ["crypto regulation", "sec", "bitcoin etf", "crypto ban", "stablecoin"],
    "economic": ["fed rate", "inflation", "recession", "gdp", "unemployment", "interest rate"],
}


async def fetch_rss_headlines(feed_url: str, max_items: int = 20) -> list[dict]:
    """Fetch and parse RSS feed headlines (simple XML parsing without lxml)."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(feed_url)
            if resp.status_code != 200:
                return []

        # Simple XML parsing for RSS items
        text = resp.text
        items = []
        pos = 0
        while len(items) < max_items:
            item_start = text.find("<item>", pos)
            if item_start == -1:
                item_start = text.find("<item ", pos)
            if item_start == -1:
                break
            item_end = text.find("</item>", item_start)
            if item_end == -1:
                break
            item_xml = text[item_start:item_end]

            title = _extract_xml_tag(item_xml, "title")
            description = _extract_xml_tag(item_xml, "description")
            pub_date = _extract_xml_tag(item_xml, "pubDate")
            link = _extract_xml_tag(item_xml, "link")

            items.append({
                "title": title,
                "description": description,
                "published": pub_date,
                "link": link,
            })
            pos = item_end + 7
        return items
    except (httpx.HTTPError, httpx.TimeoutException):
        return []


def _extract_xml_tag(xml: str, tag: str) -> str:
    """Extract text content from an XML tag."""
    start = xml.find(f"<{tag}>")
    if start == -1:
        start = xml.find(f"<{tag} ")
    if start == -1:
        return ""
    # Find the end of the opening tag
    tag_end = xml.find(">", start)
    if tag_end == -1:
        return ""
    content_start = tag_end + 1
    # Handle CDATA
    close = xml.find(f"</{tag}>", content_start)
    if close == -1:
        return ""
    content = xml[content_start:close].strip()
    # Strip CDATA wrapper if present
    if content.startswith("<![CDATA["):
        content = content[9:]
    if content.endswith("]]>"):
        content = content[:-3]
    return content.strip()


def detect_keyword_signals(headlines: list[dict]) -> dict:
    """
    Scan headlines for signal keywords and compute velocity.
    Returns keyword categories with hit counts and matched headlines.
    """
    signals = {}
    for category, keywords in SIGNAL_KEYWORDS.items():
        hits = []
        for h in headlines:
            text = (h.get("title", "") + " " + h.get("description", "")).lower()
            matched_kws = [kw for kw in keywords if kw in text]
            if matched_kws:
                hits.append({
                    "title": h.get("title", ""),
                    "keywords": matched_kws,
                    "published": h.get("published", ""),
                })
        if hits:
            signals[category] = {
                "hit_count": len(hits),
                "velocity": len(hits) / max(len(headlines), 1),
                "headlines": hits[:5],  # top 5 matches
            }
    return signals


async def get_news_signals() -> dict:
    """Fetch all RSS feeds and analyze for keyword signals."""
    cached = _get_cached("news_signals", 300)  # 5 min cache
    if cached is not None:
        return cached

    all_headlines = []
    tasks = [fetch_rss_headlines(url) for url in RSS_FEEDS.values()]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for r in results:
        if isinstance(r, list):
            all_headlines.extend(r)

    signals = detect_keyword_signals(all_headlines)
    result = {
        "total_headlines": len(all_headlines),
        "signals": signals,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _set_cached("news_signals", result)
    return result


# ── Helpers ─────────────────────────────────────────────────────

def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")
