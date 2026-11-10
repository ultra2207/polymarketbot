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

    def get_dynamic_gas_price_gwei(self) -> float:
        """Query dynamic gas price in Gwei with fallback to fast oracle / 40 Gwei."""
        return _gas.get_dynamic_gas_price_gwei(self.chain_client)

    def estimate_merge_cost_usd(
        self,
        pol_usd_price: Optional[float] = None,
        gas_units: Optional[int] = None,
    ) -> float:
        """Calculate dynamic dollar gas cost for CTF complete-set merge: Gas Units * Gas Price * POL/USD."""
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

    def estimate_gas_fee(self, pol_usd_price: Optional[float] = None) -> Tuple[float, float, int]:
        """Estimate dynamic Polygon PoS gas fee for mergePositions.

        Returns:
            (gas_fee_usd, gas_price_gwei, gas_units)
        """
        px = pol_usd_price if pol_usd_price is not None else _gas.get_dynamic_pol_usd_price()
        gas_units = self.gas_profile.gas_merge_direct_typical  # ~78,000 gas units
        gas_price_gwei = self.get_dynamic_gas_price_gwei()
        gas_fee_usd = _gas.estimate_merge_cost_usd(
            pol_usd_price=px,
            gas_units=gas_units,
            gas_price_gwei=gas_price_gwei,
            chain_client=self.chain_client,
        )
        return round(gas_fee_usd, 4), round(gas_price_gwei, 1), gas_units


    def _eip1559_fee_params_wei(self) -> Tuple[int, int]:
        """(maxPriorityFeePerGas, maxFeePerGas) in Wei from the live gas oracle."""
        oracle_gwei = float(self.get_dynamic_gas_price_gwei())
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


    def estimate_gas_fee_redeem(self, pol_usd_price: Optional[float] = None) -> Tuple[float, float, int]:
        """Estimate dynamic Polygon PoS gas fee for redeemPositions.

        Returns:
            (gas_fee_usd, gas_price_gwei, gas_units)
        """
        px = pol_usd_price if pol_usd_price is not None else _gas.get_dynamic_pol_usd_price()
        gas_units = 45_000  # typical redeem gas cost ~45,000 units
        gas_price_gwei = self.get_dynamic_gas_price_gwei()
        gas_fee_pol = gas_units * (gas_price_gwei * 1e-9)
        gas_fee_usd = gas_fee_pol * float(px)
        return round(gas_fee_usd, 4), round(gas_price_gwei, 1), gas_units

