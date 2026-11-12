"""Gas-fee estimation and EIP-1559 fee parameters used by the client."""

from __future__ import annotations

from polymarketbot.logs import get_logger

from . import gas as _gas

logger = get_logger("ctf_client")


class CtfFeeMixin:
    """Gas cost estimates and the signed fee caps."""

    def get_dynamic_gas_price_gwei(self) -> float | None:
        """Measured all-in gas price in Gwei, or None when unmeasurable."""
        return _gas.get_dynamic_gas_price_gwei(self.chain_client)

    def estimate_merge_cost_usd(
        self,
        pol_usd_price: float | None = None,
        gas_units: int | None = None,
    ) -> float | None:
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
        gas_cost_usd: float | None = None,
        fee_basis_points: float = 0.0,
        pol_usd_price: float | None = None,
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
        self, pol_usd_price: float | None = None
    ) -> tuple[float | None, float | None, int]:
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


    def _eip1559_fee_params_wei(self) -> tuple[int, int]:
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
        # Both fee fields come from the chain's own numbers. There is no Gwei
        # floor and no "× 1.25 headroom": a hand-set clamp used to override the
        # measured oracle in both directions (a 25 Gwei floor paid 25× over a
        # 1 Gwei network, a 50 Gwei tip ceiling capped a congested one).
        try:
            block = self.chain_client._rpc("eth_getBlockByNumber", ["latest", False])
        except Exception as exc:
            raise _gas.GasUnmeasured(
                f"cannot sign a settlement: latest block unreadable ({exc})"
            ) from exc
        if not isinstance(block, dict) or not block.get("baseFeePerGas"):
            raise _gas.GasUnmeasured(
                "cannot sign a settlement: latest block publishes no baseFeePerGas"
            )
        base_fee_gwei = int(str(block["baseFeePerGas"]), 16) / 1e9
        if base_fee_gwei <= 0.0:
            # A zero base fee is a pre-1559 block; the oracle price alone is then
            # the whole fee and there is no tip to derive.
            max_fee_gwei = oracle_gwei
            return round(max_fee_gwei * 1e9), round(max_fee_gwei * 1e9)

        priority_gwei = max(0.0, oracle_gwei - base_fee_gwei)
        # EIP-1559's own next-block projection from gasUsed vs the target, so the
        # cap follows the chain's schedule instead of a fixed multiplier.
        next_base_gwei = base_fee_gwei
        try:
            gas_used = int(str(block["gasUsed"]), 16)
            gas_limit = int(str(block["gasLimit"]), 16)
            target = gas_limit / 2.0
            if target > 0.0:
                next_base_gwei = max(
                    0.0, base_fee_gwei * (1.0 + (gas_used - target) / target / 8.0)
                )
        except (KeyError, TypeError, ValueError) as exc:
            logger.debug(f"next base fee unprojectable ({exc}); using the current base fee")
        max_fee_gwei = max(oracle_gwei, next_base_gwei + priority_gwei)
        return round(priority_gwei * 1e9), round(max_fee_gwei * 1e9)


    def estimate_gas_fee_redeem(
        self, pol_usd_price: float | None = None
    ) -> tuple[float | None, float | None, int]:
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

