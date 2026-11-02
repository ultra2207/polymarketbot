"""Synthetic Order Book for Polymarket paper trading and market impact simulation.

Ingests real Polymarket CLOB L2 snapshots, injects virtual maker quotes,
tracks combined real-time depth D_total(P) = D_real(P) + D_virtual(P), and
recomputes effective best bid, best ask, mid-price, and Stoikov micro-price.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from polymarketbot.logs import get_logger

logger = get_logger("simulator")

PRICE_PRECISION = 6
PRICE_EPSILON = 1e-7


def _norm_px(price: float) -> float:
    """Normalize price float to fixed decimal precision for hash key consistency."""
    return round(float(price), PRICE_PRECISION)


def _parse_level(item: Any) -> Tuple[float, float]:
    """Extract (price, size) tuple from diverse Polymarket API snapshot representations."""
    if isinstance(item, dict):
        px = float(item.get("price", 0.0))
        sz = float(item.get("size", 0.0))
        return _norm_px(px), max(0.0, float(sz))
    if isinstance(item, (list, tuple)) and len(item) >= 2:
        return _norm_px(float(item[0])), max(0.0, float(item[1]))
    if hasattr(item, "price") and hasattr(item, "size"):
        return _norm_px(float(getattr(item, "price"))), max(0.0, float(getattr(item, "size")))
    return 0.0, 0.0


@dataclass
class DepthInfo:
    """Real, virtual, and aggregated depth at a specific price level."""

    price: float
    real: float
    virtual: float
    total: float

    def to_dict(self) -> dict[str, float]:
        return {
            "price": self.price,
            "real": round(self.real, 4),
            "virtual": round(self.virtual, 4),
            "total": round(self.total, 4),
        }


@dataclass
class VirtualMakerQuote:
    """Internal representation of a virtual maker quote placed into the synthetic book."""

    quote_id: str
    side: str  # "BUY" or "SELL"
    price: float
    size: float
    timestamp: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "quote_id": self.quote_id,
            "side": self.side,
            "price": self.price,
            "size": round(self.size, 4),
            "timestamp": self.timestamp,
        }


class SyntheticOrderBook:
    """Synthetic L2 Order Book with virtual maker quote injection and depth tracking.

    Integrates real-world CLOB snapshots with our virtual resting limit orders.
    Calculates synthetic effective best bid/ask, effective mid-price, and
    depth-weighted Stoikov micro-price.
    """

    def __init__(self, asset_id: str = "", tick_size: float = 0.01) -> None:
        self.asset_id = asset_id
        self.tick_size = float(tick_size)

        # Real order book ladders: price -> real_size
        self._real_bids: dict[float, float] = {}
        self._real_asks: dict[float, float] = {}

        # Virtual maker quotes registry: quote_id -> VirtualMakerQuote
        self._virtual_quotes: dict[str, VirtualMakerQuote] = {}

        # Virtual aggregated depth: price -> virtual_size
        self._virtual_bids: dict[float, float] = {}
        self._virtual_asks: dict[float, float] = {}

        # Cached computed metrics
        self._last_snapshot_ts: float = 0.0
        self._best_bid_real: Optional[float] = None
        self._best_ask_real: Optional[float] = None
        self._virtual_bid: Optional[float] = None
        self._virtual_ask: Optional[float] = None
        self._best_bid_eff: Optional[float] = None
        self._best_ask_eff: Optional[float] = None
        self._mid_price_eff: Optional[float] = None
        self._micro_price: Optional[float] = None

    # -------------------------------------------------------------------------
    # Snapshot Ingestion
    # -------------------------------------------------------------------------

    def apply_snapshot(
        self,
        bids: Sequence[Any],
        asks: Sequence[Any],
        timestamp: Optional[float] = None,
    ) -> None:
        """Ingest real L2 order book snapshot from Polymarket CLOB.

        Args:
            bids: Iterable of bid price levels (dict, list, or tuple).
            asks: Iterable of ask price levels (dict, list, or tuple).
            timestamp: Snapshot timestamp in epoch seconds.
        """
        self._real_bids.clear()
        self._real_asks.clear()

        for b in bids:
            px, sz = _parse_level(b)
            if px > 0.0 and sz > 0.0:
                self._real_bids[px] = self._real_bids.get(px, 0.0) + sz

        for a in asks:
            px, sz = _parse_level(a)
            if px > 0.0 and sz > 0.0:
                self._real_asks[px] = self._real_asks.get(px, 0.0) + sz

        if timestamp is not None:
            self._last_snapshot_ts = float(timestamp)

        self._recompute_metrics()

    # -------------------------------------------------------------------------
    # Virtual Maker Quote Injection & Modification
    # -------------------------------------------------------------------------

    def inject_quote(
        self,
        side: str,
        price: float,
        size: float,
        quote_id: Optional[str] = None,
        timestamp: Optional[float] = None,
    ) -> str:
        """Inject a virtual maker quote into the synthetic order book.

        Args:
            side: "BUY" or "SELL".
            price: Quote price level.
            size: Quote size in shares.
            quote_id: Optional unique identifier (generated if omitted).
            timestamp: Epoch timestamp of quote insertion.

        Returns:
            quote_id: Active quote identifier.
        """
        norm_side = side.upper()
        if norm_side not in ("BUY", "SELL"):
            raise ValueError(f"Invalid quote side '{side}'. Must be 'BUY' or 'SELL'.")

        norm_px = _norm_px(price)
        norm_sz = max(0.0, float(size))
        qid = quote_id or str(uuid.uuid4())
        ts = float(timestamp) if timestamp is not None else 0.0

        # If modifying an existing quote, cancel previous contribution first
        if qid in self._virtual_quotes:
            self._remove_virtual_quote_depth(self._virtual_quotes[qid])

        if norm_sz > 0.0:
            quote = VirtualMakerQuote(
                quote_id=qid,
                side=norm_side,
                price=norm_px,
                size=norm_sz,
                timestamp=ts,
            )
            self._virtual_quotes[qid] = quote
            self._add_virtual_quote_depth(quote)
        else:
            self._virtual_quotes.pop(qid, None)

        self._recompute_metrics()
        return qid

    def cancel_quote(self, quote_id: str) -> bool:
        """Remove a virtual maker quote from the synthetic order book."""
        quote = self._virtual_quotes.pop(quote_id, None)
        if quote is None:
            return False

        self._remove_virtual_quote_depth(quote)
        self._recompute_metrics()
        return True

    def update_quote_size(self, quote_id: str, new_size: float) -> bool:
        """Update resting size of an existing virtual maker quote."""
        quote = self._virtual_quotes.get(quote_id)
        if quote is None:
            return False

        norm_sz = max(0.0, float(new_size))
        if norm_sz <= 0.0:
            return self.cancel_quote(quote_id)

        # Delta update to virtual level depth
        delta = norm_sz - quote.size
        quote.size = norm_sz

        v_map = self._virtual_bids if quote.side == "BUY" else self._virtual_asks
        v_map[quote.price] = max(0.0, v_map.get(quote.price, 0.0) + delta)
        if v_map[quote.price] <= PRICE_EPSILON:
            v_map.pop(quote.price, None)

        self._recompute_metrics()
        return True

    def clear_virtual_quotes(self) -> None:
        """Clear all virtual maker quotes from the order book."""
        self._virtual_quotes.clear()
        self._virtual_bids.clear()
        self._virtual_asks.clear()
        self._recompute_metrics()

    def _add_virtual_quote_depth(self, quote: VirtualMakerQuote) -> None:
        v_map = self._virtual_bids if quote.side == "BUY" else self._virtual_asks
        v_map[quote.price] = v_map.get(quote.price, 0.0) + quote.size

    def _remove_virtual_quote_depth(self, quote: VirtualMakerQuote) -> None:
        v_map = self._virtual_bids if quote.side == "BUY" else self._virtual_asks
        if quote.price in v_map:
            v_map[quote.price] = max(0.0, v_map[quote.price] - quote.size)
            if v_map[quote.price] <= PRICE_EPSILON:
                v_map.pop(quote.price, None)

    # -------------------------------------------------------------------------
    # Depth Queries & Real-time Level Tracking
    # -------------------------------------------------------------------------

    def get_depth(self, price: float, side: Optional[str] = None) -> DepthInfo:
        """Track real-time level depth: D_total(P) = D_real(P) + D_virtual(P).

        Args:
            price: Price level to inspect.
            side: Optional "BUY" or "SELL". If None, searches both sides.
        """
        norm_px = _norm_px(price)
        real_sz = 0.0
        virt_sz = 0.0

        if side is None:
            real_sz = self._real_bids.get(norm_px, 0.0) + self._real_asks.get(norm_px, 0.0)
            virt_sz = self._virtual_bids.get(norm_px, 0.0) + self._virtual_asks.get(norm_px, 0.0)
        elif side.upper() == "BUY":
            real_sz = self._real_bids.get(norm_px, 0.0)
            virt_sz = self._virtual_bids.get(norm_px, 0.0)
        elif side.upper() == "SELL":
            real_sz = self._real_asks.get(norm_px, 0.0)
            virt_sz = self._virtual_asks.get(norm_px, 0.0)
        else:
            raise ValueError(f"Invalid side '{side}'. Must be 'BUY' or 'SELL'.")

        return DepthInfo(
            price=norm_px,
            real=real_sz,
            virtual=virt_sz,
            total=real_sz + virt_sz,
        )

    def get_bid_depth(self, price: float) -> DepthInfo:
        """Return bid depth info D_total(P) = D_real(P) + D_virtual(P)."""
        return self.get_depth(price, side="BUY")

    def get_ask_depth(self, price: float) -> DepthInfo:
        """Return ask depth info D_total(P) = D_real(P) + D_virtual(P)."""
        return self.get_depth(price, side="SELL")

    def total_depth_at(self, price: float, side: str) -> float:
        """Convenience method returning scalar D_total(P)."""
        return self.get_depth(price, side=side).total

    # -------------------------------------------------------------------------
    # Level Depletion & Level Clearing
    # -------------------------------------------------------------------------

    def deplete_level(
        self,
        side: str,
        price: float,
        size: float,
    ) -> Tuple[float, bool]:
        """Deplete resting volume at price level P.

        Depletes real liquidity first, then virtual liquidity if needed.
        If the remaining volume at that price level reaches zero, wipes the
        level and pushes the book forward.

        Returns:
            Tuple of (amount_depleted, was_level_wiped).
        """
        norm_side = side.upper()
        norm_px = _norm_px(price)
        remaining_to_deplete = max(0.0, float(size))

        if norm_side == "BUY":
            real_map = self._real_bids
            virt_map = self._virtual_bids
        elif norm_side == "SELL":
            real_map = self._real_asks
            virt_map = self._virtual_asks
        else:
            raise ValueError(f"Invalid side '{side}'.")

        cur_real = real_map.get(norm_px, 0.0)
        cur_virt = virt_map.get(norm_px, 0.0)
        tot_avail = cur_real + cur_virt

        if tot_avail <= PRICE_EPSILON or remaining_to_deplete <= 0.0:
            return 0.0, False

        depleted = min(tot_avail, remaining_to_deplete)

        # Deplete real volume first
        if cur_real > 0.0:
            real_dep = min(cur_real, remaining_to_deplete)
            cur_real -= real_dep
            remaining_to_deplete -= real_dep
            if cur_real <= PRICE_EPSILON:
                real_map.pop(norm_px, None)
            else:
                real_map[norm_px] = cur_real

        # Deplete virtual volume next if surplus exists
        if remaining_to_deplete > 0.0 and cur_virt > 0.0:
            virt_dep = min(cur_virt, remaining_to_deplete)
            cur_virt -= virt_dep
            remaining_to_deplete -= virt_dep
            if cur_virt <= PRICE_EPSILON:
                virt_map.pop(norm_px, None)
            else:
                virt_map[norm_px] = cur_virt

            # Sync individual virtual quotes resting at this level
            self._sync_virtual_quotes_after_depletion(norm_side, norm_px, virt_dep)

        was_wiped = False
        if (cur_real + cur_virt) <= PRICE_EPSILON:
            was_wiped = True
            real_map.pop(norm_px, None)
            virt_map.pop(norm_px, None)

        self._recompute_metrics()
        return depleted, was_wiped

    def wipe_level(self, side: str, price: float) -> bool:
        """Wipes the price level completely from the book and pushes the book forward.

        Removes all real and virtual volume at price P.
        """
        norm_side = side.upper()
        norm_px = _norm_px(price)

        if norm_side == "BUY":
            existed = (norm_px in self._real_bids) or (norm_px in self._virtual_bids)
            self._real_bids.pop(norm_px, None)
            self._virtual_bids.pop(norm_px, None)
        elif norm_side == "SELL":
            existed = (norm_px in self._real_asks) or (norm_px in self._virtual_asks)
            self._real_asks.pop(norm_px, None)
            self._virtual_asks.pop(norm_px, None)
        else:
            raise ValueError(f"Invalid side '{side}'.")

        # Evict virtual quotes at this wiped level
        to_remove = [
            qid for qid, q in self._virtual_quotes.items()
            if q.side == norm_side and abs(q.price - norm_px) < PRICE_EPSILON
        ]
        for qid in to_remove:
            self._virtual_quotes.pop(qid, None)

        self._recompute_metrics()
        return existed

    def _sync_virtual_quotes_after_depletion(
        self,
        side: str,
        price: float,
        depleted_amount: float,
    ) -> None:
        """Deducts depleted virtual volume from the active virtual quote instances."""
        remaining_deduct = depleted_amount
        quotes_at_level = [
            q for q in self._virtual_quotes.values()
            if q.side == side and abs(q.price - price) < PRICE_EPSILON
        ]
        # Deduct in FIFO order of quote insertion
        quotes_at_level.sort(key=lambda q: q.timestamp)

        for q in quotes_at_level:
            if remaining_deduct <= 0.0:
                break
            deduct = min(q.size, remaining_deduct)
            q.size -= deduct
            remaining_deduct -= deduct
            if q.size <= PRICE_EPSILON:
                self._virtual_quotes.pop(q.quote_id, None)

    # -------------------------------------------------------------------------
    # Metric Recomputation & Effective Price Formulations
    # -------------------------------------------------------------------------

    def _recompute_metrics(self) -> None:
        """Recomputes synthetic effective best bid, best ask, mid-price, and micro-price.

        Effective Best Bid:
            max(best_bid_real, virtual_bid)
        Effective Best Ask:
            min(best_ask_real, virtual_ask)
        Effective Mid-Price:
            P_mid_eff = (max(best_bid_real, virtual_bid) + min(best_ask_real, virtual_ask)) / 2.0
        Stoikov Micro-Price:
            micro_price = (D_total(P_b) * P_a + D_total(P_a) * P_b) / (D_total(P_b) + D_total(P_a))
        """
        # 1. Real best levels
        real_bid_candidates = [p for p, sz in self._real_bids.items() if sz > PRICE_EPSILON]
        self._best_bid_real = max(real_bid_candidates) if real_bid_candidates else None

        real_ask_candidates = [p for p, sz in self._real_asks.items() if sz > PRICE_EPSILON]
        self._best_ask_real = min(real_ask_candidates) if real_ask_candidates else None

        # 2. Virtual best levels
        virt_bid_candidates = [p for p, sz in self._virtual_bids.items() if sz > PRICE_EPSILON]
        self._virtual_bid = max(virt_bid_candidates) if virt_bid_candidates else None

        virt_ask_candidates = [p for p, sz in self._virtual_asks.items() if sz > PRICE_EPSILON]
        self._virtual_ask = min(virt_ask_candidates) if virt_ask_candidates else None

        # 3. Synthetic Effective Best Bid
        if self._best_bid_real is not None and self._virtual_bid is not None:
            self._best_bid_eff = max(self._best_bid_real, self._virtual_bid)
        elif self._best_bid_real is not None:
            self._best_bid_eff = self._best_bid_real
        elif self._virtual_bid is not None:
            self._best_bid_eff = self._virtual_bid
        else:
            self._best_bid_eff = None

        # 4. Synthetic Effective Best Ask
        if self._best_ask_real is not None and self._virtual_ask is not None:
            self._best_ask_eff = min(self._best_ask_real, self._virtual_ask)
        elif self._best_ask_real is not None:
            self._best_ask_eff = self._best_ask_real
        elif self._virtual_ask is not None:
            self._best_ask_eff = self._virtual_ask
        else:
            self._best_ask_eff = None

        # 5. Effective Mid-Price: P_mid_eff = (best_bid_eff + best_ask_eff) / 2.0
        if self._best_bid_eff is not None and self._best_ask_eff is not None:
            self._mid_price_eff = round((self._best_bid_eff + self._best_ask_eff) / 2.0, PRICE_PRECISION)
        else:
            self._mid_price_eff = None

        # 6. Stoikov Micro-Price
        if self._best_bid_eff is not None and self._best_ask_eff is not None:
            q_b = self.get_bid_depth(self._best_bid_eff).total
            q_a = self.get_ask_depth(self._best_ask_eff).total
            denom = q_b + q_a
            if denom > PRICE_EPSILON:
                self._micro_price = round(
                    (q_b * self._best_ask_eff + q_a * self._best_bid_eff) / denom,
                    PRICE_PRECISION,
                )
            else:
                self._micro_price = self._mid_price_eff
        else:
            self._micro_price = None

    # -------------------------------------------------------------------------
    # Properties & Ladder Accessors
    # -------------------------------------------------------------------------

    @property
    def best_bid_real(self) -> Optional[float]:
        """Real CLOB best bid price."""
        return self._best_bid_real

    @property
    def best_ask_real(self) -> Optional[float]:
        """Real CLOB best ask price."""
        return self._best_ask_real

    @property
    def virtual_bid(self) -> Optional[float]:
        """Our virtual resting best bid price."""
        return self._virtual_bid

    @property
    def virtual_ask(self) -> Optional[float]:
        """Our virtual resting best ask price."""
        return self._virtual_ask

    @property
    def best_bid(self) -> Optional[float]:
        """Effective synthetic best bid: max(best_bid_real, virtual_bid)."""
        return self._best_bid_eff

    @property
    def best_ask(self) -> Optional[float]:
        """Effective synthetic best ask: min(best_ask_real, virtual_ask)."""
        return self._best_ask_eff

    @property
    def mid_price(self) -> Optional[float]:
        """Effective synthetic mid-price P_mid_eff = (best_bid + best_ask) / 2.0."""
        return self._mid_price_eff

    @property
    def micro_price(self) -> Optional[float]:
        """Stoikov micro-price incorporating combined D_total depth."""
        return self._micro_price

    def get_effective_bids(self) -> List[Tuple[float, float]]:
        """Consolidated synthetic bid ladder sorted descending by price [(P, D_total(P))]."""
        all_prices = set(self._real_bids.keys()).union(self._virtual_bids.keys())
        ladder: List[Tuple[float, float]] = []
        for p in sorted(all_prices, reverse=True):
            depth = self.get_bid_depth(p).total
            if depth > PRICE_EPSILON:
                ladder.append((p, round(depth, 4)))
        return ladder

    def get_effective_asks(self) -> List[Tuple[float, float]]:
        """Consolidated synthetic ask ladder sorted ascending by price [(P, D_total(P))]."""
        all_prices = set(self._real_asks.keys()).union(self._virtual_asks.keys())
        ladder: List[Tuple[float, float]] = []
        for p in sorted(all_prices):
            depth = self.get_ask_depth(p).total
            if depth > PRICE_EPSILON:
                ladder.append((p, round(depth, 4)))
        return ladder

    def to_dict(self) -> dict[str, Any]:
        """Serialize current synthetic book state to a structured dictionary."""
        return {
            "asset_id": self.asset_id,
            "best_bid_real": self._best_bid_real,
            "best_ask_real": self._best_ask_real,
            "virtual_bid": self._virtual_bid,
            "virtual_ask": self._virtual_ask,
            "best_bid_eff": self._best_bid_eff,
            "best_ask_eff": self._best_ask_eff,
            "mid_price_eff": self._mid_price_eff,
            "micro_price": self._micro_price,
            "effective_bids_top5": self.get_effective_bids()[:5],
            "effective_asks_top5": self.get_effective_asks()[:5],
            "virtual_quotes_count": len(self._virtual_quotes),
        }
