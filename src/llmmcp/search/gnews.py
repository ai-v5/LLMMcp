"""Google News search over the public RSS feed.

``https://news.google.com/rss/search?q=...`` answers plain HTTP with XML and is
not gated the way ``google.com/search`` is, which makes it a dependable fallback
for time-sensitive queries.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote_plus

from ..models import SearchError, SearchResponse, SearchResult
from .base import SearchContext, SearchRequest

NAME = "gnews"
DEFAULT_HOST = "news.google.com"
SNIPPET_LIMIT = 400

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)


def _norm_ws(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def strip_html(fragment: str | None) -> str:
    """Reduce an RSS ``<description>`` fragment to readable plain text."""
    if not fragment:
        return ""
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", fragment)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    for entity, char in (
        ("&nbsp;", " "),
        ("&amp;", "&"),
        ("&lt;", "<"),
        ("&gt;", ">"),
        ("&quot;", '"'),
        ("&#39;", "'"),
        ("&apos;", "'"),
    ):
        text = text.replace(entity, char)
    return _norm_ws(text)


def to_iso8601(value: str | None) -> str | None:
    """Convert an RSS ``pubDate`` into an ISO-8601 UTC timestamp."""
    raw = _norm_ws(value)
    if not raw:
        return None
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        return None
    if parsed is None:  # pragma: no cover - defensive
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def build_feed_url(request: SearchRequest, host: str = DEFAULT_HOST) -> str:
    lang = (request.lang or "en-US").replace("_", "-")
    region = (request.region or lang.split("-")[-1] or "US").upper()
    hl = lang
    gl = region
    ceid = f"{region}:{lang.split('-')[0]}"
    params = [f"q={quote_plus(request.query)}", f"hl={hl}", f"gl={gl}", f"ceid={ceid}"]
    if request.recency:
        window = str(request.recency).strip().lower()
        delta = {"h": "1h", "hour": "1h", "d": "1d", "day": "1d", "w": "7d", "week": "7d",
                 "m": "30d", "month": "30d", "y": "1y", "year": "1y"}.get(window)
        if delta:
            params.append(f"when={delta}")
    return f"https://{host}/rss/search?" + "&".join(params)


def parse_feed(xml_text: str, max_results: int = 8) -> list[SearchResult]:
    """Parse a Google News RSS document (pure, offline)."""
    if not xml_text or not xml_text.strip():
        raise SearchError(
            "Google News returned an empty feed.",
            code="GNEWS_EMPTY",
            hint="Retry, or use another engine.",
        )
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise SearchError(
            f"Google News returned malformed XML: {exc}",
            code="GNEWS_BAD_XML",
            hint="The proxy may have injected an HTML error page; check proxy/TLS settings.",
        ) from exc

    results: list[SearchResult] = []
    seen: set[str] = set()
    for item in root.iter("item"):
        title = _norm_ws(item.findtext("title"))
        link = _norm_ws(item.findtext("link"))
        if not title or not link.startswith(("http://", "https://")) or link in seen:
            continue
        seen.add(link)
        results.append(
            SearchResult(
                title=title,
                url=link,
                snippet=strip_html(item.findtext("description"))[:SNIPPET_LIMIT],
                published_at=to_iso8601(item.findtext("pubDate")),
                engine=NAME,
            )
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


class GoogleNewsEngine:
    """Google News RSS search."""

    name = NAME

    def __init__(self, host: str | None = None) -> None:
        self.host = host or DEFAULT_HOST

    def is_available(self, ctx: SearchContext) -> bool:
        return ctx.http is not None

    def search(self, request: SearchRequest, ctx: SearchContext) -> SearchResponse:
        if ctx.http is None:
            raise SearchError(
                "Google News search requires an HTTP client, but SearchContext.http was None.",
                code="GNEWS_NO_HTTP",
                hint="Construct the search context with the configured HttpClient.",
            )
        host = str(ctx.options.get("gnews_host") or self.host)
        url = build_feed_url(request, host)
        try:
            resp = ctx.http.get(url, headers={"User-Agent": USER_AGENT}, timeout=request.timeout)
        except Exception as exc:
            raise SearchError(
                f"Google News request failed: {type(exc).__name__}: {exc}",
                code="GNEWS_REQUEST_FAILED",
                hint="Check the corporate proxy and TLS trust store settings, then retry.",
            ) from exc

        results = parse_feed(_response_text(resp), request.max_results)
        if not results:
            raise SearchError(
                "Google News feed contained no usable items.",
                code="GNEWS_NO_RESULTS",
                hint="Broaden the query or use another engine.",
            )
        return SearchResponse(query=request.query, engine=NAME, results=results)
