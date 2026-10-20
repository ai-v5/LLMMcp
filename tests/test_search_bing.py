"""Offline tests for the Bing engine: URL uncloaking, parsing and error paths."""

from __future__ import annotations

import pathlib

import pytest

from llmmcp.models import SearchError
from llmmcp.search.base import SearchContext, SearchRequest
from llmmcp.search.bing import (
    BingEngine,
    build_search_url,
    parse_bing_results,
    query_coverage,
    unwrap_bing_url,
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


class FakeHttp:
    def __init__(self, text: str = "", status: int = 200):
        self.text = text
        self.status = status
        self.calls: list[str] = []

    def get(self, url, timeout=None, headers=None, allow_redirects=True):
        self.calls.append(url)
        return FakeResponse(text=self.text, status=self.status)


def make_ctx(*, http=None, render=None, options=None) -> SearchContext:
    return SearchContext(http=http, render=render, options=options or {})


# ------------------------------------------------------------------------- parsing
def test_parses_real_bing_page() -> None:
    results = parse_bing_results(fixture_text("bing_results.html"), 10)
    assert len(results) == 10
    assert results[0].title == "Welcome to Python.org"
    assert results[0].url == "https://www.python.org/"
    assert results[0].engine == "bing"
    assert results[0].snippet.startswith("The mission of the Python Software Foundation")
    assert results[1].url == "https://www.python.org/downloads/"


def test_unwraps_bing_click_tracker() -> None:
    cloaked = (
        "https://www.bing.com/ck/a?!&&p=07dbcd8d23fcbb073bc107cc40c9117bb6a1d1ef2c62624b4"
        "&u=a1aHR0cHM6Ly93d3cucHl0aG9uLm9yZy8&ntb=1"
    )
    assert unwrap_bing_url(cloaked) == "https://www.python.org/"


def test_unwrap_leaves_plain_urls_alone() -> None:
    assert unwrap_bing_url("https://realpython.com/x") == "https://realpython.com/x"
    assert unwrap_bing_url("") is None
    assert unwrap_bing_url(None) is None


def test_unwrap_returns_input_when_payload_is_not_base64() -> None:
    broken = "https://www.bing.com/ck/a?!&&p=1&u=a1!!!notbase64!!!"
    assert unwrap_bing_url(broken) == broken


def test_parsed_urls_are_never_bing_redirectors() -> None:
    for item in parse_bing_results(fixture_text("bing_results.html"), 10):
        assert "/ck/a" not in item.url
        assert "bing.com" not in item.url


def test_max_results_is_respected() -> None:
    assert len(parse_bing_results(fixture_text("bing_results.html"), 3)) == 3


def test_no_result_page_yields_nothing() -> None:
    assert parse_bing_results(fixture_text("bing_no_results.html"), 10) == []


def test_snippets_are_collapsed() -> None:
    for item in parse_bing_results(fixture_text("bing_results.html"), 10):
        assert "\n" not in item.snippet
        assert len(item.snippet) <= 400


# -------------------------------------------------------------------------- engine
def test_engine_returns_results() -> None:
    ctx = make_ctx(http=FakeHttp(fixture_text("bing_results.html")))
    response = BingEngine().search(SearchRequest("python asyncio tutorial", max_results=5), ctx)
    assert response.engine == "bing"
    assert response.query == "python asyncio tutorial"
    assert len(response.results) == 5
    assert response.results[0].url == "https://www.python.org/"


def test_engine_requests_with_browser_headers() -> None:
    seen: dict[str, object] = {}

    class RecordingHttp(FakeHttp):
        def get(self, url, timeout=None, headers=None, allow_redirects=True):
            seen["url"] = url
            seen["headers"] = headers or {}
            return FakeResponse(text=fixture_text("bing_results.html"), status=200)

    BingEngine().search(SearchRequest("q"), make_ctx(http=RecordingHttp()))
    assert "bing.com/search?q=q" in str(seen["url"])
    assert "Mozilla" in str(seen["headers"].get("User-Agent"))


def test_query_coverage_scores_related_page_high() -> None:
    results = parse_bing_results(fixture_text("bing_results.html"), 10)
    # Fixture is the real "python asyncio tutorial" page.
    assert query_coverage("python asyncio tutorial", results) >= 0.5


def test_query_coverage_scores_decoy_page_low() -> None:
    results = parse_bing_results(fixture_text("bing_unrelated.html"), 10)
    assert len(results) >= 5, "the decoy page parses fine -- that is the danger"
    assert query_coverage("ascend npu mfu", results) < 0.5


def test_query_coverage_ignores_empty_query() -> None:
    assert query_coverage("", []) == 1.0
    assert query_coverage("the a of", []) == 1.0


def test_engine_rejects_unrelated_results() -> None:
    """Regression: a valid-looking page for a different query must not be success."""
    with pytest.raises(SearchError) as excinfo:
        BingEngine().search(
            SearchRequest("ascend npu mfu"), make_ctx(http=FakeHttp(fixture_text("bing_unrelated.html")))
        )
    assert excinfo.value.code == "BING_UNRELATED"
    assert excinfo.value.hint


def test_engine_accepts_relevant_page_for_same_query() -> None:
    response = BingEngine().search(
        SearchRequest("python asyncio tutorial", max_results=5),
        make_ctx(http=FakeHttp(fixture_text("bing_results.html"))),
    )
    assert len(response.results) == 5


def test_engine_raises_when_no_results() -> None:
    with pytest.raises(SearchError) as excinfo:
        BingEngine().search(
            SearchRequest("zzzzqqqq"),
            make_ctx(http=FakeHttp(fixture_text("bing_no_results.html"))),
        )
    assert excinfo.value.code == "BING_NO_RESULTS"


def test_engine_raises_on_http_error() -> None:
    with pytest.raises(SearchError) as excinfo:
        BingEngine().search(SearchRequest("q"), make_ctx(http=FakeHttp("blocked", status=503)))
    assert excinfo.value.code == "BING_HTTP_ERROR"
    assert "503" in str(excinfo.value)


def test_engine_raises_without_http_client() -> None:
    with pytest.raises(SearchError) as excinfo:
        BingEngine().search(SearchRequest("q"), make_ctx(http=None))
    assert excinfo.value.code == "BING_NO_HTTP"


def test_engine_wraps_request_failure() -> None:
    class Boom:
        def get(self, *args, **kwargs):
            raise ConnectionError("proxy refused")

    with pytest.raises(SearchError) as excinfo:
        BingEngine().search(SearchRequest("q"), make_ctx(http=Boom()))
    assert excinfo.value.code == "BING_REQUEST_FAILED"
    assert "proxy refused" in str(excinfo.value)


def test_is_available_requires_http() -> None:
    engine = BingEngine()
    assert engine.is_available(make_ctx(http=FakeHttp())) is True
    assert engine.is_available(make_ctx(http=None)) is False


def test_build_search_url() -> None:
    url = build_search_url(SearchRequest("a b", max_results=8, lang="en", recency="week"))
    assert url.startswith("https://www.bing.com/search?")
    assert "q=a+b" in url
    assert "setlang=en" in url
    assert "count=" in url
