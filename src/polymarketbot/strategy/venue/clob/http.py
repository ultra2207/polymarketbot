"""Thread-local HTTP sessions, authenticated requests and cursor pagination."""

from __future__ import annotations

from typing import Any, Optional

import requests

from polymarketbot.logs import get_logger
from polymarketbot.strategy.venue.clob.common import END_CURSOR, USER_AGENT, ClobAPIError

logger = get_logger("clob_client")


class HttpMixin:
    """HTTP plumbing shared by every CLOB call."""


    def _get_session(self) -> requests.Session:
        """Return the calling thread's requests.Session (created lazily)."""
        session = getattr(self._session_local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update({
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            })
            self._session_local.session = session
            with self._sessions_lock:
                self._sessions.append(session)
        return session

    @property
    def session(self) -> requests.Session:
        """This thread's HTTP session (kept for callers that pass it on)."""
        return self._get_session()

    def close(self) -> None:
        """Close every session and connection pool created by this client (best effort)."""
        if self.hft_pool is not None:
            try:
                self.hft_pool.close()
            except Exception:
                pass
        if self._fast_cancel_pool is not None:
            try:
                self._fast_cancel_pool.close()
            except Exception:
                pass
        with self._sessions_lock:
            sessions = list(self._sessions)
            self._sessions.clear()
        for session in sessions:
            try:
                session.close()
            except Exception:
                pass

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        body: Optional[str] = None,
        auth: bool = True,
    ) -> Any:
        """Execute one CLOB REST call and return its parsed JSON body.

        Raises:
            ClobAPIError: on a non-2xx response, a transport error, or a body
                that is not valid JSON. The signature is computed over the bare
                path (query parameters are not signed), matching py-clob-client's
                ``create_level_2_headers`` (see ``py_clob_client/headers/headers.py``).
        """
        method = method.upper()
        if auth and not self.credentials.is_configured:
            raise ClobAPIError(
                f"{method} {path} requires L2 API credentials",
                status_code=401,
                path=path,
            )

        headers = self._get_auth_headers(method, path, body=body) if auth else {}
        kwargs: dict[str, Any] = {"headers": headers, "timeout": self.timeout}
        if params:
            kwargs["params"] = params
        if body is not None:
            kwargs["data"] = body

        session = self._get_session()
        url = f"{self.base_url}{path}"
        try:
            if method == "GET":
                resp = session.get(url, **kwargs)
            elif method == "POST":
                resp = session.post(url, **kwargs)
            elif method == "DELETE":
                resp = session.delete(url, **kwargs)
            else:
                resp = session.request(method, url, **kwargs)
        except requests.RequestException as e:
            raise ClobAPIError(
                f"{method} {path} transport error: {e}",
                status_code=0,
                path=path,
            ) from e

        text = getattr(resp, "text", "") or ""
        if not 200 <= resp.status_code < 300:
            raise ClobAPIError(
                f"{method} {path} returned HTTP {resp.status_code}: {text[:300]}",
                status_code=resp.status_code,
                path=path,
                response_text=text,
            )
        try:
            return resp.json()
        except ValueError as e:
            raise ClobAPIError(
                f"{method} {path} returned an unparseable body: {text[:300]}",
                status_code=resp.status_code,
                path=path,
                response_text=text,
            ) from e

    def _paginate(
        self,
        path: str,
        base_params: Optional[dict[str, Any]] = None,
        *,
        max_pages: int = 20,
    ) -> list[dict[str, Any]]:
        """Follow ``next_cursor`` over a ``{limit, next_cursor, count, data}`` endpoint.

        Reference: ``GET /data/orders`` / ``GET /data/trades`` in
        https://docs.polymarket.com/api-reference/trade/get-user-orders and
        https://raw.githubusercontent.com/Polymarket/py-clob-client/main/py_clob_client/client.py
        (``get_orders`` / ``get_trades``; ``END_CURSOR = "LTE="``).
        """
        pages = max(1, int(max_pages))
        results: list[dict[str, Any]] = []
        seen_cursors: set[str] = set()
        cursor = "MA=="
        for page in range(pages):
            params = dict(base_params or {})
            params["next_cursor"] = cursor
            payload = self._request("GET", path, params=params)
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                raise ClobAPIError(
                    f"GET {path} returned an unexpected body "
                    f"(expected an object with a 'data' list)",
                    status_code=200,
                    path=path,
                )
            results.extend(item for item in payload["data"] if isinstance(item, dict))

            next_cursor = payload.get("next_cursor")
            next_cursor = "" if next_cursor is None else str(next_cursor)
            if not next_cursor or next_cursor == END_CURSOR:
                return results
            if next_cursor in seen_cursors:
                raise ClobAPIError(
                    f"GET {path} repeated pagination cursor {next_cursor!r}; "
                    f"refusing to return a partial order list",
                    status_code=200,
                    path=path,
                )
            seen_cursors.add(next_cursor)
            if page == pages - 1:
                logger.warning(
                    f"GET {path} stopped at max_pages={pages} with more pages available; "
                    f"the list is truncated"
                )
                return results
            cursor = next_cursor
        return results
