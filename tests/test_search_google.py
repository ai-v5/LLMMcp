"""Offline tests for the Google engine: parser, block detection, degraded paths.

Fixtures are trimmed captures of a live ``headless Chrome --dump-dom`` render
(see ``tests/fixtures/search/README.md``); no test in this module touches the
network.
"""

from __future__ import annotations

import pathlib

import pytest

from llmmcp.models import SearchError
from llmmcp.search.base import SearchContext, SearchRequest
from llmmcp.search.google import (
    GoogleEngine,
    build_search_url,
    detect_block,
    parse_google_results,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "search"


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeResponse:
    """Minimal stand-in for llmmcp.http.HttpResponse."""

    def __init__(self, text: str = "", status: int = 200, headers: dict[str, str] | None = None):
        self.text = text
        self.content = text.encode("utf-8")
        self.status = status
        self.status_code = status
        self.headers = headers or {}
        self.final_url = ""

    def header(self, name: str) -> str | None:
        for key, value in self.headers.items():
            if key.lower() == name.lower():
                return value
        return None


class FakeHttp:
    """Records calls; answers /goto redirects with a unique Location per link."""

    def __init__(self, text: str = "", status: int = 200):
        self.text = text
        self.status = status
        self.calls: list[str] = []

    def get(self, url, timeout=None, headers=None, allow_redirects=True):
        self.calls.append(url)
        if "/goto" in url:
            n = len(self.calls)
            return FakeResponse(status=302, headers={"Location": f"https://target.example/{n}"})
        return FakeResponse(text=self.text, status=self.status)


def make_ctx(html: str | None = None, *, render=None, http=None, options=None) -> SearchContext:
    if http is None:
        http = FakeHttp(html or "")
    return SearchContext(http=http, render=render, options=options or {})


# --------------------------------------------------------------------------- parser
def test_parses_real_rendered_dom() -> None:
    results = parse_google_results(fixture_text("google_results.html"), max_results=10)
    assert len(results) == 6, "blocks without a recoverable URL must be dropped, not faked"
    assert results[0].title == "A conceptual overview of asyncio"
    assert results[0].url.startswith("https://docs.python.org")
    assert results[0].engine == "google"
    assert results[0].snippet and len(results[0].snippet) <= 400
    assert results[1].url.startswith("https://realpython.com")
    assert all(r.url.startswith("https://") for r in results)


def test_parse_respects_max_results() -> None:
    results = parse_google_results(fixture_text("google_results.html"), max_results=2)
    assert len(results) == 2


def test_resolver_recovers_opaque_goto_links() -> None:
    """Without a resolver 6 blocks survive; resolving /goto keeps all 9."""
    seen: list[str] = []

    def resolve(href: str) -> str:
        seen.append(href)
        return f"https://target.example/{len(seen)}"

    results = parse_google_results(fixture_text("google_results.html"), 10, resolve_url=resolve)
    assert len(results) == 9
    assert seen and all(href.startswith("/goto?url=") for href in seen)
    assert len({r.url for r in results}) == 9


def test_snippet_is_collapsed_and_truncated() -> None:
    results = parse_google_results(fixture_text("google_results.html"), 10)
    for item in results:
        assert "\n" not in item.snippet
        assert "  " not in item.snippet
        assert len(item.snippet) <= 400


def test_skips_javascript_and_internal_links() -> None:
    html = """
    <html><body>
      <div class="MjjYud"><a href="javascript:void(0)"><h3>JS only</h3></a></div>
      <div class="MjjYud"><a href="/search?q=other"><h3>Internal search</h3></a></div>
      <div class="MjjYud"><a href="/preferences"><h3>Preferences</h3></a></div>
      <div class="MjjYud"><a href="https://good.example/page"><h3>Good one</h3></a></div>
    </body></html>
    """
    results = parse_google_results(html, 10)
    assert [r.title for r in results] == ["Good one"]
    assert results[0].url == "https://good.example/page"


def test_legacy_div_g_layout_is_supported() -> None:
    html = """
    <html><body><div class="g">
      <a href="https://legacy.example/x"><h3>Legacy result</h3></a>
      <div class="VwiC3b">legacy snippet text here</div>
    </div></body></html>
    """
    results = parse_google_results(html, 5)
    assert len(results) == 1
    assert results[0].title == "Legacy result"
    assert results[0].snippet == "legacy snippet text here"


# ------------------------------------------------------------------ block detection
def test_detect_gate_page() -> None:
    html = fixture_text("google_gate.html")
    from bs4 import BeautifulSoup

    blocked = detect_block(html, BeautifulSoup(html, "html.parser"), 0)
    assert blocked is not None
    code, message, hint = blocked
    assert code == "GOOGLE_BLOCKED"
    assert "gate" in message.lower()
    assert "render" in hint.lower() or "browser" in hint.lower()


def test_detect_captcha_page() -> None:
    html = fixture_text("google_captcha.html")
    from bs4 import BeautifulSoup

    code, message, _hint = detect_block(html, BeautifulSoup(html, "html.parser"), 0)
    assert code == "GOOGLE_BLOCKED"
    assert "unusual traffic" in message.lower() or "captcha" in message.lower()


def test_inline_js_mentions_do_not_trigger_detection_when_results_exist() -> None:
    """A successful page's inline JS mentions 'captcha'/'enablejs' -- it is not a block."""
    html = fixture_text("google_results.html").replace(
        "</body>", "<script>var hasCaptchaSupport=true;var x='enablejs';</script></body>"
    )
    from bs4 import BeautifulSoup

    assert detect_block(html, BeautifulSoup(html, "html.parser"), 6) is None


# ---------------------------------------------------------------------- engine flow
def test_engine_uses_render_path() -> None:
    html = fixture_text("google_results.html")
    ctx = make_ctx(render=lambda url, timeout: html)
    response = GoogleEngine().search(SearchRequest("python asyncio tutorial", max_results=5), ctx)
    assert response.engine == "google"
    assert len(response.results) == 5
    assert response.results[0].title == "A conceptual overview of asyncio"
    assert response.warnings == []


def test_engine_resolves_goto_links_through_http() -> None:
    html = fixture_text("google_results.html")
    http = FakeHttp(html)
    ctx = make_ctx(render=lambda url, timeout: html, http=http)
    response = GoogleEngine().search(SearchRequest("python asyncio", max_results=9), ctx)
    assert len(response.results) == 9
    assert all(r.url.startswith("https://target.example/") for r in response.results)
    assert any("/goto" in call for call in http.calls)


def test_engine_can_disable_url_resolution() -> None:
    html = fixture_text("google_results.html")
    ctx = make_ctx(render=lambda url, timeout: html, options={"google_resolve_urls": False})
    response = GoogleEngine().search(SearchRequest("python asyncio", max_results=9), ctx)
    assert len(response.results) == 6
    assert response.results[0].url.startswith("https://docs.python.org")


def test_engine_raises_when_render_is_missing() -> None:
    ctx = make_ctx(html=fixture_text("google_gate.html"), render=None)
    with pytest.raises(SearchError) as excinfo:
        GoogleEngine().search(SearchRequest("python asyncio"), ctx)
    assert excinfo.value.code == "GOOGLE_BLOCKED"
    assert "SearchContext.render" in str(excinfo.value)
    assert excinfo.value.hint


def test_engine_raises_when_render_missing_and_no_http() -> None:
    ctx = SearchContext(http=None, render=None, options={})
    with pytest.raises(SearchError) as excinfo:
        GoogleEngine().search(SearchRequest("python asyncio"), ctx)
    assert excinfo.value.code == "GOOGLE_NO_RENDERER"


def test_engine_reports_render_failure() -> None:
    def boom(url: str, timeout: float) -> str:
        raise RuntimeError("chrome exited 13")

    ctx = make_ctx(render=boom)
    with pytest.raises(SearchError) as excinfo:
        GoogleEngine().search(SearchRequest("python asyncio"), ctx)
    assert excinfo.value.code == "GOOGLE_RENDER_FAILED"
    assert "chrome exited 13" in str(excinfo.value)


def test_engine_raises_on_captcha_from_render() -> None:
    ctx = make_ctx(render=lambda url, timeout: fixture_text("google_captcha.html"))
    with pytest.raises(SearchError) as excinfo:
        GoogleEngine().search(SearchRequest("python asyncio"), ctx)
    assert excinfo.value.code == "GOOGLE_BLOCKED"


def test_is_available_reflects_renderer() -> None:
    engine = GoogleEngine()
    assert engine.is_available(make_ctx(render=lambda url, timeout: "")) is True
    assert engine.is_available(make_ctx(render=None)) is False


# ------------------------------------------------------------------------- url build
def test_build_search_url_includes_all_filters() -> None:
    request = SearchRequest("python asyncio", max_results=8, lang="en", region="us", recency="week")
    url = build_search_url(request)
    assert url.startswith("https://www.google.com/search?")
    assert "q=python+asyncio" in url
    assert "num=10" in url
    assert "hl=en" in url
    assert "gl=us" in url
    assert "tbs=qdr%3Aw" in url


def test_build_search_url_honours_custom_host() -> None:
    url = build_search_url(SearchRequest("x"), host="www.google.com.hk")
    assert url.startswith("https://www.google.com.hk/search?")
