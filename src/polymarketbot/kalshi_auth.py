"""Kalshi API-key request signing for REST and WebSocket.

Kalshi authenticates every private request with three headers:

``KALSHI-ACCESS-KEY``
    the API key id (a UUID), not the key itself,
``KALSHI-ACCESS-TIMESTAMP``
    the request time in milliseconds since the epoch,
``KALSHI-ACCESS-SIGNATURE``
    base64 of the signature over ``timestamp + method + path``.

The pre-sign text is the concatenation of the decimal timestamp, the upper-case
HTTP method and the request path, e.g. ``1700000000000GET/trade-api/v2/portfolio/balance``.
The path is signed **without its query string** -- signing
``/portfolio/orders?limit=5`` produces a signature over ``/portfolio/orders``
and Kalshi rejects the request with ``401`` otherwise. The query string is
still sent, only the signature ignores it.

Two key types are accepted and Kalshi picks the verification algorithm from
the registered public key:

* Ed25519 (RFC 8032) signs the text directly, 64-byte signatures;
* RSA signs with RSA-PSS / SHA-256, MGF1-SHA256, salt length equal to the
  digest length (32 bytes).

The PEM header does not identify the key type -- ``openssl genpkey`` emits a
PKCS#8 ``PRIVATE KEY`` block for RSA too -- so the type comes from the parsed
key object, never from the text.

The key id and the PEM never leave this module: errors name the file and the
exception class only, and neither value appears in a log line or ``repr``.
"""

from __future__ import annotations

import base64
import os
import time
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

KEY_ID_ENV = "KALSHI_KEY_ID"
#: Path to the PEM private key; relative paths resolve against the CWD.
KEY_PATH_ENV = "KALSHI_PRIVATE_KEY_PATH"

HEADER_KEY = "KALSHI-ACCESS-KEY"
HEADER_SIGNATURE = "KALSHI-ACCESS-SIGNATURE"
HEADER_TIMESTAMP = "KALSHI-ACCESS-TIMESTAMP"
ACCESS_HEADERS = (HEADER_KEY, HEADER_SIGNATURE, HEADER_TIMESTAMP)

#: Kalshi rejects requests whose timestamp drifts more than a minute or so.
MAX_CLOCK_SKEW_MS = 60_000

PrivateKey = Ed25519PrivateKey | RSAPrivateKey


class KalshiAuthError(RuntimeError):
    """A local key/signing problem. Never carries key material."""


def _now_ms() -> int:
    return int(time.time() * 1000)


def signing_path(target: str) -> str:
    """The absolute path Kalshi expects in the pre-sign text.

    ``target`` may be a bare path or a full URL; the query string and fragment
    are dropped, and a non-absolute result is a programming error rather than
    a 401 from the exchange.
    """
    text = str(target or "").strip()
    if "://" in text:
        text = urlsplit(text).path
    path = text.split("?", 1)[0].split("#", 1)[0]
    if not path.startswith("/"):
        raise KalshiAuthError(
            f"signing path must be absolute (start with '/'), got {path!r}"
        )
    return path


def load_private_key(path: str | os.PathLike[str], *, password: bytes | None = None) -> PrivateKey:
    """Read and parse a PEM private key, without ever echoing its contents.

    Raises :class:`KalshiAuthError` on a missing file or unparseable PEM; the
    underlying exception class is reported, never its message or the bytes it
    was parsed from.
    """
    file = Path(path).expanduser()
    try:
        data = file.read_bytes()
    except OSError as exc:
        raise KalshiAuthError(
            f"cannot read Kalshi private key at {file}: {type(exc).__name__}"
        ) from None
    try:
        return serialization.load_pem_private_key(data, password=password)
    except (ValueError, TypeError) as exc:
        raise KalshiAuthError(
            f"cannot parse Kalshi private key at {file}: {type(exc).__name__}"
        ) from None


class KalshiSigner:
    """One API key: signs request paths into Kalshi's three headers."""

    def __init__(
        self,
        key_id: str,
        private_key: PrivateKey,
        *,
        clock: Callable[[], int] = _now_ms,
    ) -> None:
        text = str(key_id or "").strip()
        if not text:
            raise KalshiAuthError(f"{KEY_ID_ENV} is empty")
        if isinstance(private_key, Ed25519PrivateKey):
            self.kind = "ed25519"
        elif isinstance(private_key, RSAPrivateKey):
            self.kind = "rsa"
        else:
            raise KalshiAuthError(
                f"unsupported Kalshi private key type {type(private_key).__name__}"
            )
        self._key_id = text
        self._key = private_key
        self._clock = clock

    # ------------------------------------------------------------- factories
    @classmethod
    def from_path(
        cls,
        path: str | os.PathLike[str],
        *,
        key_id: str | None = None,
        password: bytes | None = None,
        clock: Callable[[], int] = _now_ms,
    ) -> KalshiSigner:
        resolved = key_id if key_id is not None else os.environ.get(KEY_ID_ENV)
        return cls(
            str(resolved or ""),
            load_private_key(path, password=password),
            clock=clock,
        )

    @classmethod
    def from_env(
        cls,
        *,
        env: Mapping[str, str] | None = None,
        clock: Callable[[], int] = _now_ms,
    ) -> KalshiSigner:
        source: Mapping[str, str] = os.environ if env is None else env
        raw_path = str(source.get(KEY_PATH_ENV) or "").strip()
        if not raw_path:
            raise KalshiAuthError(
                f"{KEY_PATH_ENV} is unset; point it at the PEM private key"
            )
        return cls.from_path(
            raw_path,
            key_id=source.get(KEY_ID_ENV) or "",
            clock=clock,
        )

    # -------------------------------------------------------------- signing
    def sign_text(self, text: str) -> str:
        """Base64 signature over exactly ``text``."""
        message = text.encode("utf-8")
        if self.kind == "ed25519":
            signature = self._key.sign(message)  # type: ignore[union-attr]
        else:
            signature = self._key.sign(  # type: ignore[union-attr]
                message,
                padding.PSS(
                    mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.DIGEST_LENGTH,
                ),
                hashes.SHA256(),
            )
        return base64.b64encode(signature).decode("ascii")

    def pre_sign_text(self, method: str, path: str, timestamp_ms: int) -> str:
        """``<timestamp><METHOD><path-without-query>``."""
        return f"{int(timestamp_ms)}{str(method).upper()}{signing_path(path)}"

    def sign_headers(
        self, method: str, path: str, *, timestamp_ms: int | None = None
    ) -> dict[str, str]:
        """The three ``KALSHI-ACCESS-*`` headers for one request."""
        stamp = int(self._clock() if timestamp_ms is None else timestamp_ms)
        signature = self.sign_text(self.pre_sign_text(method, path, stamp))
        return {
            HEADER_KEY: self._key_id,
            HEADER_SIGNATURE: signature,
            HEADER_TIMESTAMP: str(stamp),
        }

    def __repr__(self) -> str:  # no secret, no key id
        return f"KalshiSigner(kind={self.kind!r})"


_DEFAULT_SIGNER: KalshiSigner | None = None


def default_signer() -> KalshiSigner:
    """The process-wide signer built from the environment, loaded once."""
    global _DEFAULT_SIGNER
    if _DEFAULT_SIGNER is None:
        _DEFAULT_SIGNER = KalshiSigner.from_env()
    return _DEFAULT_SIGNER


def sign_headers(
    method: str, path: str, *, timestamp_ms: int | None = None
) -> dict[str, str]:
    """Sign with the environment-configured key (see :func:`default_signer`)."""
    return default_signer().sign_headers(method, path, timestamp_ms=timestamp_ms)


# ------------------------------------------------------------------ requests
def error_code(response: requests.Response) -> str | None:
    """Kalshi's machine-readable error code, when the body carries one.

    Both ``{"error": {"code": ...}}`` (the documented envelope) and a bare
    ``{"code": ...}`` are accepted; anything else -- HTML, empty body -- is
    ``None``.
    """
    try:
        payload: Any = response.json()
    except ValueError:
        return None
    if not isinstance(payload, Mapping):
        return None
    inner = payload.get("error")
    if isinstance(inner, Mapping) and inner.get("code"):
        return str(inner["code"])
    for key in ("code", "error_code"):
        if payload.get(key):
            return str(payload[key])
    return None


def signed_request(
    session: requests.Session,
    method: str,
    url: str,
    *,
    signer: KalshiSigner | None = None,
    **kwargs: Any,
) -> requests.Response:
    """Send one signed request, retrying once on ``401`` with a new timestamp.

    A ``401`` is the only status retried: it usually means the timestamp was
    stale, and one re-sign fixes it. Every other failure -- 400, 403, 429,
    5xx -- is returned to the caller untouched so it cannot be mistaken for a
    clock problem, and transport retries stay in the session's adapter.
    """
    key = signer or default_signer()
    headers = dict(kwargs.pop("headers", None) or {})
    path = signing_path(url)
    headers.update(key.sign_headers(method, path))
    response = session.request(method, url, headers=headers, **kwargs)
    if response.status_code == 401:
        response.close()
        headers.update(key.sign_headers(method, path))
        response = session.request(method, url, headers=headers, **kwargs)
    return response


def signed_get(
    session: requests.Session,
    url: str,
    *,
    signer: KalshiSigner | None = None,
    **kwargs: Any,
) -> requests.Response:
    """``GET`` via :func:`signed_request`."""
    return signed_request(session, "GET", url, signer=signer, **kwargs)
