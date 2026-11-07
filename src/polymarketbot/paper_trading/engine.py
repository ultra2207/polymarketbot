"""Real-time Paper Trading Engine for Polymarket Liquidity Reward Harvesting.

Implements:
1. Virtual resting limit orders at optimal spread offsets (s = 0.5c - 1.0c).
2. Strict post-only order placement logic.
3. Real-time trade matching, fill execution, inventory, cash, and avg_cost tracking.
4. Avellaneda-Stoikov-style inventory skewing.
5. 1-minute reward sampling epoch simulation (1,440/day) with quadratic scoring.
6. Persistence to data/processed/paper_trading/ledger.json.
"""

from __future__ import annotations

import json
import math
import os
import random
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional

from polymarketbot.logs import get_logger
from polymarketbot.paper_trading.models import (
    AntiDelusionConfig,
    BrokenBasketPolicy,
    EpochSnapshot,
    MarketConfig,
    MergeEvent,
    PendingSettlement,
    Position,
    SimulatedFill,
    TokenConfig,
    VirtualQuote,
)
from polymarketbot.paper_trading.orchestrator import (
    QuoteOrchestrator,
    QueueSnapshot,
)
from polymarketbot.paper_trading.rewards import (
    compute_competing_depth_score,
    compute_epoch_payout,
    compute_quadratic_score,
    normalise_two_sided,
)
from polymarketbot.rewards.queue_valuation import CTFGasProfile, PolygonPoSEnvironment
from polymarketbot.risk import RiskConfig, RiskManager
from polymarketbot.trading.binance_anchor import BinanceSpotFeed

MIN_REST_SECONDS = 3.5

logger = get_logger("paper_trading")


def _parse_price(x: Any) -> float:
    try:
        if isinstance(x, dict):
            return float(x.get("price", 0.0))
        if isinstance(x, (list, tuple)) and len(x) > 0:
            return float(x[0])
    except (ValueError, TypeError):
        pass
    return 0.0


def _parse_size(x: Any) -> float:
    try:
        if isinstance(x, dict):
            return float(x.get("size", 0.0))
        if isinstance(x, (list, tuple)) and len(x) > 1:
            return float(x[1])
    except (ValueError, TypeError):
        pass
    return 0.0


class PaperTradingEngine:
    """Manages virtual liquidity provision, order books, fills, and reward epochs."""

    def __init__(
        self,
        initial_cash_usd: float = 10000.0,
        base_spread_offset_cents: float = 0.75,
        default_quote_size: float = 100.0,
        max_inventory_shares: float = 1000.0,
        max_skew_cents: float = 0.5,
        auto_merge_threshold: float = 100.0,
        merge_gas_fee_usd: float = 0.0016,
        ledger_path: str | Path = "data/processed/paper_trading/ledger.json",
        risk_manager: Optional[RiskManager] = None,
        microstructure_gate: bool = False,
        microstructure_ofi_threshold: float = 0.0,
        microstructure_ofi_window_s: float = 60.0,
        orchestrator: Optional[QuoteOrchestrator] = None,
        kalshi_monitor: Optional[Any] = None,
        strategy: Optional[Any] = None,
        clock: Optional[Any] = None,
        signal_provider: Optional[Any] = None,
        anti_delusion: bool = False,
        anti_delusion_config: Optional[AntiDelusionConfig] = None,
        pos_env: Optional[PolygonPoSEnvironment] = None,
        binance_feed: Optional[BinanceSpotFeed] = None,
        binance_threshold_bps: float = 10.0,
    ) -> None:
        self.initial_cash_usd = float(initial_cash_usd)
        self.cash_usd = float(initial_cash_usd)
        self.base_spread_offset_cents = float(base_spread_offset_cents)
        self.default_quote_size = float(default_quote_size)
        self.max_inventory_shares = float(max_inventory_shares)
        self.max_skew_cents = float(max_skew_cents)
        self.auto_merge_threshold = float(auto_merge_threshold)
        self.merge_gas_fee_usd = float(merge_gas_fee_usd)
        self.ledger_path = Path(ledger_path)
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        self.risk_manager = risk_manager
        self.microstructure_gate = bool(microstructure_gate)
        self.microstructure_ofi_threshold = float(microstructure_ofi_threshold)
        self.microstructure_ofi_window_s = float(microstructure_ofi_window_s)
        self.orchestrator = orchestrator
        self.kalshi_monitor = kalshi_monitor
        # A pluggable ``StrategyModule`` (see polymarketbot.strategy). When set it
        # replaces the orchestrator block; the engine still owns sizing floors,
        # post-only clamping and risk.
        self.strategy = strategy
        # Replay needs a simulated clock and an as-of signal lookup; live keeps
        # the wall clock and no provider. Both are injected, never global.
        self.clock = clock or time.time
        self.signal_provider = signal_provider
        self._strategy_started = self.clock()
        self._kalshi_basis: dict[str, Optional[float]] = {}
        self._kalshi_missing_logged = False
        self._ofi_steps: dict[str, deque[tuple[float, int]]] = {}
        self._prev_top: dict[str, tuple[float, float]] = {}
        self.last_orchestrator_decision: dict[str, Any] = {}
        self._level_queue: dict[tuple[str, str], tuple[float, float, float, float]] = {}
        if self.risk_manager:
            self.risk_manager.register_unquote_callback(self._on_risk_unquote)

        # Anti-Delusion & Realism Upgrade State
        self.anti_delusion = bool(anti_delusion)
        self.anti_delusion_config = anti_delusion_config or AntiDelusionConfig(enabled=self.anti_delusion)
        if self.anti_delusion:
            self.anti_delusion_config.enabled = True
        self.pos_env = pos_env or PolygonPoSEnvironment()
        self.binance_feed = binance_feed
        self.binance_threshold_bps = float(binance_threshold_bps)

        self.pending_settlements: list[PendingSettlement] = []
        self.locked_settlement_cash_usd: float = 0.0
        self.total_taker_fees_usd: float = 0.0
        self.total_markout_haircut_usd: float = 0.0
        self.total_slippage_usd: float = 0.0
        self.broken_basket_events: list[dict[str, Any]] = []
        self._last_trade_ts: dict[str, float] = {}
        self._rng = random.Random(42)
        self._fill_model_coef = self._load_fill_model_coef()

        self.start_time = time.time()
        self.epoch_count = 0
        self.total_rewards_earned_usd = 0.0
        self.total_gas_fees_usd = 0.0
        self.total_merges_count = 0

        # Market registry
        self.markets: dict[str, MarketConfig] = {}  # condition_id -> config
        self.asset_to_condition: dict[str, str] = {}  # asset_id -> condition_id
        self.asset_to_outcome: dict[str, str] = {}  # asset_id -> outcome
        self._condition_yes_asset: dict[str, str] = {}  # condition_id -> YES token

        # Live book & quoting state
        self.orderbooks: dict[str, dict[str, Any]] = {}  # asset_id -> book info
        self.virtual_quotes: dict[str, dict[str, Optional[VirtualQuote]]] = {}  # asset_id -> {bid, ask}
        self.positions: dict[str, Position] = {}  # asset_id -> Position

        # Metrics & audit trail
        self.fills: list[SimulatedFill] = []
        self.merges: list[MergeEvent] = []
        self.epoch_snapshots: list[EpochSnapshot] = []

    def register_market(self, config: MarketConfig) -> None:
        """Register a target market and its tokens for quoting."""
        self.markets[config.condition_id] = config
        for tok in config.tokens:
            self.asset_to_condition[tok.token_id] = config.condition_id
            self.asset_to_outcome[tok.token_id] = tok.outcome
            if str(tok.outcome).strip().upper() == "YES":
                self._condition_yes_asset[config.condition_id] = tok.token_id
            if tok.token_id not in self.positions:
                self.positions[tok.token_id] = Position(
                    asset_id=tok.token_id,
                    outcome=tok.outcome,
                )
            if tok.token_id not in self.virtual_quotes:
                self.virtual_quotes[tok.token_id] = {"bid": None, "ask": None}
            if tok.token_id not in self.orderbooks:
                self.orderbooks[tok.token_id] = {
                    "bids": [],
                    "asks": [],
                    "mid": tok.price,
                    "best_bid": max(0.01, tok.price - 0.01),
                    "best_ask": min(0.99, tok.price + 0.01),
                    "spread_cents": 2.0,
                    "tick_size": 0.01,
                    "competing_score": 0.0,
                    "qualifying_bids_usd": 0.0,
                    "qualifying_asks_usd": 0.0,
                    "last_updated": time.time(),
                }
        logger.info(
            f"Registered market: {config.question[:45]} (${config.rate_per_day}/d, "
            f"max_spread={config.rewards_max_spread}c, min_sz={config.rewards_min_size})"
        )

    def attach_kalshi_monitor(self, monitor: Optional[Any]) -> None:
        """Attach the cross-venue monitor and scope it to registered markets.

        The engine owns the condition -> asset mapping: the monitor only polls
        the conditions this engine quotes, and each condition's YES token gives
        the Polymarket mid the basis is measured against.
        """
        self.kalshi_monitor = monitor
        if monitor is not None and hasattr(monitor, "set_conditions"):
            monitor.set_conditions(self.markets.keys())
        logger.info(
            "Kalshi basis monitor attached"
            if monitor is not None
            else "Kalshi basis monitor detached"
        )

    def _load_fill_model_coef(self) -> tuple[float, float, float]:
        """Load empirical logistic clearance coefficients from data/derived/fill_model.json."""
        default_coef = (1.9969502915862272, -3.966170297458595, -0.21375437197755692)
        cfg_path = getattr(self.anti_delusion_config, "fill_model_path", "data/derived/fill_model.json")
        p = Path(cfg_path)
        if p.exists():
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                coef = data.get("logistic", {}).get("coef")
                if coef and len(coef) == 3:
                    return (float(coef[0]), float(coef[1]), float(coef[2]))
            except Exception:
                pass
        return default_coef

    def compute_fill_probability(self, queue_ratio: float, size_at_level: float) -> float:
        """Measured print-conditional P(fill) for a resting quote from empirical logistic model."""
        ratio = min(1.0, max(0.0, float(queue_ratio)))
        size = max(0.0, float(size_at_level))
        b0, b1, b2 = self._fill_model_coef
        z = b0 + b1 * ratio + b2 * math.log1p(size)
        return 1.0 / (1.0 + math.exp(-max(-60.0, min(60.0, z))))

    def update_binance_spot(self, symbol: str, price: float, timestamp: Optional[float] = None) -> None:
        """Feed live Binance spot tick into lead-lag alpha gate."""
        if self.binance_feed is None:
            self.binance_feed = BinanceSpotFeed(symbol=symbol)
        self.binance_feed.update_price(price, timestamp=timestamp)

    def update_gas_environment(
        self,
        base_fee_gwei: float = 35.0,
        priority_fee_gwei: float = 45.0,
        pol_usd_rate: float = 0.35,
    ) -> None:
        """Update live Polygon PoS EIP-1559 gas station environment."""
        self.pos_env = PolygonPoSEnvironment(
            base_fee_gwei=float(base_fee_gwei),
            priority_fee_gwei=float(priority_fee_gwei),
            pol_usd_rate=float(pol_usd_rate),
        )

    def get_dynamic_gas_fee_usd(self, action: str = "merge", k_legs: int = 2) -> float:
        """Calculate dynamic Polygon PoS EIP-1559 gas fee for CTF on-chain actions."""
        if action == "merge":
            gas_units = CTFGasProfile.gas_merge_direct_typical  # 78,000
        elif action == "redeem":
            gas_units = 82_000  # CTF.redeemPositions
        elif action == "convert":
            gas_units = 85_000 + max(0, k_legs - 3) * 15_000  # NegRiskAdapter.convertPositions
        else:
            gas_units = 78_000
        return round(float(self.pos_env.gas_units_to_usd(gas_units)), 6)

    def compute_polymarket_taker_fee(self, shares: float, price: float) -> float:
        """Exact non-linear Polymarket taker fee: C * 0.04 * (p * (1 - p))."""
        if shares <= 0.0 or not (0.0 <= price <= 1.0):
            return 0.0
        curve = price * (1.0 - price)
        rate = getattr(self.anti_delusion_config, "taker_fee_rate", 0.04)
        raw = shares * rate * curve
        return round(float(raw), 6)

    def compute_market_impact(self, price: float, size: float, depth: float, side: str) -> float:
        """Concave Kyle's Lambda price impact: P_entry = P0 +- 0.004 * (Size / Depth)^1.2."""
        if size <= 0.0:
            return round(price, 4)
        depth_safe = max(1.0, float(depth))
        kyle_lambda = getattr(self.anti_delusion_config, "kyle_lambda", 0.004)
        kyle_exponent = getattr(self.anti_delusion_config, "kyle_exponent", 1.2)
        impact = kyle_lambda * ((size / depth_safe) ** kyle_exponent)

        if str(side).strip().upper() in ("BUY", "BID"):
            p_entry = min(0.999, price + impact)
        else:
            p_entry = max(0.001, price - impact)
        return round(p_entry, 4)

    def get_capital_utilization(self) -> dict[str, float]:
        """Track active capital utilization and unencumbered cash."""
        positions_val = sum(abs(p.shares) * p.avg_cost for p in self.positions.values())
        bids_notional = 0.0
        for quotes in self.virtual_quotes.values():
            q_bid = quotes.get("bid")
            if q_bid and q_bid.post_only_valid:
                bids_notional += q_bid.price * q_bid.size

        locked_cash = self.locked_settlement_cash_usd
        available_cash = max(0.0, self.cash_usd - bids_notional)
        encumbered = positions_val + bids_notional + locked_cash
        positions_market_val = sum(p.market_value for p in self.positions.values())
        total_equity = max(1.0, self.cash_usd + positions_market_val + locked_cash)
        utilization_rate = min(1.0, encumbered / total_equity)

        return {
            "total_equity_usd": round(total_equity, 2),
            "cash_usd": round(self.cash_usd, 2),
            "available_cash_usd": round(available_cash, 2),
            "locked_settlement_cash_usd": round(locked_cash, 2),
            "open_bids_notional_usd": round(bids_notional, 2),
            "positions_cost_basis_usd": round(positions_val, 2),
            "total_encumbered_capital_usd": round(encumbered, 2),
            "utilization_rate": round(utilization_rate, 4),
        }

    def update_kalshi_basis(self, asset_id: str, basis: Optional[float]) -> None:
        """Store the latest basis for one asset; ``None`` means stale or absent."""
        self._kalshi_basis[asset_id] = None if basis is None else float(basis)

    def _pm_yes_mid(self, condition_id: str) -> Optional[float]:
        """Polymarket YES mid for a condition, from its registered YES book."""
        yes_asset = self._condition_yes_asset.get(condition_id)
        if yes_asset is not None:
            book = self.orderbooks.get(yes_asset)
            if book:
                return float(book["mid"])
        # No YES token registered: reconstruct YES from the NO token's mid.
        for asset_id, cid in self.asset_to_condition.items():
            if cid != condition_id:
                continue
            outcome = str(self.asset_to_outcome.get(asset_id, "")).strip().upper()
            book = self.orderbooks.get(asset_id)
            if outcome == "NO" and book:
                return 1.0 - float(book["mid"])
        return None

    def _refresh_kalshi_basis(self, asset_id: str, condition_id: str) -> None:
        """Pull one asset's fresh basis from the attached monitor."""
        monitor = self.kalshi_monitor
        if monitor is None:
            if not self._kalshi_missing_logged:
                logger.warning(
                    "Orchestrator signal=kalshi has no Kalshi monitor attached; "
                    "keeping both sides until one is attached"
                )
                self._kalshi_missing_logged = True
            self.update_kalshi_basis(asset_id, None)
            return
        self._kalshi_missing_logged = False
        basis = monitor.basis(condition_id, self._pm_yes_mid(condition_id))
        outcome = str(self.asset_to_outcome.get(asset_id, "")).strip().upper()
        if basis is not None and outcome == "NO":
            # The condition's YES probability falling lifts this NO token's
            # price, so the threatened side flips for the NO quote.
            basis = -basis
        self.update_kalshi_basis(asset_id, basis)

    def _on_risk_unquote(self, asset_id: Optional[str], reason: str) -> None:
        self.cancel_quotes(asset_id)

    def cancel_quotes(self, asset_id: Optional[str] = None) -> None:
        """Fast-cancel virtual resting quotes across one or all markets."""
        if asset_id:
            if asset_id in self.virtual_quotes:
                self.virtual_quotes[asset_id] = {"bid": None, "ask": None}
        else:
            for aid in self.virtual_quotes:
                self.virtual_quotes[aid] = {"bid": None, "ask": None}
        logger.info(f"Quotes cancelled for {'all assets' if not asset_id else asset_id}")

    def cancel_quotes_for_condition(self, condition_id: str) -> None:
        """Cancel all resting virtual quotes for tokens belonging to a specific condition."""
        mkt = self.markets.get(condition_id)
        if not mkt:
            return
        for tok in mkt.tokens:
            if tok.token_id in self.virtual_quotes:
                self.virtual_quotes[tok.token_id] = {"bid": None, "ask": None}
        logger.info(f"Cancelled all resting quotes for market condition {condition_id[:12]}")

    def update_orderbook(
        self,
        asset_id: str,
        bids: list[dict[str, Any]] | list[list[str]],
        asks: list[dict[str, Any]] | list[list[str]],
        tick_size: float = 0.01,
    ) -> None:
        """Update order book snapshot and recalculate resting virtual quotes."""
        if asset_id not in self.asset_to_condition:
            return

        def parse_price(x: Any) -> float:
            if isinstance(x, dict):
                return float(x.get("price", 0.0))
            return float(x[0]) if isinstance(x, (list, tuple)) else 0.0

        valid_bids = [b for b in bids if _parse_price(b) > 0.0]
        valid_asks = [a for a in asks if _parse_price(a) > 0.0]

        best_bid = max((_parse_price(b) for b in valid_bids), default=0.0)
        best_ask = min((_parse_price(a) for a in valid_asks), default=1.0)

        if best_bid > 0.0 and best_ask < 1.0 and best_ask > best_bid:
            mid = (best_bid + best_ask) / 2.0
            spread = best_ask - best_bid
        elif best_bid > 0.0:
            mid = best_bid + 0.01
            spread = 0.02
        elif best_ask < 1.0:
            mid = best_ask - 0.01
            spread = 0.02
        else:
            mid = 0.50
            spread = 0.02

        cid = self.asset_to_condition[asset_id]
        mkt = self.markets[cid]

        # Risk monitor: check high-frequency volatility / price jump
        if self.risk_manager:
            self.risk_manager.update_market_price(asset_id, cid, mid)

        # Calculate competing depth score
        comp_score, b_usd, a_usd = compute_competing_depth_score(
            valid_bids, valid_asks, mid, mkt.rewards_max_spread
        )

        self.orderbooks[asset_id] = {
            "bids": valid_bids,
            "asks": valid_asks,
            "mid": round(mid, 4),
            "best_bid": round(best_bid, 4),
            "best_ask": round(best_ask, 4),
            "spread_cents": round(spread * 100.0, 2),
            "tick_size": tick_size,
            "competing_score": round(comp_score, 4),
            "qualifying_bids_usd": round(b_usd, 2),
            "qualifying_asks_usd": round(a_usd, 2),
            "last_updated": time.time(),
        }

        if self.microstructure_gate or (
            self.orchestrator is not None and self.orchestrator.config.enabled
        ):
            prev = self._prev_top.get(asset_id)
            if prev is not None:
                e_bid = 1 if best_bid > prev[0] else (-1 if best_bid < prev[0] else 0)
                e_ask = 1 if best_ask < prev[1] else (-1 if best_ask > prev[1] else 0)
                self._ofi_steps.setdefault(asset_id, deque()).append(
                    (time.time(), e_bid + e_ask)
                )
            self._prev_top[asset_id] = (best_bid, best_ask)

        if self.strategy is not None:
            from polymarketbot.strategy.base import BookContext

            self.strategy.on_book(
                BookContext(
                    condition_id=cid,
                    asset_id=asset_id,
                    outcome=str(self.asset_to_outcome.get(asset_id, "")),
                    mid=mid,
                    best_bid=best_bid,
                    best_ask=best_ask,
                    tick_size=tick_size,
                    spread_cents=spread * 100.0,
                )
            )

        # Update position mark-to-market
        if asset_id in self.positions:
            self.positions[asset_id].update_mark(mid)

        # Regenerate virtual quotes for this asset
        self.generate_quotes(asset_id)

    def _event_exposure(self, event_id: str | None) -> tuple[float, float]:
        """Sum market value and cost basis across every market in one event."""
        if not event_id:
            return 0.0, 0.0
        inventory = 0.0
        cost = 0.0
        for cid, config in self.markets.items():
            if config.event_id != event_id:
                continue
            for token in config.tokens:
                position = self.positions.get(token.token_id)
                if position is None:
                    continue
                inventory += abs(position.market_value)
                cost += abs(position.shares * position.avg_cost)
        return inventory, cost

    def _strategy_context(
        self,
        asset_id: str,
        book: dict[str, Any],
        queue: Optional[Any],
        ofi: Optional[float],
    ) -> Any:
        """Build the pure ``DecisionContext`` a module sees; no engine internals leak."""
        from polymarketbot.strategy.base import BookContext, DecisionContext

        cid = self.asset_to_condition[asset_id]
        ctx = BookContext(
            condition_id=cid,
            asset_id=asset_id,
            outcome=str(self.asset_to_outcome.get(asset_id, "")),
            mid=float(book["mid"]),
            best_bid=float(book["best_bid"]),
            best_ask=float(book["best_ask"]),
            tick_size=float(book.get("tick_size", 0.01)),
            spread_cents=float(book.get("spread_cents", 0.0)),
            competing_score=float(book.get("competing_score", 0.0)),
        )
        pos = self.positions.get(asset_id)
        return DecisionContext(
            book=ctx,
            seconds_elapsed=self.clock() - self._strategy_started,
            inventory_shares=float(pos.shares) if pos is not None else 0.0,
            max_inventory_shares=float(self.max_inventory_shares),
            bid_queue_ratio=float(queue.ratio("bid")) if queue is not None else 0.0,
            ask_queue_ratio=float(queue.ratio("ask")) if queue is not None else 0.0,
            bid_size_at_level=float(queue.bid_size_at_level) if queue is not None else 0.0,
            ask_size_at_level=float(queue.ask_size_at_level) if queue is not None else 0.0,
            ofi=ofi,
            realized_pnl=float(sum(p.realized_pnl for p in self.positions.values() if self.asset_to_condition.get(p.asset_id) == cid)),
            fills_count=int(sum(p.fills_count for p in self.positions.values() if self.asset_to_condition.get(p.asset_id) == cid)),
            quote_size=float(self.default_quote_size),
            bid_score=float(getattr(self.virtual_quotes.get(asset_id, {}).get("bid"), "score", 0.0) or 0.0),
            ask_score=float(getattr(self.virtual_quotes.get(asset_id, {}).get("ask"), "score", 0.0) or 0.0),
            rate_per_day=float(self.markets[self.asset_to_condition[asset_id]].rate_per_day),
            signals=self._signals_for(asset_id),
        )

    def _signals_for(self, asset_id: str) -> dict[str, float]:
        """Named exogenous signals for this asset at the current clock, or empty."""
        if self.signal_provider is None:
            return {}
        try:
            return dict(self.signal_provider(asset_id, self.clock()) or {})
        except Exception:  # noqa: BLE001 - a broken signal must not stop quoting
            return {}

    def _queue_snapshot(
        self,
        asset_id: str,
        bid_price: float,
        ask_price: float,
    ) -> Optional[QueueSnapshot]:
        """FIFO state of our quote inside its price level, per ``build_l3_queue``.

        The measured front/back split (``scripts/analyze_queue_positions.py``)
        is defined on ``queue_ratio = q_ahead / (q_ahead + q_behind)``: a quote
        joins behind the displayed size at its price (ratio 1.0) and moves toward
        the front as size arrives behind it, while a decrease consumes the front
        first. We replay exactly that state for the resting paper quotes, so the
        calibrated 0.60 threshold applies unchanged.
        """
        book = self.orderbooks.get(asset_id)
        if not book:
            return None
        existing = self.virtual_quotes.get(asset_id) or {}
        bid_ratio, bid_level = self._update_level_queue(
            asset_id, "bid", bid_price, book.get("bids", []), existing.get("bid")
        )
        ask_ratio, ask_level = self._update_level_queue(
            asset_id, "ask", ask_price, book.get("asks", []), existing.get("ask")
        )
        return QueueSnapshot(
            bid_queue_ratio=bid_ratio,
            bid_size_at_level=bid_level,
            ask_queue_ratio=ask_ratio,
            ask_size_at_level=ask_level,
        )

    def _update_level_queue(
        self,
        asset_id: str,
        side: str,
        price: float,
        levels: list[Any],
        old_quote: Optional[VirtualQuote],
    ) -> tuple[float, float]:
        tol = 1e-9
        level_size = sum(
            _parse_size(level)
            for level in levels
            if abs(_parse_price(level) - price) <= tol
        )
        retained = old_quote is not None and abs(old_quote.price - price) <= tol
        prev = self._level_queue.get((asset_id, side))
        if not retained or prev is None or abs(prev[0] - price) > tol:
            q_ahead, q_behind = level_size, 0.0
        else:
            _, q_ahead, q_behind, last_size = prev
            delta = level_size - last_size
            if delta > 0.0:
                q_behind += delta
            elif delta < 0.0:
                q_ahead = max(0.0, q_ahead + delta)
        total = q_ahead + q_behind
        ratio = q_ahead / total if total > 0.0 else 0.0
        self._level_queue[(asset_id, side)] = (price, q_ahead, q_behind, level_size)
        return ratio, level_size

    def generate_quotes(self, asset_id: str) -> None:
        """Simulate virtual resting limit orders with queue priority and inventory skewing."""
        if asset_id not in self.asset_to_condition:
            return

        if self.risk_manager:
            if (
                self.risk_manager.portfolio_state.is_halted
                or self.risk_manager.volatility_monitor.is_in_cooldown(asset_id)
            ):
                self.virtual_quotes[asset_id] = {"bid": None, "ask": None}
                return

        cid = self.asset_to_condition[asset_id]
        mkt = self.markets[cid]
        book = self.orderbooks.get(asset_id)
        if not book:
            return

        mid = book["mid"]
        best_bid = book["best_bid"]
        best_ask = book["best_ask"]
        tick = book.get("tick_size", 0.01)
        pos = self.positions[asset_id]
        inv = pos.shares

        # Inventory Skewing:
        inv_ratio = max(-1.0, min(1.0, inv / self.max_inventory_shares))
        skew_cents = inv_ratio * self.max_skew_cents

        s_bid_target = max(0.2, self.base_spread_offset_cents + skew_cents)
        s_ask_target = max(0.2, self.base_spread_offset_cents - skew_cents)

        # Raw desired limit prices
        raw_bid_price = mid - (s_bid_target / 100.0)
        raw_ask_price = mid + (s_ask_target / 100.0)

        # Size logic: ensure at least rewards_min_size
        quote_size = max(mkt.rewards_min_size, self.default_quote_size)

        # Round to valid tick size
        bid_price = round(math.floor(raw_bid_price / tick) * tick, 4)
        ask_price = round(math.ceil(raw_ask_price / tick) * tick, 4)

        # Post-Only Logic Enforcement:
        post_only_bid = True
        if bid_price >= best_ask:
            bid_price = round(min(best_bid, best_ask - tick), 4)
            post_only_bid = (bid_price < best_ask)

        post_only_ask = True
        if ask_price <= best_bid:
            ask_price = round(max(best_ask, best_bid + tick), 4)
            post_only_ask = (ask_price > best_bid)

        # Predictive price-only OFI gate (legacy): the trailing order-flow sign
        # says which resting side the next trade is about to run over. The L3
        # A/B (29 asset-hours, 780 fills) flips the net per share positive with
        # this gate and no look-ahead; the live engine's size-weighted 850/1.5c
        # thresholds did not.
        #
        # The queue-aware orchestrator supersedes that gate when enabled: it
        # keeps both sides at the front of the queue (ratio < 0.60) and only
        # shrinks or pulls the threatened side at the back, where the measured
        # fill markout is -2.01 c/share. Disabled by default.
        gate_bid_pull = False
        gate_ask_pull = False
        bid_scale = 1.0
        ask_scale = 1.0
        strategy_bid_price: Optional[float] = None
        strategy_ask_price: Optional[float] = None
        if self.strategy is not None:
            ofi = None
            steps = self._ofi_steps.get(asset_id)
            if steps:
                cutoff = self.clock() - self.microstructure_ofi_window_s
                vals = [step for ts, step in steps if ts >= cutoff]
                if vals:
                    ofi = sum(vals) / len(vals)
            queue_snap = self._queue_snapshot(asset_id, bid_price, ask_price)
            decision = self.strategy.decide(
                self._strategy_context(asset_id, book, queue_snap, ofi)
            )
            self.last_orchestrator_decision[asset_id] = decision
            gate_bid_pull = decision.bid == "PULL"
            gate_ask_pull = decision.ask == "PULL"
            bid_scale = 0.0 if gate_bid_pull else float(decision.bid_scale)
            ask_scale = 0.0 if gate_ask_pull else float(decision.ask_scale)
            strategy_bid_price = getattr(decision, "bid_price", None)
            strategy_ask_price = getattr(decision, "ask_price", None)
            # A module may pin its own price (join a level instead of improving
            # the spread). Tick-round it and re-apply the post-only clamp, so the
            # price that rests is the price the module chose.
            if strategy_bid_price is not None:
                bid_price = round(round(float(strategy_bid_price) / tick) * tick, 4)
                if bid_price >= best_ask:
                    bid_price = round(min(best_bid, best_ask - tick), 4)
                post_only_bid = bid_price < best_ask
            if strategy_ask_price is not None:
                ask_price = round(round(float(strategy_ask_price) / tick) * tick, 4)
                if ask_price <= best_bid:
                    ask_price = round(max(best_ask, best_bid + tick), 4)
                post_only_ask = ask_price > best_bid

        orchestrator_active = (
            self.strategy is None
            and self.orchestrator is not None
            and self.orchestrator.config.enabled
        )
        if orchestrator_active and self.orchestrator.config.signal == "kalshi":
            self._refresh_kalshi_basis(asset_id, cid)
            # No monitor means no basis: keep both sides rather than treat a
            # missing venue as a zero basis. _refresh logs the warning once.
            orchestrator_active = self.kalshi_monitor is not None
        if orchestrator_active:
            ofi = None
            steps = self._ofi_steps.get(asset_id)
            if steps:
                cutoff = self.clock() - self.microstructure_ofi_window_s
                vals = [step for ts, step in steps if ts >= cutoff]
                if vals:
                    ofi = sum(vals) / len(vals)
            queue_snap = self._queue_snapshot(asset_id, bid_price, ask_price)
            bin_bps = self.binance_feed.get_velocity_bps(5.0) if self.binance_feed is not None else None
            decision = self.orchestrator.decide(
                ofi, queue_snap, binance_bps=bin_bps, kalshi_basis=self._kalshi_basis.get(asset_id)
            )
            self.last_orchestrator_decision[asset_id] = decision
            gate_bid_pull = decision.bid.action == "PULL"
            gate_ask_pull = decision.ask.action == "PULL"
            bid_scale = 0.0 if gate_bid_pull else decision.bid.size_scale
            ask_scale = 0.0 if gate_ask_pull else decision.ask.size_scale
        elif self.microstructure_gate:
            steps = self._ofi_steps.get(asset_id)
            if steps:
                cutoff = self.clock() - self.microstructure_ofi_window_s
                vals = [step for ts, step in steps if ts >= cutoff]
                if vals:
                    ofi = sum(vals) / len(vals)
                    if ofi > self.microstructure_ofi_threshold:
                        gate_ask_pull = True
                    elif ofi < -self.microstructure_ofi_threshold:
                        gate_bid_pull = True

        # Binance Lead-Lag Alpha Gate pull sentinel
        if self.binance_feed is not None:
            vel_bps = self.binance_feed.get_velocity_bps(5.0)
            if vel_bps > self.binance_threshold_bps:
                gate_ask_pull = True
            elif vel_bps < -self.binance_threshold_bps:
                gate_bid_pull = True

        # If inventory is maxed out, pause buying or selling
        bid_size = quote_size * bid_scale if inv < self.max_inventory_shares else 0.0
        ask_size = quote_size * ask_scale if inv > -self.max_inventory_shares else 0.0
        if gate_bid_pull:
            bid_size = 0.0
        if gate_ask_pull:
            ask_size = 0.0

        # Delusion 8: Realistic Capital Utilization & Opportunity Gating
        if self.anti_delusion:
            avail_cash = max(0.0, self.cash_usd - self.locked_settlement_cash_usd)
            if getattr(self.anti_delusion_config, "prevent_infinite_compounding", True):
                cap_usd = getattr(self.anti_delusion_config, "max_market_capacity_usd", 5000.0)
                max_quote_shares = cap_usd / max(0.01, bid_price)
                bid_size = min(bid_size, max_quote_shares)
                ask_size = min(ask_size, max_quote_shares)
            if bid_size > 0 and (bid_size * bid_price) > avail_cash:
                bid_size = math.floor((avail_cash / max(0.01, bid_price)) / tick) * tick
                if bid_size < mkt.rewards_min_size:
                    bid_size = 0.0
        # A shrunk side stays at the reward-eligible minimum rather than below it.
        if 0.0 < bid_size < mkt.rewards_min_size:
            bid_size = mkt.rewards_min_size
        if 0.0 < ask_size < mkt.rewards_min_size:
            ask_size = mkt.rewards_min_size

        # Risk validation and asymmetric toxicity adjustments
        event_inv, event_cost = self._event_exposure(mkt.event_id)
        if self.risk_manager and bid_size > 0:
            pos_cost = abs(inv * pos.avg_cost)
            port_inv = sum(abs(p.market_value) for p in self.positions.values())
            ok, reason, adj_sz, adj_px = self.risk_manager.validate_quote(
                asset_id=asset_id,
                condition_id=cid,
                side="BUY",
                price=bid_price,
                size=bid_size,
                current_pos_shares=inv,
                current_pos_cost=pos_cost,
                current_portfolio_inventory_usd=port_inv,
                current_cash_usd=self.cash_usd,
                mid_price=mid,
                tick_size=tick,
                event_id=mkt.event_id,
                current_event_inventory_usd=event_inv,
                current_event_cost=event_cost,
            )
            if not ok:
                bid_size = 0.0
            else:
                bid_size = adj_sz
                bid_price = adj_px
                if bid_price >= best_ask:
                    bid_price = round(min(best_bid, best_ask - tick), 4)
                    post_only_bid = (bid_price < best_ask)

        if self.risk_manager and ask_size > 0:
            pos_cost = abs(inv * pos.avg_cost)
            port_inv = sum(abs(p.market_value) for p in self.positions.values())
            ok, reason, adj_sz, adj_px = self.risk_manager.validate_quote(
                asset_id=asset_id,
                condition_id=cid,
                side="SELL",
                price=ask_price,
                size=ask_size,
                current_pos_shares=inv,
                current_pos_cost=pos_cost,
                current_portfolio_inventory_usd=port_inv,
                current_cash_usd=self.cash_usd,
                mid_price=mid,
                tick_size=tick,
                event_id=mkt.event_id,
                current_event_inventory_usd=event_inv,
                current_event_cost=event_cost,
            )
            if not ok:
                ask_size = 0.0
            else:
                ask_size = adj_sz
                ask_price = adj_px
                if ask_price <= best_bid:
                    ask_price = round(max(best_ask, best_bid + tick), 4)
                    post_only_ask = (ask_price > best_bid)

        # Calculate actual spread offset in cents
        actual_bid_spread = round((mid - bid_price) * 100.0, 2)
        actual_ask_spread = round((ask_price - mid) * 100.0, 2)

        # Calculate quadratic reward scores S(v, s)
        v = mkt.rewards_max_spread
        bid_score = compute_quadratic_score(v, actual_bid_spread, bid_size) if post_only_bid else 0.0
        ask_score = compute_quadratic_score(v, actual_ask_spread, ask_size) if post_only_ask else 0.0

        existing_quotes = self.virtual_quotes.get(asset_id, {})
        old_bid = existing_quotes.get("bid")
        old_ask = existing_quotes.get("ask")

        book_bids = book.get("bids", [])
        book_asks = book.get("asks", [])

        # Update BID quote
        v_bid = None
        if bid_size > 0 and 0.001 <= bid_price < 1.0 and post_only_bid:
            if old_bid is not None and abs(old_bid.price - bid_price) < 1e-6:
                # Same price level: retain queue priority
                bid_queue_ahead = old_bid.queue_ahead
                if orchestrator_active:
                    # The policy sizes both sides explicitly; a released shrink
                    # must return to full size, a held shrink must stay down.
                    current_bid_size = bid_size
                else:
                    current_bid_size = old_bid.size if 0.0 < old_bid.size < bid_size else bid_size
            else:
                # Price changed or new quote: reset queue_ahead to competing volume resting at or better than quote price
                bid_queue_ahead = sum(
                    _parse_size(b) for b in book_bids
                    if _parse_price(b) >= bid_price - 1e-6
                )
                current_bid_size = bid_size

            bid_level = sum(_parse_size(b) for b in book_bids if abs(_parse_price(b) - bid_price) <= 1e-6)
            bid_ratio = bid_queue_ahead / max(1.0, bid_queue_ahead + bid_level)
            bid_fp = self.compute_fill_probability(bid_ratio, bid_level)

            v_bid = VirtualQuote(
                side="BUY",
                price=bid_price,
                size=current_bid_size,
                spread_from_mid_cents=actual_bid_spread,
                score=bid_score,
                post_only_valid=post_only_bid,
                queue_ahead=round(bid_queue_ahead, 4),
                timestamp=self.clock(),
                fill_probability=round(bid_fp, 4),
            )

        # Update ASK quote
        v_ask = None
        if ask_size > 0 and 0.001 < ask_price <= 0.999 and post_only_ask:
            if old_ask is not None and abs(old_ask.price - ask_price) < 1e-6:
                # Same price level: retain queue priority
                ask_queue_ahead = old_ask.queue_ahead
                if orchestrator_active:
                    current_ask_size = ask_size
                else:
                    current_ask_size = old_ask.size if 0.0 < old_ask.size < ask_size else ask_size
            else:
                # Price changed or new quote: reset queue_ahead to competing volume resting at or better than quote price
                ask_queue_ahead = sum(
                    _parse_size(a) for a in book_asks
                    if 0.0 < _parse_price(a) <= ask_price + 1e-6
                )
                current_ask_size = ask_size

            ask_level = sum(_parse_size(a) for a in book_asks if abs(_parse_price(a) - ask_price) <= 1e-6)
            ask_ratio = ask_queue_ahead / max(1.0, ask_queue_ahead + ask_level)
            ask_fp = self.compute_fill_probability(ask_ratio, ask_level)

            v_ask = VirtualQuote(
                side="SELL",
                price=ask_price,
                size=current_ask_size,
                spread_from_mid_cents=actual_ask_spread,
                score=ask_score,
                post_only_valid=post_only_ask,
                queue_ahead=round(ask_queue_ahead, 4),
                timestamp=self.clock(),
                fill_probability=round(ask_fp, 4),
            )

        self.virtual_quotes[asset_id] = {"bid": v_bid, "ask": v_ask}

    def on_trade(
        self,
        asset_id: str,
        trade_price: float,
        trade_size: float,
        trade_side: str,
        timestamp: Optional[float] = None,
    ) -> Optional[SimulatedFill]:
        """Process incoming trade and simulate fills adhering to FIFO queue priority.

        Args:
            asset_id: Polymarket asset/token ID.
            trade_price: Execution price of real trade.
            trade_size: Execution size in shares of real trade.
            trade_side: Aggressor / taker side ("BUY" or "SELL").
            timestamp: Trade timestamp (defaults to current time).
        """
        if asset_id not in self.asset_to_condition:
            return None

        if self.strategy is not None:
            from polymarketbot.strategy.base import TradeContext

            self.strategy.on_trade(
                TradeContext(
                    asset_id=asset_id,
                    price=float(trade_price),
                    size=float(trade_size),
                    side=str(trade_side),
                    seconds_elapsed=self.clock() - self._strategy_started,
                )
            )

        quotes = self.virtual_quotes.get(asset_id, {})
        v_bid = quotes.get("bid")
        v_ask = quotes.get("ask")
        ts = timestamp or time.time()
        self._last_trade_ts[asset_id] = ts

        # 0. Binance Lead-Lag Alpha Gate Sentinel (Delusion 2)
        if self.binance_feed is not None:
            vel_bps = self.binance_feed.get_velocity_bps(5.0)
            thresh = self.binance_threshold_bps
            if trade_side.upper() == "SELL" and vel_bps < -thresh and v_bid:
                logger.info(
                    f"[ALPHA GATE INTERCEPT] Pulled resting BUY quote on {asset_id[:10]}...: "
                    f"Binance velocity {vel_bps:.1f} bps < -{thresh:.1f} bps (toxic sweep prevented)"
                )
                self.virtual_quotes[asset_id]["bid"] = None
                v_bid = None
            elif trade_side.upper() == "BUY" and vel_bps > thresh and v_ask:
                logger.info(
                    f"[ALPHA GATE INTERCEPT] Pulled resting SELL quote on {asset_id[:10]}...: "
                    f"Binance velocity {vel_bps:.1f} bps > +{thresh:.1f} bps (toxic sweep prevented)"
                )
                self.virtual_quotes[asset_id]["ask"] = None
                v_ask = None

        executed_fill: Optional[SimulatedFill] = None

        # 1. Taker SELL against our resting limit BUY
        if trade_side.upper() == "SELL" and v_bid:
            # Crossed through our quote price
            if trade_price < v_bid.price - 1e-6:
                p_clear = 1.0
                if self.anti_delusion:
                    book_bids = self.orderbooks.get(asset_id, {}).get("bids", [])
                    standing_depth = sum(_parse_size(b) for b in book_bids if _parse_price(b) >= v_bid.price - 1e-6)
                    ratio = v_bid.queue_ahead / max(1.0, v_bid.queue_ahead + standing_depth)
                    p_clear = self.compute_fill_probability(ratio, standing_depth)
                    if getattr(self.anti_delusion_config, "deterministic_fills", False):
                        fill_sz = min(trade_size, v_bid.size) * max(0.1, p_clear)
                    else:
                        fill_sz = min(trade_size, v_bid.size) if self._rng.random() <= max(0.2, p_clear) else 0.0
                    if fill_sz <= 0.0:
                        logger.info(f"[FILL RATE DELUSION] Crossed through sell did not earn queue clearance (P={p_clear:.3f})")
                        return None
                else:
                    fill_sz = min(trade_size, v_bid.size)
                fill_px = v_bid.price
                v_bid.queue_ahead = 0.0
                trigger_reason = f"taker_sell_crossed_through_bid (px={trade_price} < quote={fill_px})"
                executed_fill = self._execute_buy_fill(asset_id, fill_px, fill_sz, trigger_reason, ts, fill_prob=p_clear)

            # Printed at our quote price level
            elif abs(trade_price - v_bid.price) <= 1e-6:
                if trade_size <= v_bid.queue_ahead:
                    v_bid.queue_ahead -= trade_size
                    logger.info(
                        f"[QUEUE DEPLETED] Bid @ {v_bid.price:.3f} queue ahead reduced by "
                        f"{trade_size:.1f} to {v_bid.queue_ahead:.1f}"
                    )
                    return None
                else:
                    surplus = trade_size - v_bid.queue_ahead
                    p_earn = 1.0
                    if self.anti_delusion:
                        book_bids = self.orderbooks.get(asset_id, {}).get("bids", [])
                        standing_depth = sum(_parse_size(b) for b in book_bids if abs(_parse_price(b) - v_bid.price) <= 1e-6)
                        if standing_depth <= 0:
                            standing_depth = v_bid.size
                        ratio = v_bid.queue_ahead / max(1.0, v_bid.queue_ahead + standing_depth)
                        p_clear = self.compute_fill_probability(ratio, standing_depth)
                        last_ts = self._last_trade_ts.get(asset_id, ts - 1.0)
                        dt = max(0.001, ts - last_ts)
                        v_clear = trade_size / dt
                        v_scale = min(1.2, 1.0 + 0.05 * math.log1p(v_clear / 100.0))
                        p_earn = min(1.0, max(0.01, p_clear * v_scale))
                        if getattr(self.anti_delusion_config, "deterministic_fills", False):
                            fill_sz = min(v_bid.size, surplus) * p_earn
                        else:
                            fill_sz = min(v_bid.size, surplus) if self._rng.random() <= p_earn else 0.0
                        if fill_sz <= 0.0:
                            logger.info(
                                f"[FILL RATE DELUSION] Surplus {surplus:.1f} did not earn fill (P={p_earn:.3f})"
                            )
                            v_bid.queue_ahead = 0.0
                            return None
                    else:
                        fill_sz = min(v_bid.size, surplus)
                    fill_px = v_bid.price
                    v_bid.queue_ahead = 0.0
                    trigger_reason = f"taker_sell_depleted_queue_bid (px={trade_price} == quote={fill_px}, surplus={surplus:.2f})"
                    executed_fill = self._execute_buy_fill(asset_id, fill_px, fill_sz, trigger_reason, ts, fill_prob=p_earn)

        # 2. Taker BUY against our resting limit SELL
        elif trade_side.upper() == "BUY" and v_ask:
            # Crossed through our quote price
            if trade_price > v_ask.price + 1e-6:
                p_clear = 1.0
                if self.anti_delusion:
                    book_asks = self.orderbooks.get(asset_id, {}).get("asks", [])
                    standing_depth = sum(_parse_size(a) for a in book_asks if _parse_price(a) <= v_ask.price + 1e-6)
                    ratio = v_ask.queue_ahead / max(1.0, v_ask.queue_ahead + standing_depth)
                    p_clear = self.compute_fill_probability(ratio, standing_depth)
                    if getattr(self.anti_delusion_config, "deterministic_fills", False):
                        fill_sz = min(trade_size, v_ask.size) * max(0.1, p_clear)
                    else:
                        fill_sz = min(trade_size, v_ask.size) if self._rng.random() <= max(0.2, p_clear) else 0.0
                    if fill_sz <= 0.0:
                        logger.info(f"[FILL RATE DELUSION] Crossed through buy did not earn queue clearance (P={p_clear:.3f})")
                        return None
                else:
                    fill_sz = min(trade_size, v_ask.size)
                fill_px = v_ask.price
                v_ask.queue_ahead = 0.0
                trigger_reason = f"taker_buy_crossed_through_ask (px={trade_price} > quote={fill_px})"
                executed_fill = self._execute_sell_fill(asset_id, fill_px, fill_sz, trigger_reason, ts, fill_prob=p_clear)

            # Printed at our quote price level
            elif abs(trade_price - v_ask.price) <= 1e-6:
                if trade_size <= v_ask.queue_ahead:
                    v_ask.queue_ahead -= trade_size
                    logger.info(
                        f"[QUEUE DEPLETED] Ask @ {v_ask.price:.3f} queue ahead reduced by "
                        f"{trade_size:.1f} to {v_ask.queue_ahead:.1f}"
                    )
                    return None
                else:
                    surplus = trade_size - v_ask.queue_ahead
                    p_earn = 1.0
                    if self.anti_delusion:
                        book_asks = self.orderbooks.get(asset_id, {}).get("asks", [])
                        standing_depth = sum(_parse_size(a) for a in book_asks if abs(_parse_price(a) - v_ask.price) <= 1e-6)
                        if standing_depth <= 0:
                            standing_depth = v_ask.size
                        ratio = v_ask.queue_ahead / max(1.0, v_ask.queue_ahead + standing_depth)
                        p_clear = self.compute_fill_probability(ratio, standing_depth)
                        last_ts = self._last_trade_ts.get(asset_id, ts - 1.0)
                        dt = max(0.001, ts - last_ts)
                        v_clear = trade_size / dt
                        v_scale = min(1.2, 1.0 + 0.05 * math.log1p(v_clear / 100.0))
                        p_earn = min(1.0, max(0.01, p_clear * v_scale))
                        if getattr(self.anti_delusion_config, "deterministic_fills", False):
                            fill_sz = min(v_ask.size, surplus) * p_earn
                        else:
                            fill_sz = min(v_ask.size, surplus) if self._rng.random() <= p_earn else 0.0
                        if fill_sz <= 0.0:
                            logger.info(
                                f"[FILL RATE DELUSION] Surplus {surplus:.1f} did not earn fill (P={p_earn:.3f})"
                            )
                            v_ask.queue_ahead = 0.0
                            return None
                    else:
                        fill_sz = min(v_ask.size, surplus)
                    fill_px = v_ask.price
                    v_ask.queue_ahead = 0.0
                    trigger_reason = f"taker_buy_depleted_queue_ask (px={trade_price} == quote={fill_px}, surplus={surplus:.2f})"
                    executed_fill = self._execute_sell_fill(asset_id, fill_px, fill_sz, trigger_reason, ts, fill_prob=p_earn)

        return executed_fill

    def _execute_buy_fill(
        self,
        asset_id: str,
        fill_px: float,
        fill_sz: float,
        trigger_reason: str,
        ts: float,
        fill_prob: float = 1.0,
    ) -> SimulatedFill:
        markout_drift = 0.0
        markout_haircut_usd = 0.0
        effective_px = fill_px
        if (
            self.anti_delusion
            and getattr(self.anti_delusion_config, "enable_markout_haircut", True)
            and not trigger_reason.startswith("taker_order:")
            and not trigger_reason.startswith("synthetic_cross_hedge")
            and not trigger_reason.startswith("broken_basket")
        ):
            drift_cents = getattr(self.anti_delusion_config, "markout_drift_cents", 0.5)
            markout_drift = drift_cents / 100.0
            markout_haircut_usd = fill_sz * markout_drift
            self.total_markout_haircut_usd += markout_haircut_usd
            effective_px = min(0.999, fill_px + markout_drift)

        fill_val = fill_sz * effective_px
        cid = self.asset_to_condition[asset_id]
        outcome = self.asset_to_outcome.get(asset_id, "unknown")
        pos = self.positions[asset_id]

        self.cash_usd -= fill_val
        old_shares = pos.shares
        old_cost = pos.avg_cost
        realized_pnl = 0.0

        if old_shares >= 0:
            new_shares = old_shares + fill_sz
            new_cost = ((old_shares * old_cost) + fill_val) / new_shares if new_shares > 0 else 0.0
        else:
            # Covering short position
            covered = min(abs(old_shares), fill_sz)
            realized_pnl = covered * (old_cost - effective_px)
            new_shares = old_shares + fill_sz
            if new_shares < 0:
                new_cost = old_cost
            elif new_shares > 0:
                new_cost = effective_px
            else:
                new_cost = 0.0

        pos.shares = new_shares
        pos.avg_cost = new_cost
        pos.realized_pnl += realized_pnl
        pos.fills_count += 1
        book_mid = self.orderbooks[asset_id]["mid"]
        pos.update_mark(book_mid)

        executed_fill = SimulatedFill(
            fill_id=str(uuid.uuid4())[:8],
            timestamp=ts,
            condition_id=cid,
            asset_id=asset_id,
            outcome=outcome,
            side="BUY",
            fill_price=round(effective_px, 4),
            fill_size=fill_sz,
            fill_value_usd=round(fill_val, 4),
            cash_delta=round(-fill_val, 4),
            position_shares_after=new_shares,
            avg_cost_after=round(new_cost, 4),
            realized_pnl=round(realized_pnl, 4),
            trigger_reason=trigger_reason,
            markout_drift=round(markout_drift, 4),
            markout_haircut_usd=round(markout_haircut_usd, 4),
            fill_probability=round(fill_prob, 4),
        )
        self.fills.append(executed_fill)
        logger.info(
            f"[SIMULATED FILL] BUY {fill_sz:.1f} {outcome} @ ${effective_px:.3f} (${fill_val:.2f}) | "
            f"New Inventory: {new_shares:.1f} sh | Cash: ${self.cash_usd:,.2f}"
        )

        if self.risk_manager:
            self.risk_manager.record_fill(asset_id, cid, "BUY", fill_px, fill_sz, ts)
            summary = self.get_portfolio_summary()
            self.risk_manager.update_portfolio_pnl(
                current_equity=summary["total_equity_usd"],
                realized_pnl=summary["realized_pnl_usd"],
                unrealized_pnl=summary["unrealized_pnl_usd"],
                current_cash=self.cash_usd,
                positions_market_value=summary["positions_market_value_usd"],
                timestamp=ts,
            )
            if self.risk_manager.portfolio_state.is_halted:
                self.cancel_quotes()

        # Regime B active synthetic cross-hedger to maintain delta neutrality
        if not trigger_reason.startswith("synthetic_cross_hedge"):
            self.check_and_execute_synthetic_hedge(cid, fill_px, ts)

        # Check automated CTF complete-set merge
        self.check_and_execute_merge(cid, timestamp=ts)

        # Update resting quote status
        v_bid = self.virtual_quotes.get(asset_id, {}).get("bid")
        if v_bid:
            if fill_sz >= v_bid.size - 1e-6:
                self.virtual_quotes[asset_id]["bid"] = None
            else:
                v_bid.size -= fill_sz

        self.generate_quotes(asset_id)
        return executed_fill

    def _execute_sell_fill(
        self,
        asset_id: str,
        fill_px: float,
        fill_sz: float,
        trigger_reason: str,
        ts: float,
        fill_prob: float = 1.0,
    ) -> SimulatedFill:
        markout_drift = 0.0
        markout_haircut_usd = 0.0
        effective_px = fill_px
        if (
            self.anti_delusion
            and getattr(self.anti_delusion_config, "enable_markout_haircut", True)
            and not trigger_reason.startswith("taker_order:")
            and not trigger_reason.startswith("synthetic_cross_hedge")
            and not trigger_reason.startswith("broken_basket")
        ):
            drift_cents = getattr(self.anti_delusion_config, "markout_drift_cents", 0.5)
            markout_drift = drift_cents / 100.0
            markout_haircut_usd = fill_sz * markout_drift
            self.total_markout_haircut_usd += markout_haircut_usd
            effective_px = max(0.001, fill_px - markout_drift)

        fill_val = fill_sz * effective_px
        cid = self.asset_to_condition[asset_id]
        outcome = self.asset_to_outcome.get(asset_id, "unknown")
        pos = self.positions[asset_id]

        self.cash_usd += fill_val
        old_shares = pos.shares
        old_cost = pos.avg_cost
        realized_pnl = 0.0

        if old_shares > 0:
            # Closing long position
            closed = min(old_shares, fill_sz)
            realized_pnl = closed * (effective_px - old_cost)
            new_shares = old_shares - fill_sz
            if new_shares > 0:
                new_cost = old_cost
            elif new_shares < 0:
                new_cost = effective_px
            else:
                new_cost = 0.0
        else:
            new_shares = old_shares - fill_sz
            new_cost = ((abs(old_shares) * old_cost) + fill_val) / abs(new_shares) if new_shares != 0 else 0.0

        pos.shares = new_shares
        pos.avg_cost = new_cost
        pos.realized_pnl += realized_pnl
        pos.fills_count += 1
        book_mid = self.orderbooks[asset_id]["mid"]
        pos.update_mark(book_mid)

        executed_fill = SimulatedFill(
            fill_id=str(uuid.uuid4())[:8],
            timestamp=ts,
            condition_id=cid,
            asset_id=asset_id,
            outcome=outcome,
            side="SELL",
            fill_price=round(effective_px, 4),
            fill_size=fill_sz,
            fill_value_usd=round(fill_val, 4),
            cash_delta=round(fill_val, 4),
            position_shares_after=new_shares,
            avg_cost_after=round(new_cost, 4),
            realized_pnl=round(realized_pnl, 4),
            trigger_reason=trigger_reason,
            markout_drift=round(markout_drift, 4),
            markout_haircut_usd=round(markout_haircut_usd, 4),
            fill_probability=round(fill_prob, 4),
        )
        self.fills.append(executed_fill)
        logger.info(
            f"[SIMULATED FILL] SELL {fill_sz:.1f} {outcome} @ ${effective_px:.3f} (${fill_val:.2f}) | "
            f"New Inventory: {new_shares:.1f} sh | Cash: ${self.cash_usd:,.2f}"
        )

        if self.risk_manager:
            self.risk_manager.record_fill(asset_id, cid, "SELL", fill_px, fill_sz, ts)
            summary = self.get_portfolio_summary()
            self.risk_manager.update_portfolio_pnl(
                current_equity=summary["total_equity_usd"],
                realized_pnl=summary["realized_pnl_usd"],
                unrealized_pnl=summary["unrealized_pnl_usd"],
                current_cash=self.cash_usd,
                positions_market_value=summary["positions_market_value_usd"],
                timestamp=ts,
            )
            if self.risk_manager.portfolio_state.is_halted:
                self.cancel_quotes()

        # Regime B active synthetic cross-hedger to maintain delta neutrality
        if not trigger_reason.startswith("synthetic_cross_hedge"):
            self.check_and_execute_synthetic_hedge(cid, fill_px, ts)

        # Check automated CTF complete-set merge
        self.check_and_execute_merge(cid, timestamp=ts)

        # Update resting quote status
        v_ask = self.virtual_quotes.get(asset_id, {}).get("ask")
        if v_ask:
            if fill_sz >= v_ask.size - 1e-6:
                self.virtual_quotes[asset_id]["ask"] = None
            else:
                v_ask.size -= fill_sz

        self.generate_quotes(asset_id)
        return executed_fill

    def _get_yes_no_positions(self, condition_id: str) -> tuple[Optional[Position], Optional[Position]]:
        """Retrieve YES and NO positions for a condition if both exist."""
        mkt = self.markets.get(condition_id)
        if not mkt:
            return None, None
        yes_pos = None
        no_pos = None
        for tok in mkt.tokens:
            outcome = tok.outcome.strip().upper()
            if outcome == "YES":
                yes_pos = self.positions.get(tok.token_id)
            elif outcome == "NO":
                no_pos = self.positions.get(tok.token_id)
        if (yes_pos is None or no_pos is None) and len(mkt.tokens) == 2:
            p0 = self.positions.get(mkt.tokens[0].token_id)
            p1 = self.positions.get(mkt.tokens[1].token_id)
            return p0, p1
        return yes_pos, no_pos

    def check_and_execute_synthetic_hedge(
        self,
        condition_id: str,
        fill_price: float,
        timestamp: Optional[float] = None,
    ) -> Optional[SimulatedFill]:
        """Evaluate and execute Regime B active synthetic cross-hedge to maintain delta neutrality."""
        if not self.risk_manager or not self.risk_manager.config.enable_synthetic_hedging:
            return None

        yes_pos, no_pos = self._get_yes_no_positions(condition_id)
        if not yes_pos or not no_pos:
            return None

        # Net directional inventory q = YES - NO
        net_q = yes_pos.shares - no_pos.shares
        if abs(net_q) < self.risk_manager.config.q_urgency_threshold_shares:
            return None

        # If net_q > 0 (long YES surplus): complementary asset to buy is NO
        # If net_q < 0 (long NO surplus): complementary asset to buy is YES
        comp_pos = no_pos if net_q > 0 else yes_pos
        comp_aid = comp_pos.asset_id
        comp_book = self.orderbooks.get(comp_aid)
        if not comp_book:
            return None

        comp_ask = comp_book.get("best_ask", 1.0)
        should_cross, reason, target_size = self.risk_manager.evaluate_synthetic_hedge(
            current_inventory_q=net_q,
            fill_price=fill_price,
            complementary_best_ask=comp_ask,
        )

        if not should_cross or target_size <= 0.0:
            return None

        hedge_val = target_size * comp_ask
        if self.cash_usd < hedge_val:
            target_size = max(0.0, self.cash_usd / max(1e-4, comp_ask))
            if target_size < 1.0:
                return None

        logger.info(
            f"[SYNTHETIC HEDGE TRIGGERED] {reason} | Buying {target_size:.1f} {comp_pos.outcome} @ {comp_ask:.3f}"
        )
        return self._execute_buy_fill(
            asset_id=comp_aid,
            fill_px=comp_ask,
            fill_sz=target_size,
            trigger_reason=f"synthetic_cross_hedge:{reason}",
            ts=timestamp if timestamp is not None else time.time(),
        )

    def check_and_execute_merge(
        self,
        condition_id: str,
        timestamp: Optional[float] = None,
    ) -> Optional[MergeEvent]:
        """Check if condition has reached auto_merge_threshold complete sets and execute merge."""
        yes_pos, no_pos = self._get_yes_no_positions(condition_id)
        if not yes_pos or not no_pos:
            return None

        yes_shares = max(0.0, yes_pos.shares)
        no_shares = max(0.0, no_pos.shares)
        mergeable = min(yes_shares, no_shares)

        threshold = self.auto_merge_threshold
        if self.risk_manager:
            threshold = self.risk_manager.config.min_merge_threshold_shares
            can_m, _ = self.risk_manager.can_merge(mergeable)
            if not can_m:
                return None

        if mergeable >= threshold:
            return self.execute_merge(condition_id, amount=mergeable, timestamp=timestamp)
        return None

    def execute_merge(
        self,
        condition_id: str,
        amount: Optional[float] = None,
        timestamp: Optional[float] = None,
    ) -> Optional[MergeEvent]:
        """Merge complete sets of YES + NO tokens into USDC cash, deducting gas fee."""
        yes_pos, no_pos = self._get_yes_no_positions(condition_id)
        if not yes_pos or not no_pos:
            return None

        yes_shares = max(0.0, yes_pos.shares)
        no_shares = max(0.0, no_pos.shares)
        mergeable = min(yes_shares, no_shares)

        merge_amount = min(mergeable, amount) if amount is not None else mergeable
        if merge_amount <= 0.0:
            return None

        # 1 YES + 1 NO = $1.00 USDC cash collateral
        unlocked_usd = merge_amount * 1.00
        if self.anti_delusion and getattr(self.anti_delusion_config, "dynamic_polygon_gas", True):
            gas_fee = self.get_dynamic_gas_fee_usd("merge")
        else:
            gas_fee = self.merge_gas_fee_usd

        # Cash delta: +unlocked USDC - gas fee
        self.cash_usd += (unlocked_usd - gas_fee)
        self.total_gas_fees_usd += gas_fee
        self.total_merges_count += 1

        if self.risk_manager:
            self.risk_manager.record_merge(merge_amount, gas_fee)

        # Realize PnL from merging:
        merged_cost = merge_amount * (yes_pos.avg_cost + no_pos.avg_cost)
        net_merge_pnl = unlocked_usd - merged_cost - gas_fee
        yes_pos.realized_pnl += net_merge_pnl / 2.0
        no_pos.realized_pnl += net_merge_pnl / 2.0

        yes_pos.shares -= merge_amount
        no_pos.shares -= merge_amount

        if yes_pos.shares <= 1e-6:
            yes_pos.shares = 0.0
            yes_pos.avg_cost = 0.0
        if no_pos.shares <= 1e-6:
            no_pos.shares = 0.0
            no_pos.avg_cost = 0.0

        # Update marks
        if yes_pos.asset_id in self.orderbooks:
            yes_pos.update_mark(self.orderbooks[yes_pos.asset_id]["mid"])
        if no_pos.asset_id in self.orderbooks:
            no_pos.update_mark(self.orderbooks[no_pos.asset_id]["mid"])

        ts = timestamp or time.time()
        event = MergeEvent(
            timestamp=ts,
            condition_id=condition_id,
            shares_merged=round(merge_amount, 4),
            usdc_unlocked=round(unlocked_usd, 4),
            gas_fee_usd=round(gas_fee, 4),
            remaining_yes=round(yes_pos.shares, 4),
            remaining_no=round(no_pos.shares, 4),
            cash_after=round(self.cash_usd, 2),
        )
        self.merges.append(event)
        logger.info(
            f"[CTF MERGE] Merged {merge_amount:.1f} complete sets (YES/NO) in {condition_id[:10]}... | "
            f"Unlocked: +${unlocked_usd:.2f} | Gas Fee: -${gas_fee:.4f} | "
            f"Remaining: YES={yes_pos.shares:.1f}, NO={no_pos.shares:.1f} | Cash: ${self.cash_usd:,.2f}"
        )
        return event

    def execute_taker_order(
        self,
        asset_id: str,
        side: str,
        size: float,
        max_price: Optional[float] = None,
        timestamp: Optional[float] = None,
        relayer_latency_ms: float = 120.0,
    ) -> Optional[SimulatedFill]:
        """Execute an aggressive taker order with latency decay, market impact, and taker fee."""
        if asset_id not in self.asset_to_condition or size <= 0.0:
            return None

        side_clean = side.strip().upper()
        ts = timestamp or self.clock()

        # Delusion 3: Latency Taker Sniping Delusion
        p_snipe = 1.0
        if self.anti_delusion:
            tau = max(10.0, float(relayer_latency_ms))
            decay_rate = getattr(self.anti_delusion_config, "latency_decay_rate", 15.0)
            max_prob = getattr(self.anti_delusion_config, "max_taker_snipe_prob", 0.05)
            # P(snipe) = min(0.05, exp(-decay_rate * tau / 1000.0))
            p_snipe = min(max_prob, math.exp(-decay_rate * (tau / 1000.0)))
            if self._rng.random() > p_snipe:
                logger.info(
                    f"[TAKER SNIPING DELUSION] Taker snipe of {size:.1f} {side_clean} on {asset_id[:10]} "
                    f"missed due to relayer latency ({tau:.1f}ms, P(snipe)={p_snipe:.4f} <= 5%)"
                )
                return None

        book = self.orderbooks.get(asset_id, {})
        b_bid = book.get("best_bid", 0.0)
        b_ask = book.get("best_ask", 1.0)
        p0 = b_ask if side_clean in ("BUY", "BID") else b_bid

        # Available depth at top of book
        if side_clean in ("BUY", "BID"):
            depth = sum(_parse_size(a) for a in book.get("asks", [])[:3])
        else:
            depth = sum(_parse_size(b) for b in book.get("bids", [])[:3])
        depth = max(10.0, depth)

        # Delusion 7: Non-linear Kyle's Lambda market impact
        p_entry = self.compute_market_impact(p0, size, depth, side_clean)
        slippage_usd = abs(p_entry - p0) * size

        if max_price is not None:
            if side_clean in ("BUY", "BID") and p_entry > max_price:
                logger.info(f"Taker BUY rejected: impact price {p_entry:.4f} > limit {max_price:.4f}")
                return None
            if side_clean in ("SELL", "ASK") and p_entry < max_price:
                logger.info(f"Taker SELL rejected: impact price {p_entry:.4f} < limit {max_price:.4f}")
                return None

        # Delusion 6: Exact Polymarket taker fees
        taker_fee = self.compute_polymarket_taker_fee(size, p_entry)
        self.total_taker_fees_usd += taker_fee
        self.total_slippage_usd += slippage_usd

        trigger_reason = f"taker_order:{side_clean} (P0={p0:.3f}->P_entry={p_entry:.3f}, fee=${taker_fee:.4f})"
        if side_clean in ("BUY", "BID"):
            self.cash_usd -= taker_fee
            fill = self._execute_buy_fill(asset_id, p_entry, size, trigger_reason, ts, fill_prob=p_snipe)
        else:
            self.cash_usd -= taker_fee
            fill = self._execute_sell_fill(asset_id, p_entry, size, trigger_reason, ts, fill_prob=p_snipe)

        if fill:
            fill.taker_fee_usd = round(taker_fee, 4)
            fill.slippage_usd = round(slippage_usd, 4)

        return fill

    def execute_basket_orders(
        self,
        legs: list[dict[str, Any]],
        condition_id: Optional[str] = None,
        policy: Optional[BrokenBasketPolicy] = None,
        timestamp: Optional[float] = None,
    ) -> dict[str, Any]:
        """Execute multi-leg basket/complement orders sequentially, modeling joint bundle risk."""
        if not legs:
            return {"status": "EMPTY_BASKET", "bundle_probability": 0.0, "filled_legs": []}

        ts = timestamp or self.clock()
        active_policy = policy or getattr(self.anti_delusion_config, "broken_basket_policy", BrokenBasketPolicy.UNWIND_MARKET)
        filled_legs: list[SimulatedFill] = []

        # Calculate joint bundle fill probability: P_bundle = prod(p_leg)
        leg_probs: list[float] = []
        for leg in legs:
            aid = leg["asset_id"]
            sz = float(leg.get("size", 10.0))
            book = self.orderbooks.get(aid, {})
            side = str(leg.get("side", "BUY")).upper()
            if side in ("BUY", "BID"):
                depth = sum(_parse_size(a) for a in book.get("asks", [])[:2])
            else:
                depth = sum(_parse_size(b) for b in book.get("bids", [])[:2])
            depth = max(1.0, depth)
            p_leg = min(0.95, max(0.05, 1.0 - 0.25 * (sz / depth)))
            leg_probs.append(p_leg)

        bundle_p = 1.0
        for p in leg_probs:
            bundle_p *= p

        # Sequential execution
        for i, leg in enumerate(legs):
            p_leg = leg_probs[i]
            if self.anti_delusion and self._rng.random() > p_leg:
                logger.warning(
                    f"[BROKEN BASKET DETECTED] Leg {i} ({leg.get('asset_id')}) failed to fill "
                    f"(p_leg={p_leg:.3f}, bundle_P={bundle_p:.4f})"
                )
                resolution = self.handle_broken_basket(
                    condition_id=condition_id or self.asset_to_condition.get(leg["asset_id"], "unknown"),
                    filled_legs=filled_legs,
                    failed_leg_index=i,
                    policy=active_policy,
                    timestamp=ts,
                )
                return {
                    "status": "BROKEN_BASKET_RESOLVED",
                    "bundle_probability": round(bundle_p, 4),
                    "failed_leg_index": i,
                    "filled_legs_count": len(filled_legs),
                    "resolution": resolution,
                }

            fill = self.execute_taker_order(
                asset_id=leg["asset_id"],
                side=leg["side"],
                size=leg["size"],
                max_price=leg.get("max_price"),
                timestamp=ts,
            )
            if fill is None:
                resolution = self.handle_broken_basket(
                    condition_id=condition_id or self.asset_to_condition.get(leg["asset_id"], "unknown"),
                    filled_legs=filled_legs,
                    failed_leg_index=i,
                    policy=active_policy,
                    timestamp=ts,
                )
                return {
                    "status": "BROKEN_BASKET_RESOLVED",
                    "bundle_probability": round(bundle_p, 4),
                    "failed_leg_index": i,
                    "filled_legs_count": len(filled_legs),
                    "resolution": resolution,
                }
            filled_legs.append(fill)

        return {
            "status": "BUNDLE_FILLED",
            "bundle_probability": round(bundle_p, 4),
            "filled_legs_count": len(filled_legs),
            "filled_legs": [f.to_dict() for f in filled_legs],
        }

    def handle_broken_basket(
        self,
        condition_id: str,
        filled_legs: list[SimulatedFill],
        failed_leg_index: int,
        policy: Optional[BrokenBasketPolicy] = None,
        timestamp: Optional[float] = None,
    ) -> dict[str, Any]:
        """Resolve broken execution leg via market unwind or synthetic conversion."""
        active_policy = policy or getattr(self.anti_delusion_config, "broken_basket_policy", BrokenBasketPolicy.UNWIND_MARKET)
        ts = timestamp or self.clock()
        unwind_orders: list[dict[str, Any]] = []
        total_unwind_proceeds = 0.0
        total_unwind_loss = 0.0

        if active_policy == BrokenBasketPolicy.UNWIND_MARKET:
            for fill in filled_legs:
                aid = fill.asset_id
                opp_side = "SELL" if fill.side in ("BUY", "BID") else "BUY"
                book = self.orderbooks.get(aid, {})
                unwind_px = book.get("best_bid", 0.0) if opp_side == "SELL" else book.get("best_ask", 1.0)
                fee = self.compute_polymarket_taker_fee(fill.fill_size, unwind_px)
                proceeds = fill.fill_size * unwind_px - fee
                cost = fill.fill_value_usd
                loss = cost - proceeds if fill.side in ("BUY", "BID") else proceeds - cost

                if opp_side == "SELL":
                    self._execute_sell_fill(aid, unwind_px, fill.fill_size, "broken_basket_unwind", ts)
                else:
                    self._execute_buy_fill(aid, unwind_px, fill.fill_size, "broken_basket_unwind", ts)

                total_unwind_proceeds += proceeds
                total_unwind_loss += max(0.0, loss)
                unwind_orders.append({
                    "asset_id": aid,
                    "side": opp_side,
                    "size": fill.fill_size,
                    "price": unwind_px,
                    "fee_usd": round(fee, 4),
                })
            action_summary = f"Unwound {len(filled_legs)} partial legs at CLOB market bids"

        elif active_policy == BrokenBasketPolicy.SYNTHETIC_CONVERT:
            gas_cost = self.get_dynamic_gas_fee_usd("convert", k_legs=max(3, len(filled_legs) + 1))
            self.cash_usd -= gas_cost
            self.total_gas_fees_usd += gas_cost
            action_summary = f"Synthetically converted {len(filled_legs)} legs (gas: ${gas_cost:.4f})"
        else:
            action_summary = "Aborted execution and held partial inventory intact"

        event = {
            "timestamp": ts,
            "condition_id": condition_id,
            "failed_leg_index": failed_leg_index,
            "policy_applied": active_policy.value if hasattr(active_policy, "value") else str(active_policy),
            "filled_legs_count": len(filled_legs),
            "action_summary": action_summary,
            "unwind_orders": unwind_orders,
            "total_unwind_loss_usd": round(total_unwind_loss, 4),
        }
        self.broken_basket_events.append(event)
        return event

    def settle_market(
        self,
        condition_id: str,
        payout_numerators: dict[str, float] | list[float],
        resolution_timestamp: Optional[float] = None,
    ) -> PendingSettlement:
        """Resolve a market and queue capital for the mandatory 3.11-hour UMA settlement lockup."""
        res_ts = resolution_timestamp or self.clock()
        mkt = self.markets.get(condition_id)
        if not mkt:
            raise KeyError(f"Market {condition_id} not registered")

        gross_payout = 0.0
        shares_redeemed: dict[str, float] = {}

        payout_map: dict[str, float] = {}
        if isinstance(payout_numerators, dict):
            payout_map = {k: float(v) for k, v in payout_numerators.items()}
        elif isinstance(payout_numerators, (list, tuple)):
            for i, tok in enumerate(mkt.tokens):
                w = float(payout_numerators[i]) if i < len(payout_numerators) else 0.0
                payout_map[tok.token_id] = w

        for tok in mkt.tokens:
            aid = tok.token_id
            pos = self.positions.get(aid)
            if pos and pos.shares > 0:
                weight = payout_map.get(aid, payout_map.get(tok.outcome, 0.0))
                payout = pos.shares * weight
                gross_payout += payout
                shares_redeemed[aid] = pos.shares
                pos.shares = 0.0
                pos.avg_cost = 0.0
                pos.update_mark(weight)

        redemption_gas = self.get_dynamic_gas_fee_usd("redeem")
        net_payout = round(max(0.0, gross_payout - redemption_gas), 4)

        settlement_id = f"uma_settle_{str(uuid.uuid4())[:8]}"
        lockup_sec = getattr(self.anti_delusion_config, "lockup_duration_seconds", 11196.0) if self.anti_delusion else 0.0
        unlock_ts = res_ts + lockup_sec

        settlement = PendingSettlement(
            settlement_id=settlement_id,
            condition_id=condition_id,
            resolution_timestamp=res_ts,
            unlock_timestamp=unlock_ts,
            payout_usd=round(gross_payout, 4),
            redemption_gas_usd=round(redemption_gas, 4),
            net_payout_usd=round(net_payout, 4),
            payout_numerators=payout_map,
            shares_redeemed=shares_redeemed,
            is_unlocked=(not self.anti_delusion or lockup_sec <= 0),
            unlocked_at=res_ts if (not self.anti_delusion or lockup_sec <= 0) else None,
        )

        if self.anti_delusion and lockup_sec > 0:
            self.locked_settlement_cash_usd += net_payout
            logger.info(
                f"[UMA SETTLEMENT LOCKUP] Market {condition_id[:10]}... resolved. Net ${net_payout:.2f} "
                f"locked for 3.11h until {unlock_ts:.0f} (2.0h challenge + 1.11h queue delay)"
            )
        else:
            self.cash_usd += net_payout
            self.total_gas_fees_usd += redemption_gas

        self.pending_settlements.append(settlement)
        self.cancel_quotes_for_condition(condition_id)
        return settlement

    def process_pending_settlements(self, current_timestamp: Optional[float] = None) -> list[PendingSettlement]:
        """Release matured settlements whose 3.11-hour UMA challenge window has expired."""
        now_ts = current_timestamp or self.clock()
        unlocked: list[PendingSettlement] = []

        for s in self.pending_settlements:
            if not s.is_unlocked and now_ts >= s.unlock_timestamp:
                s.is_unlocked = True
                s.unlocked_at = now_ts
                self.cash_usd += s.net_payout_usd
                self.locked_settlement_cash_usd = max(0.0, self.locked_settlement_cash_usd - s.net_payout_usd)
                self.total_gas_fees_usd += s.redemption_gas_usd
                unlocked.append(s)
                logger.info(
                    f"[UMA SETTLEMENT UNLOCKED] 3.11h lockup expired for {s.condition_id[:10]}... "
                    f"Credited +${s.net_payout_usd:.2f} USDC to cash."
                )

        return unlocked

    def run_epoch_sample(self) -> list[EpochSnapshot]:
        """Simulate a 1-minute reward sampling epoch across all registered head pools."""
        self.epoch_count += 1
        now = time.time()
        self.process_pending_settlements(now)
        new_snapshots: list[EpochSnapshot] = []

        for cid, mkt in self.markets.items():
            # A market's daily pool is one pot that every qualifying order on
            # either token competes for. Paying it per token would double the
            # income of a market maker who quotes both sides of both tokens,
            # which is exactly the shape of every strategy this harness scores.
            token_rows: list[tuple[Any, float, float, float]] = []
            market_score = 0.0
            market_comp = 0.0
            for tok in mkt.tokens:
                asset_id = tok.token_id
                quotes = self.virtual_quotes.get(asset_id, {})
                v_bid = quotes.get("bid")
                v_ask = quotes.get("ask")
                book = self.orderbooks.get(asset_id, {})
                mid = book.get("mid", 0.50)
                comp_score = book.get("competing_score", 0.0)
                # The venue scores an order only if it rested at least 3.5 s in
                # the sampled minute; a quote replaced more often earns nothing.
                now_s = self.clock()
                bid_score = (
                    v_bid.score
                    if (v_bid and v_bid.post_only_valid and now_s - v_bid.timestamp >= MIN_REST_SECONDS)
                    else 0.0
                )
                ask_score = (
                    v_ask.score
                    if (v_ask and v_ask.post_only_valid and now_s - v_ask.timestamp >= MIN_REST_SECONDS)
                    else 0.0
                )
                v_score = normalise_two_sided(bid_score, ask_score)
                market_score += v_score
                market_comp += comp_score
                token_rows.append((tok, mid, v_score, comp_score))

            share_pct, epoch_payout, projected_daily = compute_epoch_payout(
                market_score, market_comp, mkt.rate_per_day
            )
            self.total_rewards_earned_usd += epoch_payout

            for tok, mid, v_score, comp_score in token_rows:
                asset_id = tok.token_id

                snap = EpochSnapshot(
                    epoch_index=self.epoch_count,
                    timestamp=now,
                    condition_id=cid,
                    asset_id=asset_id,
                    outcome=tok.outcome,
                    mid_price=mid,
                    virtual_score=v_score,
                    competing_score=comp_score,
                    pool_share_pct=share_pct,
                    epoch_payout_usd=epoch_payout,
                    projected_daily_payout_usd=projected_daily,
                )
                self.epoch_snapshots.append(snap)
                new_snapshots.append(snap)

        logger.info(
            f"[REWARD EPOCH #{self.epoch_count}] Sampled {len(new_snapshots)} tokens | "
            f"Epoch Rewards: +${sum(s.epoch_payout_usd for s in new_snapshots):.4f} | "
            f"Cumulative Rewards: ${self.total_rewards_earned_usd:.4f}"
        )

        # Persist ledger on epoch
        self.save_ledger()
        return new_snapshots

    def get_portfolio_summary(self) -> dict[str, Any]:
        """Calculate total equity, unrealized PnL, and ROI."""
        positions_market_value = sum(p.market_value for p in self.positions.values())
        unrealized_pnl = sum(p.unrealized_pnl for p in self.positions.values())
        realized_pnl = sum(p.realized_pnl for p in self.positions.values())

        total_equity = self.cash_usd + positions_market_value + self.locked_settlement_cash_usd
        net_trading_pnl = total_equity - self.initial_cash_usd
        total_pnl = net_trading_pnl + self.total_rewards_earned_usd
        roi_pct = (total_pnl / self.initial_cash_usd) * 100.0 if self.initial_cash_usd > 0 else 0.0

        # Calculate projected daily rewards across all markets
        latest_snaps = self.epoch_snapshots[-len(self.asset_to_condition):] if self.epoch_snapshots else []
        projected_daily_rewards = sum(s.projected_daily_payout_usd for s in latest_snaps)

        util = self.get_capital_utilization()
        summary = {
            "initial_cash_usd": round(self.initial_cash_usd, 2),
            "current_cash_usd": round(self.cash_usd, 2),
            "available_cash_usd": util["available_cash_usd"],
            "locked_settlement_cash_usd": util["locked_settlement_cash_usd"],
            "positions_market_value_usd": round(positions_market_value, 2),
            "unrealized_pnl_usd": round(unrealized_pnl, 2),
            "realized_pnl_usd": round(realized_pnl, 2),
            "net_trading_pnl_usd": round(net_trading_pnl, 2),
            "total_rewards_earned_usd": round(self.total_rewards_earned_usd, 4),
            "total_equity_usd": round(total_equity, 2),
            "total_pnl_usd": round(total_pnl, 2),
            "roi_pct": round(roi_pct, 4),
            "total_gas_fees_usd": round(self.total_gas_fees_usd, 4),
            "total_merges_count": self.total_merges_count,
            "total_taker_fees_usd": round(self.total_taker_fees_usd, 4),
            "total_markout_haircut_usd": round(self.total_markout_haircut_usd, 4),
            "total_slippage_usd": round(self.total_slippage_usd, 4),
            "capital_utilization_ratio": util["utilization_rate"],
            "pending_settlements_count": len([s for s in self.pending_settlements if not s.is_unlocked]),
            "anti_delusion_active": self.anti_delusion,
            "projected_daily_rewards_usd": round(projected_daily_rewards, 2),
            "projected_annual_rewards_usd": round(projected_daily_rewards * 365.0, 2),
        }
        if self.risk_manager:
            summary["risk_status"] = self.risk_manager.portfolio_state.status.value
            summary["kill_switch_active"] = self.risk_manager.portfolio_state.kill_switch_active
            summary["is_halted"] = self.risk_manager.portfolio_state.is_halted
        return summary

    def to_ledger_dict(self) -> dict[str, Any]:
        """Serialize complete engine state and metrics to JSON-compatible dictionary."""
        now = time.time()
        uptime = round(now - self.start_time, 1)

        markets_data: dict[str, Any] = {}
        for cid, mkt in self.markets.items():
            tokens_data: dict[str, Any] = {}
            for tok in mkt.tokens:
                aid = tok.token_id
                book = self.orderbooks.get(aid, {})
                quotes = self.virtual_quotes.get(aid, {})
                pos = self.positions.get(aid)

                v_bid = quotes.get("bid")
                v_ask = quotes.get("ask")

                # Get latest epoch snapshot for this token
                latest_snap = next(
                    (s for s in reversed(self.epoch_snapshots) if s.asset_id == aid), None
                )

                tokens_data[aid] = {
                    "outcome": tok.outcome,
                    "token_id": aid,
                    "mid_price": book.get("mid", 0.50),
                    "best_bid": book.get("best_bid", 0.0),
                    "best_ask": book.get("best_ask", 1.0),
                    "spread_cents": book.get("spread_cents", 0.0),
                    "virtual_quotes": {
                        "bid": v_bid.to_dict() if v_bid else None,
                        "ask": v_ask.to_dict() if v_ask else None,
                    },
                    "competing_depth": {
                        "competing_bids_usd": book.get("qualifying_bids_usd", 0.0),
                        "competing_asks_usd": book.get("qualifying_asks_usd", 0.0),
                        "competing_score": book.get("competing_score", 0.0),
                    },
                    "reward_metrics": {
                        "epoch_count": self.epoch_count,
                        "virtual_score": round((v_bid.score if v_bid else 0.0) + (v_ask.score if v_ask else 0.0), 4),
                        "competing_score": book.get("competing_score", 0.0),
                        "pool_share_pct": round(latest_snap.pool_share_pct, 4) if latest_snap else 0.0,
                        "epoch_payout_usd": round(latest_snap.epoch_payout_usd, 4) if latest_snap else 0.0,
                        "projected_daily_payout_usd": round(latest_snap.projected_daily_payout_usd, 2) if latest_snap else 0.0,
                    },
                    "position": pos.to_dict() if pos else {},
                }

            markets_data[cid] = {
                "question": mkt.question,
                "condition_id": cid,
                "rate_per_day": mkt.rate_per_day,
                "rewards_max_spread_cents": mkt.rewards_max_spread,
                "rewards_min_size": mkt.rewards_min_size,
                "tokens": tokens_data,
            }

        return {
            "system_info": {
                "version": "1.1.0",
                "status": "RUNNING",
                "start_time_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.start_time)),
                "last_updated_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
                "uptime_seconds": uptime,
                "elapsed_epochs": self.epoch_count,
                "active_markets_count": len(self.markets),
                "active_tokens_count": len(self.asset_to_condition),
            },
            "portfolio": self.get_portfolio_summary(),
            "capital_utilization": self.get_capital_utilization(),
            "markets": markets_data,
            "recent_fills": [f.to_dict() for f in self.fills[-50:]],
            "recent_merges": [m.to_dict() for m in self.merges[-50:]],
            "recent_epochs": [s.to_dict() for s in self.epoch_snapshots[-50:]],
            "recent_settlements": [s.to_dict() for s in self.pending_settlements[-50:]],
            "broken_basket_events": self.broken_basket_events[-50:],
        }

    def save_ledger(self, custom_path: Optional[str | Path] = None) -> Path:
        """Persist state and live metrics atomically to ledger.json."""
        target = Path(custom_path) if custom_path else self.ledger_path
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp_target = target.with_suffix(".tmp")

        data = self.to_ledger_dict()
        with open(tmp_target, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

        tmp_target.replace(target)
        return target
