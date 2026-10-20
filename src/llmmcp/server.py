"""MCP server: the three tools an MCP client (or agent) actually sees.

``web_search``  search the web through the engine fallback chain
``web_fetch``   turn a URL into markdown/text/html
``web_diagnose`` explain proxy, TLS, browser and engine state

The installed ``mcp`` SDK is 2.x, where the high-level server was renamed from
``mcp.server.fastmcp.FastMCP`` to ``mcp.server.mcpserver.MCPServer``; the tool
decorator (``@server.tool(description=...)``) and ``server.run("stdio")`` are the
same shape as before.

Failures are reported as MCP tool errors whose text is a JSON object with
``code``/``message``/``hint``: the client sees why the call failed instead of a
generic crash.
"""

from __future__ import annotations

import json
import platform
import sys
from collections.abc import Callable
from functools import partial
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent

from . import __version__
from .browser import browser_info, find_browser, render
from .fetch import fetch
from .http import HttpClient
from .models import LlmmcpError, SearchError, SearchResponse
from .search.base import SearchContext, SearchRequest
from .settings import Settings, load_settings

#: Every tool returns ``CallToolResult`` (text for the model + structured JSON);
#: the ``Annotated`` arm publishes an object ``outputSchema`` on the wire.
StructuredTool = Annotated[CallToolResult, dict[str, Any]]


class Runtime:
    """Lazily created collaborators shared by all tools of one server process."""

    def __init__(self, settings: Settings | None = None, client: Any | None = None) -> None:
        self.settings = settings or load_settings()
        self._client = client
        self._browser_checked = False
        self._browser_path: str | None = None

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = HttpClient(self.settings)
        return self._client

    def browser_path(self) -> str | None:
        if not self._browser_checked:
            self._browser_path = find_browser(self.settings)
            self._browser_checked = True
        return self._browser_path

    def context(self) -> SearchContext:
        renderer: Callable[[str, float], str] | None = None
        if self.browser_path():
            renderer = partial(render, settings=self.settings)
        return SearchContext(
            http=self.client,
            render=renderer,
            options={
                "google_host": self.settings.google_host,
                "browser_path": self.browser_path(),
                "engine_chain": list(self.settings.engine_chain),
            },
        )

    def close(self) -> None:
        close = getattr(self._client, "close", None)
        if callable(close):
            close()


def _require_chain() -> Any:
    """Import the fallback chain lazily so a missing search layer is a clear error."""

    try:
        from .search import chain  # noqa: PLC0415 - optional collaborator, imported on demand
    except ImportError as exc:  # pragma: no cover - depends on the search package
        raise SearchError(
            "the search engine layer is not installed (llmmcp.search.chain is missing)",
            code="SEARCH_UNAVAILABLE",
            hint="install the llmmcp search engines; web_fetch and web_diagnose still work",
        ) from exc
    return chain


def _tool_error(exc: LlmmcpError) -> ToolError:
    payload = {"error": exc.to_dict()}
    return ToolError(json.dumps(payload, ensure_ascii=False))


def format_search_response(response: SearchResponse) -> str:
    """Human/model readable rendering of a :class:`SearchResponse`."""

    lines: list[str] = []
    lines.append(f"query: {response.query}")
    if response.results:
        for index, item in enumerate(response.results, start=1):
            lines.append(f"{index}. {item.title}")
            lines.append(f"   {item.url}")
            if item.snippet:
                lines.append(f"   {item.snippet}")
            if item.published_at:
                lines.append(f"   published: {item.published_at}")
    else:
        lines.append("(no results)")
    if response.answer:
        lines.append("")
        lines.append(f"answer: {response.answer}")
    lines.append("")
    lines.append(
        f"engine: {response.engine} | results: {len(response.results)} | elapsed: {response.elapsed_ms} ms"
    )
    if response.warnings:
        lines.append("warnings:")
        for warning in response.warnings:
            lines.append(f"- {warning}")
    return "\n".join(lines)


def _text(content: str, structured: dict[str, Any]) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=content)], structured_content=structured)


def build_server(settings: Settings | None = None, client: Any | None = None) -> MCPServer:
    """Create the MCP server with the three tools registered."""

    runtime = Runtime(settings=settings, client=client)
    server = MCPServer(
        "llmmcp",
        version=__version__,
        instructions=(
            "LLMMcp exposes web_search (engine fallback chain), web_fetch (URL to "
            "markdown/text/html) and web_diagnose (proxy/TLS/browser state)."
        ),
    )

    @server.tool(
        description=(
            "Search the web. Returns a numbered list of results (title, URL, snippet) "
            "plus structured results. engine='auto' walks the configured fallback chain."
        )
    )
    def web_search(
        query: str,
        max_results: int = 8,
        engine: str = "auto",
        lang: str | None = None,
        region: str | None = None,
        recency: str | None = None,
        timeout: float = 60.0,
    ) -> StructuredTool:
        if not query or not query.strip():
            raise ToolError(
                json.dumps({"error": {"code": "INVALID_ARGUMENT", "message": "query must not be empty"}})
            )
        request = SearchRequest(
            query=query.strip(),
            max_results=max(1, int(max_results)),
            lang=lang,
            region=region,
            recency=recency,
            timeout=float(timeout),
        )
        try:
            chain = _require_chain()
            response = chain.search(
                runtime.context(),
                request,
                engine=engine,
                chain=list(runtime.settings.engine_chain),
            )
        except LlmmcpError as exc:
            raise _tool_error(exc) from exc
        return _text(format_search_response(response), response.to_dict())

    @server.tool(
        description=(
            "Fetch a URL and return its readable content. format is markdown (default), "
            "text or html; JSON is pretty printed; binaries are rejected."
        )
    )
    def web_fetch(
        url: str,
        format: str = "markdown",
        max_chars: int = 100000,
        timeout: float = 30.0,
        include_links: bool = False,
    ) -> StructuredTool:
        try:
            renderer = partial(render, settings=runtime.settings) if runtime.browser_path() else None
            response = fetch(
                runtime.client,
                url,
                fmt=format,
                max_chars=int(max_chars),
                timeout=float(timeout),
                include_links=bool(include_links),
                render=renderer,
            )
        except LlmmcpError as exc:
            raise _tool_error(exc) from exc
        text = response.content
        if not 200 <= int(response.status) < 300:
            # A non-2xx answer is still a result; say so before the body so the
            # model never mistakes an error page for page content.
            text = f"HTTP {response.status} - {response.final_url}\n\n{text}"
        return _text(text, response.to_dict())

    @server.tool(
        description=(
            "Diagnose the local setup: resolved proxy (credentials redacted), TLS mode, "
            "browser availability, search engines and an optional live connectivity probe."
        )
    )
    def web_diagnose(probe: bool = True) -> StructuredTool:
        report = diagnose(runtime, probe=probe)
        return _text(json.dumps(report, indent=2, ensure_ascii=False), report)

    return server


# ---------------------------------------------------------------------------
# diagnose
# ---------------------------------------------------------------------------
def diagnose(runtime: Runtime, *, probe: bool = True) -> dict[str, Any]:
    """Collect credential-free environment facts, optionally probing the network."""

    settings = runtime.settings
    report: dict[str, Any] = {
        "llmmcp_version": __version__,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "settings": settings.public_dict(),
        "http": {
            "tls_active": getattr(runtime.client, "tls_active", settings.tls),
            "proxy_label": runtime.client.proxy_label() if hasattr(runtime.client, "proxy_label") else None,
            "proxy_bypassed_for_probe": None,
        },
        "browser": browser_info(settings),
        "engines": _engine_report(runtime),
    }
    probe_url = settings.probe_url
    if probe and settings.diagnose_probe:
        report["http"]["proxy_bypassed_for_probe"] = (
            not runtime.client.proxies_for(probe_url) if hasattr(runtime.client, "proxies_for") else None
        )
        report["probe"] = _probe(runtime.client, probe_url, settings)
    else:
        report["probe"] = {"enabled": False}
    return report


def _engine_report(runtime: Runtime) -> dict[str, Any]:
    configured = list(runtime.settings.engine_chain)
    info: dict[str, Any] = {"configured": configured}
    try:
        chain = _require_chain()
        available = chain.available_engines(runtime.context())
        names = [item if isinstance(item, str) else getattr(item, "name", str(item)) for item in available]
        # Resolve aliases (``google_news`` -> ``gnews``) so "missing" reflects
        # reality rather than the spelling used in the settings.
        resolver = getattr(chain, "get_engine", None)
        resolved: list[str] = []
        for name in configured:
            canonical = str(name)
            if callable(resolver):
                try:
                    canonical = str(getattr(resolver(name), "name", name))
                except LlmmcpError:
                    canonical = str(name)
            resolved.append(canonical)
        info["available"] = names
        info["resolved"] = resolved
        info["missing"] = [
            name for name, canonical in zip(configured, resolved, strict=True) if canonical not in names
        ]
    except LlmmcpError as exc:
        info["available"] = []
        info["error"] = exc.to_dict()
    except Exception as exc:  # pragma: no cover - a broken engine must not break diagnose
        info["available"] = []
        info["error"] = {"code": "ENGINE_ENUMERATION_FAILED", "message": str(exc)}
    return info


def _probe(client: Any, url: str, settings: Settings) -> dict[str, Any]:
    try:
        response = client.get(url, timeout=min(15.0, settings.timeout), max_bytes=64 * 1024)
    except LlmmcpError as exc:
        return {"enabled": True, "url": url, "ok": False, "error": exc.to_dict()}
    except Exception as exc:  # pragma: no cover - unexpected transport failure
        return {
            "enabled": True,
            "url": url,
            "ok": False,
            "error": {"code": "PROBE_FAILED", "message": str(exc)},
        }
    return {
        "enabled": True,
        "url": url,
        "ok": 200 <= int(response.status) < 400,
        "status": int(response.status),
        "final_url": response.final_url,
        "bytes": len(response.content),
        "elapsed_ms": int(response.elapsed_ms),
    }


__all__ = ["Runtime", "build_server", "diagnose", "format_search_response"]
