"""Offline tests for the MCP tool layer (registration, formatting, errors)."""

from __future__ import annotations

import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError

import llmmcp.server as server_module
from llmmcp.http import HttpResponse
from llmmcp.models import FetchError, SearchError, SearchResponse, SearchResult
from llmmcp.server import build_server, diagnose, format_search_response
from llmmcp.settings import ProxySettings, Settings

TOOL_NAMES = {"web_search", "web_fetch", "web_diagnose"}


class FakeClient:
    """Stands in for :class:`llmmcp.http.HttpClient` (no sockets)."""

    tls_active = "truststore"

    def __init__(
        self,
        *,
        status: int = 200,
        body: bytes = b"<html><head><title>Example</title></head><body><p>example body</p></body></html>",
        proxy_label: str | None = "http://gateway:8080 (source=config)",
        error: Exception | None = None,
    ) -> None:
        self.status = status
        self.body = body
        self._proxy_label = proxy_label
        self.error = error
        self.calls: list[tuple[str, dict]] = []

    def get(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        if self.error is not None:
            raise self.error
        return HttpResponse(
            status=self.status,
            url=url,
            final_url=url,
            headers={"content-type": "text/html; charset=utf-8"},
            content=self.body,
            text=self.body.decode("utf-8"),
            elapsed_ms=3,
        )

    def proxies_for(self, url: str):
        return {"https": "http://gateway:8080"} if self._proxy_label else {}

    def proxy_label(self):
        return self._proxy_label

    def close(self) -> None:
        pass


class StubChain:
    def __init__(self, response: SearchResponse | None = None) -> None:
        self.response = response or SearchResponse(
            query="ascend npu mfu",
            engine="google",
            results=[
                SearchResult(
                    title="Ascend MFU", url="https://example.com/mfu", snippet="snippet", engine="google"
                )
            ],
            elapsed_ms=42,
        )
        self.calls: list[tuple] = []

    def search(self, ctx, request, engine="auto", chain=None):
        self.calls.append((ctx, request, engine, chain))
        return self.response

    def available_engines(self, ctx):
        return ["google", "bing"]


def make_settings() -> Settings:
    return Settings(proxy=ProxySettings({"https": "http://gateway:8080"}, source="config"))


def tools(server):
    return {tool.name: tool for tool in server._tool_manager.list_tools()}


def make_server(client=None):
    return build_server(make_settings(), client or FakeClient())


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------
def test_registers_exactly_the_three_tools():
    assert set(tools(make_server())) == TOOL_NAMES


def test_web_search_input_schema():
    tool = tools(make_server())["web_search"]
    schema = tool.parameters
    assert schema["type"] == "object"
    assert schema["required"] == ["query"]
    properties = schema["properties"]
    assert set(properties) == {
        "query",
        "max_results",
        "engine",
        "lang",
        "region",
        "recency",
        "timeout",
    }
    assert properties["max_results"]["default"] == 8
    assert properties["engine"]["default"] == "auto"
    assert properties["timeout"]["default"] == 60.0


def test_web_fetch_input_schema():
    tool = tools(make_server())["web_fetch"]
    properties = tool.parameters["properties"]
    assert tool.parameters["required"] == ["url"]
    assert properties["format"]["default"] == "markdown"
    assert properties["max_chars"]["default"] == 100000
    assert properties["include_links"]["default"] is False


def test_web_diagnose_input_schema():
    tool = tools(make_server())["web_diagnose"]
    assert tool.parameters["properties"]["probe"]["default"] is True
    assert tool.parameters.get("required", []) == []


def test_tools_publish_an_output_schema():
    for name, tool in tools(make_server()).items():
        assert tool.output_schema is not None, name
        assert tool.output_schema.get("type") == "object", name


# ---------------------------------------------------------------------------
# web_search
# ---------------------------------------------------------------------------
def test_web_search_returns_text_and_structured_content(monkeypatch):
    chain = StubChain()
    monkeypatch.setattr(server_module, "_require_chain", lambda: chain)
    tool = tools(make_server())["web_search"]

    result = tool.fn(query="ascend npu mfu", max_results=5)

    text = result.content[0].text
    assert "1. Ascend MFU" in text
    assert "https://example.com/mfu" in text
    assert "engine: google" in text
    assert "elapsed: 42 ms" in text
    assert result.structured_content["query"] == "ascend npu mfu"
    assert result.structured_content["results"][0]["url"] == "https://example.com/mfu"
    assert result.is_error is False

    ctx, request, engine, configured = chain.calls[0]
    assert request.query == "ascend npu mfu"
    assert request.max_results == 5
    assert engine == "auto"
    assert configured == list(make_settings().engine_chain)
    assert ctx.render is None or callable(ctx.render)
    assert ctx.options["google_host"] == "www.google.com"


def test_web_search_rejects_an_empty_query():
    with pytest.raises(ToolError) as excinfo:
        tools(make_server())["web_search"].fn(query="   ")
    assert "INVALID_ARGUMENT" in str(excinfo.value)


def test_web_search_surfaces_search_errors(monkeypatch):
    def raise_missing():
        raise SearchError("no engines", code="SEARCH_UNAVAILABLE", hint="install the search layer")

    monkeypatch.setattr(server_module, "_require_chain", raise_missing)
    with pytest.raises(ToolError) as excinfo:
        tools(make_server())["web_search"].fn(query="x")
    payload = json.loads(str(excinfo.value))
    assert payload["error"]["code"] == "SEARCH_UNAVAILABLE"
    assert payload["error"]["hint"] == "install the search layer"


def test_web_search_formats_warnings_and_answer():
    response = SearchResponse(
        query="q",
        engine="bing",
        results=[SearchResult(title="T", url="https://e.example/1")],
        answer="42",
        warnings=["google failed: blocked"],
        elapsed_ms=10,
    )
    text = format_search_response(response)
    assert text.splitlines()[0] == "query: q"
    assert "answer: 42" in text
    assert "google failed: blocked" in text
    assert "engine: bing | results: 1 | elapsed: 10 ms" in text


def test_format_handles_no_results():
    assert "(no results)" in format_search_response(SearchResponse(query="q", engine="google"))


# ---------------------------------------------------------------------------
# web_fetch
# ---------------------------------------------------------------------------
def test_web_fetch_returns_content_and_structured_content():
    client = FakeClient(body=b"<html><head><title>E</title></head><body><p>example body</p></body></html>")
    result = tools(build_server(make_settings(), client))["web_fetch"].fn(url="https://example.com/")

    assert "example body" in result.content[0].text
    assert result.structured_content["status"] == 200
    assert result.structured_content["title"] == "E"
    assert client.calls[0][0] == "https://example.com/"

    html_result = tools(build_server(make_settings(), FakeClient()))["web_fetch"].fn(
        url="https://example.com/", format="html"
    )
    assert "<html>" in html_result.content[0].text


def test_web_fetch_surfaces_fetch_errors():
    client = FakeClient(error=FetchError("boom", code="HTTP_STATUS", hint="nope"))
    with pytest.raises(ToolError) as excinfo:
        tools(build_server(make_settings(), client))["web_fetch"].fn(url="https://example.com/")
    payload = json.loads(str(excinfo.value))
    assert payload["error"]["code"] == "HTTP_STATUS"
    assert payload["error"]["hint"] == "nope"


# ---------------------------------------------------------------------------
# web_diagnose
# ---------------------------------------------------------------------------
def test_web_diagnose_reports_state(monkeypatch):
    chain = StubChain()
    monkeypatch.setattr(server_module, "_require_chain", lambda: chain)
    client = FakeClient()
    result = tools(build_server(make_settings(), client))["web_diagnose"].fn(probe=True)

    report = json.loads(result.content[0].text)
    assert report["llmmcp_version"]
    assert report["settings"]["proxy"]["label"] == "http://gateway:8080 (source=config)"
    assert report["http"]["tls_active"] == "truststore"
    assert report["browser"]["headless"] == "--headless=new"
    assert report["browser"]["available"] in (True, False)
    assert report["engines"]["available"] == ["google", "bing"]
    assert report["engines"]["resolved"] == ["google", "bing", "gnews"]
    assert report["engines"]["missing"] == ["gnews"]
    assert report["probe"]["ok"] is True
    assert report["probe"]["status"] == 200
    assert result.structured_content["probe"]["status"] == 200


def _missing_chain():
    raise SearchError("missing", code="SEARCH_UNAVAILABLE")


def test_web_diagnose_without_probe_or_chain(monkeypatch):
    monkeypatch.setattr(server_module, "_require_chain", _missing_chain)
    client = FakeClient()
    report = diagnose(server_module.Runtime(make_settings(), client=client), probe=False)

    assert report["probe"] == {"enabled": False}
    assert report["engines"]["available"] == []
    assert report["engines"]["error"]["code"] == "SEARCH_UNAVAILABLE"
    assert client.calls == []


def test_web_diagnose_probe_failure_is_reported(monkeypatch):
    monkeypatch.setattr(server_module, "_require_chain", lambda: StubChain())
    failing = FakeClient(error=FetchError("no route", code="CONNECTION_ERROR", hint="set LLMMCP_PROXY"))
    report = diagnose(server_module.Runtime(make_settings(), client=failing), probe=True)
    assert report["probe"]["ok"] is False
    assert report["probe"]["error"]["code"] == "CONNECTION_ERROR"


def test_diagnose_never_leaks_proxy_credentials():
    settings = Settings(
        proxy=ProxySettings({"https": "http://alice:topsecret@gw:8080"}, source="LLMMCP_PROXY")
    )
    client = FakeClient(proxy_label=settings.proxy.label())
    report = diagnose(server_module.Runtime(settings, client=client), probe=False)
    dumped = json.dumps(report)
    assert "topsecret" not in dumped
    assert "alice" not in dumped
    assert "gw:8080" in dumped
