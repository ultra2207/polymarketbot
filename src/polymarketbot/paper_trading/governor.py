"""PolySimulator Rate Limit & Monthly Quota Governor.

Enforces:
1. Sub-second burst rate limit: <= 1.8 req/sec (PolySimulator free limit is 2 req/sec).
2. Per-minute limit: <= 20 req/min (PolySimulator free limit is 120 req/min).
3. 100k Monthly Budget breakdown:
   - Monthly: 100,000 req/month
   - Weekly: 23,333 req/week
   - Daily: 3,333 req/day
   - Hourly: 138 req/hour
   - Minute: 2.31 req/minute (sustained 24/7 average)
4. Free Tier Daily Order Placement Cap: <= 90 orders/day (PolySimulator hard cap is 100/day).
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from polymarketbot.logs import get_logger

log = get_logger("rate_governor")


@dataclass
class BudgetConfig:
    """Quantitative budget constraints matching PolySimulator free tier."""

    # Provider rate limits
    max_rps: float = 1.6  # Safe headroom below 2.0 req/sec limit
    min_request_interval_seconds: float = 0.65  # 650ms minimum spacing between any 2 requests
    max_rpm: int = 40  # Safe headroom below 120 req/min limit

    # 100k / month breakdown (30 days)
    monthly_budget: int = 100_000
    weekly_budget: int = 23_333
    daily_budget: int = 3_333
    hourly_budget: int = 138
    minute_budget_sustained: float = 2.31

    # Daily order placement write cap
    max_orders_per_day: int = 90  # 10 order safety buffer before provider 100 cap


@dataclass
class RateGovernorStatus:
    """Telemetry snapshot of current quota utilization."""

    requests_last_minute: int
    requests_last_hour: int
    requests_today: int
    orders_placed_today: int
    daily_order_budget_remaining: int
    hourly_budget_remaining: int
    daily_budget_remaining: int
    can_place_order: bool
    is_throttled: bool


class RateLimitGovernor:
    """Thread-safe, deterministic rate limiter and quota governor for PolySimulator."""

    def __init__(self, config: Optional[BudgetConfig] = None) -> None:
        self.config = config or BudgetConfig()

        # Timestamps of recent requests
        self._last_request_time: float = 0.0
        self._minute_window: deque[float] = deque()
        self._hour_window: deque[float] = deque()

        # Daily counters reset at 00:00:00 UTC
        self._current_utc_day: str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self._daily_request_count: int = 0
        self._daily_order_count: int = 0

    def _sync_utc_day(self) -> None:
        """Reset daily counters when UTC day rolls over at midnight."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._current_utc_day:
            log.info(
                f"[RateGovernor] UTC Day Rollover: {self._current_utc_day} -> {today}. "
                f"Resetting daily counters (Requests: {self._daily_request_count}, Orders: {self._daily_order_count})."
            )
            self._current_utc_day = today
            self._daily_request_count = 0
            self._daily_order_count = 0

    def _prune_windows(self, now: float) -> None:
        """Remove request timestamps older than 60s and 3600s."""
        cutoff_min = now - 60.0
        while self._minute_window and self._minute_window[0] < cutoff_min:
            self._minute_window.popleft()

        cutoff_hour = now - 3600.0
        while self._hour_window and self._hour_window[0] < cutoff_hour:
            self._hour_window.popleft()

    def acquire_read_slot(self) -> float:
        """Wait if necessary and register a read request. Returns seconds waited."""
        self._sync_utc_day()
        now = time.time()
        self._prune_windows(now)

        waited = 0.0

        # 1. Enforce minimum inter-request interval (sub-second RPS burst protection)
        elapsed = now - self._last_request_time
        if elapsed < self.config.min_request_interval_seconds:
            sleep_needed = self.config.min_request_interval_seconds - elapsed
            time.sleep(sleep_needed)
            waited += sleep_needed
            now = time.time()
            self._prune_windows(now)

        # 2. Enforce per-minute limit (RPM)
        if len(self._minute_window) >= self.config.max_rpm:
            earliest = self._minute_window[0]
            sleep_needed = max(0.1, 60.0 - (now - earliest) + 0.1)
            log.warning(
                f"[RateGovernor] RPM limit ({self.config.max_rpm}) reached. "
                f"Pacing requests by sleeping {sleep_needed:.2f}s."
            )
            time.sleep(sleep_needed)
            waited += sleep_needed
            now = time.time()
            self._prune_windows(now)

        # 3. Hourly quota soft-pacing
        if len(self._hour_window) >= self.config.hourly_budget:
            earliest_hour = self._hour_window[0]
            sleep_needed = max(1.0, 3600.0 - (now - earliest_hour) + 1.0)
            log.warning(
                f"[RateGovernor] Hourly quota ({self.config.hourly_budget}) reached. "
                f"Pacing by {sleep_needed:.1f}s to preserve 100k monthly budget."
            )
            time.sleep(sleep_needed)
            waited += sleep_needed
            now = time.time()
            self._prune_windows(now)

        # Record this request
        self._last_request_time = now
        self._minute_window.append(now)
        self._hour_window.append(now)
        self._daily_request_count += 1

        return waited

    def acquire_order_write_slot(self) -> tuple[bool, str]:
        """Check if an order placement write is permitted under daily quota.

        Returns:
            (is_permitted, reason_message)
        """
        self._sync_utc_day()

        if self._daily_order_count >= self.config.max_orders_per_day:
            reason = (
                f"Order placement blocked: daily limit reached ({self._daily_order_count}/{self.config.max_orders_per_day}). "
                f"Resets at 00:00:00 UTC."
            )
            log.warning(f"[RateGovernor] {reason}")
            return False, reason

        # Also acquire read slot for transport spacing
        self.acquire_read_slot()
        self._daily_order_count += 1
        return True, "Permitted"

    def get_status(self) -> RateGovernorStatus:
        """Return current status and remaining budget telemetry."""
        self._sync_utc_day()
        now = time.time()
        self._prune_windows(now)

        req_min = len(self._minute_window)
        req_hour = len(self._hour_window)
        rem_hour = max(0, self.config.hourly_budget - req_hour)
        rem_day = max(0, self.config.daily_budget - self._daily_request_count)
        rem_orders = max(0, self.config.max_orders_per_day - self._daily_order_count)

        return RateGovernorStatus(
            requests_last_minute=req_min,
            requests_last_hour=req_hour,
            requests_today=self._daily_request_count,
            orders_placed_today=self._daily_order_count,
            daily_order_budget_remaining=rem_orders,
            hourly_budget_remaining=rem_hour,
            daily_budget_remaining=rem_day,
            can_place_order=rem_orders > 0,
            is_throttled=req_min >= self.config.max_rpm or req_hour >= self.config.hourly_budget,
        )
