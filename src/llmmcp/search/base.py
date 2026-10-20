"""Search engine seam: the request/context vocabulary every engine implements.

Engines are deliberately ignorant of MCP, HTTP clients and configuration
loading: the runtime hands them a :class:`SearchContext` (an HTTP client and an
optional headless-browser renderer) and they return a normalised
:class:`~llmmcp.models.SearchResponse`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, runtime_checkable

from ..models import SearchResponse


@dataclass(frozen=True)
class SearchRequest:
    """One normalised search request."""

    query: str
    max_results: int = 8
    lang: str | None = None
    region: str | None = None
    recency: str | None = None
    timeout: float = 25.0


@dataclass(frozen=True)
class SearchContext:
    """Collaborators an engine may use.

    ``http`` is the configured :class:`llmmcp.http.HttpClient` (proxy/truststore
    aware). ``render`` renders a URL in a local headless browser and returns the
    settled DOM as HTML; it is ``None`` when no browser is configured, and
    engines that need it must degrade with a clear error instead of silently
    returning nothing. ``options`` carries engine-specific settings (for example
    the Google host) so engines stay configuration-free.
    """

    http: Any
    render: Callable[[str, float], str] | None = None
    options: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class SearchEngine(Protocol):
    """Minimal engine contract."""

    name: str

    def search(self, request: SearchRequest, ctx: SearchContext) -> SearchResponse:
        """Run one search or raise :class:`~llmmcp.models.SearchError`."""
