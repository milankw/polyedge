"""
PolyEdge Probability Models
Independent probability estimates for different market categories.
"""

import math
from dataclasses import dataclass, field


@dataclass
class ProbabilityEstimate:
    our_probability: float
    market_probability: float
    edge: float
    confidence: float  # 0.0 to 1.0
    reasoning: str
    model_name: str
    category: str
    details: dict = field(default_factory=dict)


# ── Sports Model ────────────────────────────────────────────────

class SportsModel:
    """
    Estimates match outcome probabilities using weighted composite of
    team form, home/away advantage, head-to-head, and league position.
    """

    # Weight configuration
    WEIGHTS = {
        "recent_form": 0.30,   # Last 5 games
        "season_form": 0.20,   # Last 20 games
        "home_away": 0.15,     # Home/away advantage
        "head_to_head": 0.10,  # H2H record
        "league_position": 0.15,  # Table position
        "goals_diff": 0.10,    # Goals per game differential
    }

    def estimate_probability(self, sports_context: dict) -> dict:
        """
        Compute win/draw/loss probabilities for team_a.

        sports_context should contain:
        - team_a: {form_5, form_20, league_position}
        - team_b: {form_5, form_20, league_position}
        - head_to_head: {team_a_wins, draws, total_meetings, team_a_win_rate}
        - is_home: bool (is team_a at home?)
        """
        ta = sports_context.get("team_a", {})
        tb = sports_context.get("team_b", {})
        h2h = sports_context.get("head_to_head", {})
        is_home = sports_context.get("is_home", True)

        fa5 = ta.get("form_5", {})
        fa20 = ta.get("form_20", {})
        fb5 = tb.get("form_5", {})
        fb20 = tb.get("form_20", {})

        scores = {}

        # 1. Recent form (last 5) — compare win rates
        a_recent = fa5.get("win_rate", 0.33)
        b_recent = fb5.get("win_rate", 0.33)
        scores["recent_form"] = self._strength_score(a_recent, b_recent)

        # 2. Season form (last 20) — compare points per game
        a_ppg = fa20.get("points_per_game", 1.0)
        b_ppg = fb20.get("points_per_game", 1.0)
        scores["season_form"] = self._strength_score(a_ppg / 3.0, b_ppg / 3.0)

        # 3. Home/away advantage
        if is_home:
            home_rate = fa20.get("home_win_rate", 0.45)
            away_rate = fb20.get("away_win_rate", 0.30)
        else:
            home_rate = fb20.get("home_win_rate", 0.45)
            away_rate = fa20.get("away_win_rate", 0.30)
        scores["home_away"] = self._strength_score(
            home_rate if is_home else away_rate,
            away_rate if is_home else home_rate,
        )

        # 4. Head-to-head
        h2h_rate = h2h.get("team_a_win_rate", 0.5)
        total_meetings = h2h.get("total_meetings", 0)
        if total_meetings < 3:
            scores["head_to_head"] = 0.5  # Not enough data, neutral
        else:
            scores["head_to_head"] = h2h_rate

        # 5. League position (lower = better, normalize to 0-1)
        pos_a = ta.get("league_position", 10)
        pos_b = tb.get("league_position", 10)
        max_pos = 20
        a_strength = 1.0 - (pos_a - 1) / max(max_pos - 1, 1)
        b_strength = 1.0 - (pos_b - 1) / max(max_pos - 1, 1)
        scores["league_position"] = self._strength_score(a_strength, b_strength)

        # 6. Goals differential
        a_gd = fa20.get("goals_for_avg", 1.2) - fa20.get("goals_against_avg", 1.0)
        b_gd = fb20.get("goals_for_avg", 1.2) - fb20.get("goals_against_avg", 1.0)
        # Normalize goal diff to 0-1 range (assume max diff of 2.0)
        a_norm = min(max((a_gd + 2.0) / 4.0, 0), 1)
        b_norm = min(max((b_gd + 2.0) / 4.0, 0), 1)
        scores["goals_diff"] = self._strength_score(a_norm, b_norm)

        # Weighted composite
        raw_win_a = sum(scores[k] * self.WEIGHTS[k] for k in self.WEIGHTS)

        # Convert to three-way probability (win/draw/loss)
        # Use a sigmoid-like scaling around 0.5
        win_a = self._calibrate(raw_win_a, bias=0.05 if is_home else -0.02)
        draw_base = 0.26 - abs(win_a - 0.5) * 0.3  # draws more likely in even matches
        draw_prob = max(0.08, min(0.35, draw_base))
        win_b = max(0.02, 1.0 - win_a - draw_prob)

        # Re-normalize
        total = win_a + draw_prob + win_b
        win_a /= total
        draw_prob /= total
        win_b /= total

        # Compute confidence based on data quality
        data_points = (
            fa5.get("matches_analyzed", 0) +
            fb5.get("matches_analyzed", 0) +
            total_meetings
        )
        confidence = min(1.0, data_points / 15.0) * 0.8 + 0.2

        reasoning_parts = []
        for key, score in scores.items():
            direction = "favors A" if score > 0.55 else ("favors B" if score < 0.45 else "neutral")
            reasoning_parts.append(f"{key}: {score:.2f} ({direction})")

        return {
            "win_a": round(win_a, 4),
            "draw": round(draw_prob, 4),
            "win_b": round(win_b, 4),
            "confidence": round(confidence, 3),
            "component_scores": scores,
            "reasoning": "; ".join(reasoning_parts),
        }

    @staticmethod
    def _strength_score(a_val: float, b_val: float) -> float:
        """Convert two strength values to A-win probability (0-1)."""
        total = a_val + b_val
        if total == 0:
            return 0.5
        return a_val / total

    @staticmethod
    def _calibrate(raw: float, bias: float = 0.0) -> float:
        """Calibrate raw score to probability with optional home bias."""
        adjusted = raw + bias
        return max(0.05, min(0.90, adjusted))


# ── Crypto Latency Model ───────────────────────────────────────

class CryptoLatencyModel:
    """
    Detects arbitrage between Polymarket crypto prediction prices
    and actual exchange price momentum.
    """

    def detect_arbitrage(
        self,
        polymarket_yes_price: float,
        crypto_context: dict,
    ) -> ProbabilityEstimate:
        """
        Compare Polymarket's implied probability for a crypto up/down market
        with actual exchange momentum.

        polymarket_yes_price: current YES price on Polymarket (e.g., 0.50 for "BTC up today")
        crypto_context: output from data_feeds.get_full_crypto_context()
        """
        momentum_5m = crypto_context.get("momentum_5m", {})
        momentum_1h = crypto_context.get("momentum_1h", {})
        change_5m = momentum_5m.get("change_pct", 0)
        change_1h = momentum_1h.get("change_pct", 0)
        change_24h = crypto_context.get("change_24h_pct", 0)
        volume_trend = momentum_5m.get("volume_trend", 0)

        # Estimate actual probability of "up" outcome based on momentum
        # Start from market price as base
        our_prob = polymarket_yes_price

        # Adjust based on short-term momentum
        if abs(change_5m) > 0.5:
            # Significant 5-min move — market may not have caught up
            momentum_adjustment = change_5m * 0.08  # 0.5% move -> 4% prob adjustment
            our_prob += momentum_adjustment

        # Adjust based on 1-hour trend
        if abs(change_1h) > 1.0:
            our_prob += change_1h * 0.03

        # Volume surge amplifies signal
        if volume_trend > 1.0:
            our_prob += (our_prob - 0.5) * 0.1  # Push probability further from 50%

        our_prob = max(0.02, min(0.98, our_prob))

        edge = our_prob - polymarket_yes_price
        abs_edge = abs(edge)

        # Confidence based on signal strength
        confidence = min(1.0, (abs(change_5m) / 2.0 + abs(change_1h) / 5.0) * 0.6 + 0.1)
        if volume_trend > 1.0:
            confidence = min(1.0, confidence + 0.15)

        coin_id = crypto_context.get("coin_id", "unknown")
        reasoning = (
            f"{coin_id}: 5m change={change_5m:+.2f}%, 1h change={change_1h:+.2f}%, "
            f"24h change={change_24h:+.1f}%, vol_trend={volume_trend:+.1f}. "
            f"Market at {polymarket_yes_price:.2f}, our estimate {our_prob:.3f}, "
            f"edge={edge:+.3f}"
        )

        return ProbabilityEstimate(
            our_probability=round(our_prob, 4),
            market_probability=polymarket_yes_price,
            edge=round(edge, 4),
            confidence=round(confidence, 3),
            reasoning=reasoning,
            model_name="crypto_latency",
            category=f"crypto_{coin_id}",
            details={
                "change_5m": change_5m,
                "change_1h": change_1h,
                "change_24h": change_24h,
                "volume_trend": volume_trend,
                "binance_price": crypto_context.get("binance_price", 0),
            },
        )


# ── Cross-Market Arbitrage Model ────────────────────────────────

class ArbitrageModel:
    """
    Detects arbitrage across prediction markets:
    1. YES + NO < 1.00 (guaranteed profit)
    2. Related markets with contradictory prices
    """

    def find_arbitrage(self, markets: list[dict]) -> list[dict]:
        """
        Scan a list of markets for arbitrage opportunities.
        Each market should have yes_price, no_price, id, question.
        """
        opportunities = []

        for m in markets:
            yes_p = m.get("yes_price", 0)
            no_p = m.get("no_price", 0)
            if yes_p <= 0 or no_p <= 0:
                continue

            total = yes_p + no_p

            # Type 1: Direct YES+NO arbitrage
            if total < 0.98:
                gap = 1.0 - total
                opportunities.append({
                    "type": "direct_arbitrage",
                    "market_id": m.get("id", ""),
                    "question": m.get("question", ""),
                    "yes_price": yes_p,
                    "no_price": no_p,
                    "total": round(total, 4),
                    "guaranteed_profit_pct": round(gap * 100, 2),
                    "edge": round(gap, 4),
                    "confidence": 0.95,
                    "reasoning": f"YES({yes_p:.3f}) + NO({no_p:.3f}) = {total:.3f} < 1.00. "
                                 f"Buy both for guaranteed {gap:.1%} profit.",
                })

            # Type 2: Overpriced (YES+NO > 1.02) indicates mispricing on one side
            if total > 1.02:
                overprice = total - 1.0
                opportunities.append({
                    "type": "overpriced_market",
                    "market_id": m.get("id", ""),
                    "question": m.get("question", ""),
                    "yes_price": yes_p,
                    "no_price": no_p,
                    "total": round(total, 4),
                    "overprice_pct": round(overprice * 100, 2),
                    "edge": round(overprice / 2, 4),
                    "confidence": 0.6,
                    "reasoning": f"YES({yes_p:.3f}) + NO({no_p:.3f}) = {total:.3f} > 1.00. "
                                 f"One side is overpriced by ~{overprice:.1%}.",
                })

        # Find related market contradictions
        contradictions = self._find_contradictions(markets)
        opportunities.extend(contradictions)

        opportunities.sort(key=lambda x: x["edge"], reverse=True)
        return opportunities

    def _find_contradictions(self, markets: list[dict]) -> list[dict]:
        """Find markets that contradict each other based on question text."""
        results = []
        # Simple heuristic: look for markets with very similar questions
        # but different implied probabilities
        seen = {}
        for m in markets:
            q = m.get("question", "").lower()
            # Extract key entities (crude but functional)
            for keyword in ["bitcoin", "ethereum", "trump", "biden", "fed", "rate"]:
                if keyword in q:
                    key = keyword
                    yes_p = m.get("yes_price", 0)
                    if key in seen:
                        other = seen[key]
                        diff = abs(yes_p - other["yes_price"])
                        if diff > 0.15:
                            results.append({
                                "type": "related_contradiction",
                                "market_a_id": other["id"],
                                "market_a_question": other["question"],
                                "market_a_yes": other["yes_price"],
                                "market_b_id": m.get("id", ""),
                                "market_b_question": m.get("question", ""),
                                "market_b_yes": yes_p,
                                "edge": round(diff / 2, 4),
                                "confidence": 0.4,
                                "reasoning": f"Related markets on '{keyword}' show {diff:.0%} "
                                             f"probability divergence.",
                            })
                    else:
                        seen[key] = {
                            "id": m.get("id", ""),
                            "question": m.get("question", ""),
                            "yes_price": yes_p,
                        }
        return results


# ── Long-Tail Event Model ──────────────────────────────────────

class LongTailModel:
    """
    For low-volume / long-tail markets, estimate probability using
    historical base rates and news signal velocity.
    """

    # Base rates for common event types (rough historical averages)
    BASE_RATES = {
        "election_win": 0.50,
        "ceasefire": 0.15,
        "rate_cut": 0.25,
        "rate_hike": 0.30,
        "crypto_milestone": 0.20,
        "weather_event": 0.10,
        "legislation_pass": 0.35,
        "default": 0.30,
    }

    def analyze_event(
        self,
        market: dict,
        news_signals: dict,
    ) -> ProbabilityEstimate:
        """
        Estimate probability for a low-volume market.

        market: {id, question, yes_price, no_price, volume, category}
        news_signals: output from data_feeds.get_news_signals()
        """
        question = market.get("question", "").lower()
        yes_price = market.get("yes_price", 0.5)
        volume = market.get("volume", 0)

        # Determine base rate from question content
        base_rate = self.BASE_RATES["default"]
        event_type = "default"
        for etype, rate in self.BASE_RATES.items():
            if etype.replace("_", " ") in question or etype.replace("_", "") in question:
                base_rate = rate
                event_type = etype
                break

        # Adjust based on news signal velocity
        our_prob = base_rate
        signal_adjustment = 0.0
        signal_details = []
        signals = news_signals.get("signals", {})

        for category, signal_data in signals.items():
            velocity = signal_data.get("velocity", 0)
            if self._category_relates_to_question(category, question):
                # High news velocity = event more likely
                adjustment = velocity * 0.3  # Up to 30% probability shift
                signal_adjustment += adjustment
                signal_details.append(
                    f"{category}: velocity={velocity:.2f}, adj={adjustment:+.3f}"
                )

        our_prob += signal_adjustment
        our_prob = max(0.02, min(0.98, our_prob))

        edge = our_prob - yes_price

        # Low confidence for low-volume markets without strong signals
        confidence = 0.25
        if abs(signal_adjustment) > 0.05:
            confidence += 0.2
        if volume > 5000:
            confidence += 0.1
        confidence = min(0.75, confidence)

        reasoning = (
            f"Event type: {event_type}, base rate: {base_rate:.2f}. "
            f"News signals: {'; '.join(signal_details) if signal_details else 'none'}. "
            f"Market at {yes_price:.2f}, our estimate {our_prob:.3f}, edge={edge:+.3f}"
        )

        return ProbabilityEstimate(
            our_probability=round(our_prob, 4),
            market_probability=yes_price,
            edge=round(edge, 4),
            confidence=round(confidence, 3),
            reasoning=reasoning,
            model_name="long_tail",
            category=market.get("category", "other"),
            details={
                "event_type": event_type,
                "base_rate": base_rate,
                "signal_adjustment": round(signal_adjustment, 4),
                "signal_details": signal_details,
            },
        )

    @staticmethod
    def _category_relates_to_question(signal_category: str, question: str) -> bool:
        """Check if a news signal category is relevant to the market question."""
        mapping = {
            "geopolitical": ["war", "peace", "invasion", "ceasefire", "conflict", "military", "troops"],
            "election": ["election", "president", "governor", "senate", "congress", "vote", "poll"],
            "crypto_regulation": ["crypto", "bitcoin", "ethereum", "sec", "regulation"],
            "economic": ["fed", "rate", "inflation", "recession", "gdp", "economy", "unemployment"],
        }
        keywords = mapping.get(signal_category, [])
        return any(kw in question for kw in keywords)


# ── Model Aggregator ────────────────────────────────────────────

class ModelAggregator:
    """
    Routes markets to the correct model based on category
    and returns unified edge estimates.
    """

    def __init__(self):
        self.sports_model = SportsModel()
        self.crypto_model = CryptoLatencyModel()
        self.arbitrage_model = ArbitrageModel()
        self.longtail_model = LongTailModel()

    def get_edge(
        self,
        market: dict,
        category: str,
        external_data: dict,
    ) -> ProbabilityEstimate:
        """
        Route market to correct model and return edge estimate.

        market: {id, question, yes_price, no_price, volume, ...}
        category: market category string
        external_data: dict with relevant external data for the model
        """
        yes_price = market.get("yes_price", 0.5)

        if category.startswith("sports"):
            sports_context = external_data.get("sports_context", {})
            if not sports_context:
                return self._no_data_estimate(market, category)
            result = self.sports_model.estimate_probability(sports_context)
            # Determine which outcome to compare
            our_prob = result["win_a"]
            edge = our_prob - yes_price
            return ProbabilityEstimate(
                our_probability=round(our_prob, 4),
                market_probability=yes_price,
                edge=round(edge, 4),
                confidence=result["confidence"],
                reasoning=result["reasoning"],
                model_name="sports_composite",
                category=category,
                details=result,
            )

        elif category.startswith("crypto"):
            crypto_context = external_data.get("crypto_context", {})
            if not crypto_context:
                return self._no_data_estimate(market, category)
            return self.crypto_model.detect_arbitrage(yes_price, crypto_context)

        else:
            # Use long-tail model for everything else
            news_signals = external_data.get("news_signals", {})
            return self.longtail_model.analyze_event(market, news_signals)

    def find_arbitrage(self, markets: list[dict]) -> list[dict]:
        """Delegate to arbitrage model."""
        return self.arbitrage_model.find_arbitrage(markets)

    @staticmethod
    def _no_data_estimate(market: dict, category: str) -> ProbabilityEstimate:
        """Return a low-confidence estimate when no external data is available."""
        yes_price = market.get("yes_price", 0.5)
        return ProbabilityEstimate(
            our_probability=yes_price,
            market_probability=yes_price,
            edge=0.0,
            confidence=0.1,
            reasoning="Insufficient external data for independent estimate.",
            model_name="none",
            category=category,
        )
