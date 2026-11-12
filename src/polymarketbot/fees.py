"""Polymarket fee model.

Two venues publish two different schedules (both re-read 2026-11-10):

* ``global`` (clob.polymarket.com, what this bot trades) and ``us``
  (polymarket.us, a separate exchange) do not share a formula.

    global: fee = C x rate x (p x (1 - p)) ^ exponent   (rate per category)
    us:     fee = Theta x C x p x (1 - p)               (Theta per account tier,
                                                         re-set daily from the
                                                         trailing 30-day taker
                                                         volume; cents, half-even)

  A tier-1 US account pays Theta 0.095 against 0.0695 for tier 4, so pricing a
  US venue with the global table under-charges by 36.7 %. ``venue_for_host``
  picks the schedule and ``us_taker_coefficient`` refuses an unmeasured volume.

* The global Taker Rebate Program returns a share of the fee by tier
  (``taker_rebate_rate``); it is modelled here but deliberately not credited in
  paper PnL: the bot's own 30-day weighted volume is unmeasured, and tier 0 is
  exactly 0 %, so booking nothing is correct today. Any taker-heavy sizing must
  pass a measured volume through ``weighted_taker_volume`` first.

Global taker formula (docs.polymarket.com/trading/fees):

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

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

CONFIG = Path(__file__).resolve().parents[2] / "configs" / "fees.yaml"
"""The published fee table, resolved from the package rather than the CWD.

A relative ``configs/fees.yaml`` resolved only when a process ran from the
repository root: a tool, a test runner or a service unit launched anywhere else
raised ``FileNotFoundError`` from the first fee lookup.
"""


@dataclass(frozen=True)
class FeeSchedule:
    """A market's fee configuration, mirroring Gamma's `feeSchedule`."""

    rate: float
    exponent: float = 1.0
    taker_only: bool = True
    rebate_rate: float = 0.0
    source: str = "config"

    @classmethod
    def from_gamma(cls, d: dict) -> FeeSchedule:
        return cls(
            rate=float(d.get("rate", 0.0)),
            exponent=float(d.get("exponent", 1.0)),
            taker_only=bool(d.get("takerOnly", True)),
            rebate_rate=float(d.get("rebateRate", 0.0)),
            source="gamma",
        )

    @classmethod
    def zero(cls) -> FeeSchedule:
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
        when = datetime.fromisoformat(str(e["effective_from"])).replace(tzinfo=UTC)
        out.append(_Era(
            e["name"], when, dict(e.get("rates") or {}),
            dict(e.get("rebates") or {}), dict(e.get("exponents") or {}),
            bool(e.get("derived")),
        ))
    return tuple(sorted(out, key=lambda x: x.effective_from))


def _dec(value: float) -> Decimal:
    """Exact decimal for a published parameter (0.0695, 0.30, 1000)."""
    return Decimal(str(value))


def _round_fee(value: float, dp: int = 5, mode: str = ROUND_HALF_UP) -> float:
    q = Decimal(1).scaleb(-dp)  # 0.00001 for dp=5
    return float(Decimal(repr(value)).quantize(q, rounding=mode))


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


def maker_fee_rebate(
    price: float,
    shares: float,
    schedule: FeeSchedule,
    maker_rebate_share_pct: float | None = None,
    rounding_dp: int | None = None,
) -> float:
    """Rebate paid to maker filling a resting limit order from taker fee pool.

    When taker pays fee:
        F_taker = taker_fee(shares, price, schedule)
    The resting maker receives volume share rebate:
        R_maker = gamma_rebate * F_taker

    Args:
        price: Execution price between 0.0 and 1.0.
        shares: Number of filled shares.
        schedule: Market FeeSchedule. Its measured ``rebate_rate`` is
            authoritative (Gamma ``rebateRate`` or the published era table).
        maker_rebate_share_pct: Explicit override share. ``None`` (the default)
            uses ``schedule.rebate_rate``; a schedule that pays no rebate yields
            zero rather than fabricating the old 20% model default.
        rounding_dp: Rounding decimal places (defaults to config formula rounding_dp).

    Returns:
        Maker rebate in USDC (always >= 0.0).
    """
    if shares <= 0.0 or schedule.rate <= 0.0:
        return 0.0
    if not 0.0 <= price <= 1.0:
        raise ValueError(f"price out of range: {price}")

    gamma = float(maker_rebate_share_pct) if maker_rebate_share_pct is not None else float(schedule.rebate_rate)
    if gamma <= 0.0:
        return 0.0

    dp = rounding_dp if rounding_dp is not None else int(_config()["formula"]["rounding_dp"])
    t_fee = taker_fee(shares, price, schedule, rounding_dp=dp)
    return _round_fee(t_fee * gamma, dp)


def effective_maker_price(
    price: float,
    shares: float,
    side: str,
    schedule: FeeSchedule | None = None,
    maker_rebate_usd: float | None = None,
    maker_rebate_share_pct: float | None = None,
) -> float:
    """Calculate the net effective price for a maker fill after volume share rebate.

    For BUY (bid fill):
        effective_price = price - (rebate / shares)
        e.g., buying at $0.980 with $0.0008 (0.08c) rebate -> $0.9792 net cost.

    For SELL (ask fill):
        effective_price = price + (rebate / shares)
        e.g., selling at $0.020 with $0.0008 rebate -> $0.0208 net proceeds.
    """
    if shares <= 0.0:
        return float(price)
    if maker_rebate_usd is not None:
        rebate = float(maker_rebate_usd)
    elif schedule is not None:
        rebate = maker_fee_rebate(price, shares, schedule, maker_rebate_share_pct=maker_rebate_share_pct)
    else:
        rebate = 0.0

    rebate_per_share = rebate / shares
    side_norm = side.upper()
    if side_norm == "BUY":
        return round(float(price) - rebate_per_share, 6)
    elif side_norm == "SELL":
        return round(float(price) + rebate_per_share, 6)
    else:
        raise ValueError(f"side must be BUY or SELL, got {side}")


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
        when = datetime.fromisoformat(when)
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)

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
    start = datetime.fromisoformat(str(spec["effective_from"])).replace(tzinfo=UTC)
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
PILOT_START = datetime(2026, 1, 5, tzinfo=UTC)
FEE_STRUCTURE_V2 = datetime(2026, 3, 30, tzinfo=UTC)


def _as_utc(when: datetime | str) -> datetime:
    if isinstance(when, str):
        when = datetime.fromisoformat(when)
    return when if when.tzinfo is not None else when.replace(tzinfo=UTC)


def schedule_at(row: dict, when: datetime | str) -> FeeSchedule:
    """The schedule for a fill at `when` — the function a fill model should call.

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
        when = datetime.now(UTC)
    return era_schedule(row.get("category") or "other", when)


_FEE_PAYLOAD_KEYS = ("feeSchedule", "fees")


class FeeScheduleUnreadable(RuntimeError):
    """A Gamma market carries a fee schedule that cannot be parsed.

    Refusing is the only safe response. A missing schedule is not a free
    market: substituting :meth:`FeeSchedule.zero` would understate the cost of
    every taker fill and inflate the measured edge on exactly the markets where
    the venue charges (``docs/fees.md``).
    """


def schedule_for_market(market: Mapping[str, Any]) -> FeeSchedule:
    """The authoritative schedule from one raw Gamma market payload.

    ``FeeSchedule.zero()`` is returned only for a *measured* zero: the venue
    flags ``feesEnabled`` false, or the payload carries no fee field at all
    (markets created before fees existed). A payload that is present but
    unparseable raises :class:`FeeScheduleUnreadable` with the raw value.

    Unlike :func:`schedule_from_market`, which answers "what did this market
    charge then" from persisted ``markets_pit`` rows, this reads the live
    per-market ``feeSchedule`` Gamma publishes and does not fall back to the
    era table.
    """
    cid = market.get("conditionId") or market.get("condition_id") or "?"
    raw: Any = None
    for key in _FEE_PAYLOAD_KEYS:
        if market.get(key) is not None:
            raw = market[key]
            break
    if market.get("feesEnabled") is False:
        return FeeSchedule.zero()
    if raw is None:
        if market.get("feesEnabled") is None and market.get("feeType") is None:
            return FeeSchedule.zero()
        raise FeeScheduleUnreadable(
            f"market {cid} declares fees but carries no feeSchedule: {dict(market)!r}"
        )
    if not isinstance(raw, Mapping) or not raw:
        raise FeeScheduleUnreadable(
            f"market {cid} has an empty or non-object fee schedule: {raw!r}"
        )
    try:
        return FeeSchedule.from_gamma(raw)
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise FeeScheduleUnreadable(
            f"market {cid} has an unreadable fee schedule: {raw!r}"
        ) from exc


# ---------------------------------------------------------------------------
# Venue schedules
# ---------------------------------------------------------------------------
# Two live schedules exist and they are not interchangeable:
#
#   global (clob.polymarket.com)  `fee = C * feeRate * p(1-p)`, one feeRate per
#                                 market category, rounded to 5 decimals.
#   us (polymarket.us)            `fee = Theta * C * p(1-p)`, Theta set per
#                                 account by trailing 30-day taker volume and
#                                 re-derived daily, rounded to the cent with
#                                 banker's rounding.
#
# Applying the global rate on a US venue under-charges by up to 36.7 %
# (Theta 0.095 vs 0.0695), so the US path refuses to price a tier when the
# trailing volume is unknown instead of assuming the best one.

VENUE_GLOBAL = "global"
VENUE_US = "us"


def venue_for_host(url: str) -> str:
    """Which published fee schedule applies to ``url``.

    The US exchange is a separate venue with its own schedule; anything that
    resolves to Polymarket's global CLOB uses the per-category table.
    """
    return VENUE_US if "polymarket.us" in (url or "").lower() else VENUE_GLOBAL


def _venue_config(venue: str, path: str | Path = CONFIG) -> dict:
    venues = _config(path).get("venues") or {}
    if venue not in venues:
        raise KeyError(f"no published fee schedule for venue {venue!r}")
    return venues[venue]


def us_taker_tiers(path: str | Path = CONFIG) -> tuple[tuple[str, float, float], ...]:
    """Published ``(name, min_trailing_30d_taker_volume_usd, theta)`` ladder."""
    config = _venue_config(VENUE_US, path)
    return tuple(
        (str(tier["name"]), float(tier["min_30d_taker_volume_usd"]), float(tier["theta"]))
        for tier in config.get("taker_tiers") or ()
    )


def us_taker_coefficient(
    trailing_30d_taker_volume_usd: float | None,
    path: str | Path = CONFIG,
) -> float | None:
    """Theta for a measured trailing 30-day taker volume, or ``None``.

    ``None`` means the tier is unknown. It is a refusal, never the lowest or the
    highest coefficient: an unmeasured volume must not be priced as if the
    account were the best tier.
    """
    if trailing_30d_taker_volume_usd is None:
        return None
    volume = float(trailing_30d_taker_volume_usd)
    if volume < 0.0:
        raise ValueError(f"trailing volume cannot be negative: {volume}")
    tiers = us_taker_tiers(path)
    if not tiers:
        return None
    theta = None
    for _name, threshold, candidate in sorted(tiers, key=lambda row: row[1]):
        if volume >= threshold:
            theta = candidate
    return theta


def weighted_taker_volume(
    trade_size_usd: float,
    entry_price: float,
    category: str,
    bonuses: float = 1.0,
    path: str | Path = CONFIG,
) -> float | None:
    """Global Taker Rebate Program weighted volume for one taker trade.

    ``wV = trade_size_usd * (1 - entry_price) * category_weight * bonuses``

    Returns ``None`` for a category the published weights do not cover, and for
    a fee-free market: only taker trades that pay a fee earn wV, so a geopolitics
    fill contributes nothing however large it is.
    """
    rates = (_config(path).get("venues") or {}).get("global", {}).get("taker_rebates") or {}
    weights = rates.get("weighted_volume_weights") or {}
    cat = fee_category(category, path)
    if cat not in weights:
        return None
    weight = float(weights[cat])
    if weight <= 0.0 or era_schedule(category, datetime.now(UTC), path).rate <= 0.0:
        return 0.0
    if trade_size_usd <= 0.0:
        return 0.0
    return float(trade_size_usd) * (1.0 - float(entry_price)) * weight * float(bonuses)


def taker_rebate_rate(
    trailing_30d_weighted_volume: float | None,
    path: str | Path = CONFIG,
) -> float | None:
    """Global taker rebate share for a measured 30-day weighted volume.

    ``None`` means unmeasured. It is a refusal, never the 0 % tier: booking no
    rebate is conservative, but *claiming* a tier we cannot observe is a guess.
    """
    if trailing_30d_weighted_volume is None:
        return None
    volume = float(trailing_30d_weighted_volume)
    if volume < 0.0:
        raise ValueError(f"weighted volume cannot be negative: {volume}")
    rates = (_config(path).get("venues") or {}).get("global", {}).get("taker_rebates") or {}
    tiers = rates.get("tiers") or []
    if not tiers:
        return None
    share = None
    for tier in sorted(tiers, key=lambda row: float(row["min_30d_weighted_volume"])):
        if volume >= float(tier["min_30d_weighted_volume"]):
            share = float(tier["rebate"])
    return share


def us_taker_fee(shares: float, price: float, theta: float,
                 path: str | Path = CONFIG) -> float:
    """US taker fee: ``Theta * C * p * (1 - p)``, rounded to the cent, half-even.

    Documented worked examples (docs.polymarket.us/fees, Theta 0.0695):
    1,000 at 0.10 -> 6.26; 1,000 at 0.65 -> 15.81; 1,000 at 0.30 -> 14.60.
    """
    if shares <= 0.0 or theta <= 0.0:
        return 0.0
    if not 0.0 <= price <= 1.0:
        raise ValueError(f"price out of range: {price}")
    rounding = _venue_config(VENUE_US, path)["rounding"]
    # Decimal, not float: 0.0695 * 1000 * 0.30 * 0.70 is exactly 14.595, a tie
    # that banker's rounding sends to 14.60. Computed in binary floating point it
    # lands at 14.594999999999999 and silently rounds to 14.59, so the published
    # tie-break rule would never fire.
    raw = (_dec(theta) * _dec(shares) * _dec(price) * (1 - _dec(price)))
    q = Decimal(1).scaleb(-int(rounding["dp"]))
    return float(raw.quantize(q, rounding=ROUND_HALF_EVEN))


def us_combo_taker_fee(
    shares: float,
    price: float,
    theta: float,
    when: datetime | str | None = None,
    path: str | Path = CONFIG,
) -> float:
    """US combo taker fee: ``C * p * [Theta(1-p) + fixed * (1-p)^4]``.

    The fixed coefficient on ``(1-p)^4`` drops from 0.085 to 0.06 at the
    published change time; the coefficient in force at ``when`` is used, so a
    combo priced across the cutover is priced by its own timestamp.
    """
    if shares <= 0.0 or theta <= 0.0:
        return 0.0
    if not 0.0 <= price <= 1.0:
        raise ValueError(f"price out of range: {price}")
    config = _venue_config(VENUE_US, path)
    combo = config["combo"]
    cutover = datetime.fromisoformat(str(combo["change_effective_from"]))
    if when is None:
        moment = datetime.now(UTC)
    elif isinstance(when, str):
        moment = datetime.fromisoformat(when)
    else:
        moment = when
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    fixed = (float(combo["fixed_power4_after"]) if moment >= cutover
             else float(combo["fixed_power4_before"]))
    one_minus = 1 - _dec(price)
    raw = (_dec(shares) * _dec(price)
           * (_dec(theta) * one_minus + _dec(fixed) * one_minus ** 4))
    q = Decimal(1).scaleb(-int(config["rounding"]["dp"]))
    return float(raw.quantize(q, rounding=ROUND_HALF_EVEN))
