"""The only module that opens sockets.

:class:`HttpClient` wraps ``requests.Session`` and owns the three things every
caller would otherwise get wrong: corporate proxy injection (with ``NO_PROXY``),
TLS verification through the Windows certificate store (``truststore``), and
bounded reads (``max_bytes``) with retries on transient failures.

Everything it raises is a :class:`~llmmcp.models.LlmmcpError` subclass carrying a
stable ``code``. Proxy URLs may contain credentials, so the only representation
this module hands out for humans is :meth:`HttpClient.proxy_label`.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import Any
from urllib.parse import urlsplit

import requests
from requests.utils import should_bypass_proxies

from .models import LlmmcpError
from .settings import Settings, load_settings

#: Status codes worth retrying: rate limiting and server-side hiccups.
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

_CHARSET_RE = re.compile(r"charset\s*=\s*[\"']?([\w\-:.]+)", re.IGNORECASE)
_META_CHARSET_RE = re.compile(rb"<meta[^>]+charset\s*=\s*[\"']?([\w\-:.]+)", re.IGNORECASE)
#: Dotted-quad IPv4 literal, used to tell a loopback address from a bare hostname.
_IPV4 = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")

_TRUSTSTORE_APPLIED = False


class HttpError(LlmmcpError):
    """A request could not be completed (network, proxy, TLS, encoding)."""

    code = "HTTP_ERROR"


@dataclass(frozen=True)
class HttpResponse:
    """A fully read (and possibly truncated) HTTP response."""

    status: int
    url: str
    final_url: str
    headers: Mapping[str, str] = field(default_factory=dict)
    content: bytes = b""
    text: str = ""
    truncated: bool = False
    elapsed_ms: int = 0
    encoding: str | None = None

    @property
    def status_code(self) -> int:
        """Alias matching ``requests.Response.status_code``."""

        return self.status

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 400

    def header(self, name: str, default: str | None = None) -> str | None:
        return self.headers.get(name.lower(), default)


class HttpClient:
    """Proxy- and certificate-aware ``requests`` wrapper with bounded reads."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings or load_settings()
        self._owns_session = session is None
        self.session = session if session is not None else requests.Session()
        self._sleep = sleep
        self.tls_active = "default"
        self._configure()

    # -- configuration ---------------------------------------------------
    def _configure(self) -> None:
        session = self.session
        # Own the environment completely: proxies are resolved once by
        # ``llmmcp.settings`` so the effective proxy is predictable and printable.
        session.trust_env = False
        session.headers.update(
            {
                "User-Agent": self.settings.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
            }
        )
        # The proxy is chosen per request (`proxies_for`), never on the session:
        # a session-level proxy would also apply to a host `NO_PROXY` excludes,
        # because `requests` merges an empty per-call mapping with the session one.
        self._configure_tls()

    def _configure_tls(self) -> None:
        global _TRUSTSTORE_APPLIED
        mode = self.settings.tls
        if mode == "off":
            self.session.verify = False
            try:
                import urllib3

                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            except Exception:  # pragma: no cover - urllib3 always present with requests
                pass
            self.tls_active = "off"
            return
        if mode == "truststore":
            try:
                import truststore

                if not _TRUSTSTORE_APPLIED:
                    truststore.inject_into_ssl()
                    _TRUSTSTORE_APPLIED = True
                self.tls_active = "truststore"
                return
            except Exception:  # pragma: no cover - truststore is a hard dependency
                # Fall through to the bundled CA bundle rather than failing startup.
                pass
        self.session.verify = requests.certs.where()
        self.tls_active = "certifi"

    # -- proxy helpers ---------------------------------------------------
    #: Per-request mapping that disables any proxy, including a session default.
    #: `requests` merges an empty mapping with `session.proxies`, so bypassing
    #: needs explicit `None` values rather than `{}`.
    NO_PROXY: dict[str, None] = {"http": None, "https": None}

    def proxies_for(self, url: str) -> dict[str, str | None]:
        """Proxy mapping to use for *url*, honouring ``NO_PROXY``.

        Windows Internet Settings express bypasses as shell-style wildcards
        (``127.*``, ``10.*``, ``*.corp.example``) plus ``<local>``, which
        ``requests`` does not understand, so those patterns are matched here
        before its own suffix/CIDR check. Without this a loopback or intranet URL
        is sent to the proxy and typically comes back as an access-denied page.
        """

        proxy = self.settings.proxy
        if not proxy.enabled:
            return dict(self.NO_PROXY)
        if self._bypasses_no_proxy(url):
            return dict(self.NO_PROXY)
        try:
            if should_bypass_proxies(url, proxy.no_proxy):
                return dict(self.NO_PROXY)
        except Exception:  # pragma: no cover - malformed URL falls through to using the proxy
            return dict(proxy.proxies)
        return dict(proxy.proxies)

    def _bypasses_no_proxy(self, url: str) -> bool:
        """True when a shell-style ``NO_PROXY`` pattern covers *url*'s host."""

        patterns = [item.strip().lower() for item in (self.settings.proxy.no_proxy or "").split(",")]
        host = (urlsplit(url).hostname or "").lower()
        if not host:
            return False
        for pattern in patterns:
            if not pattern:
                continue
            if pattern == "<local>":
                if "." not in host and not _IPV4.fullmatch(host):
                    return True
                continue
            candidate = pattern.lstrip(".")
            if fnmatchcase(host, pattern) or fnmatchcase(host, candidate):
                return True
            if pattern.startswith("*.") and host.endswith(pattern[1:]):
                return True
        return False

    def proxy_label(self) -> str | None:
        """Credential-free ``scheme://host:port (source=...)`` description."""

        return self.settings.proxy.label()

    # -- requests --------------------------------------------------------
    def get(
        self,
        url: str,
        *,
        timeout: float | None = None,
        max_bytes: int | None = None,
        headers: Mapping[str, str] | None = None,
        allow_redirects: bool = True,
    ) -> HttpResponse:
        """GET *url*, retrying transient failures and truncating huge bodies."""

        timeout = float(self.settings.timeout if timeout is None else timeout)
        if timeout <= 0:
            raise HttpError("timeout must be positive", code="INVALID_ARGUMENT")
        budget = int(self.settings.max_bytes if max_bytes is None else max_bytes)
        if budget <= 0:
            raise HttpError("max_bytes must be positive", code="INVALID_ARGUMENT")

        attempts = max(1, int(self.settings.retries) + 1)
        proxies = self.proxies_for(url)
        merged_headers = dict(headers or {})

        last_error: HttpError | None = None
        for attempt in range(attempts):
            started = time.perf_counter()
            try:
                response = self.session.get(
                    url,
                    timeout=timeout,
                    headers=merged_headers or None,
                    stream=True,
                    proxies=proxies,
                    allow_redirects=allow_redirects,
                )
            except requests.exceptions.RequestException as exc:
                error = _map_exception(exc, url)
                if attempt + 1 < attempts and _is_transient(exc):
                    last_error = error
                    self._backoff(attempt)
                    continue
                raise error from exc

            try:
                if response.status_code in RETRYABLE_STATUS and attempt + 1 < attempts:
                    last_error = HttpError(
                        f"HTTP {response.status_code} from {url}",
                        code="HTTP_STATUS",
                        hint="the server asked to retry later",
                    )
                    response.close()
                    self._backoff(attempt)
                    continue
                content, truncated = _read_limited(response, budget)
            except requests.exceptions.RequestException as exc:
                response.close()
                error = _map_exception(exc, url)
                if attempt + 1 < attempts and _is_transient(exc):
                    last_error = error
                    self._backoff(attempt)
                    continue
                raise error from exc
            finally:
                response.close()

            elapsed_ms = int((time.perf_counter() - started) * 1000)
            headers_out = {str(key).lower(): str(value) for key, value in response.headers.items()}
            encoding = detect_encoding(headers_out, content)
            return HttpResponse(
                status=int(response.status_code),
                url=url,
                final_url=str(response.url or url),
                headers=headers_out,
                content=content,
                text=content.decode(encoding, errors="replace"),
                truncated=truncated,
                elapsed_ms=elapsed_ms,
                encoding=encoding,
            )

        # Every attempt was retried: report the last failure rather than a generic one.
        assert last_error is not None  # noqa: S101 - loop invariant
        raise last_error

    def _backoff(self, attempt: int) -> None:
        delay = float(self.settings.backoff) * (2**attempt)
        if delay > 0:
            self._sleep(delay)

    # -- lifecycle -------------------------------------------------------
    def close(self) -> None:
        if self._owns_session:
            self.session.close()

    def __enter__(self) -> HttpClient:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _read_limited(response: requests.Response, max_bytes: int) -> tuple[bytes, bool]:
    """Stream at most *max_bytes* of body, reporting whether more was available."""

    chunks: list[bytes] = []
    total = 0
    truncated = False
    iterator = response.iter_content(chunk_size=65536)
    for chunk in iterator:
        if not chunk:
            continue
        remaining = max_bytes - total
        if len(chunk) > remaining:
            chunks.append(chunk[:remaining])
            truncated = True
            break
        chunks.append(chunk)
        total += len(chunk)
        if total == max_bytes:
            # Exactly at the budget: peek the same stream once more to learn
            # whether this is the real end or simply a cut-off.
            try:
                for extra in iterator:
                    if extra:
                        truncated = True
                        break
            except requests.exceptions.RequestException:
                truncated = False
            break
    return b"".join(chunks), truncated


def detect_encoding(headers: Mapping[str, str], content: bytes) -> str:
    """Pick the response encoding: header charset -> meta charset -> detector."""

    content_type = str(headers.get("content-type", ""))
    match = _CHARSET_RE.search(content_type)
    if match:
        return _normalise_encoding(match.group(1))

    if content:
        meta = _META_CHARSET_RE.search(content[:4096])
        if meta:
            return _normalise_encoding(meta.group(1).decode("ascii", "ignore"))

        # Valid UTF-8 is the strongest signal available; a statistical detector
        # would happily answer "ascii" or "cp1252" for the very same bytes.
        try:
            content.decode("utf-8")
            return "utf-8"
        except UnicodeDecodeError:
            pass

        try:
            from charset_normalizer import from_bytes

            best = from_bytes(content).best()
            if best is not None and best.encoding:
                return _normalise_encoding(best.encoding)
        except Exception:  # pragma: no cover - optional dependency path
            pass
    return "utf-8"


def _normalise_encoding(name: str) -> str:
    value = (name or "").strip().lower().replace("_", "-")
    if value in ("", "none"):
        return "utf-8"
    # ``utf8``/``latin1``/``ascii`` are accepted by codecs, but map the common
    # aliases so callers see a canonical name (ASCII is a UTF-8 subset).
    return {"utf8": "utf-8", "latin1": "latin-1", "gb2312": "gb18030", "ascii": "utf-8"}.get(value, value)


def _is_transient(exc: requests.exceptions.RequestException) -> bool:
    return isinstance(
        exc,
        (
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            requests.exceptions.ChunkedEncodingError,
            requests.exceptions.ContentDecodingError,
        ),
    )


def _map_exception(exc: requests.exceptions.RequestException, url: str) -> HttpError:
    """Translate a requests exception into an :class:`HttpError` with a stable code."""

    if isinstance(exc, requests.exceptions.SSLError):
        return HttpError(
            f"TLS verification failed for {url}: {exc}",
            code="TLS_ERROR",
            hint="set LLMMCP_TLS=truststore (Windows store) or LLMMCP_TLS=off for a trusted MITM proxy",
        )
    if isinstance(exc, requests.exceptions.ProxyError):
        return HttpError(
            f"proxy error for {url}: {exc}",
            code="PROXY_ERROR",
            hint="check the resolved proxy (llmmcp diagnose) and that it allows this host",
        )
    if isinstance(exc, requests.exceptions.Timeout):
        return HttpError(f"timed out fetching {url}: {exc}", code="TIMEOUT", hint="raise --timeout")
    if isinstance(exc, requests.exceptions.TooManyRedirects):
        return HttpError(f"too many redirects for {url}", code="TOO_MANY_REDIRECTS")
    if isinstance(exc, requests.exceptions.MissingSchema):
        return HttpError(f"not an absolute URL: {url}", code="INVALID_URL")
    if isinstance(exc, requests.exceptions.InvalidSchema):
        return HttpError(f"unsupported URL scheme: {url}", code="UNSUPPORTED_SCHEME")
    if isinstance(exc, requests.exceptions.ConnectionError):
        return HttpError(
            f"connection failed for {url}: {exc}",
            code="CONNECTION_ERROR",
            hint="no direct egress here: configure a proxy (LLMMCP_PROXY or the Windows proxy settings)",
        )
    return HttpError(f"request failed for {url}: {exc}", code="HTTP_ERROR")
