from __future__ import annotations

import json

from polymarketbot.data.universe_metadata import (
    apply_to_manifest,
    load_rows,
    to_row,
    write_rows,
)


def test_to_row_extracts_event_and_fee_schedule() -> None:
    row = to_row(
        {
            "conditionId": "0xAbC",
            "events": [
                {
                    "id": "30829",
                    "slug": "democratic-presidential-nominee-2028",
                    "title": "Democratic Presidential Nominee 2028",
                    "resolutionSource": "https://example.com/rules",
                }
            ],
            "feeType": "politics_fees",
            "feesEnabled": True,
            "feeSchedule": {"rate": 0.04, "exponent": 1, "rebateRate": 0.25},
            "negRisk": True,
            "tickSize": "0.01",
            "tags": [{"label": "Politics"}],
        }
    )
    assert row["condition_id"] == "0xAbC"
    assert row["event_id"] == "30829"
    assert row["event_slug"] == "democratic-presidential-nominee-2028"
    assert row["event_title"] == "Democratic Presidential Nominee 2028"
    assert row["fee_type"] == "politics_fees"
    assert row["fee_rate"] == 0.04
    assert row["fee_exponent"] == 1
    assert row["fee_rebate"] == 0.25
    assert row["resolution_source"] == "https://example.com/rules"
    assert row["tags"] == ["Politics"]


def test_apply_to_manifest_fills_fields_without_erasing(tmp_path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "markets": [
                    {"condition_id": "0xAAA", "question": "q", "fee_type": None},
                    {"condition_id": "0xBBB", "question": "kept"},
                ]
            }
        ),
        encoding="utf-8",
    )
    rows = [
        {
            "condition_id": "0xaaa",
            "event_id": "1",
            "event_slug": "e",
            "event_title": "E",
            "fee_type": "politics_fees",
            "fee_rate": 0.04,
            "fees_enabled": True,
        },
        {"condition_id": "0xbbb", "event_id": "2", "fee_type": None, "fee_rate": None},
    ]
    assert apply_to_manifest(rows, manifest) == 2
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    first, second = payload["markets"]
    assert first["event_id"] == "1" and first["fee_type"] == "politics_fees"
    assert first["question"] == "q"
    assert second["event_id"] == "2"
    assert "fee_type" not in second
    assert payload["metadata_updated_at"]


def test_metadata_parquet_round_trip(tmp_path) -> None:
    path = tmp_path / "meta.parquet"
    rows = [
        {
            "condition_id": "0xAAA",
            "event_id": "1",
            "event_title": "E",
            "fee_type": "politics_fees",
            "fee_rate": 0.04,
            "fee_exponent": 1.0,
            "fee_rebate": 0.25,
            "tags": ["Politics"],
        }
    ]
    assert write_rows(rows, path) == 1
    loaded = load_rows(path)
    assert loaded["0xaaa"]["fee_type"] == "politics_fees"
    assert loaded["0xaaa"]["tags"] == ["Politics"]


class _FakeGamma:
    """Returns only the markets it is told Gamma still publishes."""

    def __init__(self, rows):
        self.rows = rows
        self.asked: list[str] = []

    def markets(self, condition_ids):
        self.asked = list(condition_ids)
        return self.rows


def test_refresh_keeps_rows_for_conditions_gamma_no_longer_returns(tmp_path) -> None:
    """`write_rows` replaces the table, so a delisted market's fees were lost."""
    from polymarketbot.data.universe_metadata import refresh

    out = tmp_path / "meta.parquet"
    write_rows(
        [
            {"condition_id": "0xAAA", "fee_type": "politics_fees", "fee_rate": 0.04},
            {"condition_id": "0xGONE", "fee_type": "crypto_fees", "fee_rate": 0.07},
        ],
        out,
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"markets": [{"condition_id": "0xAAA"}]}), encoding="utf-8"
    )

    fake = _FakeGamma([
        {
            "conditionId": "0xAAA",
            "feeType": "politics_fees",
            "feesEnabled": True,
            "feeSchedule": {"rate": 0.04, "exponent": 1, "rebateRate": 0.25},
        }
    ])
    result = refresh(manifest, out, client=fake)

    assert "0xgone" in [cid.lower() for cid in fake.asked]  # kept rows stay in the fetch set
    assert len(fake.asked) == len({cid.lower() for cid in fake.asked})  # no double fetch
    loaded = load_rows(out)
    assert loaded["0xaaa"]["fee_rate"] == 0.04
    assert loaded["0xgone"]["fee_rate"] == 0.07  # not silently dropped
    assert result["written"] == 2


