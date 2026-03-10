"""
PolyEdge Strategy Engine
Tracks bets, computes strategy performance, and determines scaling decisions.
"""

import math
from datetime import datetime, timezone
from api_server import get_db


class StrategyEngine:
    """
    Tracks every bet, categorizes by strategy, computes performance stats,
    and determines which strategies to scale, maintain, or kill.
    """

    # Strategy classification thresholds
    MIN_BETS_SIGNIFICANT = 20
    MIN_BETS_SCALING = 50
    WIN_RATE_PROMISING = 0.52
    WIN_RATE_SCALING = 0.55
    WIN_RATE_KILLING = 0.45
    PROFIT_FACTOR_SCALING = 1.5

    def __init__(self):
        self._ensure_tables()

    def _ensure_tables(self):
        """Ensure extended strategy tracking tables exist."""
        db = get_db()
        db.executescript("""
            CREATE TABLE IF NOT EXISTS bet_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id TEXT,
                strategy_name TEXT,
                category TEXT,
                entry_price REAL,
                size REAL,
                side TEXT,
                model_edge REAL,
                model_confidence REAL,
                outcome TEXT DEFAULT 'pending',
                exit_price REAL DEFAULT 0,
                pnl REAL DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now')),
                resolved_at TEXT
            );
            CREATE TABLE IF NOT EXISTS strategy_metrics (
                name TEXT PRIMARY KEY,
                category TEXT DEFAULT '',
                total_bets INTEGER DEFAULT 0,
                wins INTEGER DEFAULT 0,
                losses INTEGER DEFAULT 0,
                pending INTEGER DEFAULT 0,
                total_pnl REAL DEFAULT 0,
                gross_wins REAL DEFAULT 0,
                gross_losses REAL DEFAULT 0,
                avg_edge_predicted REAL DEFAULT 0,
                avg_edge_realized REAL DEFAULT 0,
                status TEXT DEFAULT 'exploring',
                updated_at TEXT DEFAULT (datetime('now'))
            );
        """)
        db.commit()
        db.close()

    def record_bet(
        self,
        market_id: str,
        strategy_name: str,
        category: str,
        entry_price: float,
        size: float,
        side: str,
        model_edge: float,
        model_confidence: float,
    ) -> int:
        """Log a new bet with all metadata. Returns the bet ID."""
        db = get_db()
        cur = db.execute(
            """INSERT INTO bet_log
               (market_id, strategy_name, category, entry_price, size, side,
                model_edge, model_confidence)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (market_id, strategy_name, category, entry_price, size, side,
             model_edge, model_confidence),
        )
        bet_id = cur.lastrowid

        # Upsert strategy metrics
        db.execute(
            """INSERT INTO strategy_metrics (name, category, total_bets, pending)
               VALUES (?, ?, 1, 1)
               ON CONFLICT(name) DO UPDATE SET
                   total_bets = total_bets + 1,
                   pending = pending + 1,
                   updated_at = datetime('now')""",
            (strategy_name, category),
        )
        db.commit()
        db.close()
        return bet_id

    def resolve_bet(self, bet_id: int, outcome: str, exit_price: float) -> dict:
        """
        Mark a bet as won/lost and update strategy stats.
        outcome: 'won' or 'lost'
        """
        db = get_db()
        bet = db.execute("SELECT * FROM bet_log WHERE id = ?", (bet_id,)).fetchone()
        if not bet:
            db.close()
            return {"error": "Bet not found"}

        entry = bet["entry_price"]
        size = bet["size"]
        side = bet["side"]

        # Calculate P&L
        if side == "YES":
            if outcome == "won":
                pnl = (1.0 - entry) * size  # payout is $1 per share minus cost
            else:
                pnl = -entry * size
        else:  # NO side
            if outcome == "won":
                pnl = (1.0 - (1.0 - entry)) * size  # effectively entry * size
            else:
                pnl = -(1.0 - entry) * size

        pnl = round(pnl, 4)

        db.execute(
            """UPDATE bet_log SET outcome = ?, exit_price = ?, pnl = ?,
               resolved_at = datetime('now') WHERE id = ?""",
            (outcome, exit_price, pnl, bet_id),
        )

        # Update strategy metrics
        strategy = bet["strategy_name"]
        if outcome == "won":
            db.execute(
                """UPDATE strategy_metrics SET
                       wins = wins + 1, pending = pending - 1,
                       total_pnl = total_pnl + ?, gross_wins = gross_wins + ?,
                       updated_at = datetime('now')
                   WHERE name = ?""",
                (pnl, pnl, strategy),
            )
        else:
            db.execute(
                """UPDATE strategy_metrics SET
                       losses = losses + 1, pending = pending - 1,
                       total_pnl = total_pnl + ?, gross_losses = gross_losses + ?,
                       updated_at = datetime('now')
                   WHERE name = ?""",
                (pnl, abs(pnl), strategy),
            )

        db.commit()
        db.close()

        return {"bet_id": bet_id, "outcome": outcome, "pnl": pnl}

    def analyze_strategies(self) -> list[dict]:
        """
        For each strategy, compute comprehensive performance metrics
        and classify into exploring/promising/scaling/killing.
        """
        db = get_db()
        strategies = db.execute("SELECT * FROM strategy_metrics").fetchall()
        results = []

        for s in strategies:
            s = dict(s)
            name = s["name"]
            total = s["total_bets"]
            wins = s["wins"]
            losses = s["losses"]
            resolved = wins + losses

            # Win rate
            win_rate = wins / resolved if resolved > 0 else 0.0

            # Profit factor
            gross_w = s["gross_wins"]
            gross_l = s["gross_losses"]
            profit_factor = gross_w / gross_l if gross_l > 0 else (float("inf") if gross_w > 0 else 0)

            # Average edge (predicted vs realized)
            bets = db.execute(
                "SELECT model_edge, pnl, size FROM bet_log WHERE strategy_name = ? AND outcome != 'pending'",
                (name,),
            ).fetchall()

            avg_edge_predicted = 0.0
            avg_edge_realized = 0.0
            returns = []
            if bets:
                edges = [b["model_edge"] for b in bets]
                avg_edge_predicted = sum(edges) / len(edges)
                realized = [b["pnl"] / max(b["size"], 0.01) for b in bets]
                avg_edge_realized = sum(realized) / len(realized)
                returns = realized

            # Sharpe ratio (annualized, assuming daily bets)
            sharpe = 0.0
            if len(returns) >= 2:
                mean_r = sum(returns) / len(returns)
                var_r = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
                std_r = math.sqrt(var_r) if var_r > 0 else 0
                if std_r > 0:
                    sharpe = (mean_r / std_r) * math.sqrt(252)

            # Classify strategy
            if resolved < self.MIN_BETS_SIGNIFICANT:
                status = "exploring"
            elif resolved >= self.MIN_BETS_SCALING and win_rate >= self.WIN_RATE_SCALING and profit_factor >= self.PROFIT_FACTOR_SCALING:
                status = "scaling"
            elif win_rate >= self.WIN_RATE_PROMISING and s["total_pnl"] > 0:
                status = "promising"
            elif win_rate < self.WIN_RATE_KILLING or s["total_pnl"] < 0:
                status = "killing"
            else:
                status = "exploring"

            # Update status in DB
            db.execute(
                """UPDATE strategy_metrics SET status = ?,
                       avg_edge_predicted = ?, avg_edge_realized = ?,
                       updated_at = datetime('now')
                   WHERE name = ?""",
                (status, round(avg_edge_predicted, 4), round(avg_edge_realized, 4), name),
            )

            results.append({
                "name": name,
                "category": s["category"],
                "status": status,
                "total_bets": total,
                "resolved": resolved,
                "pending": s["pending"],
                "wins": wins,
                "losses": losses,
                "win_rate": round(win_rate, 4),
                "total_pnl": round(s["total_pnl"], 2),
                "profit_factor": round(profit_factor, 2) if profit_factor != float("inf") else 999.99,
                "sharpe_ratio": round(sharpe, 2),
                "avg_edge_predicted": round(avg_edge_predicted, 4),
                "avg_edge_realized": round(avg_edge_realized, 4),
                "gross_wins": round(gross_w, 2),
                "gross_losses": round(gross_l, 2),
            })

        db.commit()
        db.close()

        results.sort(key=lambda x: x["total_pnl"], reverse=True)
        return results

    def get_recommended_bet_size(self, strategy_name: str, base_size: float = 2.0) -> float:
        """
        Scale bet size based on strategy status.
        - exploring: base_size (e.g., $2)
        - promising: 2x base
        - scaling: 5x base
        - killing: $0 (stop betting)
        """
        db = get_db()
        row = db.execute(
            "SELECT status FROM strategy_metrics WHERE name = ?", (strategy_name,)
        ).fetchone()
        db.close()

        if not row:
            return base_size  # new strategy, use base

        status = row["status"]
        multipliers = {
            "exploring": 1.0,
            "promising": 2.0,
            "scaling": 5.0,
            "killing": 0.0,
        }
        return round(base_size * multipliers.get(status, 1.0), 2)

    def get_niche_report(self) -> dict:
        """
        Detailed breakdown of which market categories
        and signal types are most profitable.
        """
        db = get_db()

        # By category
        category_stats = db.execute("""
            SELECT category,
                   COUNT(*) as total_bets,
                   SUM(CASE WHEN outcome = 'won' THEN 1 ELSE 0 END) as wins,
                   SUM(CASE WHEN outcome = 'lost' THEN 1 ELSE 0 END) as losses,
                   COALESCE(SUM(pnl), 0) as total_pnl,
                   COALESCE(AVG(model_edge), 0) as avg_predicted_edge,
                   COALESCE(AVG(CASE WHEN outcome != 'pending' THEN pnl / MAX(size, 0.01) END), 0) as avg_realized_edge
            FROM bet_log
            GROUP BY category
            ORDER BY total_pnl DESC
        """).fetchall()

        # By strategy
        strategy_stats = db.execute("""
            SELECT strategy_name,
                   COUNT(*) as total_bets,
                   SUM(CASE WHEN outcome = 'won' THEN 1 ELSE 0 END) as wins,
                   COALESCE(SUM(pnl), 0) as total_pnl
            FROM bet_log
            GROUP BY strategy_name
            ORDER BY total_pnl DESC
        """).fetchall()

        # Best and worst individual bets
        best_bets = db.execute(
            "SELECT * FROM bet_log WHERE outcome != 'pending' ORDER BY pnl DESC LIMIT 5"
        ).fetchall()
        worst_bets = db.execute(
            "SELECT * FROM bet_log WHERE outcome != 'pending' ORDER BY pnl ASC LIMIT 5"
        ).fetchall()

        db.close()

        return {
            "by_category": [dict(r) for r in category_stats],
            "by_strategy": [dict(r) for r in strategy_stats],
            "best_bets": [dict(r) for r in best_bets],
            "worst_bets": [dict(r) for r in worst_bets],
            "summary": {
                "most_profitable_category": dict(category_stats[0])["category"] if category_stats else "none",
                "total_categories": len(category_stats),
                "total_strategies": len(strategy_stats),
            },
        }

    def get_strategy_detail(self, strategy_name: str) -> dict:
        """Get detailed info for a single strategy."""
        db = get_db()
        metrics = db.execute(
            "SELECT * FROM strategy_metrics WHERE name = ?", (strategy_name,)
        ).fetchone()

        if not metrics:
            db.close()
            return {"error": "Strategy not found"}

        recent_bets = db.execute(
            """SELECT * FROM bet_log WHERE strategy_name = ?
               ORDER BY created_at DESC LIMIT 20""",
            (strategy_name,),
        ).fetchall()

        # Daily P&L for this strategy
        daily = db.execute(
            """SELECT date(created_at) as day, SUM(pnl) as daily_pnl, COUNT(*) as bets
               FROM bet_log WHERE strategy_name = ? AND outcome != 'pending'
               GROUP BY date(created_at) ORDER BY day""",
            (strategy_name,),
        ).fetchall()

        db.close()

        return {
            "metrics": dict(metrics),
            "recent_bets": [dict(b) for b in recent_bets],
            "daily_pnl": [dict(d) for d in daily],
            "recommended_bet_size": self.get_recommended_bet_size(strategy_name),
        }
