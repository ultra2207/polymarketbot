"""Venue clients, wallet, market selection, preflight and session recording.

Each function returns the exit code the runner must abort with, or ``None``
when the phase completed and the next one may run. The codes, log text and
ordering are the original supervision coroutine's.
"""

from __future__ import annotations

import asyncio
import sys
from typing import TYPE_CHECKING

from polymarketbot.fees import FeeSchedule, FeeScheduleUnreadable, schedule_for_market
from polymarketbot.live.runner_market import _coerce_float, _settlement_available
from polymarketbot.live.runner_ui import write_banner
from polymarketbot.live.supervisor.helpers import (
    _close_engine_quietly,
    _thread_optional,
)
from polymarketbot.logs import get_logger
from polymarketbot.strategy.venue.clob import ClobAPIError

if TYPE_CHECKING:  # the run record only appears in annotations
    from polymarketbot.live.supervisor.context import RunContext

log = get_logger("real_money_trader")


async def prepare_venue(ctx: RunContext) -> int | None:
    """Build the clients and enforce the settlement gate before any money moves."""
    ctx.creds = ctx.deps.credentials_cls.from_env()
    ctx.clob_client = ctx.deps.clob_cls(credentials=ctx.creds, dry_run=ctx.dry_run)
    ctx.ctf_client = ctx.deps.ctf_cls(
        rpc_url=ctx.args.rpc_url,
        private_key=ctx.creds.private_key,
        dry_run=ctx.dry_run,
        signature_type=ctx.creds.signature_type,
    )
    ctx.settlement_available = _settlement_available(ctx.ctf_client)
    log.info(
        "[SETTLEMENT] On-chain CTF merge/redeem path: "
        + (
            "ENABLED (EOA signer)."
            if ctx.settlement_available
            else "REFUSED (proxy wallet, no signing key, or dry-run)."
        )
    )
    if not ctx.dry_run and not ctx.settlement_available and not ctx.args.allow_no_settlement:
        log.critical(
            "Refusing live trading: CTF merge/redeem is EOA-only and this account cannot "
            f"settle (signature_type={ctx.creds.signature_type}). Complete sets would hit the "
            "risk caps with no merge path. Use an EOA signer, or re-run with "
            "--allow-no-settlement to quote anyway (inventory can then only be exited by "
            "selling on the CLOB)."
        )
        return 2
    if not ctx.dry_run and not ctx.settlement_available:
        log.critical(
            "[SETTLEMENT] Live quoting allowed by --allow-no-settlement, but on-chain "
            "merge/redeem is unavailable; inventory can only be exited by selling on the CLOB."
        )
    return None


async def resolve_wallet(ctx: RunContext) -> int | None:
    """Read the wallet the sizing cap is derived from (gas reads are diagnostics)."""
    # -- Wallet: the CLOB collateral balance is the wallet. -----------------
    if ctx.dry_run:
        dry_initial = _coerce_float(ctx.args.initial_cash)
        if dry_initial is None or dry_initial <= 0.0:
            log.critical(
                f"--initial-cash must be a positive number for dry-run, got {ctx.args.initial_cash!r}."
            )
            return 1
        ctx.total_wallet = dry_initial
        log.info(
            f"[DRY-RUN WALLET] Using --initial-cash ${ctx.total_wallet:.2f} as the simulated "
            "wallet. No CLOB collateral is queried for sizing."
        )
    else:
        try:
            balance = await asyncio.to_thread(ctx.clob_client.get_balance)
        except ClobAPIError as exc:
            log.critical(f"Cannot read the CLOB collateral balance; refusing to trade live: {exc}")
            return 1
        available_cash = _coerce_float((balance or {}).get("available"))
        locked_raw = _coerce_float((balance or {}).get("locked"))
        if available_cash is None:
            log.critical(
                f"CLOB /balance returned no usable 'available' amount: {balance!r}. Refusing to size."
            )
            return 1
        locked_cash = locked_raw or 0.0
        ctx.total_wallet = available_cash
        log.info(
            f"[LIVE WALLET] CLOB available collateral ${available_cash:.2f} "
            f"(locked ${locked_cash:.2f})."
        )

    # On-chain reads are gas diagnostics only; they never size the position.
    wallet_addr = str(getattr(ctx.clob_client, "maker_address", "") or "")
    if wallet_addr and not ctx.dry_run:
        pol_balance, onchain_usdc = await asyncio.gather(
            _thread_optional("POL balance read", ctx.ctf_client.get_pol_balance, wallet_addr),
            _thread_optional("on-chain USDC read", ctx.ctf_client.get_usdc_balance, wallet_addr),
        )
        log.info(
            f"[GAS DIAGNOSTICS] POL {pol_balance} | on-chain USDC.e ${onchain_usdc} "
            "(diagnostics only; not used for sizing)."
        )
    return None


async def resolve_market(ctx: RunContext) -> int | None:
    """Resolve the market and its real token ids, refusing an unmergeable one."""
    ctx.market = await asyncio.to_thread(
        ctx.deps.resolve_market_details,
        ctx.feed,
        ctx.clob_client,
        ctx.args.condition_id,
        ctx.args.token_yes,
        ctx.args.token_no,
        ctx.trading_bankroll,
        ctx.args.min_size,
        ctx.args.max_spread,
        ctx.args.daily_reward,
    )

    if ctx.market["neg_risk"] and not ctx.dry_run:
        log.critical(
            "Refusing live trading on a neg-risk market: CTF mergePositions does not "
            "settle neg-risk positions (PolygonCtfClient has no NegRiskAdapter path). "
            f"Condition {ctx.market['condition_id']} has neg_risk=True. Pick a non-neg-risk "
            "market or run --dry-run."
        )
        return 2
    return None


def _market_fee_schedule(ctx: RunContext) -> FeeSchedule:
    """The market's own published fee schedule, or a refusal.

    The Gamma payload carried by the resolved market is the authority: a
    schedule that is present but unreadable stops startup instead of quoting
    with an assumed fee.
    """
    payload = ctx.market.get("raw_market") or ctx.market
    try:
        return schedule_for_market(payload)
    except FeeScheduleUnreadable as exc:
        log.critical(
            f"Refusing to trade {ctx.market.get('condition_id')}: {exc}"
        )
        raise SystemExit(1) from exc


def build_engine(ctx: RunContext) -> None:
    """Construct the single-market engine seeded with the sizing cap."""
    fee_schedule = _market_fee_schedule(ctx)
    ctx.engine = ctx.deps.engine_cls(
        condition_id=ctx.market["condition_id"],
        asset_id_yes=ctx.market["token_yes"],
        asset_id_no=ctx.market["token_no"],
        clob_client=ctx.clob_client,
        ctf_client=ctx.ctf_client,
        # Sizing seed for RiskConfig / quote sizing only. reconcile_startup()
        # replaces cash and the equity baseline with the venue's figures in live.
        initial_cash=ctx.trading_bankroll,
        total_wallet_balance=ctx.total_wallet,
        budget_pct=ctx.budget_pct,
        max_budget=ctx.max_budget,
        quote_size=ctx.quote_sz,
        spread_offset_cents=ctx.spread_off,
        daily_reward_rate=ctx.daily_rew,
        rewards_max_spread_cents=ctx.max_spr,
        rewards_min_size=ctx.min_sz,
        auto_merge_threshold=ctx.args.auto_merge_threshold,
        db_path=ctx.args.db_path,
        ledger_path=ctx.args.ledger_path,
        institutional_mode=not ctx.args.legacy_mode,
        competing_score_override=ctx.comp_score,
        tick_size=ctx.market["tick_size"],
        neg_risk=ctx.market["neg_risk"],
        event_id=ctx.market.get("event_id"),
        fee_rate_bps=ctx.market["fee_rate_bps"],
        min_order_usd=ctx.market["order_min_usd"],
        fee_schedule=fee_schedule,
        days_to_resolution=ctx.market["days_to_resolution"],
        book_stale_seconds=ctx.args.max_feed_staleness,
        dry_run=ctx.dry_run,
    )
    log.info(
        f"Fee schedule: rate={fee_schedule.rate} exponent={fee_schedule.exponent} "
        f"rebate_rate={fee_schedule.rebate_rate} source={fee_schedule.source}"
    )


async def run_startup_check(ctx: RunContext) -> int | None:
    """Reconcile against venue truth and refuse to trade on an unready preflight."""
    # -- Startup reconciliation (venue truth) -------------------------------
    try:
        await asyncio.to_thread(ctx.engine.reconcile_startup)
    except Exception as exc:  # noqa: BLE001 - any reconciliation failure must refuse live
        if ctx.dry_run:
            log.warning(f"Startup reconciliation failed in dry-run ({exc}); continuing to inspect.")
        else:
            log.critical(f"Startup reconciliation failed; refusing to trade live: {exc}")
            await _close_engine_quietly(ctx.engine)
            return 1

    preflight = await asyncio.to_thread(ctx.engine.preflight_check)
    sys.stdout.write(f"\n{preflight.summary()}\n")
    sys.stdout.flush()
    if not ctx.dry_run and not preflight.ready_for_live:
        log.critical(
            "Preflight is NOT ready for live trading. Aborting without placing orders; "
            "the runner does not downgrade to dry-run."
        )
        await _close_engine_quietly(ctx.engine)
        return 1

    # Rewards are paid per UTC day behind a $1 floor: settle any day that
    # already rolled over before the first quote, never mid-day.
    rewards_settled = await asyncio.to_thread(ctx.engine.settle_completed_reward_days)
    if rewards_settled > 0.0:
        log.info(f"Startup: settled rolled-over reward days for ${rewards_settled:.2f}")
    return None


async def start_recorder(ctx: RunContext) -> None:
    """Start the private session recorder; a failure never stops the run."""
    # -- Private session recording (non-blocking; the quote loop never waits) --
    ctx.recorder = None
    record_live_data = (
        ctx.args.record_live_data if ctx.args.record_live_data is not None else not ctx.dry_run
    )
    if record_live_data:
        try:
            from polymarketbot.live.recorder import LiveSessionRecorder

            ctx.recorder = LiveSessionRecorder(
                db_path=ctx.args.db_path,
                clob=ctx.clob_client,
                market=ctx.market,
                dry_run=ctx.dry_run,
                ctf_client=ctx.ctf_client,
                feed_age_fn=lambda: ctx.feed.last_message_age_seconds,
                engine_params={
                    "quote_size": ctx.quote_sz,
                    "spread_offset_cents": ctx.spread_off,
                    "bankroll_usd": ctx.trading_bankroll,
                    "budget_pct": ctx.budget_pct,
                    "institutional_mode": not ctx.args.legacy_mode,
                },
            )
            await asyncio.to_thread(ctx.recorder.start)
            ctx.engine.event_hook = ctx.recorder.record_event
            log.info(f"Live session recording -> {ctx.recorder.out_dir}")
        except Exception as exc:  # noqa: BLE001 - recording must never stop the run
            log.error(f"Live session recorder failed to start; continuing without it: {exc}")
            ctx.recorder = None


async def resolve_order_version(ctx: RunContext) -> None:
    """Find the CLOB order version without letting a dry-run touch the venue."""
    ctx.order_version = None
    try:
        if ctx.dry_run:
            # Never trigger GET /version in a dry-run: previews stay offline.
            cached_version = getattr(ctx.clob_client, "_order_version", None)
            ctx.order_version = int(cached_version) if isinstance(cached_version, int) else None
        else:
            ctx.order_version = await asyncio.to_thread(lambda: ctx.clob_client.order_version)
    except ClobAPIError as exc:
        log.warning(f"Could not resolve the CLOB order version ({exc}); orders will resolve it on first use.")


def write_run_banner(ctx: RunContext) -> None:
    """Print the operator banner that states what this run may actually do."""
    write_banner(
        dry_run=ctx.dry_run,
        order_version=ctx.order_version,
        signature_type=int(getattr(ctx.creds, "signature_type", 0) or 0),
        settlement_available=ctx.settlement_available,
        neg_risk=bool(ctx.market["neg_risk"]),
        total_wallet=ctx.total_wallet,
        trading_bankroll=ctx.trading_bankroll,
        budget_pct=ctx.budget_pct,
        max_budget=ctx.max_budget,
        condition_id=ctx.market["condition_id"],
    )
