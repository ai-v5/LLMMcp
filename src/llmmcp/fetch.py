"""URL -> readable content.

``fetch()`` turns any URL into a :class:`~llmmcp.models.FetchResponse`: HTML is
reduced to its article content (boilerplate tags removed) and rendered as
Markdown, plain text, or raw HTML; JSON is pretty printed; text passes through.
Binary payloads fail loudly with a stable code instead of dumping bytes into an
LLM context.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Comment, Tag
from markdownify import markdownify

from .models import FetchError, FetchResponse

#: Boilerplate containers that carry navigation/chrome rather than content.
DROP_TAGS = ("script", "style", "nav", "header", "footer", "aside", "iframe", "noscript")

FORMATS = ("markdown", "text", "html")

HTML_TYPES = {"text/html", "application/xhtml+xml"}
JSON_TYPES = {"application/json"}
TEXT_TYPES = {
    "text/plain",
    "text/markdown",
    "text/csv",
    "text/xml",
    "application/xml",
    "text/x-markdown",
}

_MULTI_BLANK_RE = re.compile(r"\n{3,}")
_TRAILING_SPACE_RE = re.compile(r"[ \t]+\n")


def fetch(
    client: Any,
    url: str,
    fmt: str = "markdown",
    max_chars: int = 100000,
    timeout: float | None = None,
    include_links: bool = False,
    render: Callable[[str, float], str] | None = None,
    render_threshold: int = 200,
) -> FetchResponse:
    """Retrieve *url* and render it as ``markdown`` (default), ``text`` or ``html``.

    ``client`` is a :class:`llmmcp.http.HttpClient` (anything exposing
    ``get(url, timeout=...) -> HttpResponse`` works, which keeps this testable).

    ``render`` is an optional ``(url, timeout) -> html`` callable (the headless
    browser). Documentation sites often ship an empty shell and fill it from
    JavaScript; when the extracted HTML is shorter than ``render_threshold``
    characters the page is rendered once and re-extracted, so an SPA does not
    silently return an empty document. A render failure keeps the plain HTTP
    result instead of failing the fetch.
    """

    if fmt not in FORMATS:
        raise FetchError(
            f"unsupported format {fmt!r}",
            code="INVALID_ARGUMENT",
            hint=f"use one of: {', '.join(FORMATS)}",
        )
    if max_chars is None or int(max_chars) <= 0:
        raise FetchError("max_chars must be positive", code="INVALID_ARGUMENT")

    scheme = urlsplit(url).scheme.lower()
    if scheme not in ("http", "https"):
        raise FetchError(
            f"unsupported URL scheme {scheme or '(none)'}",
            code="UNSUPPORTED_SCHEME",
            hint="only http:// and https:// URLs can be fetched",
        )

    response = client.get(url, timeout=timeout)
    status = int(getattr(response, "status", 0))
    # A reachable server that answers 4xx/5xx is a result, not a transport
    # failure: the status and whatever body it sent are what the caller asked
    # for, and the web seam treats non-2xx the same way.

    headers = getattr(response, "headers", None) or {}
    content_type = str(headers.get("content-type", "") or "").split(";")[0].strip().lower()
    body = response.text if isinstance(response.text, str) else ""
    title: str | None = None
    truncated = bool(getattr(response, "truncated", False))

    if content_type in HTML_TYPES or content_type in ("", "text/html"):
        rendered, title = _render_markup(body, fmt, include_links)
        # The raw HTML of a JavaScript shell is long while its readable text is
        # empty, so emptiness is judged on extracted text for every format.
        probe = rendered if fmt != "html" else html_to_text(parse_html(body))
        if render is not None and len(probe.strip()) < render_threshold:
            rendered, title = _render_with_browser(
                render,
                str(getattr(response, "final_url", url) or url),
                fmt,
                include_links,
                # Leave headroom under the caller's own budget so a slow render
                # returns the plain result instead of tripping the outer timeout.
                max(5.0, float(timeout or 30.0) - 4.0),
                rendered=rendered,
                title=title,
            )
    elif content_type in JSON_TYPES or content_type.endswith("+json"):
        rendered = pretty_json(body)
    elif content_type in TEXT_TYPES or content_type.startswith("text/"):
        rendered = body
    else:
        raise FetchError(
            f"cannot represent {content_type or 'unknown content type'} from {url}",
            code="UNSUPPORTED_CONTENT_TYPE",
            hint="llmmcp fetches HTML, JSON and plain text only (not images, archives or binaries)",
        )

    text, char_truncated = truncate(rendered or "", int(max_chars))
    content_bytes = getattr(response, "content", b"") or b""

    return FetchResponse(
        url=url,
        final_url=str(getattr(response, "final_url", url) or url),
        status=status,
        content_type=content_type,
        title=title or (extract_title(parse_html(body)) if content_type in HTML_TYPES else None),
        content=text,
        truncated=truncated or char_truncated,
        bytes_read=len(content_bytes),
        elapsed_ms=int(getattr(response, "elapsed_ms", 0) or 0),
    )


# ---------------------------------------------------------------------------
# transforms
# ---------------------------------------------------------------------------
def _render_markup(html: str, fmt: str, include_links: bool) -> tuple[str, str | None]:
    """Convert one HTML document into the requested format and its title."""

    if fmt == "html":
        return html, extract_title(parse_html(html))
    soup = parse_html(html)
    title = extract_title(soup)
    rendered = (
        html_to_markdown(soup, include_links=include_links)
        if fmt == "markdown"
        else html_to_text(soup)
    )
    return rendered, title


def _render_with_browser(
    render: Callable[[str, float], str],
    url: str,
    fmt: str,
    include_links: bool,
    timeout: float,
    *,
    rendered: str,
    title: str | None,
) -> tuple[str, str | None]:
    """Re-extract a JavaScript shell through the headless browser.

    A page whose plain HTML carries almost no text is usually an SPA shell; the
    browser is asked once for the settled DOM. Anything that still yields less
    text than the plain result is discarded, so this can only improve output.
    """

    try:
        dom = render(url, timeout)
    except Exception:  # noqa: BLE001 - a render failure must not fail the fetch
        return rendered, title
    if not isinstance(dom, str) or not dom.strip():
        return rendered, title
    rendered_dom, rendered_title = _render_markup(dom, fmt, include_links)
    # Compare readable text, not markup: for the html format the shell's inline
    # scripts make the raw length meaningless.
    if _readable_length(rendered_dom, fmt) <= _readable_length(rendered, fmt):
        return rendered, title
    return rendered_dom, rendered_title or title


def _readable_length(markup: str, fmt: str) -> int:
    """Length of the text a reader would see, whatever the output format is."""

    if fmt != "html":
        return len(markup.strip())
    return len(html_to_text(parse_html(markup)).strip())


def parse_html(html: str) -> BeautifulSoup:
    """Parse HTML with the stdlib parser (no extra dependency)."""

    return BeautifulSoup(html or "", "html.parser")


def extract_title(soup: BeautifulSoup) -> str | None:
    node = soup.find("title")
    if node is None:
        return None
    text = " ".join(node.get_text(" ", strip=True).split())
    return text or None


def clean_soup(soup: BeautifulSoup) -> BeautifulSoup:
    """Remove comments and boilerplate containers in place."""

    for comment in soup.find_all(string=lambda value: isinstance(value, Comment)):
        comment.extract()
    for tag_name in DROP_TAGS:
        for node in soup.find_all(tag_name):
            node.decompose()
    return soup


def _content_root(soup: BeautifulSoup) -> Tag:
    """Prefer ``<main>``/``<article>``/``<body>`` over the whole document."""

    for candidate in ("main", "article"):
        node = soup.find(candidate)
        if node is not None:
            return node
    return soup.body or soup


def html_to_markdown(
    soup: BeautifulSoup,
    *,
    include_links: bool = False,
    heading_style: str = "ATX",
) -> str:
    """Render the article body of *soup* as Markdown."""

    clean_soup(soup)
    root = _content_root(soup)
    if not include_links:
        for anchor in root.find_all("a"):
            anchor.unwrap()
    markdown = markdownify(str(root), heading_style=heading_style, strip=list(DROP_TAGS))
    return normalise_whitespace(markdown)


def html_to_text(soup: BeautifulSoup) -> str:
    """Render the article body of *soup* as plain text."""

    clean_soup(soup)
    root = _content_root(soup)
    return normalise_whitespace(root.get_text("\n", strip=True))


def normalise_whitespace(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _TRAILING_SPACE_RE.sub("\n", text)
    text = _MULTI_BLANK_RE.sub("\n\n", text)
    return text.strip()


def pretty_json(body: str) -> str:
    """Pretty print JSON; return the payload untouched when it is not JSON."""

    try:
        return json.dumps(json.loads(body), indent=2, ensure_ascii=False)
    except (ValueError, TypeError):
        return body


def truncate(text: str, max_chars: int) -> tuple[str, bool]:
    """Cut *text* to *max_chars*, reporting whether anything was dropped."""

    if max_chars <= 0 or len(text) <= max_chars:
        return text, False
    return text[:max_chars], True
