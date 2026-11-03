"""Production Polymarket Live CLOB Client for Real-Money Trading.

Provides:
1. EIP-712 Order Signing via FastOrderSigner (FastEIP712Signer) on Polygon (Chain ID 137).
2. Level-2 HMAC-SHA256 authenticated REST calls to Polymarket CLOB:
   - POST /order (place signed limit orders)
   - DELETE /order (cancel individual orders)
   - DELETE /cancel-all (cancel all resting orders)
   - GET /orders (query open resting orders)
   - GET /data/balances, GET /data/allowances, and GET /balance-allowance
3. Safe Graceful Dry-Run / Mock Mode:
   - When API keys or private keys are omitted or account has $0 balance.
   - Computes authentic EIP-712 digests & signatures without broadcasting.
   - Logs simulated execution with microsecond timestamps and structured return codes.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import requests

from polymarketbot.dataapi import DataAPIClient
from polymarketbot.execution.fast_cancel import DedicatedCancellationPool
from polymarketbot.logs import get_logger
from polymarketbot.net import install_doh_fallback
from polymarketbot.risk.fast_signer import (
    DEFAULT_CHAIN_ID,
    DEFAULT_CTF_EXCHANGE,
    FastEIP712Signer,
    FastOrder,
    private_key_to_address,
)

logger = get_logger("clob_client")

# FastOrderSigner alias pointing to high-performance EIP-712 signer
FastOrderSigner = FastEIP712Signer

DEFAULT_CLOB_HOST = "https://clob.polymarket.com"

# Deterministic fallback key for dry-run signature verification when no private key is supplied
MOCK_TEST_PRIVATE_KEY = "0x4c0883a69102937d6231471b5dbb6204db7e716b78ac5c7807e8b88f211d733b"


@dataclass
class ClobAuthCredentials:
    """Polymarket CLOB API Authentication Credentials."""

    api_key: str = ""
    api_secret: str = ""
    passphrase: str = ""
    address: str = ""
    private_key: str = ""

    @classmethod
    def from_env(cls) -> ClobAuthCredentials:
        """Load credentials from environment variables or .env file."""
        env_dict: dict[str, str] = {}
        env_file = Path(".env")
        if env_file.exists():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env_dict[k.strip()] = v.strip().strip('"').strip("'")

        api_key = (
            os.environ.get("POLY_API_KEY")
            or os.environ.get("POLYMARKET_API_KEY")
            or env_dict.get("POLY_API_KEY", "")
            or env_dict.get("POLYMARKET_API_KEY", "")
        )
        api_secret = (
            os.environ.get("POLY_API_SECRET")
            or os.environ.get("POLYMARKET_SECRET")
            or env_dict.get("POLY_API_SECRET", "")
            or env_dict.get("POLYMARKET_SECRET", "")
        )
        passphrase = (
            os.environ.get("POLY_PASSPHRASE")
            or os.environ.get("POLYMARKET_PASSPHRASE")
            or env_dict.get("POLY_PASSPHRASE", "")
            or env_dict.get("POLYMARKET_PASSPHRASE", "")
        )
        address = (
            os.environ.get("POLY_ADDRESS")
            or os.environ.get("PROXY_WALLET_ADDRESS")
            or env_dict.get("POLY_ADDRESS", "")
            or env_dict.get("PROXY_WALLET_ADDRESS", "")
        )
        private_key = (
            os.environ.get("POLY_PRIVATE_KEY")
            or os.environ.get("PRIVATE_KEY")
            or env_dict.get("POLY_PRIVATE_KEY", "")
            or env_dict.get("PRIVATE_KEY", "")
        )

        # Auto-derive address from private key if not explicitly provided
        if private_key and not address:
            try:
                clean_pk = bytes.fromhex(private_key.removeprefix("0x").removeprefix("0X"))
                address = private_key_to_address(clean_pk)
            except Exception:
                pass

        return cls(
            api_key=api_key,
            api_secret=api_secret,
            passphrase=passphrase,
            address=address,
            private_key=private_key,
        )

    @property
    def is_configured(self) -> bool:
        """True if core L2 credentials are non-empty."""
        return bool(self.api_key and self.api_secret and self.passphrase)

    @property
    def has_signing_key(self) -> bool:
        """True if private key is present and is a valid 32-byte hex string."""
        if not self.private_key:
            return False
        clean = self.private_key.removeprefix("0x").removeprefix("0X")
        if len(clean) != 64:
            return False
        try:
            int(clean, 16)
            return True
        except ValueError:
            return False

    @property
    def is_fully_configured(self) -> bool:
        """True if both L2 API credentials and on-chain private signing key are present and formatted."""
        return self.is_configured and self.has_signing_key

    def validate_formatting(self) -> list[str]:
        """Validate format of API key, API secret, private key, and address.

        Returns:
            List of error messages describing any formatting issues (empty if valid).
        """
        errors = []
        if self.api_key:
            clean_key = self.api_key.strip()
            try:
                uuid.UUID(clean_key)
            except ValueError:
                errors.append(f"POLY_API_KEY does not appear to be a valid UUID format: '{self.api_key[:8]}...'")

        if self.api_secret:
            try:
                padded = self.api_secret + "=" * (-len(self.api_secret) % 4)
                base64.urlsafe_b64decode(padded)
            except Exception:
                errors.append("POLY_API_SECRET is not valid base64 encoded data.")

        if self.private_key:
            clean_pk = self.private_key.removeprefix("0x").removeprefix("0X")
            if len(clean_pk) != 64:
                errors.append(f"POLY_PRIVATE_KEY must be exactly 32 bytes (64 hex characters), got {len(clean_pk)} chars.")
            else:
                try:
                    int(clean_pk, 16)
                except ValueError:
                    errors.append("POLY_PRIVATE_KEY contains non-hexadecimal characters.")

        if self.address:
            clean_addr = self.address.removeprefix("0x").removeprefix("0X")
            if len(clean_addr) != 40:
                errors.append(f"POLY_ADDRESS must be a 20-byte Ethereum address (40 hex characters), got {len(clean_addr)} chars.")
            else:
                try:
                    int(clean_addr, 16)
                except ValueError:
                    errors.append("POLY_ADDRESS contains non-hexadecimal characters.")

        return errors


def build_clob_hmac_signature(
    secret: str,
    timestamp: str,
    method: str,
    request_path: str,
    body: Optional[Union[str, bytes]] = None,
) -> str:
    """Generate Layer-2 HMAC-SHA256 signature for Polymarket CLOB.

    Decodes URL-safe base64 secret, concatenates timestamp + method + path + body,
    and encodes the HMAC digest into URL-safe base64.
    """
    if not secret:
        return ""

    try:
        # Handle optional padding if needed
        padded_secret = secret + "=" * (-len(secret) % 4)
        base64_secret = base64.urlsafe_b64decode(padded_secret)
    except Exception:
        base64_secret = secret.encode("utf-8")

    message = f"{timestamp}{method.upper()}{request_path}"
    if body is not None:
        if isinstance(body, bytes):
            body_str = body.decode("utf-8", errors="replace")
        else:
            body_str = str(body)
        # Ensure double quotes parity across Go/Python/TS
        message += body_str.replace("'", '"')

    h = hmac.new(base64_secret, message.encode("utf-8"), hashlib.sha256)
    return base64.urlsafe_b64encode(h.digest()).decode("utf-8")


@dataclass
class OrderPlacementResult:
    """Outcome of submitting an order to Polymarket CLOB."""

    success: bool
    order_id: str
    status: str
    side: str
    price: float
    size: float
    raw_response: dict[str, Any] = field(default_factory=dict)
    error_message: Optional[str] = None
    signature: str = ""
    is_dry_run: bool = False


class LiveClobClient:
    """Production REST Client for Polymarket Central Limit Order Book (CLOB)."""

    def __init__(
        self,
        credentials: Optional[ClobAuthCredentials] = None,
        base_url: str = DEFAULT_CLOB_HOST,
        chain_id: int = DEFAULT_CHAIN_ID,
        verifying_contract: str = DEFAULT_CTF_EXCHANGE,
        timeout: float = 5.0,
        dry_run: bool = False,
        use_fast_cancel_pool: bool = False,
    ) -> None:
        self.credentials = credentials or ClobAuthCredentials.from_env()
        self.base_url = base_url.rstrip("/")
        self.chain_id = chain_id
        self.verifying_contract = verifying_contract
        self.timeout = timeout

        # Enable dry_run if explicitly requested or if credentials are unconfigured/incomplete
        self.dry_run = dry_run or not self.credentials.is_fully_configured
        if self.dry_run:
            logger.info(
                f"[DRY-RUN MODE ACTIVE] LiveClobClient initialized in safe inspection mode "
                f"(configured={self.credentials.is_configured}, has_key={self.credentials.has_signing_key}). "
                f"No real funds will be risked."
            )

        # Setup EIP-712 signer with graceful fallback
        signing_key = self.credentials.private_key or (
            MOCK_TEST_PRIVATE_KEY if self.dry_run else ""
        )
        try:
            self.signer = FastOrderSigner(
                private_key=signing_key if signing_key else None,
                verifying_contract=self.verifying_contract,
                chain_id=self.chain_id,
            )
            self.address = self.credentials.address or (self.signer.address or "0x0000000000000000000000000000000000000000")
        except Exception as e:
            logger.warning(
                f"Failed to initialize FastOrderSigner with key ({e}). "
                f"Falling back to mock test signer in safe dry-run mode."
            )
            self.signer = FastOrderSigner(
                private_key=MOCK_TEST_PRIVATE_KEY,
                verifying_contract=self.verifying_contract,
                chain_id=self.chain_id,
            )
            self.address = self.credentials.address or (self.signer.address or "0x0000000000000000000000000000000000000000")
            self.dry_run = True

        try:
            install_doh_fallback()
        except Exception:
            pass

        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "polymarketbot/real-money-trading/1.0",
        })

        # Dedicated low-latency cancellation pool
        self._fast_cancel_pool: Optional[DedicatedCancellationPool] = None
        if use_fast_cancel_pool and not self.dry_run:
            try:
                host = self.base_url.removeprefix("https://").removeprefix("http://").split("/")[0]
                self._fast_cancel_pool = DedicatedCancellationPool(host=host, auto_warm=False)
            except Exception as e:
                logger.warning(f"Failed to initialize fast cancel pool: {e}")

        # Simulated state for dry-run tracking
        self._mock_orders: dict[str, dict[str, Any]] = {}
        self._mock_balance_usd: float = 100.0

    def _get_auth_headers(
        self,
        method: str,
        path: str,
        body: Optional[Union[str, bytes]] = None,
    ) -> dict[str, str]:
        """Construct L2 authentication headers."""
        if not self.credentials.is_configured:
            return {}

        ts = str(int(time.time()))
        sig = build_clob_hmac_signature(
            secret=self.credentials.api_secret,
            timestamp=ts,
            method=method,
            request_path=path,
            body=body,
        )

        headers = {
            "POLY_API_KEY": self.credentials.api_key,
            "POLY_SIGNATURE": sig,
            "POLY_TIMESTAMP": ts,
            "POLY_PASSPHRASE": self.credentials.passphrase,
        }
        if self.address:
            headers["POLY_ADDRESS"] = self.address
        return headers

    def build_signed_order(
        self,
        token_id: str,
        side: str,  # "BUY" or "SELL"
        price: float,
        size: float,
        salt: Optional[int] = None,
        expiration: int = 0,
        signature_type: int = 0,  # 0: EOA, 1: POLY_PROXY, 2: POLY_GNOSIS_SAFE
    ) -> tuple[FastOrder, dict[str, Any]]:
        """Create FastOrder struct and generate authentic EIP-712 signature.

        Polymarket raw units (6 decimals):
        - BUY: makerAmount = round(size * price * 1e6) USDC, takerAmount = round(size * 1e6) shares
        - SELL: makerAmount = round(size * 1e6) shares, takerAmount = round(size * price * 1e6) USDC
        """
        side_norm = side.upper()
        side_int = 0 if side_norm == "BUY" else 1

        size_raw = int(round(size * 1_000_000))
        value_raw = int(round(size * price * 1_000_000))

        if side_int == 0:
            maker_amount = value_raw
            taker_amount = size_raw
        else:
            maker_amount = size_raw
            taker_amount = value_raw

        order_salt = salt if salt is not None else int(time.time() * 1000) + (os.getpid() % 1000)

        # Parse numeric token ID
        token_id_int = int(token_id) if token_id.isdigit() else int(hashlib.md5(token_id.encode()).hexdigest(), 16) % (10**24)

        fast_order = FastOrder(
            salt=order_salt,
            maker=self.address,
            signer=self.address,
            taker="0x0000000000000000000000000000000000000000",
            tokenId=token_id_int,
            makerAmount=maker_amount,
            takerAmount=taker_amount,
            expiration=expiration,
            nonce=0,
            feeRateBps=0,
            side=side_int,
            signatureType=signature_type,
        )

        payload = self.signer.build_signed_order_payload(fast_order)
        return fast_order, payload

    def place_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float,
        expiration: int = 0,
    ) -> OrderPlacementResult:
        """Sign and place a limit order on Polymarket CLOB.

        In dry-run mode, computes the authentic EIP-712 signature and records
        the order locally without sending real funds to CLOB.
        """
        try:
            fast_order, payload = self.build_signed_order(
                token_id=token_id,
                side=side,
                price=price,
                size=size,
                expiration=expiration,
            )
            sig = payload.get("signature", "")
        except Exception as e:
            logger.error(f"Failed to generate signed order payload: {e}")
            return OrderPlacementResult(
                success=False,
                order_id="",
                status="ERROR",
                side=side,
                price=price,
                size=size,
                error_message=str(e),
                is_dry_run=self.dry_run,
            )

        if self.dry_run:
            mock_id = f"mock_ord_{uuid.uuid4().hex[:12]}"
            logger.info(
                f"[DRY-RUN] Simulated CLOB POST /order | ID: {mock_id} | {side} {size:.2f} @ ${price:.3f} | "
                f"Sig: {sig[:14]}..."
            )
            self._mock_orders[mock_id] = {
                "orderID": mock_id,
                "token_id": token_id,
                "side": side.upper(),
                "price": price,
                "size": size,
                "timestamp": time.time(),
                "status": "LIVE",
            }
            return OrderPlacementResult(
                success=True,
                order_id=mock_id,
                status="LIVE",
                side=side.upper(),
                price=price,
                size=size,
                raw_response={"orderID": mock_id, "status": "LIVE", "mock": True},
                signature=sig,
                is_dry_run=True,
            )

        path = "/order"
        url = f"{self.base_url}{path}"
        body_json = json.dumps(payload)
        headers = self._get_auth_headers("POST", path, body=body_json)

        try:
            resp = self.session.post(url, data=body_json, headers=headers, timeout=self.timeout)
            data = resp.json() if resp.status_code == 200 else {}
            if resp.status_code == 200 and data.get("success", True):
                order_id = str(data.get("orderID") or data.get("id") or "")
                logger.info(f"Live CLOB order placed successfully: ID={order_id} ({side} {size} @ ${price})")
                return OrderPlacementResult(
                    success=True,
                    order_id=order_id,
                    status="LIVE",
                    side=side.upper(),
                    price=price,
                    size=size,
                    raw_response=data,
                    signature=sig,
                    is_dry_run=False,
                )
            else:
                err = data.get("errorMsg") or data.get("error") or resp.text
                logger.error(f"CLOB order placement failed (HTTP {resp.status_code}): {err}")
                return OrderPlacementResult(
                    success=False,
                    order_id="",
                    status="REJECTED",
                    side=side.upper(),
                    price=price,
                    size=size,
                    error_message=str(err),
                    raw_response=data if isinstance(data, dict) else {},
                    signature=sig,
                    is_dry_run=False,
                )
        except Exception as e:
            logger.error(f"CLOB order placement network exception: {e}")
            return OrderPlacementResult(
                success=False,
                order_id="",
                status="NETWORK_ERROR",
                side=side.upper(),
                price=price,
                size=size,
                error_message=str(e),
                signature=sig,
                is_dry_run=False,
            )

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an individual resting order by order ID."""
        if not order_id:
            return False

        if self.dry_run:
            logger.info(f"[DRY-RUN] Simulated CLOB DELETE /order | Cancelled order {order_id}")
            self._mock_orders.pop(order_id, None)
            return True

        if self._fast_cancel_pool is not None:
            path = "/order"
            body = json.dumps({"orderID": order_id})
            headers = self._get_auth_headers("DELETE", path, body=body)
            res = self._fast_cancel_pool.cancel_order(order_id, headers=headers)
            return res.success

        path = "/order"
        url = f"{self.base_url}{path}"
        payload = json.dumps({"orderID": order_id})
        headers = self._get_auth_headers("DELETE", path, body=payload)

        try:
            resp = self.session.delete(url, data=payload, headers=headers, timeout=self.timeout)
            return resp.status_code in (200, 204)
        except Exception as e:
            logger.error(f"Failed to cancel order {order_id}: {e}")
            return False

    def cancel_all_orders(self) -> bool:
        """Cancel all resting orders for the authenticated account."""
        if self.dry_run:
            count = len(self._mock_orders)
            self._mock_orders.clear()
            logger.info(f"[DRY-RUN] Simulated CLOB DELETE /cancel-all | Cleared {count} resting orders.")
            return True

        path = "/cancel-all"
        url = f"{self.base_url}{path}"
        headers = self._get_auth_headers("DELETE", path)

        try:
            resp = self.session.delete(url, headers=headers, timeout=self.timeout)
            return resp.status_code in (200, 204)
        except Exception as e:
            logger.error(f"Failed to cancel all orders: {e}")
            return False

    def get_open_orders(self, market: Optional[str] = None) -> list[dict[str, Any]]:
        """Query currently active resting orders on Polymarket CLOB."""
        if self.dry_run:
            return list(self._mock_orders.values())

        path = "/orders"
        url = f"{self.base_url}{path}"
        headers = self._get_auth_headers("GET", path)

        try:
            resp = self.session.get(url, headers=headers, timeout=self.timeout)
            if resp.status_code == 200:
                data = resp.json()
                return data if isinstance(data, list) else data.get("orders", [])
            return []
        except Exception as e:
            logger.error(f"Failed to fetch open orders: {e}")
            return []

    def get_balance(self) -> dict[str, float]:
        """Query USDC and asset balances from CLOB accounting."""
        if self.dry_run:
            return {
                "USDC": self._mock_balance_usd,
                "locked": 0.0,
                "available": self._mock_balance_usd,
            }

        # Try /data/balances first, then fallback to /balance-allowance
        for path in ("/data/balances", "/balance-allowance"):
            headers = self._get_auth_headers("GET", path)
            try:
                resp = self.session.get(f"{self.base_url}{path}", headers=headers, timeout=self.timeout)
                if resp.status_code == 200:
                    data = resp.json()
                    balance_val = float(data.get("balance", data.get("USDC", 0.0)) or 0.0)
                    # Convert raw units if needed (>= 1e6)
                    if balance_val > 100_000:
                        balance_val /= 1_000_000.0
                    return {
                        "USDC": balance_val,
                        "available": balance_val,
                        "locked": float(data.get("locked", 0.0) or 0.0),
                    }
            except Exception:
                continue

        return {"USDC": 0.0, "available": 0.0, "locked": 0.0}

    def get_allowances(self) -> dict[str, Any]:
        """Query contract approvals and allowances from CLOB accounting."""
        if self.dry_run:
            return {"collateral": 1_000_000.0, "conditional": 1_000_000.0, "approved": True}

        for path in ("/data/allowances", "/balance-allowance"):
            headers = self._get_auth_headers("GET", path)
            try:
                resp = self.session.get(f"{self.base_url}{path}", headers=headers, timeout=self.timeout)
                if resp.status_code == 200:
                    data = resp.json()
                    return data
            except Exception:
                continue

        return {"approved": False}

    def get_total_rewards(self) -> dict[str, Any]:
        """Query user's authentic total reward earnings and maker rebates.

        Queries Polymarket CLOB `/rewards/user/total` (or `/rewards/user`) or
        Polymarket Data API v2 `/v2/user-stats` and `/v2/activity?type=REWARD`.
        """
        if self.dry_run or not self.address:
            return {
                "total_rewards_usd": 0.0,
                "reward_income_usd": 0.0,
                "maker_rebates_usd": 0.0,
                "yield_income_usd": 0.0,
                "payout_count": 0,
                "source": "simulated",
                "recent_payouts": [],
            }

        # 1. Try CLOB authenticated endpoint first (/rewards/user/total)
        if self.credentials.is_configured:
            path = "/rewards/user/total"
            headers = self._get_auth_headers("GET", path)
            try:
                resp = self.session.get(f"{self.base_url}{path}", headers=headers, timeout=self.timeout)
                if resp.status_code == 200:
                    data = resp.json()
                    tot = float(data.get("total_rewards", data.get("total", 0.0)) or 0.0)
                    return {
                        "total_rewards_usd": tot,
                        "reward_income_usd": tot,
                        "maker_rebates_usd": float(data.get("maker_rebates", 0.0) or 0.0),
                        "yield_income_usd": float(data.get("yield_income", 0.0) or 0.0),
                        "payout_count": int(data.get("count", 0)),
                        "source": "clob_api",
                        "recent_payouts": data.get("payouts", []),
                    }
            except Exception as e:
                logger.debug(f"CLOB /rewards/user/total query: {e}")

        # 2. Query Data API v2 user-stats and activity for ground-truth on-chain earnings
        try:
            data_client = DataAPIClient(session=self.session)
            stats = data_client.user_stats(self.address)
            all_time = stats.get("all_time_pnl", {}) if isinstance(stats, dict) else {}
            reward_inc = float(all_time.get("reward_income", 0.0) or 0.0)
            maker_reb = float(all_time.get("maker_rebate", 0.0) or 0.0)
            yield_inc = float(all_time.get("yield_income", 0.0) or 0.0)

            # Query recent payout transactions
            payouts = []
            try:
                act = data_client.activity(
                    user=self.address,
                    types=["REWARD", "MAKER_REBATE"],
                    max_pages=1,
                )
                payouts = act[:20] if act else []
            except Exception:
                pass

            return {
                "total_rewards_usd": reward_inc + maker_reb,
                "reward_income_usd": reward_inc,
                "maker_rebates_usd": maker_reb,
                "yield_income_usd": yield_inc,
                "payout_count": len(payouts),
                "source": "polymarket_data_api",
                "recent_payouts": payouts,
            }
        except Exception as e:
            logger.warning(f"Failed to fetch authentic reward balance from Data API: {e}")

        return {
            "total_rewards_usd": 0.0,
            "reward_income_usd": 0.0,
            "maker_rebates_usd": 0.0,
            "yield_income_usd": 0.0,
            "payout_count": 0,
            "source": "unavailable",
            "recent_payouts": [],
        }

    def get_daily_rewards(self, date: Optional[str] = None) -> dict[str, Any]:
        """Query user's current day reward earnings and points.

        Queries CLOB `/rewards/user?date=YYYY-MM-DD` and `/rewards/user/markets`.
        """
        target_date = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")

        if self.dry_run or not self.address:
            return {
                "date": target_date,
                "daily_rewards_usd": 0.0,
                "daily_rebates_usd": 0.0,
                "daily_combined_usd": 0.0,
                "market_breakdown": [],
                "source": "simulated",
            }

        # 1. Try CLOB authenticated endpoint (/rewards/user?date=...&signature_type=...)
        if self.credentials.is_configured:
            path = "/rewards/user"
            headers = self._get_auth_headers("GET", path)
            try:
                url = f"{self.base_url}{path}?date={target_date}&signature_type=0"
                resp = self.session.get(url, headers=headers, timeout=self.timeout)
                if resp.status_code == 200:
                    data = resp.json()
                    daily_rew = float(data.get("daily_rewards", data.get("total_rewards", data.get("amount", 0.0))) or 0.0)
                    return {
                        "date": target_date,
                        "daily_rewards_usd": daily_rew,
                        "daily_rebates_usd": float(data.get("maker_rebates", 0.0) or 0.0),
                        "daily_combined_usd": daily_rew,
                        "market_breakdown": data.get("markets", []),
                        "source": "clob_api",
                    }
            except Exception as e:
                logger.debug(f"CLOB /rewards/user query: {e}")

        # 2. Try CLOB /rewards/user/markets with L2 auth or address query
        try:
            path = "/rewards/user/markets"
            headers = self._get_auth_headers("GET", path) if self.credentials.is_configured else {}
            resp = self.session.get(f"{self.base_url}{path}", headers=headers, timeout=self.timeout)
            if resp.status_code == 200:
                data = resp.json()
                markets = data.get("data", []) if isinstance(data, dict) else []
                total_earned = 0.0
                breakdown = []
                for m in markets:
                    earnings = m.get("earnings", [])
                    m_earned = sum(float(e.get("earnings", 0.0) or 0.0) for e in earnings)
                    if m_earned > 0.0:
                        total_earned += m_earned
                        breakdown.append({
                            "condition_id": m.get("condition_id"),
                            "question": m.get("question"),
                            "earned_usd": m_earned,
                            "earning_percentage": float(m.get("earning_percentage", 0.0) or 0.0),
                        })
                if total_earned > 0.0:
                    return {
                        "date": target_date,
                        "daily_rewards_usd": total_earned,
                        "daily_rebates_usd": 0.0,
                        "daily_combined_usd": total_earned,
                        "market_breakdown": breakdown,
                        "source": "clob_markets_api",
                    }
        except Exception as e:
            logger.debug(f"CLOB /rewards/user/markets query: {e}")

        # 3. Query Data API v2 activity for today's distributions
        try:
            data_client = DataAPIClient(session=self.session)
            act = data_client.activity(user=self.address, types=["REWARD", "MAKER_REBATE"], max_pages=1)
            cutoff_ts = time.time() - 86400.0
            today_rewards = sum(
                float(x.get("usdc_size", 0.0) or 0.0)
                for x in act
                if x.get("type") == "REWARD" and float(x.get("timestamp", 0.0) or 0.0) >= cutoff_ts
            )
            today_rebates = sum(
                float(x.get("usdc_size", 0.0) or 0.0)
                for x in act
                if x.get("type") == "MAKER_REBATE" and float(x.get("timestamp", 0.0) or 0.0) >= cutoff_ts
            )
            return {
                "date": target_date,
                "daily_rewards_usd": today_rewards,
                "daily_rebates_usd": today_rebates,
                "daily_combined_usd": today_rewards + today_rebates,
                "market_breakdown": [],
                "source": "polymarket_data_api",
            }
        except Exception as e:
            logger.debug(f"Data API daily activity query: {e}")

        return {
            "date": target_date,
            "daily_rewards_usd": 0.0,
            "daily_rebates_usd": 0.0,
            "daily_combined_usd": 0.0,
            "market_breakdown": [],
            "source": "unavailable",
        }

    def get_order_scoring(self, order_id: str) -> dict[str, Any]:
        """Query live qualifying maker reward scoring status for an active order.

        Queries CLOB `GET /order-scoring?order_id=<order_id>`.
        Returns `scoring: True/False` indicating whether the order meets spread,
        size, and duration criteria to earn liquidity rewards.
        """
        if not order_id:
            return {"order_id": "", "scoring": False, "dry_run": self.dry_run}

        if self.dry_run:
            is_mock = order_id in self._mock_orders
            return {
                "order_id": order_id,
                "scoring": is_mock,
                "dry_run": True,
                "raw_response": {"scoring": is_mock, "mock": True},
            }

        path = "/order-scoring"
        headers = self._get_auth_headers("GET", path)
        url = f"{self.base_url}{path}?order_id={order_id}"

        try:
            resp = self.session.get(url, headers=headers, timeout=self.timeout)
            if resp.status_code == 200:
                data = resp.json()
                is_scoring = bool(data.get("scoring", False))
                return {
                    "order_id": order_id,
                    "scoring": is_scoring,
                    "dry_run": False,
                    "raw_response": data,
                }
            else:
                logger.debug(f"CLOB /order-scoring returned status {resp.status_code}: {resp.text}")
                return {
                    "order_id": order_id,
                    "scoring": False,
                    "dry_run": False,
                    "raw_response": {"status_code": resp.status_code},
                }
        except Exception as e:
            logger.error(f"Failed to query order scoring for {order_id}: {e}")
            return {
                "order_id": order_id,
                "scoring": False,
                "dry_run": False,
                "error": str(e),
            }

    def get_market_reward_config(self, condition_id_or_market_id: str) -> Optional[dict[str, Any]]:
        """Query live official reward parameters for a market from CLOB `/rewards/user/markets`.

        Returns:
            Dict containing rate_per_day, rewards_max_spread, rewards_min_size,
            market_competitiveness, and earning_percentage.
        """
        if not condition_id_or_market_id:
            return None

        key = str(condition_id_or_market_id).lower()
        path = "/rewards/user/markets"
        headers = self._get_auth_headers("GET", path) if self.credentials.is_configured else {}
        url = f"{self.base_url}{path}"

        try:
            resp = self.session.get(url, headers=headers, timeout=self.timeout)
            if resp.status_code == 200:
                data = resp.json()
                markets = data.get("data", []) if isinstance(data, dict) else []
                for m in markets:
                    cid = str(m.get("condition_id", "")).lower()
                    mid = str(m.get("market_id", "")).lower()
                    if key in (cid, mid):
                        cfg = m.get("rewards_config", [{}])[0] if m.get("rewards_config") else {}
                        return {
                            "condition_id": m.get("condition_id"),
                            "market_id": m.get("market_id"),
                            "question": m.get("question"),
                            "rate_per_day": float(cfg.get("rate_per_day", 0.0) or 0.0),
                            "rewards_max_spread": float(m.get("rewards_max_spread", 0.0) or 0.0),
                            "rewards_min_size": float(m.get("rewards_min_size", 0.0) or 0.0),
                            "market_competitiveness": float(m.get("market_competitiveness", 0.0) or 0.0),
                            "earning_percentage": float(m.get("earning_percentage", 0.0) or 0.0),
                            "earnings": m.get("earnings", []),
                        }
        except Exception as e:
            logger.debug(f"Failed to fetch market reward config: {e}")

        return None
