"""Live UMA proposal poller: the settlement pillar's verdict feed.

The parquet tape the settlement fair-value model reads is only as fresh as the
last collector run.  The pillar therefore polls the same ``ProposePrice`` /
``DisputePrice`` logs itself and merges them into the shared ``UmaVerdictTape``,
so the oracle verdict advances with the chain instead of with the collector.

Why the walk is windowed per provider
-------------------------------------
Measured 2026-11-09: dRPC's free plan rejects any ``eth_getLogs`` range wider
than 100 blocks, while publicnode serves 10,000 and rejects more.  The previous
monolith asked for one fixed 2,048-block range, was rejected every sweep, and
held its cursor forever - the settlement pillar had no oracle-priced market for
83 h.  A window is therefore a property of the *provider*: the first attempt
asks for the widest useful window, a rejection halves it down to a 100-block
floor and re-walks the same block (no gaps), and the served width is persisted
so the next sweep starts at the provider's known size.  A provider that cannot
serve even the floor rests instead of being hammered.

The walk is capped at ``UMA_POLL_MAX_WINDOWS`` requests per sweep, the tip is
read ``UMA_POLL_CONFIRMATIONS`` blocks behind the head, and the first sweep of
a process re-walks one liveness window so a cursor parked at the tip cannot
hide a proposal that is still tradable.  The cursor is persisted atomically;
malformed state is treated as "no state", never as a reason to crash.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from polymarketbot.strategy.onchain.core import RATE_LIMIT, classify_rpc_error, provider_name
from polymarketbot.data.uma import UMA_ADDRESSES, decode_log
from polymarketbot.logs import get_logger
from polymarketbot.strategy.settlement.fair_value import (
    TapeProposal,
    UmaVerdictTape,
)

log = get_logger("paper_uma_poll")

# The widest ``eth_getLogs`` range ever requested.  Measured free endpoints:
# publicnode serves 10,000, dRPC's free plan only 100.
UMA_POLL_MAX_WINDOW_BLOCKS: int = 1_000
# First attempt for a provider whose size is not known yet.
UMA_POLL_PROBE_WINDOW_BLOCKS: int = 1_000
# Never probe below this: dRPC's free plan serves 100 blocks.
UMA_POLL_MIN_WINDOW_BLOCKS: int = 100
# Requests per sweep.  The probe sequence 1,000 -> 500 -> 250 -> 125 -> 100
# alone uses five; the remaining three advance the cursor at the served size.
UMA_POLL_MAX_WINDOWS: int = 8
# The old monolith's fixed request size, now only the *minimum* boot lookback
# (a cold start always re-reads at least this many blocks).
UMA_POLL_MAX_BLOCKS: int = 2_048
# Used when the tape has not yet measured a liveness window.
UMA_POLL_LIVENESS_FALLBACK_SEC: float = 7_200.0
# The tip is read this far behind head: publicnode rejects the newest block.
UMA_POLL_CONFIRMATIONS: int = 2
# A cursor further behind than this cannot describe a live verdict; jump it
# forward instead of walking weeks of history at 100 blocks per request.
UMA_POLL_BACKLOG_MAX_BLOCKS: int = 10_000
# Rest a provider that rate-limits or cannot serve even the floor.
UMA_POLL_PROVIDER_COOLDOWN_S: float = 120.0
# When no provider served the current window, retry the sweep after this long.
UMA_POLL_BACKOFF_S: float = 30.0

# The events this poller merges.  ``ProposePrice`` and ``DisputePrice`` are the
# only Optimistic Oracle events that change the verdict index.
_POLL_EVENTS = frozenset({"ProposePrice", "DisputePrice"})
# 1.0 in the Optimistic Oracle's int256 fixed-point price encoding.
_ORACLE_PRICE_ONE = 1e18
_MAX_PLAUSIBLE_LIVENESS_SEC = 6 * 3600.0


class _ProviderState(dict):
    """One provider's persisted probe state (plain dict for JSON round-trip)."""


def _topics() -> Tuple[str, ...]:
    """topic0 of every event this module decodes (lazy: pulls the ABI table)."""
    from polymarketbot.data.uma import SPEC_BY_CONTRACT_TOPIC

    return tuple(
        sorted(
            {
                spec.topic0
                for spec in SPEC_BY_CONTRACT_TOPIC.values()
                if spec.name in _POLL_EVENTS
            }
        )
    )


class UmaProposalPoller:
    """Incremental ``eth_getLogs`` walk that keeps the UMA verdict tape live."""

    def __init__(
        self,
        *,
        tape: UmaVerdictTape,
        providers: list,
        state_path: str | Path,
        block_interval_s: float,
        liveness_fallback_sec: float,
        max_blocks: int,
        max_windows: int,
        confirmations: int = 0,
    ) -> None:
        self.tape = tape
        self.providers = [p for p in providers if p is not None]
        self.state_path = Path(state_path)
        self.block_interval_s = max(0.0, float(block_interval_s))
        self.liveness_fallback_sec = max(0.0, float(liveness_fallback_sec))
        self.max_blocks = max(1, int(max_blocks))
        self.max_windows = max(1, int(max_windows))
        self.confirmations = max(0, int(confirmations))
        # A window is never wider than the constructor's hard cap, the
        # provider's measured maximum, or the probe size.
        self.max_window_blocks = max(
            1, min(self.max_blocks, UMA_POLL_MAX_WINDOW_BLOCKS)
        )
        self.probe_window_blocks = min(
            self.max_window_blocks, UMA_POLL_PROBE_WINDOW_BLOCKS
        )
        self.min_window_blocks = max(
            1, min(self.probe_window_blocks, UMA_POLL_MIN_WINDOW_BLOCKS)
        )
        self._state = self.load_state(self.state_path)
        # The first sweep of a process re-walks one liveness window; later
        # sweeps continue from the cursor.  ``load_state`` cannot decide this:
        # the cursor file is shared by every process that polls.
        self._booted = False
        self._warn_at: Dict[str, float] = {}
        self._topics = _topics()

    # -- persistence --------------------------------------------------------
    @classmethod
    def load_state(cls, path: str | Path) -> Dict[str, Any]:
        """Read the cursor file; malformed or missing state means a cold start."""
        empty: Dict[str, Any] = {"cursor": None, "providers": {}, "next_at": 0.0}
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return empty
        if not isinstance(raw, dict):
            return dict(empty, providers={})
        state = dict(empty)
        try:
            cursor = raw.get("cursor")
            if cursor is not None:
                state["cursor"] = int(cursor)
            state["next_at"] = float(raw.get("next_at") or 0.0)
            entries = raw.get("providers") or {}
            if not isinstance(entries, dict):
                raise TypeError("providers is not a mapping")
            for url, entry in entries.items():
                if not isinstance(entry, dict):
                    continue
                window = entry.get("window")
                state["providers"][str(url)] = _ProviderState(
                    window=int(window) if window else None,
                    cooldown_until=float(entry.get("cooldown_until") or 0.0),
                    max_tried=bool(entry.get("max_tried")),
                )
        except (TypeError, ValueError, AttributeError):
            return dict(empty, providers={})
        return state

    def state(self) -> Dict[str, Any]:
        """The live cursor/probe state (the same object ``poll`` mutates)."""
        return self._state

    def _save_state(self) -> None:
        """Atomically persist the cursor.  An unwritable path degrades to memory."""
        path = self.state_path
        state = self._state
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(
                json.dumps(
                    {
                        "cursor": state.get("cursor"),
                        "providers": state.get("providers") or {},
                        "next_at": state.get("next_at") or 0.0,
                        "updated_ts": time.time(),
                    }
                ),
                encoding="utf-8",
            )
            os.replace(tmp, path)
        except OSError as exc:
            self._warn(
                "state",
                f"UMA live poll: cursor state unwritable ({exc}); "
                "resuming from memory only.",
                300.0,
            )

    def _warn(self, key: str, message: str, interval_s: float = 300.0) -> None:
        """Log a repeated failure at most once per interval."""
        now = time.time()
        if now - self._warn_at.get(key, 0.0) < interval_s:
            return
        self._warn_at[key] = now
        log.warning(message)

    # -- polling ------------------------------------------------------------
    def _head(self) -> Optional[int]:
        """Confirmed head from the first provider that answers."""
        for provider in self.providers:
            try:
                head = int(provider.block_number())
            except Exception:  # noqa: BLE001 - a dead provider is a failover
                continue
            if head > 0:
                return head - self.confirmations
        return None

    def _start_cursor(self, head: int) -> Optional[int]:
        """Where this sweep starts: cursor, or one liveness window on boot."""
        state = self._state
        measured = float(getattr(self.tape, "measured_liveness_sec", 0.0) or 0.0)
        window_sec = measured or self.liveness_fallback_sec
        lookback = (
            int(window_sec / self.block_interval_s) if self.block_interval_s > 0 else 0
        )
        lookback = max(self.max_blocks, lookback)
        cursor = state.get("cursor")
        if cursor is None:
            cursor = max(0, head - lookback)
        cursor = int(cursor)
        if not self._booted:
            self._booted = True
            boot_floor = max(0, head - lookback)
            if cursor > boot_floor:
                log.info(
                    f"UMA live poll: first sweep of this process re-walks "
                    f"{head - boot_floor:,} block(s) of liveness window from "
                    f"{boot_floor:,} (cursor was at {cursor:,})."
                )
                cursor = boot_floor
        if cursor > head:
            return None
        oldest_useful = head - UMA_POLL_BACKLOG_MAX_BLOCKS
        if cursor < oldest_useful:
            self._warn(
                "backlog",
                f"UMA live poll: cursor {cursor:,} is {head - cursor:,} blocks "
                f"behind head; jumping to {oldest_useful:,} (older proposals "
                "cannot be a live verdict).",
                600.0,
            )
            cursor = oldest_useful
        state["cursor"] = cursor
        return cursor

    def poll(self, now: float) -> int:
        """Advance the cursor one bounded sweep; return proposals merged."""
        now_ts = float(now)
        head = self._head()
        if head is None or head <= 0:
            return 0
        if not self.providers:
            return 0
        state = self._state
        cursor = self._start_cursor(head)
        if cursor is None:
            return 0
        if now_ts < float(state.get("next_at") or 0.0):
            return 0

        known: Dict[str, _ProviderState] = state.setdefault("providers", {})
        urls = [str(getattr(p, "url", "") or f"provider_{i}") for i, p in enumerate(self.providers)]
        clients = dict(zip(urls, self.providers))
        order = sorted(
            urls,
            key=lambda u: (
                -int((known.get(u) or {}).get("window") or self.max_window_blocks),
                urls.index(u),
            ),
        )
        addresses = sorted(UMA_ADDRESSES)
        topics = [list(self._topics)]
        windows = 0
        proposals_merged = 0
        disputes_merged = 0
        provider_used = ""
        last_error: Optional[BaseException] = None

        while cursor <= head and windows < self.max_windows:
            progressed = False
            pending = list(order)
            requeued: Dict[str, int] = {}
            while pending and not progressed and windows < self.max_windows:
                url = pending.pop(0)
                entry = known.setdefault(
                    url, _ProviderState(window=None, cooldown_until=0.0, max_tried=False)
                )
                if now_ts < float(entry.get("cooldown_until") or 0.0):
                    continue
                window = int(entry.get("window") or self.probe_window_blocks)
                window = max(
                    self.min_window_blocks,
                    min(self.max_window_blocks, window, self.max_blocks),
                )
                probing = False
                if not entry.get("max_tried") and window < self.max_window_blocks:
                    window = min(self.max_window_blocks, self.max_blocks)
                    probing = True

                to_block = min(head, cursor + window - 1)
                req_from = cursor
                span = to_block - req_from + 1
                if span < self.min_window_blocks:
                    # A short tail is padded backwards, never skipped: the
                    # cursor still advances to ``to_block``.
                    req_from = max(0, to_block - self.min_window_blocks + 1)
                    span = to_block - req_from + 1

                windows += 1
                try:
                    logs = clients[url].get_logs(
                        address=addresses,
                        from_block=req_from,
                        to_block=to_block,
                        topics=topics,
                    )
                except Exception as exc:  # noqa: BLE001 - provider failure is expected
                    last_error = exc
                    name = provider_name(url)
                    if classify_rpc_error(exc) == RATE_LIMIT:
                        entry["cooldown_until"] = now_ts + UMA_POLL_PROVIDER_COOLDOWN_S
                        self._warn(
                            f"rl:{name}",
                            f"UMA live poll: {name} rate limited; resting it "
                            f"{UMA_POLL_PROVIDER_COOLDOWN_S:.0f}s and re-walking "
                            f"{cursor:,}..{to_block:,} on another provider.",
                        )
                    elif probing:
                        entry["max_tried"] = True
                        entry["window"] = min(span, self.probe_window_blocks)
                        if requeued.get(url, 0) < 2 or not pending:
                            requeued[url] = requeued.get(url, 0) + 1
                            pending.insert(0, url)
                        self._warn(
                            f"window:{name}",
                            f"UMA live poll: {name} rejected a {span}-block "
                            f"eth_getLogs window ({exc}); window now "
                            f"{entry['window']} blocks.",
                        )
                    elif span > self.min_window_blocks:
                        entry["max_tried"] = True
                        entry["window"] = max(
                            self.min_window_blocks, span // 2
                        )
                        if requeued.get(url, 0) < 2 or not pending:
                            requeued[url] = requeued.get(url, 0) + 1
                            pending.insert(0, url)
                        self._warn(
                            f"window:{name}",
                            f"UMA live poll: {name} rejected a {span}-block "
                            f"eth_getLogs window ({exc}); window now "
                            f"{entry['window']} blocks.",
                        )
                    else:
                        entry["cooldown_until"] = now_ts + UMA_POLL_PROVIDER_COOLDOWN_S
                        self._warn(
                            f"logs:{name}",
                            f"UMA live poll: {name} cannot serve a {span}-block "
                            f"window ({exc}); cursor held at {cursor:,}.",
                        )
                    self._save_state()
                    continue

                proposals, disputes = self._decode_logs(
                    logs,
                    now_ts=now_ts,
                    recv_ns=time.time_ns(),
                    head=head,
                    block_interval_s=self.block_interval_s,
                )
                if proposals or disputes:
                    proposals_merged += self.tape.ingest(proposals, disputes)
                disputes_merged += len(disputes)
                if probing:
                    entry["max_tried"] = True
                    entry["window"] = self.max_window_blocks
                elif span >= window:
                    entry["window"] = max(int(entry.get("window") or 0), span)
                cursor = to_block + 1
                state["cursor"] = cursor
                state["next_at"] = 0.0
                provider_used = provider_name(url)
                self._save_state()
                progressed = True

            if not progressed:
                state["next_at"] = now_ts + UMA_POLL_BACKOFF_S
                self._save_state()
                self._warn(
                    "sweep",
                    f"UMA live poll: no provider served the window at "
                    f"{cursor:,}..{head:,} ({last_error}); cursor held at "
                    f"{cursor:,}, resuming in {UMA_POLL_BACKOFF_S:.0f}s.",
                )
                break

        if cursor != state.get("cursor"):
            state["cursor"] = cursor
        log.info(
            f"UMA live poll: {windows} window(s), {proposals_merged} proposal "
            f"verdict(s), {disputes_merged} dispute(s) merged; cursor {cursor:,} "
            f"(head {head:,}, {max(0, head - cursor + 1):,} block(s) behind) via "
            f"{provider_used or 'no provider'}."
        )
        return proposals_merged

    # -- decoding -----------------------------------------------------------
    def _decode_logs(
        self,
        logs: Sequence[Dict[str, Any]],
        *,
        now_ts: float,
        recv_ns: int,
        head: int,
        block_interval_s: float,
    ) -> Tuple[List[TapeProposal], List[str]]:
        """Logs -> (proposals, disputed questions).

        ``ProposePrice`` has no condition id; the tape indexes it by question
        and the radar binds the exact id from its scanned universe later.
        """
        from polymarketbot.data.uma import ancillary_title, parse_log

        events: Dict[str, Tuple[str, Optional[TapeProposal]]] = {}
        for raw in logs:
            row = parse_log(raw, recv_ts_ns=recv_ns)
            if row is None:
                continue
            decoded = decode_log(row)
            if not decoded:
                continue
            question = ancillary_title(decoded.get("ancillary_text")) or ancillary_title(
                decoded.get("ancillary_data")
            )
            if not question:
                continue
            event = str(decoded.get("event") or "")
            if event == "DisputePrice":
                events[question] = ("dispute", None)
                continue
            if event != "ProposePrice":
                continue
            identifier = str(decoded.get("identifier_text") or "")
            if identifier and identifier != "YES_OR_NO_QUERY":
                continue
            try:
                price = float(str(decoded.get("proposed_price"))) / _ORACLE_PRICE_ONE
            except (TypeError, ValueError):
                continue
            if price >= 0.999:
                price = 1.0
            elif price <= 0.001:
                price = 0.0
            else:
                # A fractional (scalar) proposal says nothing about which
                # outcome pays; it is not a YES/NO verdict.
                continue
            expiry_ts = 0.0
            try:
                candidate = float(decoded.get("expiry"))
                if candidate > 0.0:
                    expiry_ts = candidate
            except (TypeError, ValueError):
                expiry_ts = 0.0
            # ``eth_getLogs`` returns no block timestamp.  Estimate it from the
            # measured block interval so the proposal's age and the overlay's
            # lookback cut-off stay meaningful; the oracle's own ``expiry`` is
            # authoritative for liveness whenever it is present.
            log_ts = now_ts
            if head > 0 and block_interval_s > 0:
                try:
                    log_block = int(row.get("block_number") or 0)
                    if 0 < log_block <= head:
                        log_ts = now_ts - (head - log_block) * block_interval_s
                except (TypeError, ValueError):
                    log_ts = now_ts
            liveness = 0.0
            if expiry_ts:
                liveness = max(0.0, expiry_ts - now_ts)
                if liveness > _MAX_PLAUSIBLE_LIVENESS_SEC:
                    liveness = 0.0
            events[question] = (
                "propose",
                TapeProposal(
                    question=question,
                    condition_id=str(decoded.get("condition_id") or "").lower(),
                    proposed_price=price,
                    ts=log_ts,
                    liveness_sec=liveness,
                    expiry_ts=expiry_ts,
                ),
            )
        proposals = [p for kind, p in events.values() if kind == "propose" and p]
        disputes = [q for q, (kind, _p) in events.items() if kind == "dispute"]
        return proposals, disputes

    # -- decision gates -----------------------------------------------------
    def is_live(
        self,
        proposal: Optional[TapeProposal],
        category: str = "settlement",
        now: Optional[float] = None,
    ) -> bool:
        """True while ``proposal`` can still be the tradable oracle verdict.

        The oracle's ``expiry_ts`` is the clock when it is present; otherwise
        the proposal's own liveness (or the tape's measured window, or this
        poller's fallback) supplies it.  A proposal is *not* discarded when its
        liveness window closes - the relayer/redeem legs are what remain - but
        it must stay inside one measured settlement horizon past that deadline.
        """
        if proposal is None:
            return False
        now_ts = time.time() if now is None else float(now)
        measured = float(getattr(self.tape, "measured_liveness_sec", 0.0) or 0.0)
        expiry = float(getattr(proposal, "expiry_ts", 0.0) or 0.0)
        if expiry > 0.0:
            deadline = expiry
        else:
            window = (
                float(getattr(proposal, "liveness_sec", 0.0) or 0.0)
                or measured
                or self.liveness_fallback_sec
            )
            if window <= 0.0:
                return False
            deadline = float(getattr(proposal, "ts", 0.0) or 0.0) + window
        if deadline <= 0.0:
            return False
        horizon = measured if measured > 0.0 else self.liveness_fallback_sec
        return now_ts <= deadline + horizon

    def fundable(
        self,
        verdict: Any,
        *,
        category: str = "settlement",
        now: Optional[float] = None,
    ) -> bool:
        """The funding gate: only a live/or final verdict may fund a buy.

        ``gamma_payout`` is the market's final payout vector and always funds.
        An ``uma_tape`` verdict must still resolve to a proposal on the tape
        whose liveness holds at decision time.  Anything else has no
        independently measurable value and must not fund.
        """
        source = str(
            getattr(verdict, "source", "") or getattr(verdict, "verdict_source", "") or ""
        )
        if not source:
            return False
        if source != "uma_tape":
            return True
        condition_id = str(getattr(verdict, "condition_id", "") or "")
        question = str(getattr(verdict, "question", "") or "")
        proposal = self.tape.proposal_for(condition_id, question)
        return self.is_live(proposal, category, now)
