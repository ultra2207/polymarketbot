"""The paper engine's measured microstructure gate.

Paper has the same L2 books and public prints live feeds into
``UnifiedRiskEngine``, so it can run the same quote-level gate rather than skip
microstructure entirely. This module is the adapter between the two:

* :func:`build` loads the shared fitted :class:`RiskCalibration` (the OFI /
  pull / widen fit) and builds one :class:`UnifiedRiskEngine` per measured
  market tick, using the paper engine's own reprice cadence
  (``engine.interval``) as the decision horizon and the paper engine's shared
  ``RiskConfig``. A missing calibration, config, horizon or tick source is a
  disabled gate with a stated reason - never a fabricated coefficient, horizon
  or tick.
* :func:`feed` pushes every two-sided book and every dated print to the gate.
  One-sided / missing books and prints without a timestamp are skipped, never
  defaulted.
* :func:`attach` wires the built gate into ``RiskManager.set_micro_risk`` and
  logs the gate state, measured source and reason. When the gate is not built
  it leaves ``RiskManager._micro`` unset, which makes ``validate_quote`` skip
  the microstructure step instead of guessing one.

Per-market tick limitation
--------------------------
``UnifiedRiskEngine`` binds a single ``tick_size`` at construction because its
fill and markout tables are keyed by tick. The paper venue has a per-market
tick, and the published ``Market`` payload carries it (Gamma
``orderPriceMinTickSize``, ``strategy.venue.reads.Market.tick``). Rather than
pick one tick for the whole universe, this adapter owns one shared engine per
measured tick and dispatches ``evaluate`` by the token's own market tick. The
shared engine therefore still never sees a fabricated tick; the limitation is
that a token with no measured market tick has no gate, and the adapter reports
``measured=False`` for it. The fit's fill/markout grid only covers the ticks it
measured, so an unmeasured tick degrades to ``MAINTAIN`` with
``measured=False`` inside the shared engine.

The adapter is attached to ``RiskManager`` (which calls ``evaluate``) rather
than a raw ``UnifiedRiskEngine``: that is the only way to honour per-market
ticks without changing the shared engine's contract.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from polymarketbot.logs import get_logger
from polymarketbot.risk.contracts import RiskAction, RiskDecision
from polymarketbot.risk.models import RiskConfig
from polymarketbot.risk.unified import UnifiedRiskEngine
from polymarketbot.strategy.books import ladder
from polymarketbot.strategy.reward.measurement import (
    load_maker_markout,
    load_quote_fill_horizon,
)

log = get_logger(__name__)

#: Sentinel so ``None`` can mean "this measured input is absent" while the
#: default still loads it. Tests and callers can inject a measured fixture.
_UNSET: Any = object()


# ------------------------------------------------------------------ measuring
def _calibration_path() -> str:
    """The artifact ``load_risk_calibration`` reads, for the gate log."""
    try:
        from polymarketbot.live.base import RISK_CALIBRATION_PATH

        return str(RISK_CALIBRATION_PATH)
    except Exception:  # noqa: BLE001 - the path is only a log label
        return "data/derived/risk_calibration.json"


def _load_calibration() -> Any | None:
    """The shared measured OFI / pull / widen fit, or ``None`` when unfitted."""
    try:
        from polymarketbot.live.base import load_risk_calibration

        return load_risk_calibration()
    except Exception as exc:  # noqa: BLE001 - a missing fit is a disabled gate
        log.warning(f"paper microstructure gate: risk calibration unreadable ({exc})")
        return None


def _shared_config(engine: Any) -> RiskConfig | None:
    """The paper engine's shared ``RiskConfig``, or ``None`` when absent.

    This is the same object the live engine passes. A default ``RiskConfig()``
    would silently substitute a different bankroll and cap set, so absence is a
    refusal instead.
    """
    config = getattr(getattr(engine, "risk_manager", None), "config", None)
    return config if isinstance(config, RiskConfig) else None


def _reprice_horizon(engine: Any) -> float | None:
    """The paper engine's quote lifetime between reprices, or ``None``."""
    try:
        horizon = float(getattr(engine, "interval", 0.0) or 0.0)
    except (TypeError, ValueError):
        return None
    return horizon if horizon > 0.0 else None


def _measured_ticks(engine: Any) -> dict[str, float]:
    """``token_id -> measured market tick`` from the paper universe payload.

    The tick is Gamma's ``orderPriceMinTickSize`` captured on ``Market.tick``.
    A market without a positive tick, or a token without a market, is simply
    absent: there is no fallback constant.
    """
    by_condition = getattr(engine, "by_condition", None)
    markets: Iterable[Any]
    if isinstance(by_condition, Mapping) and by_condition:
        markets = by_condition.values()
    else:
        markets = getattr(engine, "markets", ()) or ()

    ticks: dict[str, float] = {}
    for market in markets:
        try:
            tick = float(getattr(market, "tick", 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
        if not 0.0 < tick < 1.0:
            continue
        for token in getattr(market, "token_ids", ()) or ():
            token_id = str(token)
            if token_id:
                ticks[token_id] = tick
    return ticks


def _load_tables() -> tuple[Any, Any] | None:
    """The shared measured fill/markout tables, or ``None`` when unavailable."""
    try:
        return load_quote_fill_horizon(), load_maker_markout()
    except (OSError, ValueError) as exc:
        log.warning(
            "paper microstructure gate: measured fill/markout tables unavailable "
            f"({exc})"
        )
        return None


# -------------------------------------------------------------------- adapter
@dataclass
class PaperMicroRisk:
    """One measured ``UnifiedRiskEngine`` per per-market tick, plus provenance."""

    calibration: Any | None
    config: RiskConfig | None
    horizon_seconds: float | None
    source: str
    reason: str
    fill_horizon: Any | None = None
    markout: Any | None = None
    engines: dict[float, UnifiedRiskEngine] = field(default_factory=dict)
    token_ticks: dict[str, float] = field(default_factory=dict)
    attached: bool = False
    _logged_state: str | None = field(default=None, repr=False)

    @property
    def built(self) -> bool:
        """True when at least one measured-tick gate exists."""
        return bool(self.engines)

    @property
    def tick_sizes(self) -> tuple[float, ...]:
        """The measured ticks this adapter has a gate for."""
        return tuple(sorted(self.engines))

    def gate_for_token(self, token_id: str) -> UnifiedRiskEngine | None:
        """The shared engine bound to this token's measured tick, or ``None``."""
        tick = self.token_ticks.get(str(token_id))
        if tick is None:
            return None
        return self.engines.get(tick)

    def evaluate(
        self, token_id: str, side: str, price: float, mid: float, ts: float
    ) -> RiskDecision:
        """Dispatch one quote decision to the engine for its market tick.

        A token with no measured tick never had a gate built. Returning
        ``MAINTAIN`` with ``measured=False`` is the shared engine's own
        "unmeasured inputs" answer: it neither invents a tick nor blocks a
        quote the paper engine has no evidence about.
        """
        gate = self.gate_for_token(token_id)
        if gate is None:
            return RiskDecision(
                action=RiskAction.MAINTAIN,
                side=str(side).upper(),
                reason=(
                    "token has no measured market tick; microstructure "
                    "unmeasured (no guessed tick)"
                ),
                measured=False,
                ts=float(ts),
            )
        return gate.evaluate(token_id, side, price, mid, ts)

    def ensure(self, engine: Any) -> None:
        """Create a shared engine for every newly measured market tick.

        Construction is deferred until the universe walker has published a
        market tick, so ``build`` can run before the first tick without
        assuming a tick. A newly built tick engine is picked up by the already
        attached adapter; only the first build attaches it to ``RiskManager``.
        """
        if (
            self.calibration is None
            or self.config is None
            or self.horizon_seconds is None
            or self.fill_horizon is None
            or self.markout is None
        ):
            return

        self.token_ticks.update(_measured_ticks(engine))
        for tick in sorted(set(self.token_ticks.values())):
            if tick in self.engines:
                continue
            try:
                self.engines[tick] = UnifiedRiskEngine(
                    tick_size=tick,
                    flow_factory=self.calibration.flow_factory(),
                    horizon_seconds=self.horizon_seconds,
                    consecutive_fills_threshold=(
                        self.calibration.consecutive_fills_threshold
                    ),
                    config=self.config,
                    fill_horizon=self.fill_horizon,
                    markout=self.markout,
                )
            except (AttributeError, TypeError, ValueError) as exc:
                log.warning(
                    f"paper microstructure gate: cannot build tick {tick:g} "
                    f"engine ({exc}); that tick stays unmeasured"
                )
        if not self.engines and self.calibration is not None:
            self.reason = (
                "no measured per-market tick yet (Market.tick / Gamma "
                "orderPriceMinTickSize); the unified engine binds one tick, so "
                "the adapter waits for a measured market tick instead of "
                "assuming one"
            )


@dataclass(frozen=True)
class FeedStats:
    """What one ``feed`` call accepted, for the engine log and the tests."""

    books: int = 0
    prints: int = 0
    skipped_books: int = 0
    skipped_prints: int = 0
    reason: str = ""


# ---------------------------------------------------------------------- build
def build(
    engine: Any,
    *,
    calibration: Any = _UNSET,
    config: RiskConfig | None = _UNSET,
    fill_horizon: Any = _UNSET,
    markout: Any = _UNSET,
) -> PaperMicroRisk:
    """Load the measured inputs and build the paper's microstructure gate.

    ``calibration`` defaults to the shared ``load_risk_calibration()`` fit.
    Passing ``None`` explicitly states "there is no fit" and yields a disabled
    adapter; it never falls back to guessed coefficients. ``config`` defaults
    to the shared ``RiskConfig`` on ``engine.risk_manager``. The fill/markout
    tables are the shared measured artifacts unless injected.
    """
    if calibration is _UNSET:
        calibration = _load_calibration()
    if config is _UNSET:
        config = _shared_config(engine)

    path = _calibration_path()
    source = (
        f"risk_calibration={path}; flow=its measured OFI coefficients; "
        f"horizon=engine.interval; tick=per-market Gamma "
        f"orderPriceMinTickSize; fills/markout=measured "
        f"ctf_quote_fill_horizon/ctf_maker_markout"
    )

    if calibration is None:
        return PaperMicroRisk(
            calibration=None,
            config=config,
            horizon_seconds=None,
            source=source,
            reason=(
                f"no fitted risk calibration at {path}; the shared "
                "microstructure gate stays disabled (no guessed coefficients)"
            ),
        )
    if config is None:
        return PaperMicroRisk(
            calibration=calibration,
            config=None,
            horizon_seconds=None,
            source=source,
            reason=(
                "no shared RiskConfig on engine.risk_manager; refusing to build "
                "the gate from defaults"
            ),
        )
    horizon = _reprice_horizon(engine)
    if horizon is None:
        return PaperMicroRisk(
            calibration=calibration,
            config=config,
            horizon_seconds=None,
            source=source,
            reason=(
                "paper reprice horizon unmeasured (engine.interval is missing "
                "or non-positive); refusing to price a quote lifetime that was "
                "never stated"
            ),
        )
    if fill_horizon is _UNSET or markout is _UNSET:
        tables = _load_tables()
        if tables is None:
            return PaperMicroRisk(
                calibration=calibration,
                config=config,
                horizon_seconds=horizon,
                source=source,
                reason=(
                    "measured fill/markout tables unavailable; the shared "
                    "engine's economics cannot be priced"
                ),
            )
        fill_horizon, markout = tables

    adapter = PaperMicroRisk(
        calibration=calibration,
        config=config,
        horizon_seconds=horizon,
        source=source,
        reason="",
        fill_horizon=fill_horizon,
        markout=markout,
    )
    adapter.ensure(engine)
    if not adapter.built:
        adapter.reason = (
            "no measured per-market tick yet (Market.tick / Gamma "
            "orderPriceMinTickSize); the unified engine binds one tick, so the "
            "adapter waits for a measured market tick instead of assuming one"
        )
    return adapter


# --------------------------------------------------------------------- attach
def _state_message(adapter: PaperMicroRisk) -> str:
    if adapter.built:
        ticks = ", ".join(f"{tick:g}" for tick in adapter.tick_sizes)
        return (
            "paper microstructure gate ENABLED "
            f"(ticks={ticks}; horizon={adapter.horizon_seconds:g}s; "
            f"consecutive_fills_threshold="
            f"{adapter.calibration.consecutive_fills_threshold}; "
            f"source={adapter.source}; reason=measured fit plus measured "
            "per-market ticks)"
        )
    return (
        f"paper microstructure gate DISABLED (source={adapter.source}; "
        f"reason={adapter.reason})"
    )


def _log_state(adapter: PaperMicroRisk) -> None:
    message = _state_message(adapter)
    if adapter._logged_state == message:
        return
    adapter._logged_state = message
    if adapter.built:
        log.info(message)
    else:
        log.warning(message)


def _attach_if_built(engine: Any, adapter: PaperMicroRisk) -> bool:
    """Wire the built gate into the shared risk manager, once, or explain why not."""
    if not adapter.built:
        _log_state(adapter)
        return False
    manager = getattr(engine, "risk_manager", None)
    setter = getattr(manager, "set_micro_risk", None)
    if setter is None:
        adapter.reason = (
            "engine has no shared RiskManager; the built gate cannot be attached"
        )
        _log_state(adapter)
        return False
    if not adapter.attached:
        setter(adapter)
        adapter.attached = True
    _log_state(adapter)
    return True


def attach(engine: Any, adapter: PaperMicroRisk | None = None) -> PaperMicroRisk:
    """Build (or adopt) the gate and attach it to the engine's risk manager.

    Called at startup this records the disabled state when the measured tick
    is not known yet; ``feed`` calls it again once the universe walker has
    published one. ``set_micro_risk`` is called only when an engine was built
    from a real calibration, a shared config and a measured tick.
    """
    if adapter is None:
        adapter = build(engine)
    engine._paper_micro = adapter
    adapter.ensure(engine)
    _attach_if_built(engine, adapter)
    return adapter


# ----------------------------------------------------------------------- feed
def _book_sides(book: Mapping[str, Any]) -> tuple[list[tuple[float, float]], list[tuple[float, float]]] | None:
    """The book's two valid ladders, or ``None`` when it is not two-sided."""
    bids = ladder(book, asks=False)
    asks = ladder(book, asks=True)
    if not bids or not asks:
        return None
    best_bid, best_ask = bids[0][0], asks[0][0]
    if not (0.0 < best_bid < best_ask < 1.0):
        return None
    return bids, asks


def feed(
    engine: Any,
    books: Mapping[str, Mapping[str, Any]],
    prints: Iterable[Any],
    now: float,
) -> FeedStats:
    """Push one tick's two-sided books and dated prints into the gate.

    Every accepted book goes through ``on_book(token, bids, asks, now)`` and
    every accepted print through ``on_trade(token, price, size, side, ts)``.
    Books without both sides, crossed books, prints without a positive
    timestamp and tokens with no measured market tick are skipped: each is
    missing data, not a zero.
    """
    adapter = getattr(engine, "_paper_micro", None)
    if not isinstance(adapter, PaperMicroRisk):
        return FeedStats(reason="paper microstructure gate is not attached")
    if adapter.calibration is None:
        return FeedStats(reason=adapter.reason)

    adapter.ensure(engine)
    _attach_if_built(engine, adapter)
    if not adapter.built:
        return FeedStats(reason=adapter.reason)

    stamp = float(now)
    accepted_books = 0
    skipped_books = 0
    for token_id, book in books.items():
        token = str(token_id)
        gate = adapter.gate_for_token(token)
        sides = (
            _book_sides(book)
            if gate is not None and isinstance(book, Mapping)
            else None
        )
        if gate is None or sides is None:
            skipped_books += 1
            continue
        bids, asks = sides
        gate.on_book(token, bids, asks, stamp)
        accepted_books += 1

    accepted_prints = 0
    skipped_prints = 0
    for print_ in prints:
        token = str(getattr(print_, "token_id", "") or "")
        try:
            ts = float(getattr(print_, "ts", 0.0) or 0.0)
            price = float(getattr(print_, "price", 0.0) or 0.0)
            size = float(getattr(print_, "size", 0.0) or 0.0)
        except (TypeError, ValueError):
            skipped_prints += 1
            continue
        gate = adapter.gate_for_token(token)
        if (
            gate is None
            or not token
            or ts <= 0.0
            or not 0.0 < price < 1.0
            or size <= 0.0
        ):
            skipped_prints += 1
            continue
        gate.on_trade(
            token, price, size, str(getattr(print_, "side", "") or "").upper(), ts
        )
        accepted_prints += 1

    return FeedStats(
        books=accepted_books,
        prints=accepted_prints,
        skipped_books=skipped_books,
        skipped_prints=skipped_prints,
    )


def get(engine: Any) -> PaperMicroRisk | None:
    """The engine's attached paper gate, if any (for the recorder/tests)."""
    adapter = getattr(engine, "_paper_micro", None)
    return adapter if isinstance(adapter, PaperMicroRisk) else None
