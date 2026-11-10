#!/usr/bin/env python
"""Replay `polygon_logs` into the derived UMA event table.

Reads every ``data/tape/polygon_logs/**/*.parquet`` part, decodes UMA
Optimistic Oracle and Polymarket adapter events with
`polymarketbot.data.uma`, and appends them to
``data/derived/uma_events.parquet``.

Reruns are incremental: every output row records the tape part it came from,
so a part already present in the output is skipped. The output is rewritten
atomically (``.tmp`` + ``os.replace``), so a kill can lose the current part but
can never leave a half-written table behind.

A run is also bounded by ``--max-seconds`` (default 300) counting from start;
if the budget runs out the rows decoded so far are committed and the remaining
parts are left for the next run.

Usage:
    uv run python -m polymarketbot.data.uma_replay
    uv run python -m polymarketbot.data.uma_replay --reset
    uv run python -m polymarketbot.data.uma_replay --tape-root data/tape_smoke_x
    uv run python -m polymarketbot.data.uma_replay --probe --blocks 200

``--probe`` is a read-only sanity check against the chain: one `eth_getLogs`
per configured address over the last ``--blocks`` blocks, printing how many logs
each contract produced and which topic0s appeared. It answers "is the collector
subscribed to the right addresses?" without waiting for the tape to grow.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Sequence

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from polymarketbot.chain import ProviderPool, rpc_urls
from polymarketbot.data.config import DEFAULT_CONFIG, load_config
from polymarketbot.data.streams.polygon import (
    HttpRpc,
    PolygonSettings,
    parse_log,
    plan_windows,
)
from polymarketbot.data.uma import (
    CONTRACT_BY_ADDRESS,
    TOPIC0S,
    UMA_ADDRESSES,
    UMA_EVENTS_SCHEMA,
    UMA_EVENT_COLUMNS,
    decode_log,
)
from polymarketbot.logs import get_logger

DEFAULT_TAPE_ROOT = Path("data/tape")
DEFAULT_OUTPUT = Path("data/derived/uma_events.parquet")

# Columns read from the tape. Everything needed to rebuild a log row.
RAW_COLUMNS = (
    "recv_ts_ns",
    "block_number",
    "block_hash",
    "tx_hash",
    "tx_index",
    "log_index",
    "address",
    "topic0",
    "topic1",
    "topic2",
    "topic3",
    "data",
    "removed",
)

BATCH_ROWS = 65_536
# Distinct topic0s printed per address by `--probe`; the CTF emits many.
MAX_PROBE_TOPICS = 10


def polygon_section(config_path: Path = DEFAULT_CONFIG) -> dict[str, Any]:
    """The `streams.polygon` section, or ``{}`` when the config is unreadable."""
    try:
        config = load_config(config_path)
    except (OSError, ValueError):
        return {}
    return dict(config.get("streams", {}).get("polygon", {}))


def configured_addresses(config_path: Path = DEFAULT_CONFIG) -> tuple[str, ...]:
    """Every contract the live collector subscribes to, validated the same way.

    The replay filters on the same four keys as the stream (``ctf``, ``uma_oo``,
    ``uma_v1_adapter``, ``uma_adapter``), so a configured address with zero logs
    is visible in the report instead of silently missing from the filter. Falls
    back to the addresses `uma` can decode when the config is unreadable, so
    replaying a copied tape still works.
    """
    section = polygon_section(config_path)
    if not section.get("addresses"):
        return tuple(sorted(UMA_ADDRESSES))
    return PolygonSettings.from_section(section, fallbacks=[]).addresses


def probe_chunk(config_path: Path = DEFAULT_CONFIG) -> int:
    """Block window `--probe` splits into when a provider rejects the range.

    The stream's own `log_block_span` is the cap that makes the live fallback
    work (ten blocks on the Alchemy free tier), and it is exactly what makes a
    200-block probe work on the same provider.
    """
    section = polygon_section(config_path)
    try:
        return max(1, int(section.get("log_block_span", 10) or 10))
    except (TypeError, ValueError):
        return 10


def probe_logs(
    client: HttpRpc, address: str, low: int, high: int, *, chunk: int = 0
) -> list[dict[str, Any]]:
    """Logs for one address over [low, high], split only when the range fails.

    One address per call: a combined call cannot say which contract was missing
    when the answer is empty, and one provider error would hide the addresses
    that answered. `chunk` retries in smaller windows for providers that cap
    `eth_getLogs` (the Alchemy free tier caps at ten blocks).
    """
    try:
        return client.get_logs([address], low, high)
    except Exception:  # noqa: BLE001 - the split retry below is the fallback
        if chunk <= 0 or high - low + 1 <= chunk:
            raise
    rows: list[dict[str, Any]] = []
    for start, end in plan_windows(low, high, chunk):
        rows.extend(client.get_logs([address], start, end))
    return rows


def run_probe(
    addresses: Sequence[str],
    blocks: int,
    *,
    chunk: int = 0,
    log: Any = None,
    client: HttpRpc | None = None,
) -> int:
    """Read-only `eth_getLogs` probe: per-address log counts and topic0s seen."""
    log = log or get_logger("replay_uma")
    client = client or HttpRpc(ProviderPool(rpc_urls()))
    try:
        head = client.block_number()
        if head is None:
            log.error("probe: eth_blockNumber returned no block; no range to scan")
            return 1
        low = max(0, head - max(1, blocks) + 1)
        log.info(
            "probe: blocks {}..{} ({} blocks) for {} addresses",
            low,
            head,
            head - low + 1,
            len(addresses),
        )
        failures = 0
        for address in addresses:
            role = CONTRACT_BY_ADDRESS.get(address.lower(), "no UMA ABI")
            try:
                rows = probe_logs(client, address, low, head, chunk=chunk)
            except Exception as exc:  # noqa: BLE001 - one dead address must not stop the probe
                failures += 1
                log.error(
                    "probe {} ({}): {}: {}",
                    address,
                    role,
                    type(exc).__name__,
                    exc,
                )
                continue
            topics = Counter(
                str((parsed or {}).get("topic0") or "").lower()
                for parsed in (
                    parse_log(raw, recv_ts_ns=0, recv_mono_ns=0) for raw in rows
                )
            )
            log.info("probe {} ({}): {} logs", address, role, len(rows))
            for topic0, count in topics.most_common(MAX_PROBE_TOPICS):
                log.info("  topic0 {} c={}", topic0 or "(none)", count)
            if len(topics) > MAX_PROBE_TOPICS:
                log.info("  (+{} more topic0s)", len(topics) - MAX_PROBE_TOPICS)
        log.info(
            "probe done: {}/{} addresses answered", len(addresses) - failures, len(addresses)
        )
        return 1 if failures else 0
    finally:
        client.close()


def log_parts(tape_root: Path) -> list[Path]:
    """Every polygon log part, oldest first (the path starts with the date)."""
    return sorted((tape_root / "polygon_logs").rglob("*.parquet"))


def part_id(path: Path, tape_root: Path) -> str:
    try:
        return path.relative_to(tape_root).as_posix()
    except ValueError:
        return path.as_posix()


def default_journal(output: Path) -> Path:
    return output.with_suffix(".parts.json")


def load_journal(journal: Path) -> dict[str, int]:
    if not journal.exists():
        return {}
    try:
        payload = json.loads(journal.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    scanned = payload.get("scanned") if isinstance(payload, dict) else None
    if not isinstance(scanned, dict):
        return {}
    return {str(part): int(count or 0) for part, count in scanned.items()}


def save_journal(journal: Path, scanned: dict[str, int]) -> None:
    journal.parent.mkdir(parents=True, exist_ok=True)
    tmp = journal.with_name(journal.name + ".tmp")
    tmp.write_text(
        json.dumps({"scanned": dict(sorted(scanned.items()))}, indent=1),
        encoding="utf-8",
    )
    os.replace(tmp, journal)


def load_existing(output: Path) -> tuple[pa.Table | None, set[str]]:
    if not output.exists():
        return None, set()
    table = pq.read_table(output)
    if table.num_rows == 0:
        return table, set()
    if "source_part" not in table.column_names:
        # An older/partial output cannot be skipped safely; decode every part
        # again rather than claim rows that were never written.
        return table, set()
    done = {value for value in table.column("source_part").to_pylist() if value}
    return table, done


def scan_part(
    path: Path, source_part: str, addresses: Sequence[str]
) -> tuple[list[dict[str, Any]], Counter, int]:
    """Decode one parquet part; returns rows, observed counts, and decode failures."""
    observed: Counter = Counter()
    rows: list[dict[str, Any]] = []
    failed = 0
    value_set = sorted({address.lower() for address in addresses})
    known_topics = set(TOPIC0S)
    for batch in pq.ParquetFile(path).iter_batches(batch_size=BATCH_ROWS, columns=list(RAW_COLUMNS)):
        table = pa.Table.from_batches([batch])
        if table.num_rows == 0:
            continue
        lowered = pc.utf8_lower(table.column("address"))
        mask = pc.is_in(lowered, value_set=pa.array(value_set, pa.string()))
        selected = table.filter(pc.fill_null(mask, False))
        if selected.num_rows == 0:
            continue
        for row in selected.to_pylist():
            key = (str(row.get("address") or "").lower(), str(row.get("topic0") or "").lower())
            observed[key] += 1
            decoded = decode_log(row, source_part=source_part)
            if decoded is not None:
                rows.append(decoded)
            elif key[0] in CONTRACT_BY_ADDRESS and key[1] in known_topics:
                # A known event at a decodable address that will not decode is a
                # real problem (wrong address tag, malformed payload), not noise.
                failed += 1
    return rows, observed, failed


def write_output(output: Path, rows: list[dict[str, Any]]) -> None:
    rows = sorted(
        rows,
        key=lambda row: (
            row.get("block_number") or 0,
            row.get("log_index") or 0,
        ),
    )
    table = pa.Table.from_pylist(rows, schema=UMA_EVENTS_SCHEMA)
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_name(output.name + ".tmp")
    pq.write_table(table, tmp, compression="zstd", compression_level=7)
    os.replace(tmp, output)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tape-root", type=Path, default=DEFAULT_TAPE_ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--journal",
        type=Path,
        default=None,
        help="sidecar of already-scanned parts (default: <output>.parts.json)",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="decode every part and replace the output instead of appending",
    )
    parser.add_argument(
        "--max-parts",
        type=int,
        default=0,
        help="stop after this many new parts (0 = no part limit)",
    )
    parser.add_argument(
        "--max-seconds",
        type=float,
        default=300.0,
        help="wall-clock budget for this invocation; 0 disables it",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="decode and report, but do not write the output",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help="collector config whose streams.polygon.addresses define the filter",
    )
    parser.add_argument(
        "--probe",
        action="store_true",
        help="read-only eth_getLogs probe per configured address, then exit",
    )
    parser.add_argument(
        "--blocks",
        type=int,
        default=200,
        help="with --probe: how many of the newest blocks to scan",
    )
    parser.add_argument(
        "--chunk",
        type=int,
        default=0,
        help="with --probe: window size to retry with when a provider rejects the "
        "full range (0 = use the configured log_block_span)",
    )
    args = parser.parse_args(argv)

    log = get_logger("replay_uma")
    started = time.monotonic()

    addresses = configured_addresses(args.config)
    if args.probe:
        chunk = args.chunk if args.chunk > 0 else probe_chunk(args.config)
        return run_probe(addresses, args.blocks, chunk=chunk, log=log)

    parts = log_parts(args.tape_root)
    log.info("tape={} parts={} output={}", args.tape_root, len(parts), args.output)
    log.info(
        "address filter ({}) from {}: {}",
        len(addresses),
        args.config,
        ", ".join(addresses),
    )
    log.info(
        "topic0 decode set ({}): {}",
        len(TOPIC0S),
        ", ".join(TOPIC0S),
    )

    existing, done = load_existing(args.output)
    journal_path = args.journal or default_journal(args.output)
    journaled: dict[str, int] = load_journal(journal_path)
    if existing is None and journaled:
        # The output was deleted; the journal alone cannot say which rows were
        # written, so decode everything again.
        log.warning("journal={} exists without output; ignoring it", journal_path)
        journaled = {}
    done |= set(journaled)
    if args.reset:
        existing, done, journaled = None, set(), {}
        # Removing the old output first means a killed reset cannot leave old
        # rows behind for the next incremental run to append to.
        args.output.unlink(missing_ok=True)
        log.info("reset: removed {} and ignored the journal", args.output)
    else:
        log.info(
            "existing output rows={} parts_done={} (journal={})",
            0 if existing is None else existing.num_rows,
            len(done),
            journal_path,
        )

    new_rows: list[dict[str, Any]] = []
    observed: Counter = Counter()
    scanned_now: dict[str, int] = {}
    processed = 0
    stopped_early = False
    decode_errors = 0

    for index, path in enumerate(parts):
        pid = part_id(path, args.tape_root)
        if pid in done:
            continue
        if args.max_parts and processed >= args.max_parts:
            stopped_early = True
            log.info("stopping: --max-parts={} reached", args.max_parts)
            break
        if args.max_seconds and time.monotonic() - started >= args.max_seconds:
            stopped_early = True
            log.info("stopping: --max-seconds={} reached at part {}", args.max_seconds, pid)
            break
        try:
            rows, part_observed, part_failed = scan_part(path, pid, addresses)
        except Exception as exc:  # noqa: BLE001 - one corrupt part must not stop replay
            decode_errors += 1
            log.warning("part {} failed: {}: {}", pid, type(exc).__name__, exc)
            continue
        observed.update(part_observed)
        decode_errors += part_failed
        new_rows.extend(rows)
        scanned_now[pid] = len(rows)
        processed += 1
        if (index + 1) % 10 == 0 or index + 1 == len(parts):
            log.info(
                "scanned {}/{} parts={} new_rows={}",
                index + 1,
                len(parts),
                processed,
                len(new_rows),
            )

    by_event: Counter = Counter(row["event"] for row in new_rows)
    by_contract: Counter = Counter(row["contract"] for row in new_rows)

    by_address: Counter = Counter()
    for (address, _topic0), count in observed.items():
        by_address[address] += count
    log.info("raw logs per configured address ({}):", len(addresses))
    for address in addresses:
        key = address.lower()
        total = by_address.get(key, 0)
        role = CONTRACT_BY_ADDRESS.get(key, "no UMA ABI")
        if total:
            log.info("  {} {} rows={}", address, role, total)
        else:
            log.warning("  {} {} rows=0 (no logs in the scanned parts)", address, role)
    uma_observed = {
        key: count for key, count in observed.items() if key[0] in CONTRACT_BY_ADDRESS
    }
    if uma_observed:
        log.info(
            "raw UMA logs={} by (address, topic0):", sum(uma_observed.values())
        )
        for (address, topic0), count in Counter(uma_observed).most_common(10):
            log.info("  {} {} c={}", address, topic0 or "(none)", count)
    else:
        log.warning(
            "no polygon_logs rows at a decodable UMA address; the tape holds no UMA/adapter events"
        )

    log.info(
        "new UMA events={} (parts={}, decode_errors={}, stopped_early={})",
        len(new_rows),
        processed,
        decode_errors,
        stopped_early,
    )
    for event, count in by_event.most_common():
        log.info("  event {} c={}", event, count)
    for contract, count in by_contract.most_common():
        log.info("  contract {} c={}", contract, count)

    total = (0 if existing is None else existing.num_rows) + len(new_rows)
    if total == 0:
        log.warning(
            "0 UMA rows: address filter={} topic0_filter={}",
            list(addresses),
            list(TOPIC0S),
        )

    if args.dry_run:
        log.info("dry-run: output not written; would contain rows={}", total)
        return 0

    rows_out: list[dict[str, Any]] = []
    if existing is not None and existing.num_rows:
        # Cast first so a schema change in uma.py is always visible here rather
        # than silently dropping a column on the next append.
        existing = existing.select(list(UMA_EVENT_COLUMNS))
        rows_out.extend(existing.to_pylist())
    rows_out.extend(new_rows)
    # An empty result still writes the schema, so a downstream query sees
    # "0 rows" instead of a missing file.
    write_output(args.output, rows_out)
    save_journal(journal_path, {**journaled, **scanned_now})
    log.info("wrote {} rows to {} (journal parts={})", len(rows_out), args.output, len(journaled) + len(scanned_now))
    log.info("done in {:.1f}s", time.monotonic() - started)
    return 0


if __name__ == "__main__":
    sys.exit(main())
