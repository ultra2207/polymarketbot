"""Reward-universe discovery: resumable keyset cursor, set replacement on a full pass."""

from __future__ import annotations

import json
from pathlib import Path

from polymarketbot.tools.reward_tape import RewardUniverse


def _market(condition_id: str, rate: float = 20.0, **overrides) -> dict:
    row = {
        "conditionId": condition_id,
        "slug": f"slug-{condition_id}",
        "clobRewards": [{"rewardsDailyRate": str(rate)}],
        "clobTokenIds": json.dumps([f"{condition_id}-up", f"{condition_id}-down"]),
        "outcomes": json.dumps(["Up", "Down"]),
        "rewardsMinSize": 50,
        "rewardsMaxSpread": 3,
        "closed": False,
        "active": True,
        "endDateIso": "2026-12-01T00:00:00Z",
    }
    row.update(overrides)
    return row


class FakeGamma:
    """Serves keyset pages keyed by the cursor that starts them."""

    def __init__(self, pages: dict[str | None, tuple[list[dict], str | None]]):
        self.pages = pages
        self.calls: list[str | None] = []

    def markets_keyset(self, *, closed=None, version=None, after_cursor=None,
                       limit=100, extra=None):
        self.calls.append(after_cursor)
        markets, next_cursor = self.pages[after_cursor]
        return {"markets": markets, "next_cursor": next_cursor}


def test_partial_cycle_persists_the_cursor_and_resumes(tmp_path: Path):
    cursor = tmp_path / "cursor.json"
    client = FakeGamma({
        None: ([_market("0xa")], "page2"),
        "page2": ([_market("0xb")], None),
    })
    universe = RewardUniverse(client, min_rate=15.0, max_markets=100,
                              max_pages=1, cursor_path=cursor)

    first = universe.discover(1000.0)
    assert [m["condition_id"] for m in first] == ["0xa"]
    saved = json.loads(cursor.read_text(encoding="utf-8"))
    assert saved["cursor"] == "page2"
    assert [m["condition_id"] for m in saved["markets"]] == ["0xa"]

    second = universe.discover(2000.0)
    # Resumed at page2 instead of re-walking page 1; the completed pass replaces
    # the set (0xa is gone because the full walk did not list it).
    assert client.calls == [None, "page2"]
    assert [m["condition_id"] for m in second] == ["0xb"]
    assert json.loads(cursor.read_text(encoding="utf-8"))["cursor"] is None


def test_a_found_market_survives_cycles_until_a_full_pass_completes(tmp_path: Path):
    cursor = tmp_path / "cursor.json"
    client = FakeGamma({
        None: ([_market("0xa")], "p2"),
        "p2": ([_market("0xb")], "p3"),
        "p3": ([], None),
    })
    universe = RewardUniverse(client, 15.0, 100, max_pages=1, cursor_path=cursor)
    assert [m["condition_id"] for m in universe.discover(1000.0)] == ["0xa"]
    assert [m["condition_id"] for m in universe.discover(1001.0)] == ["0xa", "0xb"]
    # The cycle that reaches the end of the walk is authoritative.
    assert [m["condition_id"] for m in universe.discover(1002.0)] == []


def test_without_a_cursor_path_every_cycle_is_a_full_walk(tmp_path: Path):
    client = FakeGamma({None: ([_market("0xa")], None)})
    universe = RewardUniverse(client, 15.0, 100, max_pages=3)
    universe.discover(1000.0)
    universe.discover(2000.0)
    assert client.calls == [None, None]


def test_resume_false_ignores_a_saved_cursor(tmp_path: Path):
    cursor = tmp_path / "cursor.json"
    client = FakeGamma({
        None: ([_market("0xa")], "page2"),
        "page2": ([_market("0xb")], None),
    })
    universe = RewardUniverse(client, 15.0, 100, max_pages=1, cursor_path=cursor)
    universe.discover(1000.0)          # stops at page2, saves it
    probe = universe.discover(1001.0, resume=False)
    assert client.calls == [None, None]  # probe re-walked from the start
    assert [m["condition_id"] for m in probe] == ["0xa"]


def test_expired_markets_are_dropped_when_the_state_loads(tmp_path: Path):
    cursor = tmp_path / "cursor.json"
    cursor.write_text(json.dumps({
        "cursor": "p2",
        "markets": [
            {"condition_id": "0xold", "tokens": ["a", "b"], "window_end_ms": 1_000,
             "reward_rate": 99.0, "sides": {}, "slug": "old"},
            {"condition_id": "0xlive", "tokens": ["c", "d"], "window_end_ms": 10 ** 13,
             "reward_rate": 50.0, "sides": {}, "slug": "live"},
        ],
    }), encoding="utf-8")
    # A partial cycle (it has a follow-on cursor) returns the accumulated set,
    # so the expired row would be visible here if loading did not drop it.
    client = FakeGamma({"p2": ([], "p3")})
    markets = RewardUniverse(client, 15.0, 100, max_pages=1,
                             cursor_path=cursor).discover(5000.0)
    assert [m["condition_id"] for m in markets] == ["0xlive"]


def test_unreadable_cursor_falls_back_to_a_fresh_walk(tmp_path: Path):
    cursor = tmp_path / "cursor.json"
    cursor.write_text("{not json", encoding="utf-8")
    client = FakeGamma({None: ([_market("0xa")], None)})
    markets = RewardUniverse(client, 15.0, 100, max_pages=2,
                             cursor_path=cursor).discover(1000.0)
    assert [m["condition_id"] for m in markets] == ["0xa"]
    assert json.loads(cursor.read_text(encoding="utf-8"))["cursor"] is None


def test_markets_below_the_rate_floor_or_unlisted_are_skipped(tmp_path: Path):
    client = FakeGamma({None: ([
        _market("0xlow", rate=1.0),
        _market("0xclosed", closed=True),
        _market("0xonetoken", clobTokenIds=json.dumps(["only"])),
        _market("0xok"),
    ], None)})
    markets = RewardUniverse(client, 15.0, 100, max_pages=1).discover(1000.0)
    assert [m["condition_id"] for m in markets] == ["0xok"]
