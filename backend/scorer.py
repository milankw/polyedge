"""
PolyEdge Dynamic Scoring System v1
===================================

Computes two aggregate scores from existing filter metrics:

  Score_trade  (0-100) — per-trade quality ranking for sizing decisions
  Score_wallet (0-100) — wallet-centric ranking for tier classification

Sub-scores (all 0-based, capped as noted):
  S_liq   — Market liquidity depth          (0-40)
  S_time  — Entry timing freshness          (0-30)
  S_conc  — Concentration / whale risk      (0-20)
  S_vol   — 24h trading volume              (0-30)
  S_uniq  — Unique trader diversity         (0-20)
  S_skill — Wallet skill + track record     (0-70)  = S_win(0-50) + S_trkN(0-20)
  S_risk  — Bankroll exposure safety        (0-20)

Score_wallet = weighted(S_skill*3, S_liq*1, S_vol*1, S_uniq*1) / 6
Score_trade  = weighted(S_skill*3, S_time*2, S_liq*2, S_vol*2, S_uniq*1, S_conc*2, S_risk*2) / 14

Wallet tiers:
  >= 90  Prime
  >= 75  Core
  >= 60  Opportunistic
  >= 45  Watchlist
  <  45  (internal only)

Trade tiers:
  >= 80  Full size
  65-80  0.5-0.75x size
  50-65  Consensus only (future)
  <  50  Ignore
"""


def score_liquidity(liquidity_usd: float) -> float:
    """S_liq: 0-40 based on market liquidity."""
    if liquidity_usd < 75_000:
        return 0.0
    return 40.0 * min(liquidity_usd / 150_000, 1.0)


def score_entry_timing(minutes_since_open: float) -> float:
    """S_time: 0-30 based on freshness of entry."""
    if minutes_since_open > 120:
        return 0.0
    return 30.0 * (1.0 - minutes_since_open / 120.0)


def score_concentration(wallet_share_pct: float) -> float:
    """S_conc: 0-20 based on whale risk (lower share = higher score)."""
    if wallet_share_pct > 10:
        return 0.0
    return 20.0 * (1.0 - wallet_share_pct / 10.0)


def score_volume_24h(volume_24h_usd: float) -> float:
    """S_vol: 0-30 based on 24h trading volume."""
    if volume_24h_usd < 25_000:
        return 0.0
    return 30.0 * min(volume_24h_usd / 75_000, 1.0)


def score_unique_traders(unique_traders: int) -> float:
    """S_uniq: 0-20 based on trader diversity."""
    if unique_traders < 50:
        return 0.0
    return 20.0 * min(unique_traders / 150.0, 1.0)


def score_track_record(resolved_markets: int) -> float:
    """S_trkN: 0-20 based on number of resolved markets."""
    if resolved_markets < 20:
        return 0.0
    return 20.0 * min(resolved_markets / 100.0, 1.0)


def score_win_rate(win_rate_pct: float) -> float:
    """S_win: 0-50 based on win rate quality. Input is 0-100 percentage."""
    win_rate = win_rate_pct / 100.0 if win_rate_pct > 1 else win_rate_pct
    if win_rate < 0.55:
        return 0.0
    return 50.0 * min((win_rate - 0.55) / 0.20, 1.0)


def score_skill(win_rate_pct: float, resolved_markets: int) -> float:
    """S_skill: 0-70 combined wallet skill score."""
    return score_track_record(resolved_markets) + score_win_rate(win_rate_pct)


def score_bankroll_exposure(exposure_pct: float) -> float:
    """S_risk: 0-20 based on bankroll exposure safety."""
    if exposure_pct > 5:
        return 0.0
    return 20.0 * (1.0 - exposure_pct / 5.0)


def _clamp(val: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(val, hi))


def compute_score_wallet(
    s_skill: float,
    s_liq: float,
    s_vol: float,
    s_uniq: float,
) -> float:
    """Wallet-centric score: weighted average, clamped 0-100."""
    raw = (3 * s_skill + 1 * s_liq + 1 * s_vol + 1 * s_uniq) / 6.0
    return round(_clamp(raw), 1)


def compute_score_trade(
    s_skill: float,
    s_time: float,
    s_liq: float,
    s_vol: float,
    s_uniq: float,
    s_conc: float,
    s_risk: float,
) -> float:
    """Trade-centric score: weighted average, clamped 0-100."""
    raw = (
        3 * s_skill
        + 2 * s_time
        + 2 * s_liq
        + 2 * s_vol
        + 1 * s_uniq
        + 2 * s_conc
        + 2 * s_risk
    ) / 14.0
    return round(_clamp(raw), 1)


def wallet_tier(score: float) -> str:
    """Classify wallet into a tier label."""
    if score >= 90:
        return "Prime"
    if score >= 75:
        return "Core"
    if score >= 60:
        return "Opportunistic"
    if score >= 45:
        return "Watchlist"
    return ""


def trade_tier(score: float) -> str:
    """Classify trade into an action tier."""
    if score >= 80:
        return "Full"
    if score >= 65:
        return "Reduced"
    if score >= 50:
        return "Consensus"
    return "Skip"


def score_trade_full(
    liquidity_usd: float,
    minutes_since_open: float,
    wallet_share_pct: float,
    volume_24h_usd: float,
    unique_traders: int,
    win_rate_pct: float,
    resolved_markets: int,
    exposure_pct: float,
) -> dict:
    """
    Compute all sub-scores plus both aggregate scores for a candidate trade.
    Returns a dict with all individual scores, aggregates, and tier labels.
    """
    s_liq = score_liquidity(liquidity_usd)
    s_time = score_entry_timing(minutes_since_open)
    s_conc = score_concentration(wallet_share_pct)
    s_vol = score_volume_24h(volume_24h_usd)
    s_uniq = score_unique_traders(unique_traders)
    s_skill = score_skill(win_rate_pct, resolved_markets)
    s_risk = score_bankroll_exposure(exposure_pct)

    sc_wallet = compute_score_wallet(s_skill, s_liq, s_vol, s_uniq)
    sc_trade = compute_score_trade(s_skill, s_time, s_liq, s_vol, s_uniq, s_conc, s_risk)

    return {
        "s_liq": round(s_liq, 1),
        "s_time": round(s_time, 1),
        "s_conc": round(s_conc, 1),
        "s_vol": round(s_vol, 1),
        "s_uniq": round(s_uniq, 1),
        "s_skill": round(s_skill, 1),
        "s_risk": round(s_risk, 1),
        "score_wallet": sc_wallet,
        "score_trade": sc_trade,
        "wallet_tier": wallet_tier(sc_wallet),
        "trade_tier": trade_tier(sc_trade),
    }
