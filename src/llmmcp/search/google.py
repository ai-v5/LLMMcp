"""Google web search.

Plain HTTP clients only ever receive Google's JavaScript gate page
(``/httpservice/retry/enablejs``), so the primary path renders the results page
with a local headless browser through :attr:`SearchContext.render`. A direct
HTTP attempt is kept as an explicit, honestly-reported fallback.

Result markup notes (measured on the live site, see ``FINDINGS.md``):

* result blocks are ``div.MjjYud`` (older layouts use ``div.g``);
* the title is the ``h3`` inside the block;
* the anchor's ``href`` is ``/goto?url=<opaque>`` -- the payload is encrypted,
  so the real target is *not* recoverable from the HTML alone. The ``<cite>``
  element carries a (path-truncated) display URL, and a plain GET on the
  ``/goto`` link answers ``302`` with the exact target in ``Location``.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode, urlparse

from bs4 import BeautifulSoup

from ..models import SearchError, SearchResponse, SearchResult
from .base import SearchContext, SearchRequest

NAME = "google"
DEFAULT_HOST = "www.google.com"

SNIPPET_LIMIT = 400
RESULT_BLOCK_SELECTORS = ("div.MjjYud", "div.g")
SNIPPET_SELECTORS = ("div.VwiC3b", "div.IsZvec", "span.aCOpRe", "div.lEBKkf")

# Google-internal destinations that are never a search result.
_INTERNAL_PATHS = ("/search", "/preferences", "/advanced_search", "/setprefs", "/sorry")
# Recency filter vocabulary -> Google's ``tbs=qdr:*`` values.
_RECENCY_TBS = {
    "h": "qdr:h", "hour": "qdr:h",
    "d": "qdr:d", "day": "qdr:d", "24h": "qdr:d",
    "w": "qdr:w", "week": "qdr:w", "7d": "qdr:w",
    "m": "qdr:m", "month": "qdr:m", "30d": "qdr:m",
    "y": "qdr:y", "year": "qdr:y",
}
_CAPTCHA_MARKERS = ("unusual traffic", "recaptcha", "our systems have detected")
_GATE_MARKERS = ("enablejs", "/httpservice/retry/")


def _norm_ws(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def build_search_url(request: SearchRequest, host: str = DEFAULT_HOST) -> str:
    """Compose the Google results URL for one request."""
    params: dict[str, Any] = {"q": request.query}
    if request.max_results:
        # Google honours ``num`` loosely; ask for a little headroom, capped sanely.
        params["num"] = min(max(int(request.max_results), 10), 20)
    if request.lang:
        params["hl"] = request.lang
    if request.region:
        params["gl"] = request.region
    if request.recency:
        tbs = _RECENCY_TBS.get(str(request.recency).strip().lower())
        if tbs:
            params["tbs"] = tbs
    return f"https://{host}/search?{urlencode(params)}"


def _host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def _is_google_url(url: str) -> bool:
    host = _host_of(url)
    if not host:
        return False
    return (
        host == "google.com"
        or host.startswith("google.")
        or host.endswith(".google.com")
        or ".google." in host
    )


def _clean_url(candidate: str | None) -> str | None:
    """Normalise one candidate URL, rejecting non-navigable / internal links."""
    if not candidate:
        return None
    candidate = candidate.strip().replace("&amp;", "&")
    if not candidate or candidate.startswith(("#", "javascript:", "data:", "mailto:")):
        return None
    if candidate.startswith("//"):
        candidate = "https:" + candidate
    if candidate.startswith("/"):
        # A bare relative path: only meaningful if it is a redirector we can resolve.
        return candidate
    if not candidate.startswith(("http://", "https://")):
        return None
    path = urlparse(candidate).path or "/"
    if _is_google_url(candidate) and path.startswith(_INTERNAL_PATHS):
        return None
    return candidate


def _cite_url(block: Any) -> str | None:
    """Reconstruct a display URL from the ``<cite>`` breadcrumb (may be truncated)."""
    cite = block.select_one("cite")
    if cite is None:
        return None
    text = _norm_ws(cite.get_text(" "))
    if not text:
        return None
    if not text.startswith(("http://", "https://")):
        return None
    # "https://docs.python.org › howto › a-conceptual-..." -> ".../howto/a-conceptual-..."
    text = re.sub(r"\s*[›»]\s*", "/", text)
    text = re.sub(r"/{2,}", "/", text.replace("://", "\0")).replace("\0", "://")
    return _clean_url(text.rstrip("./"))


def _snippet_of(block: Any, title_el: Any) -> str:
    for selector in SNIPPET_SELECTORS:
        node = block.select_one(selector)
        if node is not None:
            text = _norm_ws(node.get_text(" "))
            if text:
                return text[:SNIPPET_LIMIT]
    title_text = _norm_ws(title_el.get_text(" ")) if title_el is not None else ""
    for node in block.select("span, div"):
        if node.select_one("h3") is not None or node.name == "h3":
            continue
        text = _norm_ws(node.get_text(" "))
        if text and text != title_text and len(text) > 24:
            return text[:SNIPPET_LIMIT]
    return ""


def detect_block(html: str, soup: BeautifulSoup, result_count: int) -> tuple[str, str, str] | None:
    """Return ``(code, message, hint)`` when the document is a block/gate page.

    ``captcha`` and ``enablejs`` strings occur inside the inline JavaScript of
    *successful* result pages too, so they only count as evidence when no
    result block could be parsed.
    """
    if result_count > 0:
        return None
    lowered = html.lower()
    title = _norm_ws(soup.title.get_text(" ")) if soup.title else ""
    if "/sorry/" in lowered or "our systems have detected" in lowered or "unusual traffic" in lowered:
        return (
            "GOOGLE_BLOCKED",
            "Google served a captcha / 'unusual traffic' interstitial instead of results.",
            "Retry later, slow the request rate, or switch to the bing engine.",
        )
    if "recaptcha" in lowered:
        return (
            "GOOGLE_BLOCKED",
            "Google served a reCAPTCHA challenge instead of results.",
            "Retry later or switch to the bing engine.",
        )
    if any(marker in lowered for marker in _GATE_MARKERS):
        return (
            "GOOGLE_BLOCKED",
            "Google returned its JavaScript gate page (no results without a rendered browser).",
            "Enable the headless browser so the search layer can render the page "
            "(ctx.render / LLMMCP_BROWSER), or use the bing engine.",
        )
    return (
        "GOOGLE_BLOCKED",
        f"Google page contained no parseable result blocks (title: {title or 'n/a'}).",
        "The layout may have changed, or Google served an interstitial; retry or use bing.",
    )


def parse_google_results(
    html: str,
    max_results: int = 8,
    resolve_url: Callable[[str], str | None] | None = None,
) -> list[SearchResult]:
    """Extract results from rendered Google HTML (pure, no network of its own).

    ``resolve_url`` is an optional callable turning a ``/goto?url=`` (or other
    relative) href into the real target; it is only consulted when the block
    exposes no absolute URL.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    blocks: list[Any] = []
    for selector in RESULT_BLOCK_SELECTORS:
        blocks = soup.select(selector)
        if blocks:
            break

    results: list[SearchResult] = []
    seen: set[str] = set()
    for block in blocks:
        title_el = block.select_one("h3")
        if title_el is None:
            continue
        title = _norm_ws(title_el.get_text(" "))
        if not title:
            continue

        anchor = None
        for candidate in block.select("a[href]"):
            candidate = candidate
            if candidate.select_one("h3") is not None or candidate.find_parent("h3") is not None:
                anchor = candidate
                break
            if anchor is None:
                anchor = candidate
        href = (anchor.get("href") if anchor is not None else "") or ""
        href = href.strip()

        url = _clean_url(href)
        if url is not None and url.startswith("/") and resolve_url is not None:
            url = _clean_url(resolve_url(url))
        if url is not None and url.startswith("/"):
            url = _cite_url(block) or url
        if url is None or url.startswith("/"):
            url = _cite_url(block)

        # ``javascript:``/internal-only blocks are dropped rather than kept half-formed.
        if not url or not url.startswith(("http://", "https://")):
            continue
        if url in seen:
            continue
        seen.add(url)

        results.append(
            SearchResult(
                title=title,
                url=url,
                snippet=_snippet_of(block, title_el),
                engine=NAME,
            )
        )
        if len(results) >= max_results:
            break
    return results


def _http_get_allow_redirects(ctx: SearchContext, url: str, timeout: float) -> Any:
    """Call ``ctx.http.get`` preferring a non-following request when supported."""
    getter = getattr(ctx.http, "get", None)
    if getter is None:
        raise SearchError("HTTP client does not expose get().", code="GOOGLE_NO_HTTP")
    kwargs: dict[str, Any] = {"timeout": timeout}
    try:
        if "allow_redirects" in inspect.signature(getter).parameters:
            kwargs["allow_redirects"] = False
    except (TypeError, ValueError):
        pass
    return getter(url, **kwargs)


def _response_text(resp: Any) -> str:
    if resp is None:
        return ""
    for attr in ("text", "content"):
        value = getattr(resp, attr, None)
        if isinstance(value, str):
            return value
        if isinstance(value, (bytes, bytearray)):
            try:
                return value.decode("utf-8", "replace")
            except Exception:  # pragma: no cover - defensive
                return ""
    return resp if isinstance(resp, str) else ""


def _response_header(resp: Any, name: str) -> str | None:
    headers = getattr(resp, "headers", None)
    if not headers:
        return None
    try:
        for key, value in headers.items():
            if str(key).lower() == name.lower():
                return value
    except AttributeError:  # pragma: no cover - defensive
        return None
    return None


def _make_resolver(ctx: SearchContext, host: str, timeout: float) -> Callable[[str], str | None] | None:
    """Resolve Google's opaque ``/goto?url=`` redirects to their real target."""
    if ctx.http is None:
        return None

    def resolve(href: str) -> str | None:
        absolute = href if href.startswith("http") else f"https://{host}{href}"
        try:
            resp = _http_get_allow_redirects(ctx, absolute, timeout)
        except Exception:  # network hiccup must not kill the whole search
            return None
        location = _response_header(resp, "location")
        if location:
            cleaned = _clean_url(location)
            if cleaned and cleaned.startswith(("http://", "https://")):
                return cleaned
        final = getattr(resp, "final_url", None)
        if final and not _is_google_url(final):
            return _clean_url(final)
        return None

    return resolve


class GoogleEngine:
    """Google web search via rendered results page, with an HTTP fallback."""

    name = NAME

    def __init__(self, host: str | None = None) -> None:
        self.host = host or DEFAULT_HOST

    # -- capabilities ----------------------------------------------------
    def is_available(self, ctx: SearchContext) -> bool:
        return ctx.render is not None

    def _resolve_host(self, ctx: SearchContext) -> str:
        return str(ctx.options.get("google_host") or self.host or DEFAULT_HOST)

    # -- engine contract -------------------------------------------------
    def search(self, request: SearchRequest, ctx: SearchContext) -> SearchResponse:
        host = self._resolve_host(ctx)
        url = build_search_url(request, host)

        if ctx.render is None:
            return self._search_http_fallback(request, ctx, url, host)

        try:
            html = ctx.render(url, request.timeout)
        except SearchError:
            raise
        except Exception as exc:  # renderer failure -> clear, actionable error
            raise SearchError(
                f"headless render failed for Google search: {type(exc).__name__}: {exc}",
                code="GOOGLE_RENDER_FAILED",
                hint="Check the configured browser path, the corporate proxy and available RAM.",
            ) from exc

        return self._parse_response(request, ctx, html or "", host, via="render")

    def _search_http_fallback(
        self, request: SearchRequest, ctx: SearchContext, url: str, host: str
    ) -> SearchResponse:
        """Last-resort plain HTTP attempt: report honestly when Google gates us."""
        if ctx.http is None:
            raise SearchError(
                "Google search needs a headless browser (SearchContext.render), "
                "and no HTTP client is available either.",
                code="GOOGLE_NO_RENDERER",
                hint="Enable the local headless browser (Chrome/Edge) and retry, "
                "or request the bing engine.",
            )
        try:
            resp = _http_get_allow_redirects(ctx, url, request.timeout)
        except Exception as exc:
            raise SearchError(
                f"Google HTTP fallback failed: {type(exc).__name__}: {exc}",
                code="GOOGLE_HTTP_FAILED",
                hint="Check proxy/TLS settings, or enable the headless browser.",
            ) from exc
        html = _response_text(resp)
        results = parse_google_results(html, request.max_results)
        blocked = detect_block(html, BeautifulSoup(html or "", "html.parser"), len(results))
        if blocked is not None and not results:
            code, message, hint = blocked
            raise SearchError(
                f"{message} (plain HTTP cannot render Google; SearchContext.render was None)",
                code=code,
                hint=hint,
            )
        return self._build_response(request, results, via="http")

    def _parse_response(
        self,
        request: SearchRequest,
        ctx: SearchContext,
        html: str,
        host: str,
        via: str,
    ) -> SearchResponse:
        resolver = None
        if ctx.options.get("google_resolve_urls", True):
            resolver = _make_resolver(ctx, host, min(8.0, float(request.timeout or 8.0)))
        results = parse_google_results(html, request.max_results, resolve_url=resolver)
        if not results:
            soup = BeautifulSoup(html or "", "html.parser")
            code, message, hint = detect_block(html, soup, 0)
            raise SearchError(message, code=code, hint=hint)
        return self._build_response(request, results, via=via)

    def _build_response(
        self, request: SearchRequest, results: list[SearchResult], via: str
    ) -> SearchResponse:
        return SearchResponse(
            query=request.query,
            engine=NAME,
            results=results,
            warnings=[f"google: parsed via {via}"] if via != "render" else [],
        )
