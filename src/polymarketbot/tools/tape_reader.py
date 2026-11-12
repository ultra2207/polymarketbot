"""Read tape partitions that were written by more than one recorder.

Why this exists
---------------
The tape grew under three writers: the deleted collector, ``reward_tape`` and
``crypto_updown_books``. They agree on column *names* but not on every column
*type*, measured on ``data/tape/clob_trades`` (638 parts, 2026-11-11)::

    shard            int16          | int64
    fee_rate_bps     double         | null
    transaction_hash large_string   | null

DuckDB coerces all three silently, which is why the harvester reads the tape
fine. PyArrow does not: ``pq.read_table(paths)`` fails before reading a row.
Without ``partitioning=None`` it fails on the hive columns instead
(``date`` is ``date32`` inside the files and ``dictionary<string>`` from the
directory names); with it, it fails on the null-typed columns
(``Unsupported cast from double to null``).

Both failures name neither the column nor the part, so this module:

* lists the files itself and disables hive partitioning,
* unifies each column across the parts (null yields to the observed type,
  integer widths widen to ``int64``, ``string``/``large_string`` to
  ``large_string``, everything else must already match),
* reads and conforms one part at a time, and
* raises :class:`TapeSchemaError` naming the column, both types and a sample
  part when the types genuinely cannot be reconciled.

Usage
-----
    from polymarketbot.tools.tape_reader import read_tape, tape_files

    rows = read_tape("data/tape/clob_trades", columns=["recv_ts_ns", "asset_id"])
    only_hour_07 = read_tape(tape_files("data/tape/clob_trades", hours=["07"]))
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from polymarketbot.logs import get_logger

log = get_logger("tape_reader")


class TapeSchemaError(RuntimeError):
    """Two parts of the same tape table disagree on a column's type."""


def tape_files(
    root: str | Path,
    *,
    hours: Sequence[str] | None = None,
    table: str | None = None,
) -> list[Path]:
    """Every ``date=*/hour=*/*.parquet`` part under ``root``, oldest first.

    ``root`` may be the tape root (``data/tape``) with ``table="clob_trades"``,
    or the table directory itself. ``hours`` keeps only those hour partitions
    (``"07"``), which is how a bounded measurement selects its input.

    The file name is not part of the contract: ``clob_trades`` writes
    ``minute=*.parquet`` parts, while ``clob_events`` writes ``part-*.parquet``.
    Globbing ``minute=*.parquet`` made every events read raise
    ``FileNotFoundError`` (measured: 0 of 703 parts found), so the hour
    directory is the unit.
    """
    base = Path(root)
    if table is not None:
        base = base / table
    files = sorted(base.glob("date=*/hour=*/*.parquet"))
    if hours:
        wanted = {f"hour={h}" for h in hours}
        files = [path for path in files if path.parent.name in wanted]
    return files


def _widen(types: Sequence[pa.DataType], column: str, path: Path) -> pa.DataType:
    """One type for ``column`` across the parts, or a named refusal."""
    distinct = [t for t in types if not pa.types.is_null(t)]
    if not distinct:
        return pa.null()
    unique = {str(t) for t in distinct}
    if len(unique) == 1:
        return distinct[0]
    if all(pa.types.is_integer(t) for t in distinct):
        return pa.int64()
    if all(pa.types.is_floating(t) for t in distinct):
        return pa.float64()
    if all(pa.types.is_string(t) or pa.types.is_large_string(t) for t in distinct):
        return pa.large_string()
    raise TapeSchemaError(
        f"{column}: parts disagree on type ({', '.join(sorted(unique))}); "
        f"see e.g. {path}"
    )


def _target_schema(
    files: Sequence[Path], columns: Sequence[str] | None
) -> tuple[pa.Schema, list[tuple[Path, pa.Schema]]]:
    """The unified schema, plus each part's own schema (read once, from the footer)."""
    schemas = [(path, pq.read_schema(path)) for path in files]
    names: list[str] = []
    for _path, schema in schemas:
        for name in schema.names:
            if name not in names:
                names.append(name)
    if columns is not None:
        missing = [c for c in columns if c not in names]
        if missing:
            raise TapeSchemaError(f"column(s) not in any part: {missing}")
        names = [n for n in names if n in set(columns)]
    fields = []
    for name in names:
        types = [
            schema.field(name).type for _path, schema in schemas if name in schema.names
        ]
        fields.append(pa.field(name, _widen(types, name, files[0])))
    return pa.schema(fields), schemas


def _conform(table: pa.Table, target: pa.Schema, path: Path) -> pa.Table:
    arrays = []
    for field in target:
        if field.name in table.column_names:
            column = table.column(field.name)
            if not column.type.equals(field.type):
                try:
                    column = column.cast(field.type)
                except (pa.ArrowInvalid, pa.ArrowNotImplementedError) as exc:
                    raise TapeSchemaError(
                        f"{field.name}: cannot cast {column.type} to {field.type} "
                        f"in {path} ({exc})"
                    ) from exc
            arrays.append(column)
        else:
            arrays.append(pa.nulls(table.num_rows, type=field.type))
    return pa.Table.from_arrays(arrays, schema=target)


def read_tape(
    source: str | Path | Iterable[str | Path],
    *,
    columns: Sequence[str] | None = None,
    hours: Sequence[str] | None = None,
    table: str | None = None,
) -> pa.Table:
    """Read a tape table with per-part type unification.

    ``source`` is a root directory, a list of part paths, or the output of
    :func:`tape_files`. An empty tape is a refusal, not an empty table: a
    caller that measures over it would report zero rather than admit the
    input is missing.
    """
    if isinstance(source, (str, Path)):
        files = tape_files(source, hours=hours, table=table)
    else:
        files = [Path(p) for p in source]
        if hours:
            wanted = {f"hour={h}" for h in hours}
            files = [p for p in files if p.parent.name in wanted]
        files = sorted(files)
    if not files:
        raise FileNotFoundError(
            f"no parquet parts under {source!r}"
            + (f" for hours {list(hours)}" if hours else "")
        )
    target, schemas = _target_schema(files, columns)
    parts = []
    for path, schema in schemas:
        # Read only the columns that exist in this part; the conform step fills
        # the ones it lacks with typed nulls.
        present = [name for name in target.names if name in schema.names]
        if present:
            table = pq.read_table(path, columns=present)
        else:
            # A part that carries none of the requested columns still contributes
            # its row count; keep it so nulls are typed for the right length.
            rows = pq.ParquetFile(path).metadata.num_rows
            table = pa.table({"__rows": pa.nulls(rows)})
        parts.append(_conform(table, target, path))
    merged = pa.concat_tables(parts, promote_options="none")
    log.debug(f"read tape: {merged.num_rows:,} rows from {len(files)} part(s)")
    return merged
