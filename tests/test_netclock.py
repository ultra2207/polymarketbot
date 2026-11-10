from __future__ import annotations

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from polymarketbot import netclock as nc

SECOND = 1_000_000_000
MS = 1_000_000


# ------------------------------------------------------------------ offsets
def test_estimate_offset_zero_when_clocks_agree():
    t1 = 1_793_771_000 * SECOND
    t2 = t1 + 200 * MS
    ts = t1 + 100 * MS  # stamped at the symmetric midpoint
    offset_ns, rtt_ns = nc.estimate_offset(t1, ts, t2)
    assert offset_ns == 0
    assert rtt_ns == 200 * MS


def test_estimate_offset_positive_when_venue_clock_is_ahead():
    t1 = 1_793_771_000 * SECOND
    t2 = t1 + 300 * MS
    true_offset = 412 * MS
    ts = t1 + 150 * MS + true_offset
    offset_ns, rtt_ns = nc.estimate_offset(t1, ts, t2)
    assert offset_ns == true_offset
    assert rtt_ns == 300 * MS


def test_estimate_offset_negative_when_venue_clock_is_behind():
    t1 = 1_793_771_000 * SECOND
    t2 = t1 + 80 * MS
    ts = t1 + 40 * MS - 250 * MS
    offset_ns, _ = nc.estimate_offset(t1, ts, t2)
    assert offset_ns == -250 * MS


def test_estimate_offset_truncates_an_odd_midpoint_by_one_nanosecond():
    t1 = 1_793_771_000 * SECOND
    t2 = t1 + 1  # one nanosecond round trip
    offset_ns, rtt_ns = nc.estimate_offset(t1, t1, t2)
    assert rtt_ns == 1
    assert offset_ns == 0  # floor((t1 + t1 + 1) / 2) == t1


def test_estimate_offset_rejects_a_response_before_the_request():
    with pytest.raises(ValueError):
        nc.estimate_offset(10, 10, 9)


def test_one_way_offset_understates_the_offset_by_the_delay():
    venue_ts = 1_793_771_000 * SECOND + 412 * MS
    one_way_delay = 60 * MS
    recv_ts = venue_ts + one_way_delay - 412 * MS  # local clock 412 ms behind
    assert nc.one_way_offset_ns(venue_ts, recv_ts) == 412 * MS - one_way_delay
    assert nc.one_way_offset_ns(venue_ts, recv_ts) < 412 * MS


def test_interval_is_half_the_round_trip_plus_the_venue_window():
    binance = nc.ClockSample("binance", "fapi.binance.com", 800 * MS, 240 * MS, 0)
    assert nc.offset_interval(binance) == (800 * MS - 120 * MS, 800 * MS + 120 * MS)
    clob = nc.ClockSample("clob", "clob.polymarket.com", 400 * MS, 240 * MS, 0)
    assert nc.offset_interval(clob) == (400 * MS - 120 * MS, 400 * MS + 120 * MS + SECOND)


def test_bracket_intersects_the_samples_of_one_venue():
    samples = [
        nc.ClockSample("clob", "clob.polymarket.com", -115 * MS, 160 * MS, 0),
        nc.ClockSample("clob", "clob.polymarket.com", 604 * MS, 170 * MS, 0),
    ]
    lo, hi = nc.offset_bracket(samples)
    assert lo == 604 * MS - 85 * MS
    assert hi == -115 * MS + 80 * MS + SECOND


def test_bracket_uses_the_low_rtt_cluster_when_samples_disagree():
    samples = [
        nc.ClockSample("binance", "fapi.binance.com", 700 * MS, 20 * MS, 0),
        nc.ClockSample("binance", "fapi.binance.com", 900 * MS, 20 * MS, 0),
    ]
    lo, hi = nc.offset_bracket(samples)
    # Equal RTTs keep both in the cluster; their intervals disagree, so the
    # tightest single sample (the first by stable sort) is returned.
    assert lo == 700 * MS - 10 * MS
    assert hi == 700 * MS + 10 * MS


def test_bracket_rejects_a_high_rtt_outlier():
    samples = [
        nc.ClockSample("binance", "fapi.binance.com", 700 * MS, 20 * MS, 0),
        nc.ClockSample("binance", "fapi.binance.com", 5_000 * MS, 2_000 * MS, 0),
    ]
    lo, hi = nc.offset_bracket(samples)
    assert lo == 700 * MS - 10 * MS
    assert hi == 700 * MS + 10 * MS


def test_bracket_of_nothing_is_an_error():
    with pytest.raises(ValueError):
        nc.offset_bracket([])


# ------------------------------------------------------------------ parsing
def test_parse_binance_time_converts_milliseconds():
    payload = {"serverTime": 1_793_771_316_561}
    assert nc.parse_binance_time(payload) == 1_793_771_316_561 * MS


def test_parse_clob_time_converts_whole_seconds():
    assert nc.parse_clob_time(1_793_771_316) == 1_793_771_316 * SECOND


def test_parse_clob_time_accepts_a_mapping_and_a_string():
    assert nc.parse_clob_time({"time": 1_793_771_316}) == 1_793_771_316 * SECOND
    assert nc.parse_clob_time({"timestamp": "1793771316561"}) == 1_793_771_316_561 * MS
    assert nc.parse_clob_time("1793771316") == 1_793_771_316 * SECOND


def test_parse_time_detects_nanoseconds_and_microseconds():
    assert nc._to_ns(1_793_771_316_561_000) == 1_793_771_316_561_000 * 1000  # microseconds
    assert nc._to_ns(1_793_771_316_561_000_000) == 1_793_771_316_561_000_000  # nanoseconds
    assert nc._to_ns(1_793_771_316.5) == 1_793_771_316_500_000_000  # fractional seconds


@pytest.mark.parametrize("payload", [None, True, {}, {"foo": 1}, 0, -5, "nope"])
def test_parse_rejects_payloads_without_a_time(payload):
    with pytest.raises((ValueError, TypeError)):
        nc.parse_server_time("clob", payload)


def test_parse_server_time_rejects_an_unknown_venue():
    with pytest.raises(KeyError):
        nc.parse_server_time("kalshi", {"time": 1})


# ------------------------------------------------------------------ requests
class _FakeResponse:
    def __init__(self, status_code: int, body: bytes) -> None:
        self.status_code = status_code
        self.content = body


class _FakeSession:
    def __init__(self, body: bytes, status_code: int = 200) -> None:
        self.body = body
        self.status_code = status_code
        self.calls: list[str] = []

    def get(self, url: str, timeout: float | None = None) -> _FakeResponse:
        self.calls.append(url)
        return _FakeResponse(self.status_code, self.body)


def test_sample_venue_measures_the_round_trip_between_the_clock_reads(monkeypatch):
    readings = iter([1_000 * SECOND, 1_000 * SECOND + 240 * MS])
    monkeypatch.setattr(nc.time, "time_ns", lambda: next(readings))
    session = _FakeSession(b'{"serverTime": 1793771316561}')
    sample = nc.sample_venue("binance", session=session)
    assert sample.stream == "binance"
    assert sample.host == "fapi.binance.com"
    assert sample.rtt_ns == 240 * MS
    # ts = 1793771316.561 s; midpoint of the local reads is 1000 s + 120 ms
    assert sample.offset_ns == 1_793_771_316_561 * MS - (1_000 * SECOND + 120 * MS)
    assert sample.recv_ts_ns == 1_000 * SECOND + 240 * MS
    assert session.calls == [nc.BINANCE_TIME_URL]


def test_sample_venue_raises_on_a_non_200_response():
    session = _FakeSession(b"1", status_code=429)
    with pytest.raises(RuntimeError, match="429"):
        nc.sample_venue("clob", session=session)


def test_probe_is_round_robin_and_skips_failures(monkeypatch):
    seen: list[tuple[int, str]] = []

    def fake_sample(venue, *, url=None, session=None, timeout=nc.DEFAULT_TIMEOUT_SECONDS):
        seen.append((len(seen), venue))
        if venue == "clob" and len([1 for _, v in seen if v == "clob"]) == 1:
            raise RuntimeError("boom")
        return nc.ClockSample(venue, "host", 1, 2, 3)

    monkeypatch.setattr(nc, "sample_venue", fake_sample)
    monkeypatch.setattr(nc.time, "sleep", lambda seconds: None)
    samples = nc.probe(["binance", "clob"], count=2, interval_seconds=0)
    assert [sample.stream for sample in samples] == ["binance", "binance", "clob"]
    assert [venue for _, venue in seen] == ["binance", "clob", "binance", "clob"]


# ------------------------------------------------------------------- storage
def test_clock_offset_schema_matches_the_sample_row():
    sample = nc.ClockSample("clob", "clob.polymarket.com", 5, 7, 9)
    assert sample.to_row() == {
        "stream": "clob",
        "host": "clob.polymarket.com",
        "offset_ns": 5,
        "rtt_ns": 7,
        "recv_ts_ns": 9,
    }
    assert nc.CLOCK_OFFSET_SCHEMA.names == list(sample.to_row())
    assert nc.CLOCK_OFFSET_SCHEMA.field("offset_ns").type == pa.int64()


def test_record_samples_writes_one_parquet_part_with_the_schema(tmp_path):
    samples = [
        nc.ClockSample("binance", "fapi.binance.com", 412 * MS, 240 * MS, 1_793_771_316 * SECOND),
        nc.ClockSample("binance", "fapi.binance.com", 410 * MS, 260 * MS, 1_793_771_317 * SECOND),
        nc.ClockSample("clob", "clob.polymarket.com", -390 * MS, 300 * MS, 1_793_771_318 * SECOND),
    ]
    path = nc.record_samples(samples, tmp_path)
    assert path is not None and path.exists()
    assert path.parent.parent.name.startswith("date=")
    table = pq.read_table(path)
    assert table.schema.names == nc.CLOCK_OFFSET_SCHEMA.names
    assert table.num_rows == 3
    assert not list((tmp_path / "clock_offset").glob("**/*.tmp"))


def test_record_samples_is_a_no_op_without_samples(tmp_path):
    assert nc.record_samples([], tmp_path) is None
    assert not (tmp_path / "clock_offset").exists()


def test_load_samples_returns_every_recorded_sample_per_venue(tmp_path):
    first = [
        nc.ClockSample("binance", "fapi.binance.com", 400 * MS, 300 * MS, 1_793_771_316 * SECOND),
        nc.ClockSample("clob", "clob.polymarket.com", -400 * MS, 320 * MS, 1_793_771_316 * SECOND),
    ]
    second = [
        nc.ClockSample("binance", "fapi.binance.com", 412 * MS, 120 * MS, 1_793_771_400 * SECOND),
        nc.ClockSample("clob", "clob.polymarket.com", -395 * MS, 280 * MS, 1_793_771_400 * SECOND),
    ]
    nc.record_samples(first, tmp_path)
    nc.record_samples(second, tmp_path)
    samples = nc.load_samples(tmp_path)
    assert len(samples["binance"]) == 2
    assert sorted(s.offset_ns for s in samples["binance"]) == [400 * MS, 412 * MS]
    best = nc.best_sample(samples["binance"])
    assert best is not None
    assert best.offset_ns == 412 * MS
    assert best.rtt_ns == 120 * MS


def test_load_samples_keeps_the_newest_parts_only(tmp_path):
    for index in range(3):
        nc.record_samples(
            [nc.ClockSample("clob", "clob.polymarket.com", index * MS, 100 * MS, index)],
            tmp_path,
        )
    assert len(nc.load_samples(tmp_path, max_parts=2)["clob"]) == 2
    assert len(nc.load_samples(tmp_path)["clob"]) == 3


def test_load_samples_is_empty_before_any_probe(tmp_path):
    assert nc.load_samples(tmp_path) == {}
