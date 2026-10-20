"""Shared result and error contracts for LLMMcp.

Every search engine, the fetch pipeline, and the MCP tool layer speak these
types, which is what keeps ``llmmcp.search`` independent of the server and the
transport.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class LlmmcpError(RuntimeError):
    """Base error carrying a stable machine-readable ``code`` and an optional hint."""

    code = "LLMMCP_ERROR"

    def __init__(self, message: str, *, code: str | None = None, hint: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code
        self.hint = hint

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"code": self.code, "message": str(self)}
        if self.hint:
            data["hint"] = self.hint
        return data


class SearchError(LlmmcpError):
    """A search engine failed or was blocked."""

    code = "SEARCH_FAILED"


class FetchError(LlmmcpError):
    """A URL could not be retrieved or represented."""

    code = "FETCH_FAILED"


@dataclass(frozen=True)
class SearchResult:
    """One citeable search hit."""

    title: str
    url: str
    snippet: str = ""
    published_at: str | None = None
    engine: str = ""

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "engine": self.engine,
        }
        if self.published_at:
            data["published_at"] = self.published_at
        return data


@dataclass(frozen=True)
class SearchResponse:
    """Outcome of one search request after engine fallback."""

    query: str
    engine: str
    results: list[SearchResult] = field(default_factory=list)
    answer: str | None = None
    warnings: list[str] = field(default_factory=list)
    elapsed_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "query": self.query,
            "engine": self.engine,
            "results": [item.to_dict() for item in self.results],
            "elapsed_ms": self.elapsed_ms,
        }
        if self.answer:
            data["answer"] = self.answer
        if self.warnings:
            data["warnings"] = list(self.warnings)
        return data


@dataclass(frozen=True)
class FetchResponse:
    """Outcome of retrieving one URL."""

    url: str
    final_url: str
    status: int
    content_type: str
    title: str | None
    content: str
    truncated: bool = False
    bytes_read: int = 0
    elapsed_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "final_url": self.final_url,
            "status": self.status,
            "content_type": self.content_type,
            "title": self.title,
            "content": self.content,
            "truncated": self.truncated,
            "bytes_read": self.bytes_read,
            "elapsed_ms": self.elapsed_ms,
        }
