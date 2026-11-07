"""Polymarket fee model.

Formula (docs, verified 2026-11-01):

    fee = C x rate x (p x (1 - p)) ^ exponent

* `C` is shares traded, `p` is price.
* Makers pay zero; only takers pay.
* Fees round to 5 decimal places, so anything below 0.00001 becomes zero.

The per-market `feeSchedule` from Gamma is authoritative. The era table in
`configs/fees.yaml` is a fallback for markets whose schedule has not been
fetched, and it encodes the published history:

    2026-01-05  crypto pilot begins (15m, then 5m markets)
    2026-03-30  Fee Structure V2: per-category rates
    2026-07-10  sports rate 0.03 -> 0.05, sports rebate 0.25 -> 0.15
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from functools import lru_cache
from pathlib import Path

import yaml

CONFIG = Path("configs/fees.yaml")


@dataclass(frozen=True)
class FeeSchedule:
    """A market's fee configuration, mirroring Gamma's `feeSchedule`."""

    rate: float
    exponent: float = 1.0
    taker_only: bool = True
    rebate_rate: float = 0.0
    source: str = "config"

    @classmethod
    def from_gamma(cls, d: dict) -> "FeeSchedule":
        return cls(
            rate=float(d.get("rate", 0.0)),
            exponent=float(d.get("exponent", 1.0)),
            taker_only=bool(d.get("takerOnly", True)),
            rebate_rate=float(d.get("rebateRate", 0.0)),
            source="gamma",
        )

    @classmethod
    def zero(cls) -> "FeeSchedule":
        return cls(rate=0.0, exponent=1.0, taker_only=True, rebate_rate=0.0,
                   source="zero")


@dataclass(frozen=True)
class _Era:
    name: str
    effective_from: datetime
    rates: dict[str, float]
    rebates: dict[str, float]
    exponents: dict[str, float]
    derived: bool = False


@lru_cache(maxsize=1)
def _config(path: str | Path = CONFIG) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _eras(path: str | Path = CONFIG) -> tuple[_Era, ...]:
    out = []
    for e in _config(path)["eras"]:
        when = datetime.fromisoformat(str(e["effective_from"])).replace(tzinfo=timezone.utc)
        out.append(_Era(
            e["name"], when, dict(e.get("rates") or {}),
            dict(e.get("rebates") or {}), dict(e.get("exponents") or {}),
            bool(e.get("derived")),
        ))
    return tuple(sorted(out, key=lambda x: x.effective_from))


def _round_fee(value: float, dp: int = 5) -> float:
    q = Decimal(1).scaleb(-dp)  # 0.00001 for dp=5
    return float(Decimal(repr(value)).quantize(q, rounding=ROUND_HALF_UP))


def taker_fee(shares: float, price: float, schedule: FeeSchedule,
              rounding_dp: int | None = None) -> float:
    """Fee in USDC for a taker fill. Makers pay nothing."""
    if shares <= 0 or schedule.rate <= 0:
        return 0.0
    if not 0.0 <= price <= 1.0:
        raise ValueError(f"price out of range: {price}")

    dp = rounding_dp if rounding_dp is not None else int(_config()["formula"]["rounding_dp"])
    curve = price * (1.0 - price)
    raw = shares * schedule.rate * (curve ** schedule.exponent)
    return _round_fee(raw, dp)


def maker_rebate(taker_fees: float, schedule: FeeSchedule) -> float:
    """Rebate paid to the resting maker, funded from collected taker fees."""
    return _round_fee(taker_fees * schedule.rebate_rate)


def fee_category(market_category: str, path: str | Path = CONFIG) -> str:
    """Map our coarse category to a fee-table category."""
    mapping = _config(path).get("category_map", {})
    return mapping.get(market_category, "other")


def era_schedule(category: str, when: datetime | str,
                 path: str | Path = CONFIG) -> FeeSchedule:
    """Fallback schedule from the published era table.

    `category` may be either our market category or a fee category; both are
    mapped through `category_map`.
    """
    if isinstance(when, str):
        when = datetime.fromisoformat(when.replace("Z", "+00:00"))
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)

    cat = fee_category(category, path)
    rate = 0.0
    rebate = 0.0
    exponent = 1.0
    era_name = "fee_free"
    derived = False
    for era in _eras(path):
        if era.effective_from <= when:
            era_name = era.name
            rate = float(era.rates.get(cat, 0.0))
            rebate = float(era.rebates.get(cat, 0.0))
            exponent = float(era.exponents.get(cat, 1.0))
            derived = era.derived
        else:
            break

    return FeeSchedule(
        rate=rate,
        exponent=exponent,
        taker_only=True,
        rebate_rate=rebate,
        source=f"era:{era_name}{':derived' if derived else ''}",
    )


def fee_curve_peak_rate(rate: float, exponent: float = 1.0) -> float:
    """Fee as a fraction of a $1 payout at p=0.5, for sanity checks.

    At p=0.5, p(1-p)=0.25, so the peak per share is rate * 0.25**exponent.
    """
    return rate * (0.25 ** exponent)


def schedule_for_fee_type(fee_type: str, when: datetime | str,
                          path: str | Path = CONFIG) -> FeeSchedule | None:
    """Schedule implied by a Gamma `feeType`, gated on its activation date.

    Returns `None` for an unknown type so the caller can fall back. Before
    `effective_from` the market was fee-free: fees were introduced per family
    (`crypto` 2026-01-05, `sports` 2026-02-18, everything else 2026-03-30), so a
    market that merely *carries* today's `feeType` did not necessarily pay it.
    """
    spec = (_config(path).get("fee_types") or {}).get(fee_type)
    if spec is None:
        return None
    when = _as_utc(when)
    start = datetime.fromisoformat(str(spec["effective_from"])).replace(tzinfo=timezone.utc)
    if when < start:
        return FeeSchedule.zero()
    return FeeSchedule(
        rate=float(spec.get("rate", 0.0)),
        exponent=float(spec.get("exponent", 1.0)),
        taker_only=True,
        rebate_rate=float(spec.get("rebate", 0.0)),
        source=f"type:{fee_type}",
    )


# `feeType` is stamped at market creation, so it dates the market rather than the
# fill: a 5-minute up/down market created in March 2026 reports `crypto_fees`
# (0.25, exponent 2) while its May twin reports `crypto_fees_v2` (0.07). Both
# variants are present in the data. The per-market schedule therefore carries
# more information than the category era table — it is the only source that
# knows about the 2026-02-18 NCAAB/Serie A sports rate (0.0175, 9,842 markets)
# and the crypto pilot curve, neither of which the era table models.
#
# Known bias: gating on `effective_from` charges 0.07 to long-dated crypto
# markets from 2026-01-05, whereas the changelog extended crypto fees to
# 1H/4H/daily/weekly markets only on 2026-03-06. That over-charges at most ten
# weeks of one family and never under-charges — the safe direction, because
# under-stating fees inflates every measured edge.
PILOT_START = datetime(2026, 1, 5, tzinfo=timezone.utc)
FEE_STRUCTURE_V2 = datetime(2026, 3, 30, tzinfo=timezone.utc)


def _as_utc(when: datetime | str) -> datetime:
    if isinstance(when, str):
        when = datetime.fromisoformat(when.replace("Z", "+00:00"))
    return when if when.tzinfo is not None else when.replace(tzinfo=timezone.utc)


def schedule_at(row: dict, when: datetime | str) -> FeeSchedule:
    """The schedule for a fill at `when` — the function a simulator should call.

    `schedule_from_market` answers "what does this market charge *now*"; this
    answers "what did it charge *then*". Gamma publishes only the current
    schedule, so using it unguarded charges 2026 fees on 2025 fills.

    Order matters:

    1. A known `feeType` decides outright, gated on that type's activation
       date. It is the most precise source we have.
    2. With no type, the category era decides *whether* fees applied, and the
       per-market rate decides *how much* — the rate alone would leak 2026 fees
       back into 2025.
    """
    when = _as_utc(when)

    fee_type = row.get("fee_type")
    if fee_type:
        sched = schedule_for_fee_type(fee_type, when)
        if sched is not None:
            return sched

    category = row.get("category") or "other"
    era = era_schedule(category, when)
    if era.rate <= 0:
        return era

    if row.get("fee_rate") is not None:
        return FeeSchedule(
            rate=float(row["fee_rate"]),
            exponent=float(row.get("fee_exponent") or 1.0),
            taker_only=True,
            rebate_rate=float(row.get("fee_rebate") or 0.0),
            source="gamma:gated",
        )
    return era


def schedule_from_market(row: dict) -> FeeSchedule:
    """The canonical entry point: per-market schedule first, era table second.

    `row` may be a `markets_pit` row (or dict) carrying `fee_rate`,
    `fee_exponent`, `fee_rebate`, `category`, `created_at`.
    """
    rate = row.get("fee_rate")
    if rate is not None:
        return FeeSchedule(
            rate=float(rate),
            exponent=float(row.get("fee_exponent") or 1.0),
            taker_only=True,
            rebate_rate=float(row.get("fee_rebate") or 0.0),
            source="gamma",
        )

    when = row.get("created_at") or row.get("end_date")
    if when is None:
        when = datetime.now(timezone.utc)
    return era_schedule(row.get("category") or "other", when)


# ---------------------------------------------------------------------------
# Taker Rebate Program (docs.polymarket.com/programs/taker-rebates)
#
# Live 2026-05-28. Tiers are set by 30-day Weighted Volume, and a rebate only
# applies to trades made *from the moment a tier is reached*, so the rate is a
# function of the account's own history - not a property of the market. That is
# why it lives here as a separate model rather than inside FeeSchedule.
#
# The pipeline doc warns that every "dead after fees" verdict used the gross
# taker fee. This module makes the correction measurable; what it cannot do is
# make it large, because the entry tier is 3 %.
# ---------------------------------------------------------------------------

REBATE_LIVE_FROM = "2026-05-28"

# (minimum 30-day weighted volume, tier name, rebate rate). Tier 0 below $2,000.
REBATE_TIERS: tuple[tuple[float, str, float], ...] = (
    (2_000.0, "Bronze", 0.03),
    (20_000.0, "Silver", 0.08),
    (200_000.0, "Gold", 0.18),
    (1_000_000.0, "Platinum", 0.32),
    (4_000_000.0, "Diamond", 0.44),
    (10_000_000.0, "Obsidian", 0.50),
)

# Category weights from the published table. Only fee-paying trades earn volume,
# so a fee-free market (geopolitics) earns nothing regardless of its weight.
W_VOLUME_WEIGHT: dict[str, float] = {
    "sports": 1.0,
    "politics": 1.3,
    "finance": 1.3,
    "mentions": 1.3,
    "tech": 1.3,
    "economics": 1.7,
    "culture": 1.7,
    "weather": 1.7,
    "other": 1.7,
    "crypto": 2.3,
    "geopolitics": 0.0,
}

DEFAULT_W_VOLUME_WEIGHT: float = W_VOLUME_WEIGHT["other"]


def weighted_volume(notional: float, entry_price: float, category: str) -> float:
    """Weighted Volume earned by one taker trade.

        wV = Trade Size x (1 - Entry Price) x Category Weight x Bonuses

    `notional` is dollars paid (shares x price). Bonuses are promotional and are
    not modelled.
    """
    if notional <= 0:
        return 0.0
    if not 0.0 <= entry_price <= 1.0:
        raise ValueError(f"price out of range: {entry_price}")
    weight = W_VOLUME_WEIGHT.get(category.lower(), DEFAULT_W_VOLUME_WEIGHT)
    return notional * (1.0 - entry_price) * weight


def taker_tier(weighted_volume_30d: float) -> tuple[str, float]:
    """Tier name and rebate rate for a 30-day weighted volume."""
    name, rate = "None", 0.0
    for threshold, tier_name, tier_rate in REBATE_TIERS:
        if weighted_volume_30d >= threshold:
            name, rate = tier_name, tier_rate
    return name, rate


def taker_rebate(taker_fees: float, weighted_volume_30d: float,
                 when: datetime | str,
                 price: float = 0.5, category: str = "other") -> float:
    """Rebate on a batch of taker fees, or 0 before the program opened.

    Deliberately conservative in two ways: the tier is evaluated on the *current*
    volume with no forward progression, and promotional bonuses are ignored.
    """
    if _as_utc(when) < _as_utc(REBATE_LIVE_FROM):
        return 0.0
    # A fee-free market earns no weighted volume, so it can never reach a tier
    # on its own; the guard keeps the semantics honest rather than profitable.
    if W_VOLUME_WEIGHT.get(category.lower(), DEFAULT_W_VOLUME_WEIGHT) == 0.0:
        return 0.0
    _, rate = taker_tier(weighted_volume_30d)
    return _round_fee(taker_fees * rate)


def turnover_needed_for_tier(threshold: float, capital: float,
                             price: float, category: str) -> float:
    """How many times 30-day capital must turn over to reach a tier.

    The number that decides whether a rebate is ever relevant at our size.
    """
    per_unit = capital * (1.0 - price) * W_VOLUME_WEIGHT.get(category.lower(), DEFAULT_W_VOLUME_WEIGHT)
    if per_unit <= 0:
        return float("inf")
    return threshold / per_unit
