"""Gas-fee estimation and EIP-1559 fee parameters used by the client."""

from __future__ import annotations

from typing import Optional, Tuple

from polymarketbot.logs import get_logger

from . import gas as _gas
from .constants import (
    _MAX_MAX_FEE_WEI,
    _MAX_PRIORITY_FEE_WEI,
    _MIN_MAX_FEE_WEI,
    _MIN_PRIORITY_FEE_WEI,
)

logger = get_logger("ctf_client")


class CtfFeeMixin:
    """Gas cost estimates and the signed fee caps."""

    def get_dynamic_gas_price_gwei(self) -> Optional[float]:
        """Measured all-in gas price in Gwei, or None when unmeasurable."""
        return _gas.get_dynamic_gas_price_gwei(self.chain_client)

    def estimate_merge_cost_usd(
        self,
        pol_usd_price: Optional[float] = None,
        gas_units: Optional[int] = None,
    ) -> Optional[float]:
        """Dollar cost of one complete-set merge, or None when gas is unmeasured."""
        px = pol_usd_price if pol_usd_price is not None else _gas.get_dynamic_pol_usd_price()
        units = gas_units or self.gas_profile.gas_merge_direct_typical
        gwei = self.get_dynamic_gas_price_gwei()
        return _gas.estimate_merge_cost_usd(
            pol_usd_price=px,
            gas_units=units,
            gas_price_gwei=gwei,
            chain_client=self.chain_client,
        )

    def calculate_optimal_merge_threshold(
        self,
        gas_cost_usd: Optional[float] = None,
        fee_basis_points: float = 0.0,
        pol_usd_price: Optional[float] = None,
    ) -> float:
        """Calculate optimal complete set merge threshold using dynamic gas cost."""
        cost = (
            gas_cost_usd
            if gas_cost_usd is not None
            else self.estimate_merge_cost_usd(pol_usd_price=pol_usd_price)
        )
        return _gas.calculate_optimal_merge_threshold(
            gas_cost_usd=cost,
            fee_basis_points=fee_basis_points,
            pol_usd_price=pol_usd_price,
            chain_client=self.chain_client,
        )

    def estimate_gas_fee(
        self, pol_usd_price: Optional[float] = None
    ) -> Tuple[Optional[float], Optional[float], int]:
        """Estimate dynamic Polygon PoS gas fee for mergePositions.

        Returns:
            (gas_fee_usd, gas_price_gwei, gas_units)
        """
        px = pol_usd_price if pol_usd_price is not None else _gas.get_dynamic_pol_usd_price()
        gas_units = self.gas_profile.gas_merge_direct_typical  # ~78,000 gas units
        gas_price_gwei = self.get_dynamic_gas_price_gwei()
        if gas_price_gwei is None:
            return None, None, gas_units
        gas_fee_usd = _gas.estimate_merge_cost_usd(
            pol_usd_price=px,
            gas_units=gas_units,
            gas_price_gwei=gas_price_gwei,
            chain_client=self.chain_client,
        )
        if gas_fee_usd is None:
            return None, round(gas_price_gwei, 1), gas_units
        return round(gas_fee_usd, 4), round(gas_price_gwei, 1), gas_units


    def _eip1559_fee_params_wei(self) -> Tuple[int, int]:
        """(maxPriorityFeePerGas, maxFeePerGas) in Wei from the measured gas price.

        Raises :class:`GasUnmeasured` when no price can be measured: signing a
        transaction priced off a guess is how a merge silently becomes a loss.
        """
        measured = self.get_dynamic_gas_price_gwei()
        if measured is None:
            raise _gas.GasUnmeasured(
                "cannot sign a settlement: no measured Polygon gas price"
            )
        oracle_gwei = float(measured)
        base_fee_gwei = 0.0
        try:
            block = self.chain_client._rpc("eth_getBlockByNumber", ["latest", False])
            if isinstance(block, dict) and block.get("baseFeePerGas"):
                base_fee_gwei = int(str(block["baseFeePerGas"]), 16) / 1e9
        except Exception as exc:  # noqa: BLE001 - oracle still provides a floor
            logger.debug(f"baseFeePerGas lookup failed, deriving from gas oracle: {exc}")

        if base_fee_gwei <= 0.0:
            base_fee_gwei = oracle_gwei * 0.7

        priority_gwei = min(
            _MAX_PRIORITY_FEE_WEI / 1e9,
            max(_MIN_PRIORITY_FEE_WEI / 1e9, oracle_gwei - base_fee_gwei),
        )
        # Headroom over the next base fee: EIP-1559 requires maxFee >= base + tip.
        max_fee_gwei = min(
            _MAX_MAX_FEE_WEI / 1e9,
            max(
                _MIN_MAX_FEE_WEI / 1e9,
                oracle_gwei,
                base_fee_gwei * 1.25 + priority_gwei,
            ),
        )
        return int(round(priority_gwei * 1e9)), int(round(max_fee_gwei * 1e9))


    def estimate_gas_fee_redeem(
        self, pol_usd_price: Optional[float] = None
    ) -> Tuple[Optional[float], Optional[float], int]:
        """Dollar gas for redeemPositions, or None when either input is unmeasured.

        Returns:
            (gas_fee_usd, gas_price_gwei, gas_units)
        """
        px = pol_usd_price if pol_usd_price is not None else _gas.get_dynamic_pol_usd_price()
        gas_units = self.gas_profile.gas_redeem_typical
        gas_price_gwei = self.get_dynamic_gas_price_gwei()
        if px is None or gas_price_gwei is None:
            return None, (None if gas_price_gwei is None else round(gas_price_gwei, 1)), gas_units
        gas_fee_pol = gas_units * (gas_price_gwei * 1e-9)
        return round(gas_fee_pol * float(px), 6), round(gas_price_gwei, 1), gas_units

