from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "bench_cancel_path", ROOT / "scripts" / "bench_cancel_path.py"
)
bench = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(bench)


def test_path_upper_is_the_signing_time_plus_one_rtt() -> None:
    assert bench.path_upper_ms(0.0121, 200.0) == 200.0
    assert bench.path_upper_ms(0.0, 217.5) == 217.5


def test_local_cancel_auth_is_sub_millisecond() -> None:
    result = bench.local_sign_ms(50)
    assert result["iterations"] == 50.0
    assert 0.0 <= result["p50_ms"] < 1.0
    assert result["p50_ms"] <= result["p99_ms"] <= result["max_ms"]
