"""L2 API credentials and HMAC-SHA256 request authentication."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ClobAuthCredentials:
    """Polymarket CLOB API Authentication Credentials.

    ``api_*`` are the L2 API key credentials (the API key is a UUID and is sent
    as the order ``owner``). ``private_key`` is the EOA signing key.
    ``address`` is the funder/proxy wallet that holds collateral (the maker);
    when empty the EOA derived from ``private_key`` acts as maker too.
    """

    api_key: str = ""
    api_secret: str = ""
    passphrase: str = ""
    address: str = ""
    private_key: str = ""
    # 0: EOA, 1: POLY_PROXY, 2: POLY_GNOSIS_SAFE, 3: POLY_1271 (Deposit Wallet)
    signature_type: int = 0

    def __post_init__(self) -> None:
        try:
            signature_type = int(self.signature_type)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"POLY_SIGNATURE_TYPE must be one of 0, 1, 2, 3 (got {self.signature_type!r})"
            ) from exc
        if signature_type not in (0, 1, 2, 3):
            raise ValueError(
                f"POLY_SIGNATURE_TYPE must be one of 0, 1, 2, 3 (got {signature_type})"
            )
        self.signature_type = signature_type

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

        def _lookup(*names: str) -> str:
            for name in names:
                value = os.environ.get(name) or env_dict.get(name, "")
                if value:
                    return value
            return ""

        api_key = _lookup("POLY_API_KEY", "POLYMARKET_API_KEY")
        api_secret = _lookup("POLY_API_SECRET", "POLYMARKET_SECRET")
        passphrase = _lookup("POLY_PASSPHRASE", "POLYMARKET_PASSPHRASE")
        # Funder / proxy wallet (maker). Never derived here: it is the wallet that
        # actually holds the collateral, which may be a proxy or a Gnosis Safe.
        address = _lookup("POLY_ADDRESS", "PROXY_WALLET_ADDRESS")
        private_key = _lookup("POLY_PRIVATE_KEY", "PRIVATE_KEY")
        signature_type_raw = _lookup("POLY_SIGNATURE_TYPE", "POLYMARKET_SIGNATURE_TYPE")

        signature_type = 0
        if signature_type_raw:
            try:
                signature_type = int(signature_type_raw)
            except ValueError as exc:
                raise ValueError(
                    f"POLY_SIGNATURE_TYPE must be one of 0, 1, 2, 3 (got {signature_type_raw!r})"
                ) from exc

        return cls(
            api_key=api_key,
            api_secret=api_secret,
            passphrase=passphrase,
            address=address,
            private_key=private_key,
            signature_type=signature_type,
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
            except (ValueError, TypeError):
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

        if self.signature_type not in (0, 1, 2, 3):
            errors.append(f"POLY_SIGNATURE_TYPE must be one of 0, 1, 2, 3 (got {self.signature_type}).")

        return errors


def build_clob_hmac_signature(
    secret: str,
    timestamp: str,
    method: str,
    request_path: str,
    body: str | bytes | None = None,
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
    except (ValueError, TypeError):
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


class AuthHeadersMixin:
    """L2 request header construction."""


    def _get_auth_headers(
        self,
        method: str,
        path: str,
        body: str | bytes | None = None,
    ) -> dict[str, str]:
        """Construct L2 authentication headers.

        ``POLY_ADDRESS`` must be the EOA that owns the API key (the signer),
        matching py-clob-client's ``create_level_2_headers``, which always sends
        ``signer.address()``. It is never the funder/proxy wallet: that address
        is only carried by the order as ``maker``.
        """
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
        if self.signer_address:
            headers["POLY_ADDRESS"] = self.signer_address
        return headers
