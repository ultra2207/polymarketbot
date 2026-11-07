"""Production-grade Simulated Portfolio for Polymarket CTF Paper Trading.

Tracks:
1. Cash balance (starting with $100.00 default or configurable).
2. Token inventory (YES and NO shares), average cost basis, realized and unrealized PnL.
3. Real-world CTF Split and Merge Mechanics:
   - Split: splits 1.00 USDC into 1 YES + 1 NO.
   - Merge: converts min(YES, NO) complete sets back into $1.00 USDC cash, resetting exposure and deducting $0.015 Polygon PoS gas fees.
4. Comprehensive audit trail, transaction history, and balance verification.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from polymarketbot.logs import get_logger
from polymarketbot.trading.ctf import (
    calculate_optimal_merge_threshold,
    estimate_merge_cost_usd,
)

log = get_logger("simulator.portfolio")


class TransactionType(str, Enum):
    INITIAL_DEPOSIT = "INITIAL_DEPOSIT"
    FILL_BUY_MAKER = "FILL_BUY_MAKER"
    FILL_BUY_TAKER = "FILL_BUY_TAKER"
    FILL_SELL_MAKER = "FILL_SELL_MAKER"
    FILL_SELL_TAKER = "FILL_SELL_TAKER"
    CTF_SPLIT = "CTF_SPLIT"
    CTF_MERGE = "CTF_MERGE"
    REWARD_PAYOUT = "REWARD_PAYOUT"
    GAS_FEE = "GAS_FEE"


@dataclass
class PortfolioTransaction:
    """Audit trail record for every balance or position change."""

    tx_id: str
    timestamp: float
    tx_type: str
    condition_id: str
    token_side: str  # "YES", "NO", "SET", "CASH"
    price: float
    shares: float
    gross_amount: float
    fee: float
    net_cash_delta: float
    realized_pnl: float
    cash_after: float
    notes: str = ""
    maker_rebate: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Position:
    """Position state for a specific binary market condition."""

    condition_id: str
    shares_yes: float = 0.0
    shares_no: float = 0.0
    avg_cost_yes: float = 0.0
    avg_cost_no: float = 0.0
    realized_pnl: float = 0.0

    @property
    def net_exposure(self) -> float:
        """Net directional exposure: positive = Long YES, negative = Long NO."""
        return self.shares_yes - self.shares_no

    @property
    def complete_sets(self) -> float:
        """Number of complete pairs (1 YES + 1 NO) eligible for CTF Merge."""
        return max(0.0, min(self.shares_yes, self.shares_no))

    def get_unrealized_pnl(self, mark_yes: float, mark_no: float) -> float:
        """Calculate mark-to-market unrealized PnL."""
        pnl_yes = self.shares_yes * (mark_yes - self.avg_cost_yes) if self.shares_yes > 0 else 0.0
        pnl_no = self.shares_no * (mark_no - self.avg_cost_no) if self.shares_no > 0 else 0.0
        return pnl_yes + pnl_no

    def get_market_value(self, mark_yes: float, mark_no: float) -> float:
        """Current market value of held tokens."""
        return (self.shares_yes * mark_yes) + (self.shares_no * mark_no)

    def to_dict(self) -> dict[str, Any]:
        return {
            "condition_id": self.condition_id,
            "shares_yes": round(self.shares_yes, 4),
            "shares_no": round(self.shares_no, 4),
            "avg_cost_yes": round(self.avg_cost_yes, 4),
            "avg_cost_no": round(self.avg_cost_no, 4),
            "net_exposure": round(self.net_exposure, 4),
            "complete_sets": round(self.complete_sets, 4),
            "realized_pnl": round(self.realized_pnl, 4),
        }


@dataclass
class MergeResult:
    """Output summary of a CTF Merge transaction."""

    condition_id: str
    complete_sets_merged: float
    usdc_gross: float
    gas_fee_deducted: float
    net_cash_unlocked: float
    cost_basis_retired: float
    realized_gain: float
    remaining_yes: float
    remaining_no: float
    cash_after: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SplitResult:
    """Output summary of a CTF Split transaction."""

    condition_id: str
    cash_split: float
    shares_yes_created: float
    shares_no_created: float
    cash_after: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SimulatedPortfolio:
    """Self-hosted simulated ledger adhering to real-world Polymarket CTF economics."""

    def __init__(
        self,
        initial_cash: float = 100.0,
        gas_fee_merge: Optional[float] = None,
        taker_fee_bps: float = 0.0,
        ctf_client: Optional[Any] = None,
    ) -> None:
        self.initial_cash = float(initial_cash)
        self.cash: float = float(initial_cash)
        self.ctf_client = ctf_client
        if gas_fee_merge is not None:
            self.gas_fee_merge: float = float(gas_fee_merge)
        elif self.ctf_client is not None and hasattr(self.ctf_client, "estimate_merge_cost_usd"):
            self.gas_fee_merge = float(self.ctf_client.estimate_merge_cost_usd())
        else:
            self.gas_fee_merge = float(estimate_merge_cost_usd())
        self.taker_fee_rate: float = float(taker_fee_bps) / 10_000.0

        # State storage
        self.positions: Dict[str, Position] = {}
        self.transactions: List[PortfolioTransaction] = []

        # Aggregate metrics
        self.total_gas_fees: float = 0.0
        self.total_rewards_earned: float = 0.0
        self.total_realized_pnl: float = 0.0
        self.total_merges_count: int = 0
        self.total_splits_count: int = 0
        self.total_volume_traded_usd: float = 0.0

        # Record initial ledger genesis
        self._record_tx(
            tx_type=TransactionType.INITIAL_DEPOSIT.value,
            condition_id="GENESIS",
            token_side="CASH",
            price=1.0,
            shares=self.cash,
            gross_amount=self.cash,
            fee=0.0,
            net_cash_delta=self.cash,
            realized_pnl=0.0,
            notes=f"Initial paper portfolio funded with ${self.cash:.2f} USDC",
        )
        log.info(f"SimulatedPortfolio initialized with ${self.cash:.2f} USDC (Merge gas: ${self.gas_fee_merge:.4f})")

    def get_optimal_merge_threshold(self, fee_basis_points: float = 0.0) -> float:
        """Calculate optimal merge threshold using current dynamic gas pricing."""
        cost = (
            self.ctf_client.estimate_merge_cost_usd()
            if self.ctf_client is not None and hasattr(self.ctf_client, "estimate_merge_cost_usd")
            else self.gas_fee_merge
        )
        return calculate_optimal_merge_threshold(gas_cost_usd=cost, fee_basis_points=fee_basis_points)

    def _record_tx(
        self,
        tx_type: str,
        condition_id: str,
        token_side: str,
        price: float,
        shares: float,
        gross_amount: float,
        fee: float,
        net_cash_delta: float,
        realized_pnl: float,
        notes: str = "",
        maker_rebate: float = 0.0,
    ) -> PortfolioTransaction:
        tx = PortfolioTransaction(
            tx_id=str(uuid.uuid4())[:8],
            timestamp=time.time(),
            tx_type=tx_type,
            condition_id=condition_id,
            token_side=token_side,
            price=price,
            shares=shares,
            gross_amount=gross_amount,
            fee=fee,
            net_cash_delta=net_cash_delta,
            realized_pnl=realized_pnl,
            cash_after=self.cash,
            notes=notes,
            maker_rebate=float(maker_rebate),
        )
        self.transactions.append(tx)
        return tx

    def get_position(self, condition_id: str) -> Position:
        """Fetch or create position state for a given market."""
        if condition_id not in self.positions:
            self.positions[condition_id] = Position(condition_id=condition_id)
        return self.positions[condition_id]

    def split(self, condition_id: str, amount_usdc: float) -> SplitResult:
        """CTF Split: Converts amount_usdc cash into amount_usdc YES + amount_usdc NO.

        Requires self.cash >= amount_usdc. Cost basis is distributed equally ($0.50 each)
        or proportionally to current capital allocation.
        """
        amount = float(amount_usdc)
        if amount <= 0:
            raise ValueError(f"Split amount must be positive, got {amount}")
        if self.cash < amount:
            raise ValueError(f"Insufficient cash for CTF Split: requested ${amount:.2f}, available ${self.cash:.2f}")

        pos = self.get_position(condition_id)

        # Deduct cash
        self.cash -= amount
        self.total_splits_count += 1

        # Calculate new average cost basis for YES and NO
        # $1.00 creates 1 YES + 1 NO -> standard allocation $0.50 basis each
        split_basis_per_token = 0.50

        total_yes_shares = pos.shares_yes + amount
        pos.avg_cost_yes = ((pos.shares_yes * pos.avg_cost_yes) + (amount * split_basis_per_token)) / total_yes_shares
        pos.shares_yes = total_yes_shares

        total_no_shares = pos.shares_no + amount
        pos.avg_cost_no = ((pos.shares_no * pos.avg_cost_no) + (amount * split_basis_per_token)) / total_no_shares
        pos.shares_no = total_no_shares

        self._record_tx(
            tx_type=TransactionType.CTF_SPLIT.value,
            condition_id=condition_id,
            token_side="SET",
            price=1.00,
            shares=amount,
            gross_amount=amount,
            fee=0.0,
            net_cash_delta=-amount,
            realized_pnl=0.0,
            notes=f"CTF Split: ${amount:.2f} USDC -> {amount:.2f} YES + {amount:.2f} NO",
        )

        log.info(f"[{condition_id[:8]}] CTF Split: ${amount:.2f} USDC into YES+NO. Cash now: ${self.cash:.2f}")

        return SplitResult(
            condition_id=condition_id,
            cash_split=amount,
            shares_yes_created=amount,
            shares_no_created=amount,
            cash_after=self.cash,
        )

    def merge(
        self,
        condition_id: str,
        amount_sets: Optional[float] = None,
        gas_fee: Optional[float] = None,
    ) -> Optional[MergeResult]:
        """CTF Merge: Converts min(YES, NO) complete sets back into $1.00 USDC cash.

        Deducts dynamic Polygon PoS gas fees, resets exposure, and records realized PnL.
        """
        pos = self.get_position(condition_id)
        available_sets = pos.complete_sets

        if available_sets < 1e-4:
            return None

        sets_to_merge = available_sets if amount_sets is None else min(available_sets, float(amount_sets))
        if sets_to_merge < 1e-4:
            return None

        # Gross proceeds: 1 complete set = $1.00 USDC
        usdc_gross = sets_to_merge * 1.00
        if gas_fee is not None:
            actual_gas = float(gas_fee)
        elif self.ctf_client is not None and hasattr(self.ctf_client, "estimate_merge_cost_usd"):
            actual_gas = float(self.ctf_client.estimate_merge_cost_usd())
            self.gas_fee_merge = actual_gas
        else:
            actual_gas = self.gas_fee_merge

        gas_fee = actual_gas
        net_cash = usdc_gross - gas_fee

        # Deduct shares from inventory
        pos.shares_yes -= sets_to_merge
        pos.shares_no -= sets_to_merge

        # Cost basis retired
        cost_basis_retired = sets_to_merge * (pos.avg_cost_yes + pos.avg_cost_no)
        realized_gain = net_cash - cost_basis_retired

        # Clean tiny dust
        if pos.shares_yes < 1e-6:
            pos.shares_yes = 0.0
            pos.avg_cost_yes = 0.0
        if pos.shares_no < 1e-6:
            pos.shares_no = 0.0
            pos.avg_cost_no = 0.0

        # Update portfolio cash and metrics
        self.cash += net_cash
        self.total_gas_fees += gas_fee
        self.total_realized_pnl += realized_gain
        pos.realized_pnl += realized_gain
        self.total_merges_count += 1

        self._record_tx(
            tx_type=TransactionType.CTF_MERGE.value,
            condition_id=condition_id,
            token_side="SET",
            price=1.00,
            shares=sets_to_merge,
            gross_amount=usdc_gross,
            fee=gas_fee,
            net_cash_delta=net_cash,
            realized_pnl=realized_gain,
            notes=(
                f"CTF Merge: {sets_to_merge:.2f} sets -> +${net_cash:.2f} USDC (Gross: ${usdc_gross:.2f}, "
                f"Gas: -${gas_fee:.3f}, PnL: ${realized_gain:+.4f})"
            ),
        )

        log.info(
            f"[{condition_id[:8]}] CTF Merge: {sets_to_merge:.2f} complete sets -> "
            f"+${net_cash:.2f} USDC. Gas: ${gas_fee:.3f}. Realized PnL: ${realized_gain:+.4f}. "
            f"Remaining YES: {pos.shares_yes:.2f}, NO: {pos.shares_no:.2f}, Cash: ${self.cash:.2f}"
        )

        return MergeResult(
            condition_id=condition_id,
            complete_sets_merged=sets_to_merge,
            usdc_gross=usdc_gross,
            gas_fee_deducted=gas_fee,
            net_cash_unlocked=net_cash,
            cost_basis_retired=cost_basis_retired,
            realized_gain=realized_gain,
            remaining_yes=pos.shares_yes,
            remaining_no=pos.shares_no,
            cash_after=self.cash,
        )

    def process_fill(
        self,
        condition_id: str,
        side: str,  # "BUY" or "SELL"
        outcome: str,  # "YES" or "NO"
        price: float,
        size: float,
        is_maker: bool = True,
        notes: str = "",
        maker_rebate_usd: float = 0.0,
    ) -> PortfolioTransaction:
        """Execute a simulated trade fill against the portfolio."""
        side_norm = side.upper()
        outcome_norm = outcome.upper()
        price = float(price)
        size = float(size)

        if size <= 0 or price <= 0:
            raise ValueError(f"Invalid fill parameters: size={size}, price={price}")

        pos = self.get_position(condition_id)
        notional = price * size
        fee = notional * (0.0 if is_maker else self.taker_fee_rate)
        rebate = float(maker_rebate_usd) if is_maker else 0.0
        tx_type = (
            (TransactionType.FILL_BUY_MAKER if is_maker else TransactionType.FILL_BUY_TAKER)
            if side_norm == "BUY"
            else (TransactionType.FILL_SELL_MAKER if is_maker else TransactionType.FILL_SELL_TAKER)
        )

        realized_gain = 0.0

        if side_norm == "BUY":
            cash_needed = max(0.0, notional + fee - rebate)
            if self.cash < cash_needed:
                log.warning(
                    f"Insufficient cash for BUY fill: needed ${cash_needed:.2f}, available ${self.cash:.2f}. "
                    f"Auto-adjusting size or rejecting."
                )
                if self.cash <= 0:
                    raise ValueError(f"Zero cash available for BUY fill (needed ${cash_needed:.2f})")
                # Clip size to available cash
                size = max(0.01, (self.cash - fee + rebate) / price)
                notional = price * size
                cash_needed = max(0.0, notional + fee - rebate)

            self.cash -= cash_needed
            if rebate > 0:
                self.total_rebates_usd = getattr(self, "total_rebates_usd", 0.0) + rebate

            # Net cost basis accounts for maker fee rebate
            net_basis = notional - rebate
            if outcome_norm == "YES":
                current_shares = pos.shares_yes
                current_cost = pos.avg_cost_yes
                new_shares = current_shares + size
                pos.avg_cost_yes = ((current_shares * current_cost) + net_basis) / new_shares
                pos.shares_yes = new_shares
            elif outcome_norm == "NO":
                current_shares = pos.shares_no
                current_cost = pos.avg_cost_no
                new_shares = current_shares + size
                pos.avg_cost_no = ((current_shares * current_cost) + net_basis) / new_shares
                pos.shares_no = new_shares
            else:
                raise ValueError(f"Unsupported outcome: {outcome_norm}")

            net_cash_delta = -cash_needed

        elif side_norm == "SELL":
            current_shares = pos.shares_yes if outcome_norm == "YES" else pos.shares_no
            current_cost = pos.avg_cost_yes if outcome_norm == "YES" else pos.avg_cost_no

            # Real Polymarket CTF Short-Selling Mechanics:
            # 1. If holding outcome shares >= size: sell held shares directly for cash.
            # 2. If holding outcome shares < size:
            #    Sell held shares first, then for the deficit (size - held), mint complete sets
            #    using (1 - price) USDC collateral, leaving the maker holding the COMPLEMENT token
            #    (e.g., selling YES leaves you long NO at basis 1 - price; selling NO leaves you long YES).
            held_to_sell = min(current_shares, size)
            deficit_to_mint = max(0.0, size - held_to_sell)

            cash_delta = 0.0
            realized_gain = 0.0

            # Step A: Sell held shares (if any)
            if held_to_sell > 0:
                notional_held = price * held_to_sell
                fee_held = notional_held * (0.0 if is_maker else self.taker_fee_rate)
                proceeds_held = notional_held - fee_held
                gain_held = round((price - current_cost) * held_to_sell - fee_held, 6)
                
                cash_delta += proceeds_held
                realized_gain += gain_held
                self.total_realized_pnl += gain_held
                pos.realized_pnl += gain_held

                if outcome_norm == "YES":
                    pos.shares_yes -= held_to_sell
                    if pos.shares_yes <= 1e-6:
                        pos.shares_yes = 0.0
                        pos.avg_cost_yes = 0.0
                else:
                    pos.shares_no -= held_to_sell
                    if pos.shares_no <= 1e-6:
                        pos.shares_no = 0.0
                        pos.avg_cost_no = 0.0

            # Step B: Deficit short-sold via CTF collateralized mint
            if deficit_to_mint > 0:
                collateral_per_share = round(1.0 - price, 6)
                collateral_needed = collateral_per_share * deficit_to_mint
                fee_mint = (price * deficit_to_mint) * (0.0 if is_maker else self.taker_fee_rate)
                total_cash_outflow = collateral_needed + fee_mint

                if self.cash + cash_delta < total_cash_outflow:
                    log.warning(
                        f"Insufficient cash collateral for CTF short sell: needed ${total_cash_outflow:.2f}, "
                        f"available ${self.cash + cash_delta:.2f}. Adjusting deficit."
                    )
                    deficit_to_mint = max(0.0, (self.cash + cash_delta) / (collateral_per_share + price * (0.0 if is_maker else self.taker_fee_rate)))
                    collateral_needed = collateral_per_share * deficit_to_mint
                    fee_mint = (price * deficit_to_mint) * (0.0 if is_maker else self.taker_fee_rate)
                    total_cash_outflow = collateral_needed + fee_mint

                cash_delta -= total_cash_outflow

                # Short selling YES leaves maker holding complement NO tokens
                complement_side = "NO" if outcome_norm == "YES" else "YES"
                if complement_side == "NO":
                    curr_no = pos.shares_no
                    curr_cost_no = pos.avg_cost_no
                    new_no = curr_no + deficit_to_mint
                    pos.avg_cost_no = ((curr_no * curr_cost_no) + collateral_needed) / new_no if new_no > 0 else 0.0
                    pos.shares_no = new_no
                else:
                    curr_yes = pos.shares_yes
                    curr_cost_yes = pos.avg_cost_yes
                    new_yes = curr_yes + deficit_to_mint
                    pos.avg_cost_yes = ((curr_yes * curr_cost_yes) + collateral_needed) / new_yes if new_yes > 0 else 0.0
                    pos.shares_yes = new_yes

            if rebate > 0:
                cash_delta += rebate
                realized_gain += rebate
                self.total_realized_pnl += rebate
                pos.realized_pnl += rebate
                self.total_rebates_usd = getattr(self, "total_rebates_usd", 0.0) + rebate
            actual_size = held_to_sell + deficit_to_mint
            notional = price * actual_size
            self.cash += cash_delta
            net_cash_delta = cash_delta
        else:
            raise ValueError(f"Unsupported side: {side_norm}")

        self.total_volume_traded_usd += notional

        tx = self._record_tx(
            tx_type=tx_type.value,
            condition_id=condition_id,
            token_side=outcome_norm,
            price=price,
            shares=size,
            gross_amount=notional,
            fee=fee,
            net_cash_delta=net_cash_delta,
            realized_pnl=realized_gain,
            notes=notes or f"Fill {side_norm} {size:.2f} {outcome_norm} @ ${price:.3f} ({'MAKER' if is_maker else 'TAKER'})",
            maker_rebate=rebate,
        )

        log.info(
            f"[{condition_id[:8]}] Fill processed: {side_norm} {size:.2f} {outcome_norm} @ ${price:.3f} "
            f"| Net Cash: ${self.cash:.2f} | Pos YES: {pos.shares_yes:.2f}, NO: {pos.shares_no:.2f}"
        )
        return tx

    def credit_reward(self, amount: float, condition_id: str = "", note: str = "") -> PortfolioTransaction:
        """Credit Polymarket quadratic maker reward payout to portfolio cash balance."""
        amount = float(amount)
        if amount <= 0:
            return None  # type: ignore

        self.cash += amount
        self.total_rewards_earned += amount
        self.total_realized_pnl += amount

        tx = self._record_tx(
            tx_type=TransactionType.REWARD_PAYOUT.value,
            condition_id=condition_id or "REWARDS_POOL",
            token_side="CASH",
            price=1.0,
            shares=amount,
            gross_amount=amount,
            fee=0.0,
            net_cash_delta=amount,
            realized_pnl=amount,
            notes=note or f"Epoch Reward Payout: +${amount:.4f} USDC",
        )
        log.info(f"Credited reward +${amount:.4f} USDC. New cash: ${self.cash:.2f}")
        return tx

    def get_market_value(self, marks: Dict[str, Tuple[float, float]]) -> float:
        """Calculate market value of all open token holdings.

        marks: Dict[condition_id, Tuple[mark_price_yes, mark_price_no]]
        """
        val = 0.0
        for cond_id, pos in self.positions.items():
            mark_yes, mark_no = marks.get(cond_id, (pos.avg_cost_yes or 0.5, pos.avg_cost_no or 0.5))
            val += pos.get_market_value(mark_yes, mark_no)
        return val

    def get_unrealized_pnl(self, marks: Dict[str, Tuple[float, float]]) -> float:
        """Calculate total unrealized mark-to-market PnL across all markets."""
        unrealized = 0.0
        for cond_id, pos in self.positions.items():
            mark_yes, mark_no = marks.get(cond_id, (pos.avg_cost_yes or 0.5, pos.avg_cost_no or 0.5))
            unrealized += pos.get_unrealized_pnl(mark_yes, mark_no)
        return unrealized

    def get_equity(self, marks: Dict[str, Tuple[float, float]]) -> float:
        """Total Portfolio Equity = Cash + Value of All Held Token Shares."""
        return self.cash + self.get_market_value(marks)

    def get_total_pnl(self, marks: Dict[str, Tuple[float, float]]) -> float:
        """Total PnL since genesis: Equity - Initial Cash."""
        return self.get_equity(marks) - self.initial_cash

    def get_state(self, marks: Optional[Dict[str, Tuple[float, float]]] = None) -> dict[str, Any]:
        """Serialize complete portfolio snapshot for ledger and UI dashboards."""
        marks = marks or {}
        market_val = self.get_market_value(marks)
        unrealized_pnl = self.get_unrealized_pnl(marks)
        equity = self.cash + market_val
        total_pnl = equity - self.initial_cash

        return {
            "initial_cash": round(self.initial_cash, 2),
            "cash": round(self.cash, 4),
            "market_value": round(market_val, 4),
            "equity": round(equity, 4),
            "unrealized_pnl": round(unrealized_pnl, 4),
            "realized_pnl": round(self.total_realized_pnl, 4),
            "total_rewards_earned": round(self.total_rewards_earned, 4),
            "total_gas_fees": round(self.total_gas_fees, 4),
            "total_pnl": round(total_pnl, 4),
            "total_merges_count": self.total_merges_count,
            "total_splits_count": self.total_splits_count,
            "total_volume_traded_usd": round(self.total_volume_traded_usd, 4),
            "positions": {cid: pos.to_dict() for cid, pos in self.positions.items()},
            "recent_transactions": [tx.to_dict() for tx in self.transactions[-25:]],
        }
