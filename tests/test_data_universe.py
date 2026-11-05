from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone

import pytest

from polymarketbot.data.universe import UniverseState
from polymarketbot.logs import get_logger

universe_logger = get_logger("data.universe")


def _row(
    cid: str,
    *,
    daily: float = 20.0,
    end: str | None = "2027-01-01T00:00:00Z",
    question: str = "q",
    slug: str = "s",
    volume: str = "100",
) -> dict:
    campaign = {"rewardsDailyRate": daily, "startDate": "2026-01-01T00:00:00Z"}
    if end:
        campaign["endDate"] = end
    return {
        "conditionId": cid,
        "closed": False,
        "archived": False,
        "clobTokenIds": json.dumps([f"{cid}-yes", f"{cid}-no"]),
        "question": question,
        "slug": slug,
        "volume": volume,
        "liquidity": "50",
        "negRisk": False,
        "rewardsMinSize": "1",
        "rewardsMaxSpread": "3.5",
        "clobRewards": [campaign],
    }


def test_build_sums_only_active_campaigns(tmp_path):
    state = UniverseState(tmp_path / "manifest.json")
    rows = [
        _row("0xactive"),
        _row("0xexpired", end="2026-01-02T00:00:00Z"),
        _row("0xfuture", end="2027-01-01T00:00:00Z"),
    ]
    rows[2]["clobRewards"][0]["startDate"] = "2027-01-01T00:00:00Z"
    markets = state._build(rows)
    assert set(markets) == {"0xactive"}
    assert markets["0xactive"].daily_pool == 20.0
    assert markets["0xactive"].tokens == ("0xactive-yes", "0xactive-no")


def test_build_rejects_closed_and_tokenless(tmp_path):
    state = UniverseState(tmp_path / "manifest.json")
    closed = _row("0xclosed")
    closed["closed"] = True
    tokenless = _row("0xnotokens")
    tokenless["clobTokenIds"] = "[]"
    assert state._build([closed, tokenless]) == {}


def test_refresh_applies_a_complete_shrink(tmp_path):
    # A complete crawl that halves the universe is a real event (midterms
    # resolving), not a partial page. `_fetch_markets` raises on a failed or
    # truncated page, so the shrink is applied.
    state = UniverseState(tmp_path / "manifest.json")
    state.markets = state._build([_row(f"0x{i}") for i in range(10)])
    state.asset_to_market = {
        t: m.condition_id for m in state.markets.values() for t in m.tokens
    }

    def fake_fetch() -> list[dict]:
        return [_row(f"0x{i}") for i in range(2)]

    def no_rewards() -> list[dict]:
        raise RuntimeError("rewards endpoint offline in tests")

    state._fetch_markets = fake_fetch  # type: ignore[method-assign]
    state._fetch_reward_markets = no_rewards  # type: ignore[method-assign]
    asyncio.run(state.refresh())
    assert len(state.markets) == 2


def _reward_row(cid: str, rate: float = 100.0, **over: object) -> dict:
    row = {
        "condition_id": cid,
        "market_slug": f"slug-{cid}",
        "question": f"question-{cid}",
        "market_id": "123",
        "end_date": "2027-01-01T00:00:00Z",
        "rewards_config": [
            {
                "rate_per_day": rate,
                "start_date": "2026-01-01",
                "end_date": "2500-12-31",
            }
        ],
        "rewards_min_size": 200,
        "rewards_max_spread": 4.5,
        "volume_24hr": 1000,
        "tokens": [{"token_id": f"{cid}-yes"}, {"token_id": f"{cid}-no"}],
    }
    row.update(over)
    return row


def test_reward_walk_builds_markets_and_keeps_fresh_sentinels(tmp_path):
    state = UniverseState(
        tmp_path / "manifest.json",
        sentinel_question_patterns=["\\bbitcoin\\b"],
        sentinel_max_markets=2,
    )
    state.markets = state._build(
        [_row("0xsentinel", daily=0.0, question="Bitcoin Up or Down - 3PM ET")]
    )
    rows = [
        _reward_row("0xreward1", rate=100.0),
        _reward_row("0xreward2", rate=25.0),
        _reward_row("0xthin", rate=5.0),
    ]
    state._fetch_reward_markets = lambda: rows  # type: ignore[method-assign]
    state._fetch_markets = lambda: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("Gamma crawl must not run when the reward walk succeeds")
    )
    asyncio.run(state.refresh(notify=False))
    assert set(state.markets) == {"0xreward1", "0xreward2", "0xsentinel"}
    assert state.markets["0xreward1"].daily_pool == 100.0
    assert state.markets["0xreward1"].tokens == ("0xreward1-yes", "0xreward1-no")
    assert state.markets["0xsentinel"].sentinel is True


def test_reward_walk_falls_back_to_the_gamma_crawl(tmp_path):
    state = UniverseState(tmp_path / "manifest.json")

    def no_rewards() -> list[dict]:
        raise RuntimeError("endpoint down")

    state._fetch_reward_markets = no_rewards  # type: ignore[method-assign]
    state._fetch_markets = lambda: [_row("0xgamma")]  # type: ignore[method-assign]
    asyncio.run(state.refresh(notify=False))
    assert set(state.markets) == {"0xgamma"}


def test_reward_row_sums_active_campaigns_and_filters_below_cutoff(tmp_path):
    state = UniverseState(tmp_path / "manifest.json")
    row = _reward_row(
        "0xmulti",
        rewards_config=[
            {"rate_per_day": 10.0, "start_date": "2020-01-01", "end_date": "2500-12-31"},
            {"rate_per_day": 10.0, "start_date": "2020-01-01", "end_date": "2500-12-31"},
            {"rate_per_day": 1000.0, "start_date": "2500-01-01", "end_date": "2600-01-01"},
        ],
    )
    market = state._market_from_reward_row(row)
    assert market is not None and market.daily_pool == 20.0
    thin = state._market_from_reward_row(_reward_row("0xthin", rate=10.0))
    assert thin is None


def test_old_list_manifest_loads_but_is_stale(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps([{"condition_id": "0x1", "clob_tokens": ["a", "b"]}]))
    state = UniverseState(path)
    assert state._load_manifest() is True
    assert state._stale is True
    assert state.asset_to_market == {"a": "0x1", "b": "0x1"}


def test_poll_incremental_adds_new_and_removes_closed(tmp_path):
    state = UniverseState(tmp_path / "manifest.json", poll_seconds=60)
    state.markets = state._build([_row("0xold")])
    state.asset_to_market = {
        t: m.condition_id for m in state.markets.values() for t in m.tokens
    }
    state.last_market_id = 100
    state.last_poll_ts = time.time() - 60

    def fake(since_id: int, since_ts: float):
        assert since_id == 100
        closed = _row("0xold")
        closed["closed"] = True
        return [_row("0xnew")], [closed]

    state._fetch_incremental = fake  # type: ignore[method-assign]
    update = asyncio.run(state.poll_incremental())

    assert "0xnew" in state.markets
    assert "0xold" not in state.markets
    assert "0xnew-yes" in state.asset_to_market
    assert "0xold-yes" not in state.asset_to_market
    assert update is not None
    assert [m.condition_id for m in update.added_markets] == ["0xnew"]
    assert [m.condition_id for m in update.removed_markets] == ["0xold"]
    assert (tmp_path / "universe_state.json").exists()


def test_on_market_resolved_removes_and_queues(tmp_path):
    state = UniverseState(tmp_path / "manifest.json")
    state.markets = state._build([_row("0x1")])
    state.asset_to_market = {
        t: m.condition_id for m in state.markets.values() for t in m.tokens
    }

    state.on_market_resolved("0x1")
    assert state.markets == {}
    assert state.asset_to_market == {}
    update = state.updates.get_nowait()
    assert update.removed_assets == ["0x1-yes", "0x1-no"]

    state.on_market_resolved("0x1")  # idempotent: no second diff
    assert state.updates.empty()


def test_next_full_walk_delay_uses_cache_age(tmp_path):
    state = UniverseState(tmp_path / "manifest.json", refresh_seconds=3600)
    state.last_full_walk_ts = 1_000.0
    assert state.next_full_walk_delay(now=1_000.0 + 1_800) == pytest.approx(1_800)
    assert state.next_full_walk_delay(now=1_000.0 + 3_600) == 0.0
    assert state.next_full_walk_delay(now=1_000.0 + 7_200) == 0.0


def _locked(tmp_path, cids: list[str]):
    path = tmp_path / "manifest_locked.json"
    path.write_text(
        json.dumps(
            [
                {
                    "condition_id": cid,
                    "clob_tokens": [f"{cid}-yes", f"{cid}-no"],
                    "daily_pool": 20.0,
                    "min_size": 1.0,
                    "max_spread": 3.5,
                    "neg_risk": False,
                    "question": "q",
                    "slug": "s",
                    "volume": 100.0,
                    "liquidity": 50.0,
                }
                for cid in cids
            ]
        )
    )
    return path


def test_locked_mode_excludes_a_gamma_row_outside_the_set(tmp_path):
    locked = _locked(tmp_path, ["0xin"])
    state = UniverseState(
        tmp_path / "manifest.json", mode="locked", locked_manifest=locked
    )
    markets = state._build([_row("0xin"), _row("0xout")])
    assert set(markets) == {"0xin"}


def test_locked_mode_retains_a_market_absent_from_gamma(tmp_path):
    locked = _locked(tmp_path, ["0xpresent", "0xgone"])
    state = UniverseState(
        tmp_path / "manifest.json", mode="locked", locked_manifest=locked
    )

    def fake_fetch() -> list[dict]:
        return [_row("0xpresent")]

    state._fetch_markets = fake_fetch  # type: ignore[method-assign]
    asyncio.run(state.refresh())

    assert set(state.markets) == {"0xpresent", "0xgone"}
    assert state.markets["0xgone"].tokens == ("0xgone-yes", "0xgone-no")
    assert "0xgone-yes" in state.asset_to_market


def test_locked_refresh_never_adds_outside_the_set(tmp_path):
    locked = _locked(tmp_path, ["0xa"])
    state = UniverseState(
        tmp_path / "manifest.json", mode="locked", locked_manifest=locked
    )

    def fake_fetch() -> list[dict]:
        return [_row("0xa"), _row("0xnew")]

    state._fetch_markets = fake_fetch  # type: ignore[method-assign]
    update = asyncio.run(state.refresh())

    assert set(state.markets) == {"0xa"}
    assert "0xnew" not in state.markets
    assert "0xnew-yes" not in state.asset_to_market
    assert {m.condition_id for m in update.added_markets} <= {"0xa"}


def test_rolling_mode_accepts_a_new_row_above_the_cutoff(tmp_path):
    state = UniverseState(tmp_path / "manifest.json")
    assert state.mode == "rolling"
    markets = state._build([_row("0xnew", daily=25.0), _row("0xsmall", daily=5.0)])
    assert set(markets) == {"0xnew"}


def test_unknown_universe_mode_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="universe_mode"):
        UniverseState(tmp_path / "manifest.json", mode="fixed")


def test_locked_start_logs_mode_and_presence(tmp_path):
    locked = _locked(tmp_path, ["0xa", "0xb"])
    state = UniverseState(
        tmp_path / "manifest.json",
        mode="locked",
        locked_manifest=locked,
        poll_seconds=0,
    )

    def fake_fetch() -> list[dict]:
        return [_row("0xa")]

    state._fetch_markets = fake_fetch  # type: ignore[method-assign]

    messages: list[str] = []
    sink = universe_logger.add(
        lambda m: messages.append(m.record["message"]),
        level="INFO",
        filter=lambda r: r["extra"].get("name") == "data.universe",
    )

    async def run() -> None:
        await state.start()
        await state.stop()
        # The refresh loop's first crawl is what reports coverage; run it here
        # rather than racing the background task.
        await state.refresh()

    try:
        asyncio.run(run())
    finally:
        universe_logger.remove(sink)

    assert any(m.startswith("universe mode: locked") for m in messages)
    assert "locked universe: 1/2 markets present at Gamma" in messages
    assert set(state.markets) == {"0xa", "0xb"}


def test_start_uses_cache_without_a_full_fetch(tmp_path):
    state = UniverseState(tmp_path / "manifest.json", poll_seconds=0)
    state.markets = state._build([_row("0x1")])
    state.asset_to_market = {
        t: m.condition_id for m in state.markets.values() for t in m.tokens
    }
    state.generated_at = datetime.now(timezone.utc).isoformat()
    state.last_full_walk_ts = time.time()
    state._write_manifest()
    state._write_state()

    called: list[int] = []

    def fake_fetch() -> list[dict]:
        called.append(1)
        return []

    state._fetch_markets = fake_fetch  # type: ignore[method-assign]

    async def run() -> None:
        await state.start()
        await state.stop()

    asyncio.run(run())
    assert called == []
    assert len(state.markets) == 1


# --------------------------------------------------------------------------- sentinels
def test_below_cutoff_crypto_row_is_kept_as_a_sentinel(tmp_path):
    sentinel_cfg = ["\\bbitcoin\\b", "\\bbtc\\b"]
    state = UniverseState(
        tmp_path / "manifest.json", sentinel_question_patterns=sentinel_cfg
    )
    row = _row(
        "0xbtc",
        daily=0.0,
        question="Bitcoin Up or Down - November 5, 3PM ET",
        slug="bitcoin-up-or-down-november-5-3pm-et",
    )
    markets = state._build([row])
    assert set(markets) == {"0xbtc"}
    market = markets["0xbtc"]
    assert market.sentinel is True
    assert market.daily_pool == 0.0
    assert market.to_dict()["sentinel"] is True


def test_below_cutoff_row_without_a_pattern_is_excluded(tmp_path):
    state = UniverseState(tmp_path / "manifest.json")
    row = _row("0xpolitics", daily=5.0, question="Will the bill pass?", slug="bill")
    assert state._build([row]) == {}


def test_reward_qualified_rows_are_not_sentinels(tmp_path):
    state = UniverseState(tmp_path / "manifest.json")
    row = _row("0xbtc", daily=40.0, question="Bitcoin Up or Down - 3PM ET")
    markets = state._build([row])
    assert markets["0xbtc"].sentinel is False
    assert markets["0xbtc"].daily_pool == 40.0


def test_sentinel_disabled_ignores_the_pattern(tmp_path):
    state = UniverseState(tmp_path / "manifest.json", sentinel_enabled=False)
    row = _row("0xbtc", daily=0.0, question="Bitcoin Up or Down - 3PM ET")
    assert state._build([row]) == {}


def test_crawl_cap_keeps_the_highest_volume_sentinels(tmp_path):
    state = UniverseState(
        tmp_path / "manifest.json",
        sentinel_question_patterns=["\\bbitcoin\\b"],
        sentinel_max_markets=2,
    )
    rows = [
        _row("0xa", daily=0.0, question="Bitcoin Up", volume="10"),
        _row("0xb", daily=0.0, question="Bitcoin Up", volume="30"),
        _row("0xc", daily=0.0, question="Bitcoin Up", volume="20"),
    ]
    markets = state._build(rows)
    assert set(markets) == {"0xb", "0xc"}


def test_poll_cap_drops_the_lowest_volume_sentinel(tmp_path):
    state = UniverseState(
        tmp_path / "manifest.json",
        sentinel_question_patterns=["\\bbitcoin\\b"],
        sentinel_max_markets=0,
    )
    state.markets = state._build(
        [
            _row("0xa", daily=0.0, question="Bitcoin Up", volume="10"),
            _row("0xb", daily=0.0, question="Bitcoin Up", volume="30"),
            _row("0xc", daily=0.0, question="Bitcoin Up", volume="20"),
            _row("0xreward", daily=25.0),
        ]
    )
    state.sentinel_max_markets = 2
    state.asset_to_market = {
        t: m.condition_id for m in state.markets.values() for t in m.tokens
    }
    dropped = state._cap_sentinels()
    assert [m.condition_id for m in dropped] == ["0xa"]
    assert set(state.markets) == {"0xb", "0xc", "0xreward"}
    assert "0xa-yes" not in state.asset_to_market


def test_manifest_reward_total_excludes_sentinel_pools(tmp_path):
    state = UniverseState(tmp_path / "manifest.json")
    state.markets = state._build(
        [
            _row("0xreward", daily=25.0),
            _row("0xbtc", daily=10.0, question="Bitcoin Up or Down - 3PM ET"),
        ]
    )
    state.asset_to_market = {
        t: m.condition_id for m in state.markets.values() for t in m.tokens
    }
    state._write_manifest(version=False)
    payload = json.loads((tmp_path / "manifest.json").read_text())
    assert payload["count"] == 2
    assert payload["sentinel_count"] == 1
    assert payload["total_daily_pool"] == 25.0
    assert state.reward_pool_total == 25.0
    assert state.sentinel_count == 1


def test_locked_mode_adds_sentinels_outside_the_pinned_set(tmp_path):
    locked = _locked(tmp_path, ["0xin"])
    state = UniverseState(
        tmp_path / "manifest.json",
        mode="locked",
        locked_manifest=locked,
        sentinel_question_patterns=["\\bbitcoin\\b"],
    )
    btc = _row("0xbtc", daily=0.0, question="Bitcoin Up or Down - 3PM ET")

    def fake_fetch() -> list[dict]:
        return [_row("0xin"), btc]

    state._fetch_markets = fake_fetch  # type: ignore[method-assign]
    asyncio.run(state.refresh())

    assert set(state.markets) == {"0xin", "0xbtc"}
    assert state.markets["0xbtc"].sentinel is True
    assert state.markets["0xin"].sentinel is False
    # The pinned set is unchanged: the sentinel is subscribed on top of it.
    assert set(state.locked) == {"0xin"}
    assert state.sentinel_count == 1
    assert state.reward_pool_total == 20.0
    payload = json.loads((tmp_path / "manifest.json").read_text())
    assert payload["sentinel_count"] == 1
    assert payload["total_daily_pool"] == 20.0


def test_sentinel_round_trips_through_the_manifest(tmp_path):
    path = tmp_path / "manifest.json"
    state = UniverseState(path)
    state.markets = state._build(
        [_row("0xbtc", daily=0.0, question="Bitcoin Up or Down - 3PM ET")]
    )
    state._write_manifest(version=False)
    reloaded = UniverseState(path)
    assert reloaded._load_manifest() is True
    assert reloaded.markets["0xbtc"].sentinel is True
    assert reloaded.reward_pool_total == 0.0


def test_start_logs_the_sentinel_count(tmp_path):
    state = UniverseState(tmp_path / "manifest.json", poll_seconds=0)
    state.markets = state._build(
        [
            _row("0xreward", daily=25.0),
            _row("0xbtc", daily=0.0, question="Bitcoin Up or Down - 3PM ET"),
        ]
    )
    state.asset_to_market = {
        t: m.condition_id for m in state.markets.values() for t in m.tokens
    }
    state.generated_at = datetime.now(timezone.utc).isoformat()
    state.last_full_walk_ts = time.time()
    state._write_manifest(version=False)
    state._write_state()

    messages: list[str] = []
    sink = universe_logger.add(
        lambda m: messages.append(m.record["message"]),
        level="INFO",
        filter=lambda r: r["extra"].get("name") == "data.universe",
    )

    async def run() -> None:
        await state.start()
        await state.stop()

    try:
        asyncio.run(run())
    finally:
        universe_logger.remove(sink)

    assert "sentinel: 1 markets" in messages


def test_config_declares_the_sentinel_keys():
    from pathlib import Path

    import yaml

    path = Path(__file__).resolve().parents[1] / "configs" / "data_collection.yaml"
    clob = yaml.safe_load(path.read_text(encoding="utf-8"))["streams"]["clob"]
    assert clob["sentinel_enabled"] is True
    assert clob["sentinel_question_patterns"] == [
        "\\bbitcoin\\b",
        "\\bbtc\\b",
        "\\bethereum\\b",
        "\\beth\\b",
        "\\bsolana\\b",
        "\\bsol\\b",
    ]
