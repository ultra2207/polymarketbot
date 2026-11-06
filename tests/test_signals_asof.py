"""As-of semantics of prepared signals: a replay may never see the future.

A signal table joined forward would let every cross-venue idea "predict" the
move it is meant to measure, so the lookup is pinned here: the value in force at
time t is the last row at or before t, and nothing before the first row exists.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd
import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "replay_all_markets.py"


def _load():
    spec = importlib.util.spec_from_file_location("replay_all_markets", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _book() -> object:
    replay = _load()
    frame = pd.DataFrame(
        {
            "asset_id": ["a", "a", "a", "b"],
            "ts_ms": [1_000, 2_000, 3_000, 1_500],
            "basis": [0.1, 0.2, 0.3, -0.1],
            "age_s": [1.0, 2.0, 3.0, 1.5],
        }
    )
    return replay.SignalBook(frame)


def test_value_at_or_before_the_time() -> None:
    book = _book()
    assert book("a", 1.0) == {"basis": 0.1, "age_s": 1.0}
    assert book("a", 2.5) == {"basis": 0.2, "age_s": 2.0}
    assert book("a", 3.0) == {"basis": 0.3, "age_s": 3.0}


def test_no_look_ahead_before_the_first_row() -> None:
    assert _book()("a", 0.5) == {}


def test_unknown_asset_is_empty_not_zero() -> None:
    assert _book()("missing", 9_999.0) == {}


def test_signals_validate_required_columns() -> None:
    replay = _load()
    build = importlib.util.spec_from_file_location(
        "build_idea_signals",
        Path(__file__).resolve().parent.parent / "scripts" / "build_idea_signals.py",
    )
    assert build is not None and build.loader is not None
    module = importlib.util.module_from_spec(build)
    build.loader.exec_module(module)
    with pytest.raises(ValueError):
        module.validate(pd.DataFrame({"asset_id": ["a"], "value": [1.0]}))
    with pytest.raises(ValueError):
        module.validate(pd.DataFrame({"asset_id": ["a"], "ts_ms": [1], "note": ["x"]}))
    module.validate(pd.DataFrame({"asset_id": ["a"], "ts_ms": [1], "basis": [0.1]}))
