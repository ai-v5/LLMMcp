"""Bing web search via the server-rendered HTML endpoint.

Unlike Google, ``https://www.bing.com/search?q=...`` returns parseable results to
a plain HTTP client (``HTTP 200``, ~10 ``li.b_algo`` blocks) as long as the
request carries a normal browser ``User-Agent``/``Accept-Language``. On corporate
networks the TLS chain is intercepted by a MITM proxy, so the HTTP client must
use the Windows certificate store (``truststore``) -- that is the core
``HttpClient``'s job, not this engine's.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Sequence
from typing import Any
from urllib.parse import parse_qs, quote_plus, urlparse

from bs4 import BeautifulSoup

from ..models import SearchError, SearchResponse, SearchResult
from .base import SearchContext, SearchRequest

NAME = "bing"
DEFAULT_HOST = "www.bing.com"

SNIPPET_LIMIT = 400
SNIPPET_SELECTORS = (
    "div.b_caption p",
    "p.b_lineclamp2",
    "p.b_lineclamp3",
    "p.b_lineclamp4",
    "div.b_caption",
    "p.b_algoSlug",
)
# Bing's own navigation surfaces, never a search result.
_INTERNAL_PATHS = ("/search", "/account", "/preferences", "/settings", "/copilot")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
REQUEST_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-User": "?1",
    "Sec-Fetch-Dest": "document",
    "Upgrade-Insecure-Requests": "1",
}


def _norm_ws(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


_STOPWORDS = frozenset(
    {"the", "a", "an", "of", "for", "and", "or", "to", "in", "on", "is", "vs", "about"}
)

#: Minimum share of distinct query terms that must appear in the parsed results.
#: Measured on this network (see FINDINGS.md): genuine pages scored 0.67-1.00,
#: while Bing's degraded/poisoned answers scored 0.00-0.33.
RELEVANCE_MIN_COVERAGE = 0.5


def query_tokens(text: str | None) -> set[str]:
    """Lowercased alphanumeric query terms worth matching on."""
    return {
        tok
        for tok in re.findall(r"[a-z0-9]+", (text or "").lower())
        if len(tok) >= 2 and tok not in _STOPWORDS
    }


def query_coverage(query: str, results: Sequence[SearchResult]) -> float:
    """Fraction of the query's distinct terms mentioned by the results.

    Bing can answer a query with a well-formed results page that belongs to a
    *different* query (degraded / anti-automation response). Parsing such a page
    "succeeds" but the content is wrong, so this score is used to refuse it.
    """
    wanted = query_tokens(query)
    if not wanted:
        return 1.0
    blob = " ".join(
        f"{item.title} {item.url} {item.snippet}".lower() for item in results
    )
    matched = {token for token in wanted if token in blob}
    return len(matched) / len(wanted)


def _host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def unwrap_bing_url(href: str | None) -> str | None:
    """Recover the real target from Bing's click-tracker wrapper.

    Result anchors point at ``https://www.bing.com/ck/a?!&&p=<sig>&u=a1<base64url>``
    where the ``u`` parameter (``a1`` prefix stripped) base64url-encodes the real
    URL. Returning the wrapper would hand callers an opaque redirect.
    """
    if not href:
        return None
    href = href.strip().replace("&amp;", "&")
    if "/ck/a" not in href:
        return href
    try:
        params = parse_qs(urlparse(href).query)
    except ValueError:
        return href
    raw = (params.get("u") or [""])[0]
    if raw.startswith("a1"):
        payload = raw[2:]
        payload += "=" * (-len(payload) % 4)
        try:
            decoded = base64.urlsafe_b64decode(payload).decode("utf-8", "replace")
        except Exception:
            return href
        if decoded.startswith(("http://", "https://")):
            return decoded
    return href


def _cite_url(block: Any) -> str | None:
    """Display URL from ``<cite>`` (``host › path``), used when uncloaking fails."""
    cite = block.select_one("cite")
    if cite is None:
        return None
    text = _norm_ws(cite.get_text(" "))
    if not text.startswith(("http://", "https://")):
        return None
    text = re.sub(r"\s*[›»]\s*", "/", text)
    text = re.sub(r"/{2,}", "/", text.replace("://", "\0")).replace("\0", "://")
    return text.rstrip("./")


def _clean_url(candidate: str | None) -> str | None:
    if not candidate:
        return None
    candidate = candidate.strip().replace("&amp;", "&")
    if not candidate.startswith(("http://", "https://")):
        return None
    host = _host_of(candidate)
    path = urlparse(candidate).path or "/"
    if (host == "bing.com" or host.endswith(".bing.com")) and path.startswith(_INTERNAL_PATHS):
        return None
    return candidate


def build_search_url(request: SearchRequest, host: str = DEFAULT_HOST) -> str:
    params = f"q={quote_plus(request.query)}&form=QBLH"
    if request.max_results:
        params += f"&count={min(max(int(request.max_results) * 2, 10), 50)}"
    if request.lang:
        params += f"&setlang={quote_plus(str(request.lang))}"
    region = (request.region or "").strip().upper()
    if region:
        # ``mkt`` is the documented market selector; ``cc`` is the legacy one.
        params += f"&mkt={quote_plus(region + '-' + (request.lang or 'en'))}&cc={region}"
    if request.recency:
        window = _RECENCY_FILTERS.get(str(request.recency).lower(), "3")
        params += "&filters=" + quote_plus('ex1:"ez5_' + window + '"' )
    return f"https://{host}/search?{params}"


_RECENCY_FILTERS = {
    "h": "1", "hour": "1", "d": "2", "day": "2",
    "w": "3", "week": "3", "m": "4", "month": "4",
}


def parse_bing_results(html: str, max_results: int = 8) -> list[SearchResult]:
    """Extract results from a Bing results page (pure, offline)."""
    soup = BeautifulSoup(html or "", "html.parser")
    results: list[SearchResult] = []
    seen: set[str] = set()
    for block in soup.select("li.b_algo"):
        anchor = block.select_one("h2 a[href]") or block.select_one("a[href]")
        if anchor is None:
            continue
        title = _norm_ws(anchor.get_text(" "))
        url = _clean_url(unwrap_bing_url(anchor.get("href")))
        if url is None or (url and "/ck/a" in url):
            # Uncloaking failed: fall back to the visible <cite> breadcrumb.
            url = _clean_url(_cite_url(block))
        if not title or url is None or url in seen:
            continue
        snippet = ""
        for selector in SNIPPET_SELECTORS:
            node = block.select_one(selector)
            if node is not None:
                snippet = _norm_ws(node.get_text(" "))
                if snippet:
                    break
        seen.add(url)
        results.append(
            SearchResult(title=title, url=url, snippet=snippet[:SNIPPET_LIMIT], engine=NAME)
        )
        if len(results) >= max_results:
            break
    return results


def _response_text(resp: Any) -> str:
    if resp is None:
        return ""
    for attr in ("text", "content"):
        value = getattr(resp, attr, None)
        if isinstance(value, str):
            return value
        if isinstance(value, (bytes, bytearray)):
            return value.decode("utf-8", "replace")
    return resp if isinstance(resp, str) else ""


def _response_status(resp: Any) -> int | None:
    for attr in ("status", "status_code"):
        value = getattr(resp, attr, None)
        if isinstance(value, int):
            return value
    return None


class BingEngine:
    """Bing web search over plain HTTP."""

    name = NAME

    def __init__(self, host: str | None = None) -> None:
        self.host = host or DEFAULT_HOST

    def is_available(self, ctx: SearchContext) -> bool:
        return ctx.http is not None

    def search(self, request: SearchRequest, ctx: SearchContext) -> SearchResponse:
        if ctx.http is None:
            raise SearchError(
                "Bing search requires an HTTP client, but SearchContext.http was None.",
                code="BING_NO_HTTP",
                hint="Construct the search context with the configured HttpClient.",
            )
        host = str(ctx.options.get("bing_host") or self.host)
        url = build_search_url(request, host)
        try:
            resp = ctx.http.get(url, headers=dict(REQUEST_HEADERS), timeout=request.timeout)
        except Exception as exc:
            raise SearchError(
                f"Bing request failed: {type(exc).__name__}: {exc}",
                code="BING_REQUEST_FAILED",
                hint="Check the corporate proxy and TLS trust store settings, then retry.",
            ) from exc

        status = _response_status(resp)
        html = _response_text(resp)
        results = parse_bing_results(html, request.max_results)
        if not results:
            if status is not None and status >= 400:
                raise SearchError(
                    f"Bing returned HTTP {status} with no parseable results.",
                    code="BING_HTTP_ERROR",
                    hint="Bing may be rate-limiting; retry shortly or use another engine.",
                )
            raise SearchError(
                "Bing returned a page with no result blocks (li.b_algo).",
                code="BING_NO_RESULTS",
                hint="The page may be a consent/anti-bot interstitial; retry or use another engine.",
            )

        # A well-formed results page can still belong to a different query: from
        # some egress IPs Bing answers automated traffic with unrelated (or even
        # spam) results. Returning those as success would be silently wrong.
        coverage = query_coverage(request.query, results)
        if coverage < RELEVANCE_MIN_COVERAGE:
            raise SearchError(
                f"Bing returned {len(results)} results unrelated to the query "
                f"({coverage:.0%} of query terms matched): '{results[0].title}'",
                code="BING_UNRELATED",
                hint=(
                    "Bing degrades automated traffic from this network; "
                    "retry later, or use the google/gnews engine."
                ),
            )
        warnings = []
        if coverage < 1.0:
            warnings.append(
                f"bing: only {coverage:.0%} of query terms appear in the results"
            )
        return SearchResponse(
            query=request.query, engine=NAME, results=results, warnings=warnings
        )
