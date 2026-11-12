"""The run record and the collaborators the runner façade binds.

Every field of :class:`RunContext` is a value the original single-coroutine
runner held in a local variable. It moved onto one object so the phases can
share it without threading a forty-argument call through each seam; the values
are written and read in exactly the order they were before.

:class:`Deps` exists for the names the runner module is patched on. The gating
tests and operator harnesses override ``runner.LiveClobClient``,
``runner.RealMoneyTradingEngine``, ``runner.resolve_market_details`` and
friends; those overrides must keep redirecting the phases that construct or
call them, so the façade reads its own module globals at entry and hands them
here instead of the phases importing the classes directly.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Deps:
    """Collaborators bound from the façade's module at run entry."""

    credentials_cls: Any
    clob_cls: Any
    ctf_cls: Any
    feed_cls: Any
    engine_cls: Any
    resolve_market_details: Callable[..., dict[str, Any]]
    derive_quote_size: Callable[..., float]
    optimize_spread_offset: Callable[..., Any]
    cancel_attempts: int
    cancel_backoff: float


@dataclass
class RunContext:
    """One live run's mutable state, shared by the supervision phases."""

    args: argparse.Namespace
    deps: Deps
    dry_run: bool

    # Venue clients.
    creds: Any = None
    clob_client: Any = None
    ctf_client: Any = None
    settlement_available: bool = False

    # Wallet and the sizing cap derived from it.
    total_wallet: float = 0.0
    budget_pct: float = 0.0
    max_budget: float | None = None
    trading_bankroll: float = 0.0
    quote_sz: float = 0.0
    spread_off: float = 0.0
    spread_policy: dict[str, Any] | None = None
    daily_rew: float = 0.0
    max_spr: float = 0.0
    min_sz: float = 0.0
    competing_override: bool = False
    comp_score: float | None = None

    # The market, the feed and the engine that trade it.
    market: dict[str, Any] = field(default_factory=dict)
    feed: Any = None
    engine: Any = None
    recorder: Any = None
    order_version: int | None = None

    # Loop bookkeeping.
    l2_session: Any = None
    start_time: float = 0.0
    switcher: Any = None
    switch_excluded_conditions: set = field(default_factory=set)
    last_orders_sync: float = 0.0
    last_balance_sync: float = 0.0
    last_reward_refresh: float = 0.0
    last_market_check: float = 0.0
    last_market_switch_scan: float = 0.0
    last_render: float = 0.0
    last_quote_eval: float = 0.0
    last_redeem_attempt: float = 0.0
    last_refit_at: float = 0.0
    refit_task: Any = None
    refit_process: Any = None
    refit_failures: int = 0
    feed_halt_active: bool = False
    market_halt_done: bool = False
    quotes_cancelled: bool = False
    redeem_done: bool = False
    redeem_skip_warned: bool = False
    awaiting_ws_ready: bool = False
    running: bool = True

    # Outcome.
    final_telemetry: dict[str, Any] | None = None
    exit_code: int = 0
