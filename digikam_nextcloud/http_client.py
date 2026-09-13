"""Thread-local HTTP client with connection keep-alive for WebDAV."""
from __future__ import annotations

import http.client
import logging
import ssl
import threading
from typing import Optional
from urllib.parse import urlparse

LOG = logging.getLogger(__name__)


class KeepAliveHttpClient:
    """
    Minimal HTTP(S) client that reuses one connection per thread.

    urllib.request opens a new TCP/TLS session per call, which dominates
    cost when resolving tens/hundreds of thousands of files over WebDAV.
    """

    def __init__(
        self,
        base_url: str,
        *,
        default_headers: Optional[dict[str, str]] = None,
        timeout: float = 60.0,
        ssl_context: Optional[ssl.SSLContext] = None,
        max_retries: int = 2,
    ):
        parsed = urlparse(base_url.rstrip("/"))
        if parsed.scheme not in ("http", "https"):
            raise ValueError(f"Unsupported URL scheme: {parsed.scheme!r}")
        self.scheme = parsed.scheme
        self.host = parsed.hostname or "localhost"
        self.port = parsed.port or (443 if self.scheme == "https" else 80)
        # base path prefix on the host (usually empty for https://cloud.example.com)
        self.base_path = (parsed.path or "").rstrip("/")
        self.timeout = timeout
        self.ssl_context = ssl_context
        self.default_headers = dict(default_headers or {})
        self.max_retries = max_retries
        self._local = threading.local()

    def _conn(self) -> http.client.HTTPConnection:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        if self.scheme == "https":
            conn = http.client.HTTPSConnection(
                self.host,
                self.port,
                timeout=self.timeout,
                context=self.ssl_context,
            )
        else:
            conn = http.client.HTTPConnection(
                self.host,
                self.port,
                timeout=self.timeout,
            )
        self._local.conn = conn
        return conn

    def _drop_conn(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self._local.conn = None

    def close(self) -> None:
        self._drop_conn()

    def request(
        self,
        method: str,
        path: str,
        *,
        headers: Optional[dict[str, str]] = None,
        body: Optional[bytes] = None,
        timeout: Optional[float] = None,
    ) -> tuple[int, dict[str, str], bytes]:
        """
        Perform a request. ``path`` is absolute on the server
        (e.g. /remote.php/dav/files/user/a.jpg) or relative to base_path.
        """
        if path.startswith("http://") or path.startswith("https://"):
            parsed = urlparse(path)
            req_path = parsed.path or "/"
            if parsed.query:
                req_path = f"{req_path}?{parsed.query}"
        else:
            if not path.startswith("/"):
                path = "/" + path
            # Support Nextcloud in a subpath (https://host/nextcloud/)
            if self.base_path and not (
                path == self.base_path or path.startswith(self.base_path + "/")
            ):
                path = f"{self.base_path}{path}"
            req_path = path

        hdrs = {
            "Host": self.host,
            "Connection": "keep-alive",
            "Accept-Encoding": "identity",
        }
        hdrs.update(self.default_headers)
        if headers:
            hdrs.update(headers)
        if body is not None:
            hdrs.setdefault("Content-Length", str(len(body)))

        last_err: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            conn = self._conn()
            try:
                request_timeout = self.timeout if timeout is None else timeout
                conn.timeout = request_timeout
                if conn.sock is not None:
                    conn.sock.settimeout(request_timeout)
                conn.request(method, req_path, body=body, headers=hdrs)
                resp = conn.getresponse()
                raw = resp.read()
                status = resp.status
                resp_headers = {k.lower(): v for k, v in resp.getheaders()}
                # Drop connection on non-keep-alive or errors that poison the socket
                if resp_headers.get("connection", "").lower() == "close" or status >= 500:
                    self._drop_conn()
                return status, resp_headers, raw
            except (http.client.HTTPException, OSError, TimeoutError) as e:
                last_err = e
                LOG.debug(
                    "HTTP %s %s failed (attempt %d): %s",
                    method,
                    req_path,
                    attempt + 1,
                    e,
                )
                self._drop_conn()
        raise RuntimeError(
            f"HTTP {method} {req_path} failed after retries: {last_err}"
        )
