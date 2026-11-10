"""Pre-flight readiness validation and balance synchronization."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from polymarketbot.logs import get_logger
from polymarketbot.strategy.venue.clob import ClobAPIError

logger = get_logger("real_money_engine")


@dataclass
class PreflightReport:
    """Diagnostic report assessing trading readiness."""

    ready_for_live: bool
    api_key_configured: bool
    rpc_connected: bool
    wallet_address: str
    usdc_balance: float
    pol_balance: float
    allowances_approved: bool
    dry_run_mode: bool
    recommendations: List[str] = field(default_factory=list)
    free_cash: float = 0.0
    locked_cash: float = 0.0
    order_version: int = 0

    def summary(self) -> str:
        lines = [
            f"Preflight Check: {'READY FOR LIVE' if self.ready_for_live else 'DRY-RUN / ACTION REQUIRED'}",
            f"  - Wallet: {self.wallet_address}",
            f"  - CLOB API Credentials: {'CONFIGURED' if self.api_key_configured else 'UNCONFIGURED (Dry-Run)'}",
            f"  - Polygon RPC: {'CONNECTED' if self.rpc_connected else 'FAILED / DISCONNECTED'}",
            f"  - Free Cash: ${self.free_cash:.2f}",
            f"  - Locked (resting BUY notional): ${self.locked_cash:.2f}",
            f"  - USDC Balance: ${self.usdc_balance:.2f}",
            f"  - POL Gas Balance: {self.pol_balance:.4f} POL",
            f"  - CLOB Order Version: {self.order_version}",
            f"  - CTF Allowances: {'APPROVED' if self.allowances_approved else 'UNAPPROVED / PENDING'}",
        ]
        if self.recommendations:
            lines.append("  Recommendations:")
            for rec in self.recommendations:
                lines.append(f"    * {rec}")
        return "\n".join(lines)


class PreflightMixin:
    """Pre-flight and balance-sync methods for the live engine."""

    def preflight_check(self) -> PreflightReport:
        """Inspect credentials, RPC connection, wallet balances, and contract allowances.

        Never mutates ``dry_run``: a preflight failure must surface as a report,
        not silently downgrade a live engine. Never reads the CTF signing key;
        the maker wallet is the only address that belongs in a report.
        """
        recs: list[str] = []
        ready = True

        try:
            api_ok = bool(self.clob_client.credentials.is_configured)
            format_errors = list(self.clob_client.credentials.validate_formatting())
            has_key = bool(self.clob_client.credentials.has_signing_key)
        except Exception as e:
            api_ok = False
            format_errors = [str(e)]
            has_key = False

        addr = str(getattr(self.clob_client, "maker_address", "") or "")

        rpc_ok = False
        rpc_reason = "Polygon RPC connection failed. Check POLYGON_RPC_URL or network connection."
        try:
            health = getattr(self.ctf_client.chain_client, "health_check", None)
            if callable(health):
                rpc_ok, detail = health()
                rpc_reason = f"Polygon RPC health check failed: {detail}"
            else:
                rpc_ok = self.ctf_client.chain_client.block_number() > 0
        except Exception as exc:
            rpc_ok = False
            rpc_reason = f"Polygon RPC health check failed: {exc}"

        free_cash = float(self.cash)
        locked_cash = float(self._compute_locked_cash())
        allowance_error: Optional[str] = None
        approved = False
        balance_error: Optional[str] = None
        try:
            balance = self.clob_client.get_balance()
            self.cash = float(balance["available"])
            self.locked_cash = self._compute_locked_cash()
            free_cash = self.get_cash()
            locked_cash = self.get_locked_cash()
        except ClobAPIError as e:
            balance_error = str(e)
        try:
            approved = bool(self.clob_client.get_allowances().get("approved", False))
        except ClobAPIError as e:
            allowance_error = str(e)

        try:
            order_version = int(self._order_version_safe())
        except ClobAPIError as e:
            order_version = 0
            recs.append(f"CLOB order version could not be resolved: {e}")
        except Exception as e:
            order_version = 0
            recs.append(f"CLOB order version could not be resolved: {e}")

        if not api_ok or not has_key:
            ready = False
            recs.append(
                "CLOB API credentials (POLY_API_KEY, POLY_API_SECRET, POLY_PASSPHRASE, POLY_PRIVATE_KEY) "
                "missing or incomplete. Provide them in .env to place live orders."
            )
        for fe in format_errors:
            ready = False
            recs.append(f"Credential formatting error: {fe}")
        if balance_error is not None:
            ready = False
            recs.append(f"CLOB collateral balance query failed: {balance_error}")
        if allowance_error is not None:
            ready = False
            recs.append(f"CLOB allowance query failed: {allowance_error}")
        if not rpc_ok:
            ready = False
            recs.append(rpc_reason)
        if free_cash <= 0.0:
            ready = False
            recs.append(
                f"CLOB free collateral is $0.00. Fund the maker wallet ({addr}) on Polymarket to begin quoting."
            )
        pol_error_ok = True
        if addr:
            try:
                self.pol_balance = self.ctf_client.get_pol_balance(addr)
            except Exception as e:
                pol_error_ok = False
                recs.append(f"POL gas balance could not be read from RPC: {e}")
        if self.pol_balance < 0.05:
            recs.append(
                f"POL Gas balance is low ({self.pol_balance:.4f} POL). Deposit at least 1-2 POL for CTF merges."
            )
        if not approved:
            ready = False
            recs.append(
                "CTF Exchange allowances have not been approved. Submit approvals before trading real funds."
            )
        if order_version not in (1, 2):
            ready = False
            recs.append(
                f"CLOB order version {order_version or 'unknown'} is not supported; expected 1 or 2."
            )
        if not bool(getattr(self.ctf_client, "is_settlement_enabled", False)):
            recs.append(
                "CTF on-chain settlement is disabled (dry-run, proxy signature type, or missing key): "
                "complete-set merges will be refused; quoting is unaffected."
            )

        return PreflightReport(
            ready_for_live=ready and not self.dry_run,
            api_key_configured=api_ok and has_key and not format_errors,
            rpc_connected=rpc_ok and pol_error_ok,
            wallet_address=addr,
            usdc_balance=free_cash,
            pol_balance=self.pol_balance,
            allowances_approved=approved,
            dry_run_mode=self.dry_run,
            recommendations=recs,
            free_cash=free_cash,
            locked_cash=locked_cash,
            order_version=order_version,
        )

    def sync_live_balances(self) -> dict[str, float]:
        """Synchronize CLOB free collateral, locked notional, POL, and token shares.

        ``cash`` is the CLOB available collateral only. ``budget_pct`` and
        ``max_budget`` are sizing caps and never rewrite cash or equity. A
        failed balance call leaves every figure untouched.
        """
        balances: dict[str, float] = {
            "cash": self.cash,
            "locked": self.locked_cash,
            "pol": self.pol_balance,
        }
        if not self.dry_run:
            try:
                bal_data = self.clob_client.get_balance()
                self.cash = float(bal_data["available"])
            except ClobAPIError as e:
                logger.error(
                    f"Failed to read CLOB collateral balance ({e}); cash unchanged at ${self.cash:.2f}"
                )
                self.sync_rewards()
                return balances
            except (KeyError, TypeError, ValueError) as e:
                logger.error(
                    f"CLOB balance response was not a usable collateral figure ({e}); cash unchanged"
                )
                self.sync_rewards()
                return balances

        self.locked_cash = self._compute_locked_cash()
        if not self.dry_run:
            addr = str(getattr(self.clob_client, "maker_address", "") or "")
            if addr:
                try:
                    self.pol_balance = float(self.ctf_client.get_pol_balance(addr))
                except Exception as e:
                    logger.warning(f"Failed to read POL gas balance for {addr}: {e}")

        self.total_wallet_balance = (
            self.get_cash() + self.get_locked_cash() + self._inventory_value()
        )
        balances["cash"] = self.cash
        balances["locked"] = self.locked_cash
        balances["pol"] = self.pol_balance
        self.sync_rewards()
        return balances

    def sync_balances(self) -> None:
        """Alias for sync_live_balances()."""
        self.sync_live_balances()
