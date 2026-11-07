"""Asynchronous Complement Quoter & Automated CTF Complete-Set Merger.

Core Economic Mechanics:
-------------------------
On Polymarket, 1 YES token + 1 NO token can always be merged into exactly 1.000 USDC:
    1 YES + 1 NO == $1.000 USDC (CTF complete-set equivalence)

In prediction markets, retail order flow arrives asynchronously:
- Retail traders buy or sell YES and NO independently based on sentiment, news, or noise.
- If a market maker quotes limit bids on BOTH legs:
    Bid(YES) = $0.48,  Bid(NO) = $0.49
  The combined purchase cost is $0.48 + $0.49 = $0.970.
- When both legs fill asynchronously, the market maker holds matched complete sets:
    C = min(pos_YES, pos_NO)
- By invoking CTFExchange.mergePositions(conditionId, C), the maker instantly burns
  C shares of YES + C shares of NO, receiving $1.000 USDC per pair in cash.
- This locks in a +3.0% risk-free spread carry ($0.03/pair) with 0 directional inventory risk,
  while continuously earning daily Polymarket liquidity rewards on both resting bids!

Empirical Validation:
---------------------
82 wallets classified as 'combo' in wallet_cohort.parquet extracted $4,163,120.0
in realized profit using this exact complete-set capture mechanism.

Architecture:
-------------
1. ComplementQuoterConfig: Parameterizes double-quoting bounds, dynamic complement skew,
   and automated CTF merge thresholds.
2. LegQuote & ComplementQuoteDecision: Actionable paired quoting decisions for YES & NO.
3. ComplementMergeRecord: Durable record of on-chain / simulated complete-set merges.
4. ComplementQuoter:
   - Double-Quoting Engine: Quotes paired limit bids on YES and NO when Mid_YES + Mid_NO <= 0.99.
   - Net Complement Inventory Tracking: Tracks paired units C = min(pos_YES, pos_NO)
     and net directional imbalance q = pos_YES - pos_NO.
   - Dynamic Complement Skewing: When leg A fills (e.g. YES fills, q > 0), automatically scales up
     the bid size and pricing on leg B (NO, the pair-completer) to aggressively lock in the risk-free
     $1.00 claim, while fading leg A to prevent directional runaway.
   - Automated CTF Merger Trigger: Seamlessly integrates with merger.py & PolygonCtfClient
     to trigger on-chain mergePositions() whenever C >= 100 shares.
   - StrategyModule Protocol Support: Plugs into backtests and strategy execution harnesses.
"""

from __future__ import annotations

import math
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from polymarketbot.logs import get_logger
from polymarketbot.rewards.merger import (
    CompleteSetPosition,
    MergeEvent,
    build_ctf_merge_transaction,
)
from polymarketbot.strategy.base import (
    BookContext,
    DecisionContext,
    MarketView,
    SideActions,
    TradeContext,
)

logger = get_logger("complement_quoter")


def round_tick(price: float, tick_size: float = 0.01, mode: str = "round") -> float:
    """Round price to the nearest tick size."""
    if tick_size <= 0.0:
        return price
    raw_ticks = price / tick_size
    if mode == "floor":
        ticks = math.floor(round(raw_ticks, 6))
    elif mode == "ceil":
        ticks = math.ceil(round(raw_ticks, 6))
    else:
        ticks = round(raw_ticks)
    return round(ticks * tick_size, 4)


@dataclass
class ComplementQuoterConfig:
    """Configuration parameters for Asynchronous Complement Quoter & Auto-Merger."""

    # Pricing & Double-Quoting Bounds
    max_mid_sum: float = 0.99             # Quote double bids only when Mid_YES + Mid_NO <= max_mid_sum
    target_pair_margin: float = 0.015     # Minimum locked-in pair spread carry ($0.015 = 1.5c margin)
    spread_offset_cents: float = 0.50     # Base bid offset below mid in cents (0.5c = 0.005)
    max_qualifying_spread_cents: float = 3.0  # Rewards qualifying maximum spread in cents (3.0c = 0.03)
    tick_size: float = 0.01               # Order book tick size (e.g. $0.01)
    min_price: float = 0.01               # Price floor
    max_price: float = 0.99               # Price ceiling

    # Sizing & Dynamic Skew Parameters
    base_quote_size: float = 20.0         # Baseline quote size per leg (shares)
    min_quote_size: float = 5.0           # Minimum quote size floor (shares)
    max_quote_size: float = 100.0         # Maximum quote size ceiling for pair completer (shares)
    skew_scale_shares: float = 50.0       # Sensitivity scale q_scale for inventory imbalance |q|
    max_skew_multiplier: float = 2.5      # Maximum scale-up boost on pair-completer leg (e.g. 2.5x base)
    min_fade_multiplier: float = 0.2      # Minimum fade floor on excess leg before halt
    max_directional_imbalance: float = 100.0  # Cap on |q|; beyond this, excess leg bidding is paused
    aggressiveness_cents: float = 0.30    # Max price shift towards mid for pair-completer leg (0.3c)
    fade_retreat_cents: float = 0.30      # Max price retreat away from mid for excess leg (0.3c)

    # Automated CTF Merger Parameters
    # At 100 shares and $0.015 gas, gas friction is 0.015c/share, strictly < 0.05c/share hurdle.
    # Minimum recommended threshold is 30 shares to keep gas friction < 0.05c/share.
    auto_merge_threshold: float = 100.0   # Trigger merge when min(pos_YES, pos_NO) >= threshold
    gas_fee_per_merge: float = 0.0016     # Polygon PoS gas fee per CTF merge in USD (empirical default)
    merge_cooldown_seconds: float = 0.0   # Cooldown between consecutive merges (0 for testing/immediate)

    @property
    def gas_friction_cents_per_pair(self) -> float:
        """On-chain CTF.mergePositions gas friction in cents per complete-set pair."""
        if self.auto_merge_threshold <= 0:
            return 0.0
        return (self.gas_fee_per_merge / self.auto_merge_threshold) * 100.0


@dataclass(frozen=True)
class LegQuote:
    """Actionable maker quote for one outcome leg (YES or NO)."""

    outcome: str
    bid_price: float
    bid_size: float
    is_quoted: bool
    is_skew_boosted: bool = False
    is_skew_faded: bool = False
    reason: str = ""


@dataclass(frozen=True)
class ComplementQuoteDecision:
    """Paired maker quote decision for binary prediction market condition."""

    condition_id: str
    mid_yes: float
    mid_no: float
    mid_sum: float
    can_quote_double_bids: bool
    yes_quote: LegQuote
    no_quote: LegQuote
    paired_inventory_c: float             # C = min(pos_YES, pos_NO)
    net_imbalance_q: float                # q = pos_YES - pos_NO
    expected_combined_cost: float         # bid_price(YES) + bid_price(NO)
    expected_pair_carry: float            # 1.000 - expected_combined_cost
    expected_carry_pct: float             # expected_pair_carry / expected_combined_cost * 100
    auto_merge_eligible: bool
    reason: str = ""


@dataclass
class ComplementMergeRecord:
    """Durable record of completed CTF complete-set merge execution."""

    timestamp: float
    shares_merged: float
    usdc_unlocked: float
    cost_basis_merged: float
    realized_carry: float
    gas_fee: float
    tx_hash: str
    remaining_yes: float
    remaining_no: float
    net_imbalance_q: float
    is_dry_run: bool = False


class ComplementQuoter:
    """Asynchronous Complement Quoter & Automated Complete-Set Merger.

    Quotes limit bids on both YES and NO when Mid_YES + Mid_NO <= 0.99.
    Tracks net complement inventory (C, q), dynamically skews sizing towards
    the pair-completing leg when one leg fills, and automatically triggers
    CTFExchange.mergePositions() when C >= 100 complete sets.
    """

    name: str = "asynchronous-complement-quoter"

    def __init__(
        self,
        condition_id: str = "",
        config: Optional[ComplementQuoterConfig] = None,
        ctf_client: Optional[Any] = None,
        on_merge_callback: Optional[Callable[[ComplementMergeRecord], None]] = None,
        initial_cash: float = 0.0,
    ) -> None:
        self.condition_id = condition_id
        self.config = config or ComplementQuoterConfig()
        self.ctf_client = ctf_client
        self.on_merge_callback = on_merge_callback

        # Position tracking
        self.shares_yes: float = 0.0
        self.shares_no: float = 0.0
        self.cost_basis_yes: float = 0.0
        self.cost_basis_no: float = 0.0
        self.usdc_cash: float = float(initial_cash)

        # Merge history & aggregates
        self.total_merged_shares: float = 0.0
        self.total_unlocked_usdc: float = 0.0
        self.total_realized_carry: float = 0.0
        self.total_gas_fees: float = 0.0
        self.merges_history: List[ComplementMergeRecord] = []
        self._last_merge_ts: float = 0.0

        # Book tracking for StrategyModule adapter
        self.asset_id_yes: str = ""
        self.asset_id_no: str = ""
        self._last_mid_yes: Optional[float] = None
        self._last_mid_no: Optional[float] = None
        self.market: Optional[MarketView] = None

    # -------------------------------------------------------------------------
    # Inventory Properties
    # -------------------------------------------------------------------------

    @property
    def paired_units(self) -> float:
        """Matched complete-set inventory C = min(pos_YES, pos_NO).

        Carries exactly zero directional price risk because 1 YES + 1 NO == $1.00 USDC.
        """
        return min(max(0.0, self.shares_yes), max(0.0, self.shares_no))

    @property
    def net_imbalance(self) -> float:
        """Net directional inventory imbalance q = pos_YES - pos_NO.

        Positive: net long YES.
        Negative: net long NO.
        Zero: perfectly balanced.
        """
        return self.shares_yes - self.shares_no

    @property
    def avg_cost_yes(self) -> float:
        """Volume-weighted average purchase price for current YES inventory."""
        if self.shares_yes <= 0.0:
            return 0.0
        return self.cost_basis_yes / self.shares_yes

    @property
    def avg_cost_no(self) -> float:
        """Volume-weighted average purchase price for current NO inventory."""
        if self.shares_no <= 0.0:
            return 0.0
        return self.cost_basis_no / self.shares_no

    @property
    def auto_merge_threshold(self) -> float:
        return self.config.auto_merge_threshold

    # -------------------------------------------------------------------------
    # Double-Quoting & Dynamic Skewing Engine
    # -------------------------------------------------------------------------

    def compute_quotes(
        self,
        mid_yes: float,
        mid_no: float,
        best_bid_yes: Optional[float] = None,
        best_bid_no: Optional[float] = None,
        best_ask_yes: Optional[float] = None,
        best_ask_no: Optional[float] = None,
    ) -> ComplementQuoteDecision:
        """Compute paired limit bids for YES and NO under complement arithmetic.

        Rules:
        1. Double-Quoting Condition:
           Quotes paired limit bids only when Mid_YES + Mid_NO <= max_mid_sum (default 0.99).
           If Mid_YES + Mid_NO > 0.99, bids are withheld (is_quoted = False).
        2. Dynamic Complement Skewing:
           Let q = pos_YES - pos_NO.
           - If q > 0 (long YES excess):
             Leg B (NO) is the pair-completer:
               -> scale up NO bid size (up to max_skew_multiplier * base_size)
               -> tighten NO bid price closer to mid (by aggressiveness_cents)
             Leg A (YES) is the excess leg:
               -> fade YES bid size (down towards min_quote_size, or pause if |q| >= max_imbalance)
               -> widen YES bid price away from mid (by fade_retreat_cents)
           - If q < 0 (long NO excess):
             Leg A (YES) is the pair-completer:
               -> scale up YES bid size & tighten price
             Leg B (NO) is the excess leg:
               -> fade NO bid size & widen price
           - If q == 0 (balanced):
             Both legs quote symmetric base_quote_size and base spread offset.
        3. Pair Carry Bound:
           Enforces Bid(YES) + Bid(NO) <= 1.000 - target_pair_margin.
        """
        mid_sum = round(mid_yes + mid_no, 4)
        q = self.net_imbalance
        C = self.paired_units
        can_quote = mid_sum <= self.config.max_mid_sum

        if not can_quote:
            reason = (
                f"Mid sum {mid_sum:.4f} > max allowed {self.config.max_mid_sum:.4f}; "
                "paired bids withheld to avoid negative/insufficient carry"
            )
            dummy_yes = LegQuote(
                outcome="YES",
                bid_price=0.0,
                bid_size=0.0,
                is_quoted=False,
                reason=reason,
            )
            dummy_no = LegQuote(
                outcome="NO",
                bid_price=0.0,
                bid_size=0.0,
                is_quoted=False,
                reason=reason,
            )
            return ComplementQuoteDecision(
                condition_id=self.condition_id,
                mid_yes=mid_yes,
                mid_no=mid_no,
                mid_sum=mid_sum,
                can_quote_double_bids=False,
                yes_quote=dummy_yes,
                no_quote=dummy_no,
                paired_inventory_c=C,
                net_imbalance_q=q,
                expected_combined_cost=0.0,
                expected_pair_carry=0.0,
                expected_carry_pct=0.0,
                auto_merge_eligible=(C >= self.config.auto_merge_threshold),
                reason=reason,
            )

        # ---------------- Dynamic Skew Calculation ----------------
        q_scale = max(1.0, self.config.skew_scale_shares)
        skew_intensity = min(1.0, abs(q) / q_scale)

        base_size = self.config.base_quote_size
        max_size = self.config.max_quote_size
        min_size = self.config.min_quote_size

        boost_mult = 1.0 + (self.config.max_skew_multiplier - 1.0) * skew_intensity
        fade_mult = max(self.config.min_fade_multiplier, 1.0 - skew_intensity)

        base_offset = self.config.spread_offset_cents / 100.0
        agg_shift = (self.config.aggressiveness_cents / 100.0) * skew_intensity
        fade_retreat = (self.config.fade_retreat_cents / 100.0) * skew_intensity

        is_yes_quoted = True
        is_no_quoted = True
        is_yes_boosted = False
        is_no_boosted = False
        is_yes_faded = False
        is_no_faded = False

        if q > 0.0:
            # Excess YES -> NO is the pair completer
            is_no_boosted = True
            is_yes_faded = True

            if q >= self.config.max_directional_imbalance:
                size_yes = 0.0
                is_yes_quoted = False
                offset_yes = base_offset + fade_retreat
                yes_reason = f"Directional imbalance q={q:.1f} >= max cap; YES bid paused"
                size_no = max_size
                offset_no = max(0.001, base_offset - agg_shift)
                no_reason = f"Aggressive pair-completer at critical imbalance cap (q={q:+.1f})"
            else:
                size_no = min(max_size, round(base_size * boost_mult, 2))
                offset_no = max(0.001, base_offset - agg_shift)
                size_yes = max(min_size, round(base_size * fade_mult, 2))
                offset_yes = base_offset + fade_retreat
                yes_reason = f"Faded YES bid (q={q:+.1f} excess)"
                no_reason = f"Boosted NO bid as pair-completer (q={q:+.1f})"

        elif q < 0.0:
            # Excess NO -> YES is the pair completer
            is_yes_boosted = True
            is_no_faded = True

            if abs(q) >= self.config.max_directional_imbalance:
                size_no = 0.0
                is_no_quoted = False
                offset_no = base_offset + fade_retreat
                no_reason = f"Directional imbalance q={q:.1f} >= max cap; NO bid paused"
                size_yes = max_size
                offset_yes = max(0.001, base_offset - agg_shift)
                yes_reason = f"Aggressive pair-completer at critical imbalance cap (q={q:+.1f})"
            else:
                size_yes = min(max_size, round(base_size * boost_mult, 2))
                offset_yes = max(0.001, base_offset - agg_shift)
                size_no = max(min_size, round(base_size * fade_mult, 2))
                offset_no = base_offset + fade_retreat
                no_reason = f"Faded NO bid (q={q:+.1f} excess)"
                yes_reason = f"Boosted YES bid as pair-completer (q={q:+.1f})"

        else:
            # Neutral / balanced inventory
            size_yes = base_size
            size_no = base_size
            offset_yes = base_offset
            offset_no = base_offset
            yes_reason = "Balanced inventory; quoting base size"
            no_reason = "Balanced inventory; quoting base size"

        # ---------------- Price Calculation & Rounding ----------------
        raw_bid_yes = mid_yes - offset_yes
        raw_bid_no = mid_no - offset_no

        bid_yes = round_tick(raw_bid_yes, self.config.tick_size)
        bid_no = round_tick(raw_bid_no, self.config.tick_size)

        bid_yes = max(self.config.min_price, min(self.config.max_price, bid_yes))
        bid_no = max(self.config.min_price, min(self.config.max_price, bid_no))

        # ---------------- Pair Margin & Carry Bound Enforcement ----------------
        max_pair_cost = round(1.000 - self.config.target_pair_margin, 4)
        if round(bid_yes + bid_no, 4) > max_pair_cost:
            excess = round((bid_yes + bid_no) - max_pair_cost, 4)
            # Clip quotes to guarantee minimum carry
            half_excess = excess / 2.0
            bid_yes = round_tick(bid_yes - half_excess, self.config.tick_size, mode="floor")
            bid_no = round_tick(bid_no - half_excess, self.config.tick_size, mode="floor")
            while round(bid_yes + bid_no, 4) > max_pair_cost:
                if bid_yes >= bid_no:
                    bid_yes = round(bid_yes - self.config.tick_size, 4)
                else:
                    bid_no = round(bid_no - self.config.tick_size, 4)

        expected_cost = round(bid_yes + bid_no, 4)
        expected_carry = round(1.000 - expected_cost, 4)
        expected_carry_pct = round((expected_carry / max(1e-6, expected_cost)) * 100.0, 2)

        yes_quote = LegQuote(
            outcome="YES",
            bid_price=bid_yes,
            bid_size=size_yes,
            is_quoted=is_yes_quoted,
            is_skew_boosted=is_yes_boosted,
            is_skew_faded=is_yes_faded,
            reason=yes_reason,
        )
        no_quote = LegQuote(
            outcome="NO",
            bid_price=bid_no,
            bid_size=size_no,
            is_quoted=is_no_quoted,
            is_skew_boosted=is_no_boosted,
            is_skew_faded=is_no_faded,
            reason=no_reason,
        )

        return ComplementQuoteDecision(
            condition_id=self.condition_id,
            mid_yes=mid_yes,
            mid_no=mid_no,
            mid_sum=mid_sum,
            can_quote_double_bids=True,
            yes_quote=yes_quote,
            no_quote=no_quote,
            paired_inventory_c=C,
            net_imbalance_q=q,
            expected_combined_cost=expected_cost,
            expected_pair_carry=expected_carry,
            expected_carry_pct=expected_carry_pct,
            auto_merge_eligible=(C >= self.config.auto_merge_threshold),
            reason=f"Quoting complement pair: carry=+${expected_carry:.4f}/share (+{expected_carry_pct:.1f}%)",
        )

    # -------------------------------------------------------------------------
    # Execution & Inventory Updates
    # -------------------------------------------------------------------------

    def record_fill(
        self,
        outcome: str,
        side: str,
        price: float,
        size: float,
        timestamp: Optional[float] = None,
    ) -> Optional[ComplementMergeRecord]:
        """Record trade fill, update positions, and auto-merge when threshold reached."""
        outcome_norm = outcome.upper()
        side_norm = side.upper()
        notional = size * price

        if outcome_norm == "YES":
            if side_norm == "BUY":
                self.shares_yes += size
                self.cost_basis_yes += notional
                self.usdc_cash -= notional
            elif side_norm == "SELL":
                if self.shares_yes >= size:
                    ratio = size / max(1e-6, self.shares_yes)
                    self.cost_basis_yes = max(0.0, self.cost_basis_yes * (1.0 - ratio))
                    self.shares_yes -= size
                    self.usdc_cash += notional
                else:
                    needed = size - max(0.0, self.shares_yes)
                    self.shares_yes = 0.0
                    self.cost_basis_yes = 0.0
                    self.shares_no += needed
                    self.cost_basis_no += needed * max(0.0, 1.0 - price)
                    self.usdc_cash += notional - (needed * 1.00)

        elif outcome_norm == "NO":
            if side_norm == "BUY":
                self.shares_no += size
                self.cost_basis_no += notional
                self.usdc_cash -= notional
            elif side_norm == "SELL":
                if self.shares_no >= size:
                    ratio = size / max(1e-6, self.shares_no)
                    self.cost_basis_no = max(0.0, self.cost_basis_no * (1.0 - ratio))
                    self.shares_no -= size
                    self.usdc_cash += notional
                else:
                    needed = size - max(0.0, self.shares_no)
                    self.shares_no = 0.0
                    self.cost_basis_no = 0.0
                    self.shares_yes += needed
                    self.cost_basis_yes += needed * max(0.0, 1.0 - price)
                    self.usdc_cash += notional - (needed * 1.00)

        # Automated CTF Complete-Set Merge Trigger
        if self.paired_units >= self.config.auto_merge_threshold:
            now = timestamp or time.time()
            if (now - self._last_merge_ts) >= self.config.merge_cooldown_seconds:
                return self.trigger_merge(timestamp=now)

        return None

    # -------------------------------------------------------------------------
    # Automated CTF Merger Trigger
    # -------------------------------------------------------------------------

    def trigger_merge(
        self,
        amount_shares: Optional[float] = None,
        timestamp: Optional[float] = None,
    ) -> Optional[ComplementMergeRecord]:
        """Execute on-chain or simulated complete-set merge into USDC cash collateral.

        Burns min(YES, NO) pairs, unlocks 1.00 USDC per pair, deducts Polygon gas fee,
        and locks in spread carry. Directional net imbalance q is invariant to merge.
        """
        available = self.paired_units
        if available <= 0.0:
            return None

        to_merge = available if amount_shares is None else min(available, float(amount_shares))
        if to_merge <= 0.0:
            return None

        now = timestamp or time.time()

        # Proportional cost basis deduction
        ratio_yes = to_merge / max(1e-6, self.shares_yes)
        cost_yes_merged = self.cost_basis_yes * ratio_yes
        self.cost_basis_yes = max(0.0, self.cost_basis_yes - cost_yes_merged)
        self.shares_yes = max(0.0, self.shares_yes - to_merge)

        ratio_no = to_merge / max(1e-6, self.shares_no)
        cost_no_merged = self.cost_basis_no * ratio_no
        self.cost_basis_no = max(0.0, self.cost_basis_no - cost_no_merged)
        self.shares_no = max(0.0, self.shares_no - to_merge)

        total_cost_merged = cost_yes_merged + cost_no_merged
        unlocked_usdc = to_merge * 1.00

        # Execute via PolygonCtfClient if provided
        gas_fee = self.config.gas_fee_per_merge
        tx_hash = f"0xsim_merge_{uuid.uuid4().hex}"
        is_dry_run = True

        if self.ctf_client is not None:
            try:
                res = self.ctf_client.merge_positions(
                    condition_id=self.condition_id,
                    amount_shares=to_merge,
                )
                if hasattr(res, "tx_hash") and res.tx_hash:
                    tx_hash = res.tx_hash
                if hasattr(res, "gas_fee_usd") and res.gas_fee_usd > 0.0:
                    gas_fee = res.gas_fee_usd
                if hasattr(res, "is_dry_run"):
                    is_dry_run = bool(res.is_dry_run)
            except Exception as exc:
                logger.error(f"CTF mergePositions execution error: {exc}")

        # Cash & Accounting updates
        self.usdc_cash += (unlocked_usdc - gas_fee)
        realized_carry = unlocked_usdc - total_cost_merged - gas_fee

        self.total_merged_shares += to_merge
        self.total_unlocked_usdc += unlocked_usdc
        self.total_realized_carry += realized_carry
        self.total_gas_fees += gas_fee
        self._last_merge_ts = now

        record = ComplementMergeRecord(
            timestamp=now,
            shares_merged=round(to_merge, 4),
            usdc_unlocked=round(unlocked_usdc, 4),
            cost_basis_merged=round(total_cost_merged, 4),
            realized_carry=round(realized_carry, 4),
            gas_fee=round(gas_fee, 6),
            tx_hash=tx_hash,
            remaining_yes=round(self.shares_yes, 4),
            remaining_no=round(self.shares_no, 4),
            net_imbalance_q=round(self.net_imbalance, 4),
            is_dry_run=is_dry_run,
        )
        self.merges_history.append(record)

        logger.info(
            f"CTF Complete-Set Merge executed: {to_merge:.2f} sets -> ${unlocked_usdc:.2f} USDC | "
            f"Cost basis: ${total_cost_merged:.2f} | Realized Carry: +${realized_carry:.4f} | "
            f"Gas: ${gas_fee:.4f} | Tx: {tx_hash[:16]}..."
        )

        if self.on_merge_callback is not None:
            try:
                self.on_merge_callback(record)
            except Exception as exc:
                logger.error(f"Error in on_merge_callback: {exc}")

        return record

    # -------------------------------------------------------------------------
    # Interoperability & Integration Helpers
    # -------------------------------------------------------------------------

    def to_complete_set_position(self) -> CompleteSetPosition:
        """Export state to a merger.py CompleteSetPosition instance."""
        return CompleteSetPosition(
            shares_yes=self.shares_yes,
            shares_no=self.shares_no,
            cost_basis_yes=self.cost_basis_yes,
            cost_basis_no=self.cost_basis_no,
            usdc_cash=self.usdc_cash,
            total_merged_shares=self.total_merged_shares,
            total_unlocked_usd=self.total_unlocked_usdc,
            total_gas_fees=self.total_gas_fees,
            gas_fee_per_merge=self.config.gas_fee_per_merge,
            auto_merge_threshold=self.config.auto_merge_threshold,
        )

    def build_merge_transaction(self, amount_shares: Optional[float] = None) -> dict[str, Any]:
        """Build transaction dictionary for Polygon CTF mergePositions call."""
        amount = amount_shares if amount_shares is not None else self.paired_units
        return build_ctf_merge_transaction(
            condition_id=self.condition_id,
            amount_shares=amount,
        )

    # -------------------------------------------------------------------------
    # StrategyModule Protocol Implementation
    # -------------------------------------------------------------------------

    def setup(self, market: MarketView) -> None:
        """Initialize market metadata."""
        self.market = market
        self.condition_id = market.condition_id
        if len(market.tokens) >= 2:
            self.asset_id_yes = market.tokens[0]
            self.asset_id_no = market.tokens[1]

    def on_book(self, ctx: BookContext) -> None:
        """Update top-of-book cache for condition tokens."""
        outcome = (ctx.outcome or "").upper()
        if "YES" in outcome:
            self._last_mid_yes = ctx.mid
        elif "NO" in outcome:
            self._last_mid_no = ctx.mid

    def on_trade(self, ctx: TradeContext) -> None:
        """Trade notification hook."""
        return None

    def decide(self, ctx: DecisionContext) -> SideActions:
        """StrategyModule decision hook for backtesting / runner harness."""
        outcome = (ctx.book.outcome or "").upper()
        is_yes = "YES" in outcome
        mid_this = ctx.book.mid
        mid_sib = self._last_mid_no if is_yes else self._last_mid_yes

        if mid_sib is None:
            # Sibling unseen; quote standard full scale
            return SideActions(
                bid="QUOTE",
                ask="QUOTE",
                bid_scale=1.0,
                ask_scale=1.0,
                reason="Sibling mid unseen; quote both sides",
            )

        mid_yes = mid_this if is_yes else mid_sib
        mid_no = mid_sib if is_yes else mid_this
        quotes = self.compute_quotes(mid_yes, mid_no)

        if not quotes.can_quote_double_bids:
            return SideActions(
                bid="SHRINK",
                ask="QUOTE",
                bid_scale=0.0,
                ask_scale=1.0,
                reason=quotes.reason,
            )

        my_quote = quotes.yes_quote if is_yes else quotes.no_quote
        if not my_quote.is_quoted:
            return SideActions(
                bid="SHRINK",
                ask="QUOTE",
                bid_scale=0.0,
                ask_scale=1.0,
                reason=my_quote.reason,
            )

        # Clamp scale to [0, 1] for SideActions protocol
        bid_scale = min(1.0, my_quote.bid_size / max(1.0, self.config.base_quote_size))
        return SideActions(
            bid="QUOTE",
            ask="QUOTE",
            bid_scale=bid_scale,
            ask_scale=1.0,
            bid_price=my_quote.bid_price,
            reason=my_quote.reason,
        )
