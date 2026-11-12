"""Core Shared Market-Making Architecture for Polymarket Prediction Markets.

Implements BaseMarketMakingEngine, providing unified quantitative strategies
for both self-hosted paper trading (NativePaperTradingEngine) and production
real-money trading (RealMoneyTradingEngine).

Unified Features:
1. Two-Tier Dynamic De-Skewing Engine (DynamicDeSkewOptimizer):
   - Tier 1: Passive Avellaneda-Stoikov quote fading & complete-set tightening.
   - Tier 2: Emergency active synthetic complete-set cross-hedging.
2. Cont-Kukanov-Stoikov Order Flow Imbalance (OFI) & Stoikov Micro-Price Drift Sentinels.
3. Master RiskManager Circuit Breakers:
   - High-frequency price jump detection (>2.0c within 60s -> 300s cooldown).
   - Daily loss limit ($15.00 or 5% bankroll).
   - Maximum cumulative drawdown breaker ($30.00 or 10% peak equity).
   - Emergency unquote kill-switch.
4. Continuous Capital Compounding (ContinuousOptimizationGovernor & calculate_endogenous_reserve).
5. Exact Quadratic Liquidity Rewards Scoring across 1,440 daily epochs with $1.00 payout floor.
6. Opportunistic Market Migration & Economic Switching Hurdle (CapitalMigrationEngine).
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from polymarketbot.fees import FeeSchedule
from polymarketbot.live.economics import (  # noqa: F401
    MARKET_ECONOMICS_PATH,
    MarketEconomics,
)
from polymarketbot.live.quoting import QuotingMixin
from polymarketbot.live.store import StoreMixin
from polymarketbot.live.yield_accounting import YieldMixin
from polymarketbot.logs import get_logger
from polymarketbot.risk.contracts import RiskAction, RiskDecision
from polymarketbot.risk.manager import RiskManager
from polymarketbot.risk.models import RiskConfig
from polymarketbot.risk.unified import UnifiedRiskEngine
from polymarketbot.strategy.reward.accrual import REWARD_ELIGIBILITY_SECONDS
from polymarketbot.strategy.reward.allocator import ContinuousOptimizationGovernor
from polymarketbot.strategy.reward.deskew import (
    DeSkewDecision,
    DynamicDeSkewOptimizer,
    load_deskew_calibration,
)
from polymarketbot.strategy.reward.marginal import PROTOCOL_MIN_DAILY_PAYOUT_USD
from polymarketbot.strategy.reward.microstructure import (
    MicrostructureEngine,
    compute_dynamic_cooldown_duration,
    compute_dynamic_volatility_breaker_threshold,
)
from polymarketbot.strategy.reward.queue import (
    CapitalMigrationEngine,
    HurdleDecision,
    QueuePriorityValuationModel,
    SwitchingCostDecomposition,
)
from polymarketbot.strategy.reward.regime import measure_regimes_cached
from polymarketbot.strategy.reward.scoring import (
    PROTOCOL_EPOCH_SAMPLES,
    normalize_sample_score,
)

logger = get_logger("trading.base")

# Fit artifacts. Each is written by its own fitter from measured tape; a value
# the file does not carry is *not* substituted here, because every number below
# becomes a trading decision (quote retention, or the economic hurdle to moving
# capital to another market).
DERIVED_DIR = Path(__file__).resolve().parents[3] / "data" / "derived"
QUEUE_FIT_PATH = DERIVED_DIR / "queue_fit.json"
RISK_CALIBRATION_PATH = DERIVED_DIR / "risk_calibration.json"
DESKEW_CALIBRATION_PATH = DERIVED_DIR / "deskew_calibration.json"
RESERVE_CALIBRATION_PATH = DERIVED_DIR / "reserve_calibration.json"


def load_queue_model(
    path: str | Path | None = None,
) -> QueuePriorityValuationModel | None:
    """Fitted FIFO queue-valuation model, or ``None`` when it was never fitted.

    The artifact is the ``QueueParameterFit`` EWMA state plus the measured
    capital hurdle, under the exact keyword names
    :class:`QueuePriorityValuationModel` requires. ``None`` means the caller
    must refuse to value queue priority (capital switching stays disabled);
    it never means "use a representative market".
    """
    target = Path(path or QUEUE_FIT_PATH)
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.warning(
            f"No fitted queue model at {target}; capital switching is disabled"
        )
        return None
    except (OSError, ValueError) as exc:
        logger.warning(f"Queue fit artifact {target} unreadable ({exc}); capital switching is disabled")
        return None
    if not isinstance(payload, dict):
        logger.warning(f"Queue fit artifact {target} is not an object; capital switching is disabled")
        return None
    keys = (
        "trade_arrival_rate_shares_sec",
        "cancellation_rate_intensity",
        "kyle_lambda",
        "volatility_annualized",
        "annual_capital_hurdle_rate",
    )
    missing = [key for key in keys if payload.get(key) is None]
    if missing:
        logger.warning(
            f"Queue fit artifact {target} is missing {missing}; capital switching is disabled"
        )
        return None
    try:
        return QueuePriorityValuationModel(
            **{key: float(payload[key]) for key in keys}
        )
    except (TypeError, ValueError) as exc:
        logger.warning(f"Queue fit artifact {target} rejected ({exc}); capital switching is disabled")
        return None


@dataclass(frozen=True)
class RiskCalibration:
    """Tape-fitted microstructure coefficients read from the calibration artifact."""

    c_depth: float
    z_score: float
    v_ref: float
    beta_vol: float
    pull_multiplier: float
    widen_offset_multiplier: float
    consecutive_fills_threshold: int
    #: ``False`` when the measured policy is "do not widen" (multiplier 0):
    #: non-extreme toxicity MAINTAINS instead of widening by an invented floor.
    widen_action_enabled: bool = True

    def flow_factory(self) -> Callable[[], MicrostructureEngine]:
        """Build one flow engine per token from exactly these fitted values."""
        return lambda: MicrostructureEngine(
            c_depth=self.c_depth,
            z_score=self.z_score,
            v_ref=self.v_ref,
            beta_vol=self.beta_vol,
            pull_multiplier=self.pull_multiplier,
            widen_offset_multiplier=self.widen_offset_multiplier,
        )


def load_risk_calibration(path: str | Path | None = None) -> RiskCalibration | None:
    """The OFI/pull/widen fit, or ``None`` when no artifact exists.

    ``consecutive_fills_threshold`` is part of the artifact on purpose: the
    adverse-fill streak is a strategy calibration, so it has to come from the
    same measured fit rather than from a constant in the engine.
    """
    target = Path(path or RISK_CALIBRATION_PATH)
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.warning(
            f"No risk calibration at {target}; the microstructure gate is disabled"
        )
        return None
    except (OSError, ValueError) as exc:
        logger.warning(f"Risk calibration {target} unreadable ({exc}); the microstructure gate is disabled")
        return None
    if not isinstance(payload, dict):
        logger.warning(f"Risk calibration {target} is not an object; the microstructure gate is disabled")
        return None
    ofi = payload.get("ofi") if isinstance(payload.get("ofi"), dict) else payload
    keys = (
        "c_depth",
        "z_score",
        "v_ref",
        "beta_vol",
        "pull_multiplier",
        "widen_offset_multiplier",
        "consecutive_fills_threshold",
    )
    missing = [key for key in keys if ofi.get(key) is None]
    if missing:
        logger.warning(
            f"Risk calibration {target} is missing {missing}; the microstructure gate is disabled"
        )
        return None
    try:
        values = {key: float(ofi[key]) for key in keys}
    except (TypeError, ValueError) as exc:
        logger.warning(f"Risk calibration {target} is unreadable ({exc}); the microstructure gate is disabled")
        return None

    # A zero widen multiplier is a measured policy (MAINTAIN on non-extreme
    # toxicity), not a missing coefficient.  The flag and the multiplier must
    # agree: either one saying "disabled" wins so an old artifact cannot widen.
    declared = ofi.get("widen_action_enabled")
    widen_enabled = (
        values["widen_offset_multiplier"] > 0.0
        if declared is None
        else bool(declared)
    )
    if not widen_enabled and values["widen_offset_multiplier"] > 0.0:
        logger.warning(
            f"Risk calibration {target} declares widen_action_enabled=false with "
            f"widen_offset_multiplier={values['widen_offset_multiplier']:g}; "
            "forcing the measured no-widen policy"
        )
        values["widen_offset_multiplier"] = 0.0
    elif widen_enabled and values["widen_offset_multiplier"] == 0.0:
        logger.warning(
            f"Risk calibration {target} declares widen_action_enabled=true with a "
            "zero widen offset; treating widening as disabled"
        )
        widen_enabled = False
    if not widen_enabled:
        logger.info(
            f"Risk calibration {target}: measured widen offset is zero; "
            "non-extreme toxicity MAINTAINS quotes"
        )

    return RiskCalibration(
        c_depth=values["c_depth"],
        z_score=values["z_score"],
        v_ref=values["v_ref"],
        beta_vol=values["beta_vol"],
        pull_multiplier=values["pull_multiplier"],
        widen_offset_multiplier=values["widen_offset_multiplier"],
        consecutive_fills_threshold=int(values["consecutive_fills_threshold"]),
        widen_action_enabled=widen_enabled,
    )


def _artifact_window(path: Path) -> tuple[str | None, str | None, int | None]:
    """``(start, end, partition count)`` from an artifact, for the reload log."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, None, None
    window = payload.get("window") if isinstance(payload, dict) else None
    window = window if isinstance(window, dict) else {}
    start = window.get("start") or window.get("start_utc")
    end = window.get("end") or window.get("end_utc")
    partitions = payload.get("partitions") if isinstance(payload, dict) else None
    if not isinstance(partitions, list):
        partitions = window.get("partitions")
    count = len(partitions) if isinstance(partitions, list) else None
    return (
        str(start) if start is not None else None,
        str(end) if end is not None else None,
        count,
    )


def reload_fit_artifacts(
    engine: BaseMarketMakingEngine,
    paths: dict[str, str | Path] | None = None,
) -> list[str]:
    """Swap in refitted artifacts whose file changed since the last call.

    Call this only at a tick boundary, before the engine evaluates quotes: it
    replaces the queue model, the microstructure gate and the lazily built
    de-skew optimizer, which is the only safe point for a mid-run change.  A
    first call only records the file stamps (the constructor already loaded
    those artifacts) and returns ``[]``.

    ``reserve_calibration.json`` is read by ``calculate_endogenous_reserve`` on
    every call, so it needs no swap; it is still stamped and logged here so the
    operator sees that the fit reached the engine.

    Returns the component names that were reloaded.
    """
    targets = {
        "queue_fit": Path(QUEUE_FIT_PATH),
        "risk_calibration": Path(RISK_CALIBRATION_PATH),
        "deskew_calibration": Path(DESKEW_CALIBRATION_PATH),
        "reserve_calibration": Path(RESERVE_CALIBRATION_PATH),
    }
    for name, override in (paths or {}).items():
        targets[name] = Path(override)

    stamps: dict[str, int] = getattr(engine, "_fit_artifact_stamps", {})
    if not stamps:
        engine._fit_artifact_stamps = {
            name: path.stat().st_mtime_ns
            for name, path in targets.items()
            if path.exists()
        }
        return []

    reloaded: list[str] = []
    for name, path in targets.items():
        try:
            stamp = path.stat().st_mtime_ns
        except OSError:
            continue
        if stamps.get(name) == stamp:
            continue
        stamp_stored = False
        if name == "queue_fit":
            model = load_queue_model(path)
            if model is not None:
                migration = getattr(engine, "migration_engine", None)
                if migration is not None:
                    migration.queue_model = model
                else:
                    engine.migration_engine = CapitalMigrationEngine(queue_model=model)
                stamp_stored = True
        elif name == "risk_calibration":
            calibration = load_risk_calibration(path)
            if calibration is not None:
                engine.micro_risk = UnifiedRiskEngine(
                    tick_size=float(getattr(engine, "tick_size", 0.001) or 0.001),
                    flow_factory=calibration.flow_factory(),
                    horizon_seconds=float(
                        getattr(engine, "min_reprice_interval_s", 2.0)
                    ),
                    consecutive_fills_threshold=calibration.consecutive_fills_threshold,
                    config=engine.risk_manager.config,
                )
                engine.micro_gate_active = True
                engine.risk_manager.set_micro_risk(engine.micro_risk)
                stamp_stored = True
        elif name == "deskew_calibration":
            # Drop the cached optimizer; the next evaluation rebuilds it from
            # the new artifact and the then-current measured market inputs.
            engine._deskew_optimizer = None
            engine._deskew_disabled_logged = False
            stamp_stored = True
        elif name == "reserve_calibration":
            from polymarketbot.strategy.reward.reserve import load_reserve_calibration

            try:
                calibration = load_reserve_calibration(path)
            except (TypeError, ValueError) as exc:
                logger.warning(f"Refitted {name} rejected: {exc}")
                calibration = None
            stamp_stored = calibration is not None
        if not stamp_stored:
            logger.warning(f"Refitted {name} at {path} could not be loaded; keeping the old fit")
            continue
        stamps[name] = stamp
        reloaded.append(name)
        start, end, count = _artifact_window(path)
        logger.info(
            f"reloaded {name}: window {start or 'unknown'} .. {end or 'unknown'}"
            + (f" over {count} partition(s)" if count is not None else "")
        )
    engine._fit_artifact_stamps = stamps
    return reloaded


class DisabledMicrostructureGate:
    """Stand-in for the unified risk engine when no calibration was fitted.

    ``UnifiedRiskEngine`` has no coefficient defaults, so an uncalibrated
    engine cannot construct one. The ingestion call sites still need a total
    object, so this one records nothing, ``flow`` reports ``None`` (unmeasured
    toxicity) and :meth:`evaluate` always answers ``MAINTAIN`` with
    ``measured=False``. :class:`RiskManager` is never wired to it, so the hard
    breakers are unaffected.
    """

    degraded = True
    tick_size: float = 0.0

    def reset(self) -> None:
        """No-op: nothing is tracked, so a market switch has nothing to clear."""

    def on_book(self, *args: Any, **kwargs: Any) -> None:
        """No-op: without fitted coefficients a book update decides nothing."""

    def on_trade(self, *args: Any, **kwargs: Any) -> None:
        """No-op: the L3 queue reconstruction needs the same fit."""

    def on_entry(self, *args: Any, **kwargs: Any) -> None:
        """No-op: no queue entry is tracked."""

    def on_cancel(self, *args: Any, **kwargs: Any) -> None:
        """No-op."""

    def on_fill(self, *args: Any, **kwargs: Any) -> None:
        """No-op."""

    def flow(self, token_id: str) -> MicrostructureEngine | None:
        """Unmeasured toxicity: callers must treat the sentinel as absent."""
        return None

    def evaluate(
        self, token_id: str, side: str, price: float, mid: float, ts: float
    ) -> RiskDecision:
        """Refuse to act: an uncalibrated gate has no threshold to judge."""
        return RiskDecision(
            action=RiskAction.MAINTAIN,
            side=str(side).upper(),
            reason="microstructure gate disabled: no fitted risk calibration artifact",
            measured=False,
            ts=float(ts),
        )

    def snapshot(self) -> dict[str, Any]:
        """Telemetry shape matching ``UnifiedRiskEngine.snapshot``."""
        return {"orders": 0, "tokens": {}, "degraded": True}



# Kept for import compatibility. There is deliberately no fabricated price:
# a missing book is represented by ``None`` and every quoting path must skip
# rather than trade against an invented 0.49/0.51 mark.

@dataclass
class PositionView:
    """Read-only view of a market condition's inventory and cost basis."""

    condition_id: str
    shares_yes: float = 0.0
    shares_no: float = 0.0
    avg_cost_yes: float = 0.0
    avg_cost_no: float = 0.0
    realized_pnl: float = 0.0

    @property
    def complete_sets(self) -> float:
        return min(self.shares_yes, self.shares_no)

    @property
    def net_exposure(self) -> float:
        return self.shares_yes - self.shares_no


class BaseMarketMakingEngine(StoreMixin, QuotingMixin, YieldMixin):
    """Unified Base Class for Polymarket Market Making Strategy Orchestration."""

    def __init__(
        self,
        condition_id: str,
        asset_id_yes: str = "TOKEN_YES",
        asset_id_no: str = "TOKEN_NO",
        quote_size: float | None = None,
        spread_offset_cents: float | None = None,
        daily_reward_rate: float | None = None,
        rewards_max_spread_cents: float | None = None,
        rewards_min_size: float | None = None,
        auto_merge_threshold: float | None = None,
        gas_fee_merge: float | None = None,
        institutional_mode: bool = True,
        gamma: float | None = None,
        competing_score_override: float | None = None,
        db_path: str | Path = "data/trading.db",
        ledger_path: str | Path = "data/trading_ledger.json",
        deskew_optimizer: DynamicDeSkewOptimizer | None = None,
        migration_engine: CapitalMigrationEngine | None = None,
        governor: ContinuousOptimizationGovernor | None = None,
        risk_manager: RiskManager | None = None,
        price_jump_threshold_cents: float | None = None,
        cooldown_duration_seconds: float | None = None,
        economics: MarketEconomics | None = None,
        *,
        fee_schedule: FeeSchedule,
        bankroll_usd: float,
        cash_reserve_usd: float,
    ) -> None:
        # Every economic knob comes from measured economics; explicit kwargs are
        # caller overrides and take precedence over the loaded file.
        economics = economics or MarketEconomics.load()
        # These three are measurements of *this* deployment, not strategy
        # parameters: the per-market fee curve, the trading bankroll and the
        # cash held back for de-skewing a merge. They have no defaults.
        if fee_schedule is None:
            raise ValueError("fee_schedule is required; fees are never assumed zero")
        if bankroll_usd is None or float(bankroll_usd) <= 0.0:
            raise ValueError(f"bankroll_usd must be a measured positive capital, got {bankroll_usd!r}")
        if cash_reserve_usd is None or float(cash_reserve_usd) < 0.0:
            raise ValueError(f"cash_reserve_usd must be a measured reserve, got {cash_reserve_usd!r}")
        self.fee_schedule: FeeSchedule = fee_schedule
        self.bankroll_usd: float = float(bankroll_usd)
        self.cash_reserve_usd: float = float(cash_reserve_usd)
        self.condition_id = condition_id
        self.asset_id_yes = asset_id_yes
        self.asset_id_no = asset_id_no
        self.quote_size = float(
            economics.quote_size if quote_size is None else quote_size
        )
        _offset = (
            economics.spread_offset_cents
            if spread_offset_cents is None
            else spread_offset_cents
        )
        # An unmeasured quote distance disables quoting (the tick gate in
        # ``LifecycleMixin.evaluate_quotes`` refuses) rather than fading every
        # book by a provisional 0.50c.
        self.spread_offset_cents: float | None = (
            None if _offset is None else float(_offset)
        )
        if self.spread_offset_cents is None:
            logger.warning(
                "spread_offset_cents is unmeasured; quoting is disabled until a "
                "measured quote distance is supplied"
            )
        self.daily_reward_rate = float(
            economics.daily_reward_rate if daily_reward_rate is None else daily_reward_rate
        )
        self.rewards_max_spread = float(
            economics.rewards_max_spread_cents
            if rewards_max_spread_cents is None
            else rewards_max_spread_cents
        )
        self.rewards_min_size = float(
            economics.rewards_min_size if rewards_min_size is None else rewards_min_size
        )
        self.price_jump_threshold_cents = (
            float(price_jump_threshold_cents) if price_jump_threshold_cents is not None else None
        )
        self.cooldown_duration_seconds = (
            float(cooldown_duration_seconds) if cooldown_duration_seconds is not None else None
        )

        if gas_fee_merge is not None:
            self.gas_fee_merge = float(gas_fee_merge)
        else:
            from polymarketbot.strategy.onchain.ctf import (
                GasUnmeasured,
                estimate_merge_cost_usd,
            )

            measured_gas = estimate_merge_cost_usd()
            if measured_gas is None:
                raise GasUnmeasured(
                    "on-chain merge gas is unmeasured; refusing to size quotes and "
                    "reserves against a guessed fee"
                )
            self.gas_fee_merge = float(measured_gas)

        if auto_merge_threshold is not None:
            self.auto_merge_threshold = float(auto_merge_threshold)
        else:
            from polymarketbot.strategy.onchain.ctf import (
                calculate_optimal_merge_threshold,
            )
            self.auto_merge_threshold = float(
                calculate_optimal_merge_threshold(gas_cost_usd=self.gas_fee_merge)
            )

        self.institutional_mode = bool(institutional_mode)
        self.gamma = float(economics.gamma if gamma is None else gamma)
        self.competing_score_override = (
            float(competing_score_override) if competing_score_override is not None else None
        )

        # Persistence paths
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.ledger_path = Path(ledger_path)
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)

        # Quantitative De-Skew Optimizer. Built from this market's measured
        # tape regime and the fitted calibration artifact on the first tick at
        # which a two-sided book exists (see ``_build_deskew_optimizer``).
        # ``bankroll``, ``cash_reserve``, the fee curve, the spread and the
        # print rate do not all exist before the wallet is reconciled and a
        # two-sided book has printed, so construction is lazy.
        self._deskew_optimizer: DynamicDeSkewOptimizer | None = deskew_optimizer
        self._deskew_disabled_logged = False
        self._deskew_skip_logged = False

        # Capital Migration & Switching Hurdle Engine. Queue priority can only
        # be valued from a fitted queue model; with no fit, switching is
        # disabled instead of priced against a representative market.
        if migration_engine is None:
            queue_model = load_queue_model()
            if queue_model is not None:
                migration_engine = CapitalMigrationEngine(queue_model=queue_model)
        self.migration_engine: CapitalMigrationEngine | None = migration_engine
        self.migration_history: list[dict[str, Any]] = []
        self.latest_hurdle_decision: HurdleDecision | None = None
        self.latest_switching_cost: SwitchingCostDecomposition | None = None

        # Continuous Optimization Governor
        self.governor = governor or ContinuousOptimizationGovernor()
        if self.condition_id not in self.governor.market_entry_times:
            self.governor.market_entry_times[self.condition_id] = time.time()

        # Risk Management & Circuit Breakers
        # Optional in-process hook for the live session recorder. It must never
        # block or raise: the recorder enqueues and returns immediately.
        self.event_hook: Callable[[str, dict[str, Any]], None] | None = None
        self.risk_manager = risk_manager or RiskManager(
            RiskConfig(
                price_jump_window_seconds=60.0,
                price_jump_threshold_cents=2.0,
                cooldown_duration_seconds=300.0,
                daily_loss_limit_usd=economics.daily_loss_limit_usd,
                max_drawdown_usd=economics.max_drawdown_usd,
            )
        )
        self.risk_manager.register_unquote_callback(self._on_risk_unquote)

        # One microstructure authority for every resting quote. The tick is the
        # subclass's market tick when it already knows one; a live engine
        # replaces it in ``rebind_engine_to_market`` when the market switches.
        # Its horizon is this engine's reprice cadence, assigned just below, so
        # no decision can price a 2 s quote with a 225 s fill probability.
        self._book_depth: dict[str, tuple[list, list]] = {}
        self.quote_tick_size: float = 0.001
        self.min_reprice_interval_s: float = 2.0
        self.min_reprice_ticks: int = 1

        calibration = load_risk_calibration()
        if calibration is not None:
            self.micro_risk = UnifiedRiskEngine(
                tick_size=float(getattr(self, "tick_size", 0.001) or 0.001),
                flow_factory=calibration.flow_factory(),
                horizon_seconds=self.min_reprice_interval_s,
                consecutive_fills_threshold=calibration.consecutive_fills_threshold,
                config=self.risk_manager.config,
            )
            self.micro_gate_active = True
            self.risk_manager.set_micro_risk(self.micro_risk)
        else:
            # Gate disabled (no fitted coefficients). The hard breakers
            # (daily loss, drawdown, kill switch, price-jump cooldown) stay on;
            # RiskManager is deliberately *not* wired to a decision source.
            self.micro_risk = DisabledMicrostructureGate()
            self.micro_gate_active = False
            logger.warning(
                "DEGRADED: microstructure gate disabled (no fitted "
                f"{RISK_CALIBRATION_PATH}); hard risk breakers remain active"
            )

        # Price jump rolling buffer
        self._rolling_price_ticks: deque[tuple[float, float]] = deque()
        self._volatility_cooldown_until: float = 0.0
        self._trigger_instant_scan: bool = False
        self._last_cooldown_log_time: float = 0.0

        # Best bid/ask cache: token_side -> (bid, ask)
        self._best_bids: dict[str, float] = {}
        self._best_asks: dict[str, float] = {}

        # Quote Registry: quote_id -> dict
        self.active_quotes: dict[str, dict[str, Any]] = {}

        # Real time-to-resolution for the A-S variance term. The live engine replaces
        # this from the market's end date; the default matches the research model.
        self.days_to_resolution: float = float(economics.days_to_resolution)

        # Epoch & Telemetry State
        self.epoch_index: int = 0
        self.last_epoch_ts: float = time.time()
        self.total_rewards_credited: float = 0.0
        self._last_reward_epoch: dict[str, Any] = {}
        self.latest_deskew_decision: DeSkewDecision | None = None

        # Age of the current net inventory: set when net exposure leaves zero,
        # cleared when it returns. The quoting leg passes this as the de-skew
        # optimizer's ``holding_time_seconds``.
        self._inventory_since: float | None = None

        # SQLite Connection
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._init_sqlite()

    # -------------------------------------------------------------------------
    # Database Initialization
    # -------------------------------------------------------------------------

    def _emit_event(self, kind: str, payload: dict[str, Any]) -> None:
        """Best-effort hook for the session recorder; a broken hook is ignored."""
        hook = self.event_hook
        if hook is None:
            return
        try:
            hook(kind, payload)
        except Exception as e:  # noqa: BLE001 - recording must never affect trading
            logger.debug(f"Event hook failed for {kind}: {e}")


    # -------------------------------------------------------------------------
    # Measured Capital Inputs
    # -------------------------------------------------------------------------

    @staticmethod
    def taker_fee_bps_at(schedule: FeeSchedule, price: float) -> float:
        """Taker fee as basis points of notional at ``price``.

        The venue charges ``C * rate * (p(1-p))^exponent`` dollars per share
        (``fees.taker_fee``), so the fee as a fraction of the notional a taker
        crosses is that over ``p``. This is the scalar the de-skew optimizer
        prices the active route with; it is derived from the market's own
        schedule, never from a fixed rate.
        """
        p = float(price)
        if not 0.0 < p < 1.0:
            raise ValueError(f"cannot price a taker fee at {price!r}")
        fraction = schedule.rate * ((p * (1.0 - p)) ** schedule.exponent) / p
        return float(fraction) * 10_000.0

    @property
    def deskew_optimizer(self) -> DynamicDeSkewOptimizer | None:
        """The inventory de-skew optimizer, or ``None`` when it cannot be fitted.

        It is built lazily because its inputs only exist once the wallet is
        reconciled and a two-sided book has printed: measured capital, the
        market's fee curve at the quoted mid, the live spread, the measured
        tape print rate and the Fitted :class:`DeSkewCalibration` artifact.
        ``None`` means de-skewing is disabled and quoting stays neutral; it
        never means "use a provisional threshold". A caller that injected an
        optimizer keeps it.
        """
        optimizer = self._deskew_optimizer
        if optimizer is not None:
            return optimizer
        optimizer = self._build_deskew_optimizer()
        if optimizer is not None:
            self._deskew_optimizer = optimizer
        return optimizer

    @deskew_optimizer.setter
    def deskew_optimizer(self, optimizer: DynamicDeSkewOptimizer | None) -> None:
        self._deskew_optimizer = optimizer

    def _build_deskew_optimizer(self) -> DynamicDeSkewOptimizer | None:
        """Construct the optimizer from the fit and measured market inputs.

        Every argument is a measurement: the calibration artifact, the live
        two-sided YES book (mid and spread), this market's fee curve at that
        mid, the on-chain merge gas, the published reward minimum size, the
        remaining life from the market payload, and the tape print rate from
        the same :mod:`polymarketbot.strategy.reward.regime` measurement the
        sizing supervisor consumes. Any missing input returns ``None`` so
        quoting stays neutral instead of pricing a guess.
        """
        try:
            calibration = load_deskew_calibration()
        except (TypeError, ValueError) as exc:
            if not self._deskew_disabled_logged:
                logger.warning(f"DEGRADED: de-skew disabled ({exc})")
                self._deskew_disabled_logged = True
            return None
        if calibration is None:
            if not self._deskew_disabled_logged:
                logger.warning(
                    "DEGRADED: de-skew disabled (no calibration artifact)"
                )
                self._deskew_disabled_logged = True
            return None

        bid, ask = self.get_best_bid_ask("YES")
        bid_no, ask_no = self.get_best_bid_ask("NO")
        if bid is None or ask is None or bid_no is None or ask_no is None:
            return None
        if not (0.0 < float(bid) < float(ask) < 1.0):
            return None

        mid = (float(bid) + float(ask)) / 2.0
        spread_cents = (float(ask) - float(bid)) * 100.0
        if self.rewards_min_size <= 0.0:
            if not self._deskew_disabled_logged:
                logger.warning(
                    "de-skew disabled: the market payload carries no rewards minimum size"
                )
                self._deskew_disabled_logged = True
            return None

        try:
            measurement = measure_regimes_cached(
                {self.condition_id: float(self.rewards_max_spread)}
            )
        except Exception as exc:  # noqa: BLE001 - a failed tape read is missing data
            if not self._deskew_disabled_logged:
                logger.warning(
                    f"de-skew disabled: tape regime measurement failed for "
                    f"{self.condition_id[:12]}: {exc}"
                )
                self._deskew_disabled_logged = True
            return None
        regime = measurement.regimes.get(self.condition_id)
        if regime is None or not regime.trades_per_min > 0.0:
            if not self._deskew_disabled_logged:
                reason = (
                    measurement.reason_for(self.condition_id)
                    if regime is None
                    else "measured zero prints per minute"
                )
                logger.info(
                    f"de-skew disabled for {self.condition_id[:12]}: {reason}"
                )
                self._deskew_disabled_logged = True
            return None

        days = max(1.0 / 24.0, float(self.days_to_resolution))
        return DynamicDeSkewOptimizer(
            quote_size=self.quote_size,
            bankroll=self.bankroll_usd,
            cash_reserve=self.cash_reserve_usd,
            taker_fee_bps=self.taker_fee_bps_at(self.fee_schedule, mid),
            calibration=calibration,
            mid_price=mid,
            time_to_resolution_days=days,
            spread_cents=spread_cents,
            gas_cost_usd=float(self.gas_fee_merge),
            trades_per_min=float(regime.trades_per_min),
            rewards_min_size=float(self.rewards_min_size),
            time_to_expiry_seconds=days * 86_400.0,
        )

    def _track_inventory_age(self, ts: float) -> None:
        """Start/stop the inventory clock as net exposure leaves/returns to zero."""
        net = float(getattr(self, "shares_yes", 0.0)) - float(getattr(self, "shares_no", 0.0))
        if abs(net) <= 1e-12:
            self._inventory_since = None
        elif self._inventory_since is None:
            self._inventory_since = float(ts)

    def inventory_age_seconds(self, now: float | None = None) -> float:
        """How long the current net inventory has been held (``0.0`` when flat)."""
        since = self._inventory_since
        if since is None:
            return 0.0
        return max(0.0, float(now if now is not None else time.time()) - since)

    def quote_book_age_seconds(self, ts: float) -> float | None:
        """Age of the older of the YES/NO snapshots the quote path would use.

        ``None`` means at least one side has never printed. The quoting leg
        must not price a book older than its own reprice interval, because the
        mark it would fade has already moved.
        """
        ages: list[float] = []
        book_ts_map: dict[str, float] = getattr(self, "_book_ts", {}) or {}
        for tok in ("YES", "NO"):
            book_ts = float(book_ts_map.get(tok, 0.0) or 0.0)
            if book_ts <= 0.0:
                return None
            ages.append(max(0.0, float(ts) - book_ts))
        return max(ages) if ages else None

    def _inventory_impact_per_share(self, token_side: str) -> float:
        """Measured price impact of one share of inventory, from the live depth.

        ``1 / size resting at the touch`` on the side we would have to trade to
        flatten. ``0.0`` when that depth is unknown: an unmeasured impact is no
        skew, never an invented coefficient. Real-money engines override this
        with the same measurement over their own depth cache.
        """
        tok = str(token_side).upper()
        bids, asks = self._book_depth.get(tok, ([], []))
        net = float(getattr(self, "shares_yes", 0.0)) - float(
            getattr(self, "shares_no", 0.0)
        )
        side_depth = bids if net > 0.0 else asks
        if not side_depth:
            return 0.0
        try:
            touch_size = float(
                side_depth[0][1]
                if isinstance(side_depth[0], tuple)
                else side_depth[0].size
            )
        except (AttributeError, IndexError, TypeError, ValueError):
            return 0.0
        if touch_size <= 0.0:
            return 0.0
        return 1.0 / touch_size


    # -------------------------------------------------------------------------
    # Subclass Abstract / Execution Hooks
    # -------------------------------------------------------------------------

    def _set_quote(
        self,
        token_side: str,
        quote_side: str,
        price: float,
        size: float,
        ts: float,
    ) -> None:
        """Place or update a quote (virtual maker quote or live CLOB order)."""
        raise NotImplementedError

    def _cancel_quote(self, qid: str) -> None:
        """Cancel an individual resting quote."""
        raise NotImplementedError

    def cancel_all_quotes(self) -> None:
        """Cancel all resting quotes across all tokens and sides."""
        raise NotImplementedError

    def _execute_tier2_taker_deskew(self, decision: DeSkewDecision, ts: float) -> None:
        """Execute Tier 2 emergency taker or synthetic merge."""
        raise NotImplementedError

    def trigger_ctf_merge(self, amount_sets: float | None = None) -> Any:
        """Execute complete-set CTF merge."""
        raise NotImplementedError

    def trigger_ctf_split(self, amount_usdc: float) -> Any:
        """Execute CTF split."""
        raise NotImplementedError

    def get_cash(self) -> float:
        """Get available cash balance in USD."""
        raise NotImplementedError

    def get_locked_cash(self) -> float:
        """Collateral committed to resting BUY orders.

        Replacing those orders releases the collateral, so quote sizing uses
        ``get_cash() + get_locked_cash()``. Simulation engines return 0.
        """
        return 0.0

    def get_position(self, condition_id: str | None = None) -> PositionView:
        """Get current position state for target condition ID."""
        raise NotImplementedError

    def get_equity(self, prices: dict[str, tuple[float, float]] | None = None) -> float:
        """Get total portfolio equity (cash + inventory market value)."""
        raise NotImplementedError

    def get_best_bid_ask(
        self, token_side: str
    ) -> tuple[float | None, float | None]:
        """Best real bid/ask for this outcome, or ``(None, None)`` with no book.

        There is no fabricated fallback price: callers must skip quoting when
        either side is ``None`` instead of marking the market at 0.49/0.51.
        """
        tok = token_side.upper()
        bid = self._best_bids.get(tok)
        ask = self._best_asks.get(tok)
        if bid is not None and ask is not None:
            return bid, ask
        # Cross-infer from complementary token if available
        comp = "NO" if tok == "YES" else "YES"
        b_comp = self._best_bids.get(comp)
        a_comp = self._best_asks.get(comp)
        if (
            b_comp is not None
            and a_comp is not None
            and 0.0 < b_comp < 1.0
            and 0.0 < a_comp < 1.0
        ):
            return round(1.0 - a_comp, 4), round(1.0 - b_comp, 4)
        return None, None

    def credit_reward(self, amount: float, condition_id: str, note: str = "") -> None:
        """Hook for crediting settled liquidity rewards to portfolio cash."""

    # -------------------------------------------------------------------------
    # Circuit Breakers & Volatility Protection
    # -------------------------------------------------------------------------

    @property
    def is_volatility_cooldown_active(self) -> bool:
        """Check if volatility cooldown is currently active."""
        if self._rolling_price_ticks:
            latest_tick_ts = self._rolling_price_ticks[-1][0]
            if latest_tick_ts < 1e8:  # Simulated relative timestamp
                return latest_tick_ts < self._volatility_cooldown_until
        return time.time() < self._volatility_cooldown_until

    @property
    def volatility_cooldown_until(self) -> float:
        """Timestamp until which volatility cooldown remains active."""
        return self._volatility_cooldown_until

    def _on_risk_unquote(self, asset_id: str | None, reason: str) -> None:
        """Callback from master RiskManager: emergency cancel resting quotes."""
        logger.warning(f"[RISK MANAGER UNQUOTE] Cancelling active quotes due to: {reason}")
        self.cancel_all_quotes()

    def pop_instant_scan_trigger(self) -> bool:
        """Check and reset the instant opportunity scan trigger flag."""
        if self._trigger_instant_scan:
            self._trigger_instant_scan = False
            return True
        return False

    def trigger_instant_scan(self) -> None:
        """Manually trigger an instant opportunity scan (e.g. on hold or cooldown)."""
        self._trigger_instant_scan = True

    def _check_volatility_breaker(self, mid_yes: float, ts: float | None = None) -> bool:
        """Track rolling price ticks. If mid jumps > 2.0c within 60s, cancel quotes and cool down for 300s."""
        now = ts if ts is not None else time.time()

        if now < self._volatility_cooldown_until:
            if self.active_quotes:
                self.cancel_all_quotes()
            # Throttle repetitive logging to once every 5.0 seconds
            if now - self._last_cooldown_log_time >= 5.0:
                self._last_cooldown_log_time = now
                logger.warning(
                    f"[ASYMMETRIC TOXICITY DEFENSE] Volatility cooldown active for condition {self.condition_id} "
                    f"({self._volatility_cooldown_until - now:.1f}s remaining). Quotes cancelled/suppressed."
                )
            return True

        # Evict ticks older than 60s
        cutoff = now - 60.0
        while self._rolling_price_ticks and self._rolling_price_ticks[0][0] < cutoff:
            self._rolling_price_ticks.popleft()

        if not (self._rolling_price_ticks and self._rolling_price_ticks[-1] == (now, float(mid_yes))):
            self._rolling_price_ticks.append((now, float(mid_yes)))

        prices = [p for _, p in self._rolling_price_ticks]
        max_px = max(prices)
        min_px = min(prices)
        jump_cents = (max_px - min_px) * 100.0

        if self.price_jump_threshold_cents is not None:
            dyn_thresh = self.price_jump_threshold_cents
        else:
            dyn_thresh = compute_dynamic_volatility_breaker_threshold(mid_price=float(mid_yes), spread_cents=1.0)

        if jump_cents > dyn_thresh:
            if self.cooldown_duration_seconds is not None:
                cooldown_dur = self.cooldown_duration_seconds
            else:
                cooldown_dur = compute_dynamic_cooldown_duration(
                    jump_cents_observed=jump_cents,
                    breaker_threshold_cents=dyn_thresh,
                )
            self._volatility_cooldown_until = now + cooldown_dur
            self._trigger_instant_scan = True
            self._last_cooldown_log_time = now
            self.cancel_all_quotes()
            msg = f"Mid-price jump {jump_cents:.2f}c > {dyn_thresh:.2f}c within 60s."
            # Register a *per-market* cooldown with the risk manager's volatility monitor.
            # Do NOT trip the global circuit breaker here: `is_halted` has no auto-resume,
            # so a single 2c print would permanently stop the engine.
            self.risk_manager.volatility_monitor.cooldowns[self.asset_id_yes] = (
                self._volatility_cooldown_until
            )
            logger.warning(
                f"[ASYMMETRIC TOXICITY DEFENSE] Dynamic price jump volatility breaker triggered: {msg} "
                f"Active quotes cancelled; entering {cooldown_dur:.0f}s cooldown until ts={self._volatility_cooldown_until:.1f}. "
                f"Triggering instant opportunity scanner for market rotation."
            )
            return True

        return False


    # -------------------------------------------------------------------------
    # Quoting Logic & 2-Tier De-Skewing Strategy
    # -------------------------------------------------------------------------

    def round_quote_price(self, price: float, token_side: str) -> float:
        """Round ``price`` to the market tick and clamp it to the tradeable band.

        The base class defaults to a 0.001 tick. Subclasses that trade a real CLOB
        override both ``quote_tick_size`` and this method with the market's tick.
        """
        tick = self.quote_tick_size if self.quote_tick_size > 0 else 0.001
        min_price = max(tick, 0.001)
        max_price = min(1.0 - tick, 0.999)
        if max_price < min_price:
            min_price = max_price = 0.5
        rounded = round(round(price / tick) * tick, 6)
        return round(min(max(rounded, min_price), max_price), 6)


    # -------------------------------------------------------------------------
    # Liquidity Rewards Epoch & Capital Compounding
    # -------------------------------------------------------------------------

    def process_reward_epoch(
        self,
        timestamp: float | None = None,
        competing_score: float = 0.0,
    ) -> dict[str, Any]:
        """Process 1-minute quadratic reward epoch and trigger continuous compounding."""
        ts = timestamp or time.time()
        # Align the epoch to the wall clock so indices are comparable across restarts
        # and do not drift with uptime.
        self.epoch_index = int(ts // 60) % 1440
        self.last_epoch_ts = ts

        # Official Q_min score (never Q1 + Q2): rewards are paid on the minimum of the
        # two complementary book sides, with the documented single-sided penalty.
        total_user_score, _q_one, _q_two = self.compute_quote_scores()

        if self.competing_score_override is not None and self.competing_score_override > 0.0:
            competing_score = max(self.competing_score_override, competing_score)
        else:
            competing_score = max(0.0, competing_score)

        total_pool_score = total_user_score + competing_score
        # The venue's sample normalisation and epoch budget are the reward
        # pillar's (``strategy/reward/scoring.py``): one implementation, called
        # from here rather than re-derived.
        pool_share = normalize_sample_score(total_user_score, total_pool_score)
        pool_share_pct = pool_share * 100.0

        epoch_budget_usd = self.daily_reward_rate / float(PROTOCOL_EPOCH_SAMPLES)
        raw_epoch_payout = pool_share * epoch_budget_usd
        projected_daily_usd = raw_epoch_payout * float(PROTOCOL_EPOCH_SAMPLES)

        # The venue pays this market per UTC day and only when the day's total
        # reaches $1.00. Accrue the *rate* into the day's bucket and settle only
        # days that have already rolled over, so an unsettled day is never
        # credited early and the floor is never applied to an instantaneous
        # rate. An interval in which no quote has passed the 3.5 s eligibility
        # floor accrues nothing; the scored rate is the venue's own Q_min over
        # the quotes that are resting now.
        accrual = self.reward_day_accrual()
        eligible_resting = any(
            float(q.get("size", 0.0) or 0.0) > 0.0
            and ts - float(q.get("timestamp", 0.0) or 0.0) >= REWARD_ELIGIBILITY_SECONDS
            for q in self.active_quotes.values()
        )
        accrual.record(ts, projected_daily_usd if eligible_resting else 0.0)

        today = datetime.fromtimestamp(ts, UTC).date()
        closing = [
            (day, accrual.pending_utc_day(day))
            for day in accrual.pending_days()
            if day < today
        ]
        # Settles (and clears) every rolled-over day; returns the latest payout.
        self.settle_completed_reward_days(datetime.fromtimestamp(ts, UTC))
        actual_epoch_payout = 0.0
        for day, accrued in closing:
            paid = accrued if accrued >= PROTOCOL_MIN_DAILY_PAYOUT_USD else 0.0
            if paid <= 0.0:
                continue
            actual_epoch_payout += paid
            self.credit_reward(
                paid,
                condition_id=self.condition_id,
                note=f"UTC day {day.isoformat()} reward ({pool_share_pct:.2f}% share)",
            )
        if actual_epoch_payout > 0:
            self.total_rewards_credited += actual_epoch_payout

        accrued_daily_usd = accrual.pending_utc_day(today)
        floor_met = accrued_daily_usd >= PROTOCOL_MIN_DAILY_PAYOUT_USD

        # Record to SQLite
        cur = self._conn.cursor()
        cur.execute(
            """
            INSERT INTO reward_epochs (
                epoch_index, timestamp, condition_id, user_score, competing_score,
                pool_share_pct, epoch_payout_usd, projected_daily_usd, floor_met,
                cumulative_rewards_usd
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self.epoch_index,
                ts,
                self.condition_id,
                total_user_score,
                competing_score,
                pool_share_pct,
                actual_epoch_payout,
                projected_daily_usd,
                1 if floor_met else 0,
                self.total_rewards_credited,
            ),
        )
        self._conn.commit()

        # Update Risk Manager PnL & Check Daily Loss / Drawdown Breakers
        cash_val = self.get_cash()
        equity_val = self.get_equity()
        pos = self.get_position(self.condition_id)
        self.risk_manager.update_portfolio_pnl(
            current_equity=equity_val,
            realized_pnl=pos.realized_pnl,
            unrealized_pnl=self._unrealized_pnl(),
            current_cash=cash_val,
            timestamp=ts,
        )

        # Continuous Capital Compounding via Governor. Without a book there is no
        # mid to size against, so the governor is skipped rather than fed a mark.
        b_yes, a_yes = self.get_best_bid_ask("YES")
        gov_actions = []
        if b_yes is not None and a_yes is not None:
            mid_yes = (b_yes + a_yes) / 2.0
            cand = {
                "condition_id": self.condition_id,
                "question": f"Market {self.condition_id[:12]}",
                "daily_pool": self.daily_reward_rate,
                "competing_score": max(1.0, competing_score),
                "max_spread": self.rewards_max_spread,
                "min_size": self.rewards_min_size,
                "order_min_usd": self.min_order_usd,
                "mid_yes": mid_yes,
                "real_mid": mid_yes,
            }
            # Hysteresis is solved from measurements, not configured: one
            # measured CLOB round trip (the same persisted p50 the placement
            # path pays) and the accrual yield this market actually earns per
            # dollar of resting notional. The marginal yield comes from the
            # allocator's own KKT price inside the governor. A missing input
            # holds the position; it never falls back to a dwell constant.
            from polymarketbot.live.engine.deskew import persisted_clob_rtt_s

            notional = float(self.quote_size or 0.0) * float(mid_yes)
            accrual_yield = (
                float(projected_daily_usd) / notional if notional > 0.0 else None
            )
            gov_actions = self.governor.evaluate_governor_cycle(
                current_active_markets=[self.condition_id],
                live_bankroll_usd=cash_val,
                scanned_universe=[cand],
                current_time=ts,
                clob_rtt_seconds=persisted_clob_rtt_s(),
                accrual_yield_usd_per_usd_per_day=accrual_yield,
            )
        for act in gov_actions:
            if act.action_type == "RESIZE_QUOTE" and act.condition_id == self.condition_id:
                logger.info(
                    f"Governor recommended RESIZE_QUOTE for {self.condition_id[:12]}: "
                    f"New target size={act.target_quote_size:.1f} shares. Compounding into resting quotes."
                )
                self.update_quote_size(act.target_quote_size, timestamp=ts)

        result = {
            "epoch_index": self.epoch_index,
            "timestamp": ts,
            "user_score": round(total_user_score, 4),
            "competing_score": round(competing_score, 2),
            "pool_share_pct": round(pool_share_pct, 4),
            "epoch_payout_usd": round(actual_epoch_payout, 6),
            "projected_daily_usd": round(projected_daily_usd, 2),
            "accrued_daily_usd": round(accrued_daily_usd, 4),
            "floor_met": floor_met,
            "total_rewards_usd": round(self.total_rewards_credited, 4),
            "quote_size": self.quote_size,
            "governor_actions": [act.action_type for act in gov_actions],
        }
        self._last_reward_epoch = result
        return result


    def flush_wal(self) -> None:
        """Explicitly checkpoint WAL journal to primary SQLite database file."""
        if self._conn:
            try:
                cur = self._conn.cursor()
                cur.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                self._conn.commit()
            except sqlite3.Error as e:
                logger.warning(f"Error executing WAL checkpoint: {e}")

    def close(self) -> None:
        """Close SQLite connection and unquote cleanly."""
        tracker = getattr(self, "_markout_tracker", None)
        if tracker is not None:
            try:
                tracker.stop()
            except Exception as e:  # noqa: BLE001 - shutdown cleanup must continue
                logger.debug(f"Markout tracker stop failed during close: {e}")
            self._markout_tracker = None
        try:
            self.cancel_all_quotes()
        except Exception as e:  # noqa: BLE001 - shutdown cleanup must continue
            logger.debug(f"Quote cancellation failed during close: {e}")
        try:
            self.flush_wal()
        except sqlite3.Error as e:
            logger.debug(f"WAL checkpoint failed during close: {e}")
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error as e:
                logger.debug(f"SQLite close failed: {e}")
            self._conn = None
