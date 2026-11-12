"""Centralized Configuration and Secrets Management for Polymarket Bot.

Provides BotConfig dataclass and load_config helper to safely load credentials
and endpoints from environment variables and .env files.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from pathlib import Path

from polymarketbot.logs import get_logger

log = get_logger("config")


@dataclass
class BotConfig:
    """Centralized configuration and secrets for Polymarket trading and simulation."""

    POLYMARKET_API_KEY: str = ""
    POLYMARKET_SECRET: str = ""
    POLYMARKET_PASSPHRASE: str = ""
    PRIVATE_KEY: str = ""
    PROXY_WALLET_ADDRESS: str = ""
    POLYMARKET_RELAYER_API_KEY: str = ""
    POLYMARKET_RELAYER_API_KEY_ADDRESS: str = ""
    POLYGON_RPC_URL: str = ""
    CLOB_API_URL: str = "https://clob.polymarket.com"
    CLOB_WS_URL: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    DATA_GOV_API_KEY: str = ""
    GOOGLE_CIVIC_API_KEY: str = ""
    FRED_API_KEY: str = ""
    BLS_API_KEY: str = ""

    # Convenient lowercase property accessors
    @property
    def api_key(self) -> str:
        return self.POLYMARKET_API_KEY

    @property
    def secret(self) -> str:
        return self.POLYMARKET_SECRET

    @property
    def passphrase(self) -> str:
        return self.POLYMARKET_PASSPHRASE

    @property
    def private_key(self) -> str:
        return self.PRIVATE_KEY

    @property
    def proxy_wallet_address(self) -> str:
        return self.PROXY_WALLET_ADDRESS

    @property
    def relayer_api_key(self) -> str:
        return self.POLYMARKET_RELAYER_API_KEY

    @property
    def relayer_api_key_address(self) -> str:
        return self.POLYMARKET_RELAYER_API_KEY_ADDRESS

    @property
    def polygon_rpc_url(self) -> str:
        return self.POLYGON_RPC_URL

    @property
    def clob_api_url(self) -> str:
        return self.CLOB_API_URL

    @property
    def clob_ws_url(self) -> str:
        return self.CLOB_WS_URL

    @property
    def data_gov_api_key(self) -> str:
        return self.DATA_GOV_API_KEY

    @property
    def google_civic_api_key(self) -> str:
        return self.GOOGLE_CIVIC_API_KEY

    @property
    def fred_api_key(self) -> str:
        return self.FRED_API_KEY

    @property
    def bls_api_key(self) -> str:
        return self.BLS_API_KEY

    def is_live_trading_ready(self) -> bool:
        """Check if minimum credentials required for live signed trading are set."""
        return bool(self.PRIVATE_KEY.strip())

    def is_api_authenticated(self) -> bool:
        """Check if CLOB L2/L3 API credentials are fully populated."""
        return bool(
            self.POLYMARKET_API_KEY.strip()
            and self.POLYMARKET_SECRET.strip()
            and self.POLYMARKET_PASSPHRASE.strip()
        )

    def to_dict(self, mask_secrets: bool = False) -> dict[str, str]:
        """Convert configuration to dictionary, optionally masking sensitive secrets."""
        d = asdict(self)
        if mask_secrets:
            secret_keys = {
                "POLYMARKET_API_KEY",
                "POLYMARKET_SECRET",
                "POLYMARKET_PASSPHRASE",
                "POLYMARKET_RELAYER_API_KEY",
                "PRIVATE_KEY",
            }
            for k in secret_keys:
                v = d.get(k, "")
                if not v:
                    continue
                # The trailing characters of a signing key identify nothing an
                # operator needs and narrow the search space for everyone else.
                d[k] = "***" if k == "PRIVATE_KEY" else f"***{v[-4:]}"
            # A hosted RPC URL carries the provider key as its last path
            # segment (``.../v2/<key>``), so an unmasked dump published the key
            # that the private-key masking was there to protect.
            for k in ("POLYGON_RPC_URL",):
                v = d.get(k, "")
                if v:
                    head, sep, tail = v.rpartition("/")
                    d[k] = f"{head}{sep}***" if sep and len(tail) >= 8 else v
        return d


def _parse_env_file_direct(file_path: Path) -> dict[str, str]:
    """Parse a simple KEY=VALUE formatted .env file directly without external dependencies."""
    parsed: dict[str, str] = {}
    if not file_path.is_file():
        return parsed

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export "):].strip()
                if "=" in line:
                    key, val = line.split("=", 1)
                    key = key.strip()
                    val = val.strip()
                    # Strip single or double quotes
                    if (val.startswith('"') and val.endswith('"')) or (
                        val.startswith("'") and val.endswith("'")
                    ):
                        val = val[1:-1]
                    parsed[key] = val
    except Exception as e:  # noqa: BLE001 - a malformed .env must not block startup
        log.warning(f"Error directly parsing env file {file_path}: {e}")

    return parsed


def load_config(env_path: str | Path = ".env") -> BotConfig:
    """Load configuration from .env file and environment variables.

    Loading order and precedence:
    1. os.environ environment variables (highest priority)
    2. .env file at specified env_path (via python-dotenv if installed, and direct parser)
    3. BotConfig default values

    Args:
        env_path: Path to the .env file. Defaults to ".env".

    Returns:
        BotConfig instance with resolved values.
    """
    path = Path(env_path)
    file_vars: dict[str, str] = {}

    if path.is_file():
        # Read the file without mutating os.environ. `load_dotenv` writes new
        # keys into the process environment even with override=False, which made
        # a later load_config() in the same process inherit a previous file's
        # values and broke the documented "os.environ wins" precedence.
        try:
            from dotenv import dotenv_values

            file_values = dotenv_values(dotenv_path=path)
            file_vars.update({k: v for k, v in file_values.items() if v is not None})
        except ImportError:
            # Fallback to direct parsing
            pass

        # Ensure direct parser picks up keys as well
        direct_parsed = _parse_env_file_direct(path)
        for k, v in direct_parsed.items():
            if k not in file_vars:
                file_vars[k] = v
    else:
        # Say so rather than resolve every field from defaults: a live run
        # started from the wrong directory looked configured and was not.
        log.debug(f"no env file at {path.resolve()}; using the process environment only")

    def _get_val(key: str, default: str) -> str:
        # Check os.environ first (exact match, uppercase, lowercase)
        if key in os.environ:
            return os.environ[key]
        if key.lower() in os.environ:
            return os.environ[key.lower()]
        # Check file_vars next
        if key in file_vars:
            return file_vars[key]
        if key.lower() in file_vars:
            return file_vars[key.lower()]
        # Fall back to default
        return default

    config = BotConfig(
        POLYMARKET_API_KEY=_get_val("POLYMARKET_API_KEY", ""),
        POLYMARKET_SECRET=_get_val("POLYMARKET_SECRET", ""),
        POLYMARKET_PASSPHRASE=_get_val("POLYMARKET_PASSPHRASE", ""),
        PRIVATE_KEY=_get_val("PRIVATE_KEY", ""),
        PROXY_WALLET_ADDRESS=_get_val("PROXY_WALLET_ADDRESS", ""),
        POLYMARKET_RELAYER_API_KEY=_get_val("POLYMARKET_RELAYER_API_KEY", ""),
        POLYMARKET_RELAYER_API_KEY_ADDRESS=_get_val(
            "POLYMARKET_RELAYER_API_KEY_ADDRESS", ""
        ),
        POLYGON_RPC_URL=_get_val("POLYGON_RPC_URL", ""),
        CLOB_API_URL=_get_val("CLOB_API_URL", "https://clob.polymarket.com"),
        CLOB_WS_URL=_get_val("CLOB_WS_URL", "wss://ws-subscriptions-clob.polymarket.com/ws/market"),
        DATA_GOV_API_KEY=_get_val("DATA_GOV_API_KEY", "") or _get_val("FEC_API_KEY", ""),
        GOOGLE_CIVIC_API_KEY=_get_val("GOOGLE_CIVIC_API_KEY", ""),
        FRED_API_KEY=_get_val("FRED_API_KEY", ""),
        BLS_API_KEY=_get_val("BLS_API_KEY", ""),
    )

    return config
