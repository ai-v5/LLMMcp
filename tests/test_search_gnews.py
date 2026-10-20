"""Offline tests for the Google News RSS engine."""

from __future__ import annotations

import pathlib

import pytest

from llmmcp.models import SearchError
from llmmcp.search.base import SearchContext, SearchRequest
from llmmcp.search.gnews import (
    GoogleNewsEngine,
    build_feed_url,
    parse_feed,
    strip_html,
    to_iso8601,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "search"


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeResponse:
    def __init__(self, text: str = "", status: int = 200):
        self.text = text
        self.content = text.encode("utf-8")
        self.status = status
        self.status_code = status
        self.headers: dict[str, str] = {}
        self.final_url = ""


class FakeHttp:
    def __init__(self, text: str = "", status: int = 200):
        self.text = text
        self.status = status
        self.calls: list[str] = []

    def get(self, url, timeout=None, headers=None, allow_redirects=True):
        self.calls.append(url)
        return FakeResponse(text=self.text, status=self.status)


def make_ctx(*, http=None, options=None) -> SearchContext:
    return SearchContext(http=http, render=None, options=options or {})


# ------------------------------------------------------------------------- parsing
def test_parses_real_feed() -> None:
    results = parse_feed(fixture_text("gnews_feed.xml"), 10)
    assert len(results) == 3
    assert results[0].title.startswith("Advancing the Agentic World")
    assert results[0].url.startswith("https://news.google.com/rss/articles/")
    assert results[0].engine == "gnews"
    assert results[1].title.startswith("Huawei Atlas 960E")


def test_published_at_is_iso8601_utc() -> None:
    results = parse_feed(fixture_text("gnews_feed.xml"), 10)
    assert results[0].published_at == "2026-09-17T07:00:00+00:00"
    assert results[1].published_at == "2026-09-18T06:51:00+00:00"


def test_to_iso8601_handles_bad_input() -> None:
    assert to_iso8601("not a date") is None
    assert to_iso8601("") is None
    assert to_iso8601(None) is None
    assert to_iso8601("Thu, 17 Sep 2026 07:00:00 GMT") == "2026-09-17T07:00:00+00:00"


def test_max_results_is_respected() -> None:
    assert len(parse_feed(fixture_text("gnews_feed.xml"), 2)) == 2


def test_strip_html_removes_markup() -> None:
    assert strip_html("<b>bold</b> and <a href='x'>link</a>") == "bold and link"
    assert strip_html("<script>bad()</script>ok") == "ok"
    assert strip_html("&amp;lt;") == "<"
    assert strip_html(None) == ""


def test_empty_feed_raises() -> None:
    with pytest.raises(SearchError) as excinfo:
        parse_feed("   ", 5)
    assert excinfo.value.code == "GNEWS_EMPTY"


def test_malformed_xml_raises() -> None:
    with pytest.raises(SearchError) as excinfo:
        parse_feed("<rss><channel><item>broken", 5)
    assert excinfo.value.code == "GNEWS_BAD_XML"


# -------------------------------------------------------------------------- engine
def test_engine_returns_results() -> None:
    ctx = make_ctx(http=FakeHttp(fixture_text("gnews_feed.xml")))
    response = GoogleNewsEngine().search(SearchRequest("ascend npu mfu", max_results=2), ctx)
    assert response.engine == "gnews"
    assert len(response.results) == 2
    assert response.results[0].published_at is not None


def test_engine_hits_the_rss_endpoint() -> None:
    http = FakeHttp(fixture_text("gnews_feed.xml"))
    GoogleNewsEngine().search(SearchRequest("ascend npu mfu"), make_ctx(http=http))
    assert http.calls and http.calls[0].startswith("https://news.google.com/rss/search?")
    assert "q=ascend+npu+mfu" in http.calls[0]


def test_engine_raises_without_http() -> None:
    with pytest.raises(SearchError) as excinfo:
        GoogleNewsEngine().search(SearchRequest("q"), make_ctx(http=None))
    assert excinfo.value.code == "GNEWS_NO_HTTP"


def test_engine_wraps_request_failure() -> None:
    class Boom:
        def get(self, *args, **kwargs):
            raise TimeoutError("proxy timeout")

    with pytest.raises(SearchError) as excinfo:
        GoogleNewsEngine().search(SearchRequest("q"), make_ctx(http=Boom()))
    assert excinfo.value.code == "GNEWS_REQUEST_FAILED"


def test_engine_raises_when_feed_is_empty() -> None:
    empty = '<?xml version="1.0"?><rss version="2.0"><channel><title>x</title></channel></rss>'
    with pytest.raises(SearchError) as excinfo:
        GoogleNewsEngine().search(SearchRequest("q"), make_ctx(http=FakeHttp(empty)))
    assert excinfo.value.code == "GNEWS_NO_RESULTS"


def test_build_feed_url_uses_locale() -> None:
    url = build_feed_url(SearchRequest("q", lang="zh-CN", region="cn", recency="day"))
    assert "hl=zh-CN" in url
    assert "gl=CN" in url
    assert "ceid=CN:zh" in url
    assert "when=1d" in url
