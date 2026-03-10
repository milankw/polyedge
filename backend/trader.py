"""
PolyEdge Trade Execution Engine
Handles order placement, sizing, and risk management via Polymarket CLOB API.
"""

import hashlib
import hmac
import json
import time
import logging
from datetime import datetime, timezone

import httpx

from api_server import get_db

logger = logging.getLogger(__name__)

CLOB_API = "https://clob.polymarket.com"


class PolymarketTrader:
    """
    Handles all trading operations via Polymarket CLOB API.
    Uses HMAC-SHA256 signing for authentication.
    Runs in DRY RUN mode by default until valid credentials are provided.
    """

    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        api_passphrase: str = "",
        wallet_address: str = "",
    ):
        self.api_key = api_key
        self.api_secret = api_secret
        self.api_passphrase = api_passphrase
        self.wallet_address = wallet_address
        self.dry_run = not all([api_key, api_secret, api_passphrase, wallet_address])

    @classmethod
    def from_config(cls) -> "PolymarketTrader":
        """Load credentials from database config."""
        db = get_db()
        rows = db.execute("SELECT key, value FROM config WHERE key IN (?, ?, ?, ?)",
                          ("polymarket_key", "polymarket_secret",
                           "polymarket_passphrase", "wallet_address")).fetchall()
        db.close()
        creds = {r["key"]: r["value"] for r in rows}
        return cls(
            api_key=creds.get("polymarket_key", ""),
            api_secret=creds.get("polymarket_secret", ""),
            api_passphrase=creds.get("polymarket_passphrase", ""),
            wallet_address=creds.get("wallet_address", ""),
        )

    def _sign_request(self, method: str, path: str, body: str = "") -> dict:
        """Generate HMAC-SHA256 authentication headers."""
        timestamp = str(int(time.time()))
        message = timestamp + method.upper() + path + body
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return {
            "POLY_ADDRESS": self.wallet_address,
            "POLY_SIGNATURE": signature,
            "POLY_TIMESTAMP": timestamp,
            "POLY_API_KEY": self.api_key,
            "POLY_PASSPHRASE": self.api_passphrase,
            "Content-Type": "application/json",
        }

    async def place_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float,
        order_type: str = "GTC",
    ) -> dict:
        """
        Place a limit order on Polymarket CLOB.
        Returns order details or dry-run simulation.
        """
        order_body = {
            "tokenID": token_id,
            "price": str(price),
            "size": str(size),
            "side": side.upper(),
            "orderType": order_type,
        }
        body_str = json.dumps(order_body)

        if self.dry_run:
            logger.info(f"DRY RUN: place_order {side} {size}@{price} on {token_id}")
            return {
                "status": "dry_run",
                "order": order_body,
                "message": "Order simulated (no API credentials configured)",
            }

        headers = self._sign_request("POST", "/order", body_str)
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    f"{CLOB_API}/order",
                    headers=headers,
                    content=body_str,
                )
                if resp.status_code in (200, 201):
                    return resp.json()
                return {
                    "status": "error",
                    "code": resp.status_code,
                    "detail": resp.text,
                }
        except (httpx.HTTPError, httpx.TimeoutException) as e:
            return {"status": "error", "detail": str(e)}

    async def cancel_order(self, order_id: str) -> dict:
        """Cancel an open order."""
        if self.dry_run:
            logger.info(f"DRY RUN: cancel_order {order_id}")
            return {"status": "dry_run", "order_id": order_id}

        headers = self._sign_request("DELETE", f"/order/{order_id}")
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.delete(
                    f"{CLOB_API}/order/{order_id}",
                    headers=headers,
                )
                if resp.status_code == 200:
                    return resp.json()
                return {"status": "error", "code": resp.status_code, "detail": resp.text}
        except (httpx.HTTPError, httpx.TimeoutException) as e:
            return {"status": "error", "detail": str(e)}

    async def get_open_orders(self) -> dict:
        """List all open orders."""
        if self.dry_run:
            return {"status": "dry_run", "orders": [], "message": "No API credentials configured"}

        headers = self._sign_request("GET", "/orders")
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{CLOB_API}/orders",
                    headers=headers,
                )
                if resp.status_code == 200:
                    return resp.json()
                return {"status": "error", "code": resp.status_code, "detail": resp.text}
        except (httpx.HTTPError, httpx.TimeoutException) as e:
            return {"status": "error", "detail": str(e)}

    # ── Position Sizing ─────────────────────────────────────────

    @staticmethod
    def kelly_criterion(
        our_prob: float,
        market_prob: float,
        bankroll: float,
        fraction: float = 0.25,
        max_bet_pct: float = 0.05,
        max_bet_size: float = 50.0,
    ) -> float:
        """
        Calculate optimal bet size using fractional Kelly Criterion.

        f* = (p * b - q) / b
        where:
            p = our estimated probability of winning
            q = 1 - p
            b = payout odds = (1 / market_price) - 1

        fraction: Kelly fraction (0.25 = quarter-Kelly for safety)
        max_bet_pct: maximum percentage of bankroll per bet
        max_bet_size: absolute maximum bet size
        """
        if our_prob <= market_prob or our_prob <= 0 or market_prob <= 0:
            return 0.0

        if market_prob >= 1.0:
            return 0.0

        b = (1.0 / market_prob) - 1.0  # payout odds
        if b <= 0:
            return 0.0

        p = our_prob
        q = 1.0 - p
        kelly_f = (p * b - q) / b

        if kelly_f <= 0:
            return 0.0

        # Apply fractional Kelly
        bet_fraction = kelly_f * fraction

        # Apply limits
        bet_size = bankroll * bet_fraction
        bet_size = min(bet_size, bankroll * max_bet_pct)
        bet_size = min(bet_size, max_bet_size)
        bet_size = max(0.0, bet_size)

        return round(bet_size, 2)

    # ── Risk Management ─────────────────────────────────────────

    def _load_config(self) -> dict:
        """Load trading config from database."""
        db = get_db()
        rows = db.execute("SELECT key, value FROM config").fetchall()
        db.close()
        return {r["key"]: r["value"] for r in rows}

    def _check_risk_limits(self, config: dict, size: float) -> dict:
        """Check if a trade passes risk limits. Returns {ok, reason}."""
        db = get_db()

        # Check daily budget
        daily_budget = float(config.get("daily_budget", 50.0))
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        today_spent = db.execute(
            "SELECT COALESCE(SUM(size), 0) FROM positions WHERE date(opened_at) = ?",
            (today,),
        ).fetchone()[0]

        if today_spent + size > daily_budget:
            db.close()
            return {
                "ok": False,
                "reason": f"Daily budget exceeded: ${today_spent:.2f} + ${size:.2f} > ${daily_budget:.2f}",
            }

        # Check max open positions
        open_count = db.execute(
            "SELECT COUNT(*) FROM positions WHERE status = 'open'"
        ).fetchone()[0]
        if open_count >= 20:
            db.close()
            return {"ok": False, "reason": f"Max open positions reached ({open_count})"}

        # Check max bet size
        max_bet = float(config.get("max_bet_size", 5.0))
        if size > max_bet:
            db.close()
            return {"ok": False, "reason": f"Bet size ${size:.2f} exceeds max ${max_bet:.2f}"}

        db.close()
        return {"ok": True, "reason": ""}

    async def execute_opportunity(self, opportunity: dict) -> dict:
        """
        Execute a trade for a scanned opportunity.

        opportunity: {
            market_id, token_id, side, edge, confidence,
            our_probability, market_probability, category, question, ...
        }
        """
        config = self._load_config()

        # Check if auto-trade is enabled
        if config.get("auto_trade", "false") != "true" and not self.dry_run:
            return {
                "status": "skipped",
                "reason": "Auto-trade is disabled. Enable in config or use dry_run mode.",
            }

        our_prob = opportunity.get("our_probability", 0.5)
        market_prob = opportunity.get("market_probability", 0.5)
        edge = opportunity.get("edge", 0)
        confidence = opportunity.get("confidence", 0)

        # Check minimum edge
        min_edge = float(config.get("min_edge", 0.05))
        if abs(edge) < min_edge:
            return {"status": "skipped", "reason": f"Edge {edge:.3f} below minimum {min_edge}"}

        # Calculate position size
        bankroll = float(config.get("daily_budget", 50.0)) * 10  # rough bankroll estimate
        max_bet = float(config.get("max_bet_size", 5.0))
        size = self.kelly_criterion(
            our_prob, market_prob, bankroll,
            fraction=0.25, max_bet_size=max_bet,
        )

        if size < 0.50:
            return {"status": "skipped", "reason": f"Kelly size too small: ${size:.2f}"}

        # Risk check
        risk = self._check_risk_limits(config, size)
        if not risk["ok"]:
            return {"status": "blocked", "reason": risk["reason"]}

        # Determine side and price
        side = opportunity.get("side", "YES" if edge > 0 else "NO")
        price = market_prob if side == "YES" else (1.0 - market_prob)
        token_id = opportunity.get("token_id", "")

        # Place order
        result = await self.place_order(
            token_id=token_id,
            side=side,
            price=round(price, 2),
            size=round(size, 2),
        )

        # Log to database
        db = get_db()
        strategy = opportunity.get("strategy", opportunity.get("category", "unknown"))
        db.execute(
            """INSERT INTO positions
               (market_id, side, entry_price, size, current_price, strategy, status)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                opportunity.get("market_id", ""),
                side,
                price,
                size,
                price,
                strategy,
                "open" if result.get("status") != "error" else "failed",
            ),
        )
        db.execute(
            """INSERT INTO strategies (name, category, total_bets)
               VALUES (?, ?, 1)
               ON CONFLICT(name) DO UPDATE SET total_bets = total_bets + 1""",
            (strategy, opportunity.get("category", "")),
        )
        db.commit()
        db.close()

        return {
            "status": "executed" if not self.dry_run else "dry_run",
            "side": side,
            "price": round(price, 2),
            "size": round(size, 2),
            "kelly_fraction": round(size / max(bankroll, 1), 4),
            "edge": round(edge, 4),
            "order_result": result,
        }
