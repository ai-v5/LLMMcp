"""Offline tests for the HTML/JSON/text -> FetchResponse pipeline."""

from __future__ import annotations

from pathlib import Path

import pytest

from llmmcp.fetch import FetchResponse, fetch, html_to_markdown, html_to_text, parse_html, pretty_json

FIXTURES = Path(__file__).parent / "fixtures" / "fetch"


class FakeResponse:
    def __init__(
        self,
        body: str | bytes = "",
        *,
        status: int = 200,
        content_type: str = "text/html; charset=utf-8",
        final_url: str = "https://example.com/final",
        truncated: bool = False,
        elapsed_ms: int = 12,
        headers: dict[str, str] | None = None,
    ) -> None:
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.content = body
        self.text = body.decode("utf-8", "replace")
        self.status = status
        self.headers = headers if headers is not None else {"content-type": content_type}
        self.final_url = final_url
        self.truncated = truncated
        self.elapsed_ms = elapsed_ms


class FakeClient:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.calls: list[tuple[str, float | None]] = []

    def get(self, url: str, timeout: float | None = None) -> FakeResponse:
        self.calls.append((url, timeout))
        return self.response


def article_html() -> str:
    return (FIXTURES / "article.html").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# HTML -> markdown
# ---------------------------------------------------------------------------
def test_html_to_markdown_keeps_article_and_drops_chrome():
    response = fetch(FakeClient(FakeResponse(article_html())), "https://example.com/article")
    content = response.content

    assert response.status == 200
    assert response.title == "Ascend NPU MFU notes"
    assert response.content_type == "text/html"
    assert response.final_url == "https://example.com/final"
    assert response.bytes_read == len(article_html().encode())
    assert response.elapsed_ms == 12
    assert response.truncated is False

    assert "# Ascend NPU MFU notes" in content
    assert "ratio of achieved to peak FLOPs" in content
    assert "measure tokens per second" in content
    assert "achieved_flops" in content  # fenced code survives

    # Boilerplate and scripts are gone.
    assert "tracking" not in content
    assert "analytics.js" not in content
    assert "Copyright 2026 Example Corp" not in content
    assert "Please enable JavaScript" not in content
    assert "ads.example.org" not in content
    assert "Related:" not in content


def test_include_links_controls_markdown_links():
    plain = fetch(FakeClient(FakeResponse(article_html())), "https://example.com/a")
    assert "](https://example.com/mfu)" not in plain.content
    assert "MFU appendix" in plain.content

    linked = fetch(FakeClient(FakeResponse(article_html())), "https://example.com/a", include_links=True)
    assert "](https://example.com/mfu)" in linked.content


def test_boilerplate_is_removed_even_without_main():
    html = """
    <html><head><title>T</title></head><body>
      <header>site header</header>
      <nav>menu links</nav>
      <p>keep me</p>
      <footer>footer junk</footer>
      <noscript>enable js</noscript>
      <iframe src="https://ads.example/x"></iframe>
    </body></html>
    """
    content = html_to_markdown(parse_html(html))
    assert "keep me" in content
    for junk in ("site header", "menu links", "footer junk", "enable js", "ads.example"):
        assert junk not in content


def test_comments_are_dropped():
    html = "<html><body><p>visible</p><!-- hidden comment --></body></html>"
    assert "hidden comment" not in html_to_markdown(parse_html(html))


# ---------------------------------------------------------------------------
# HTML -> text
# ---------------------------------------------------------------------------
def test_html_to_text_has_no_markdown_markup():
    response = fetch(FakeClient(FakeResponse(article_html())), "https://example.com/article", fmt="text")
    content = response.content
    assert "Ascend NPU MFU notes" in content
    assert "ratio of achieved to peak FLOPs" in content
    assert "#" not in content
    assert "](" not in content
    assert "**" not in content
    assert "Copyright 2026 Example Corp" not in content


def test_html_to_text_normalises_blank_lines():
    text = html_to_text(parse_html("<html><body><p>a</p><p></p><p></p><p>b</p></body></html>"))
    assert "\n\n\n" not in text
    assert text.startswith("a")
    assert text.endswith("b")


# ---------------------------------------------------------------------------
# raw html / json / text / binaries
# ---------------------------------------------------------------------------
def test_html_format_returns_raw_document():
    html = article_html()
    response = fetch(FakeClient(FakeResponse(html)), "https://example.com/article", fmt="html")
    assert response.content == html
    assert "<script>" in response.content


def test_json_is_pretty_printed():
    body = '{"b":2,"a":[1,2,3]}'
    response = fetch(
        FakeClient(FakeResponse(body, content_type="application/json")), "https://api.example.com/x"
    )
    assert response.content.splitlines()[0] == "{"
    assert '"b": 2' in response.content
    assert '"a": [' in response.content


def test_json_suffix_and_invalid_json():
    response = fetch(
        FakeClient(FakeResponse("not json", content_type="application/problem+json")),
        "https://api.example.com/x",
    )
    assert response.content == "not json"
    assert pretty_json("not json") == "not json"


def test_plain_text_passes_through():
    body = "line one\nline two"
    response = fetch(
        FakeClient(FakeResponse(body, content_type="text/plain; charset=utf-8")), "https://x.example/"
    )
    assert response.content == body
    assert response.title is None


def test_binary_content_type_is_rejected():
    client = FakeClient(FakeResponse(b"\x00\x01", content_type="application/octet-stream"))
    with pytest.raises(Exception) as excinfo:
        fetch(client, "https://example.com/file.bin")
    assert getattr(excinfo.value, "code", None) == "UNSUPPORTED_CONTENT_TYPE"


def test_http_error_status_is_reported():
    response = fetch(FakeClient(FakeResponse("nope", status=404)), "https://example.com/missing")
    assert response.status == 404
    assert "nope" in response.content


# ---------------------------------------------------------------------------
# bounds and argument validation
# ---------------------------------------------------------------------------
def test_max_chars_truncates_and_flags():
    response = fetch(FakeClient(FakeResponse(article_html())), "https://example.com/a", max_chars=40)
    assert len(response.content) == 40
    assert response.truncated is True


def test_http_level_truncation_is_propagated():
    response = fetch(FakeClient(FakeResponse(article_html(), truncated=True)), "https://example.com/a")
    assert response.truncated is True


def test_timeout_is_forwarded():
    client = FakeClient(FakeResponse("<html><body>x</body></html>"))
    fetch(client, "https://example.com/a", timeout=3.5)
    assert client.calls == [("https://example.com/a", 3.5)]


@pytest.mark.parametrize("url", ["ftp://example.com/x", "file:///etc/passwd", "example.com/no-scheme"])
def test_non_http_schemes_are_rejected(url):
    client = FakeClient(FakeResponse("x"))
    with pytest.raises(Exception) as excinfo:
        fetch(client, url)
    assert getattr(excinfo.value, "code", None) == "UNSUPPORTED_SCHEME"
    assert client.calls == []


def test_unknown_format_and_bad_max_chars_are_rejected():
    client = FakeClient(FakeResponse("x"))
    with pytest.raises(Exception) as excinfo:
        fetch(client, "https://example.com/a", fmt="pdf")
    assert getattr(excinfo.value, "code", None) == "INVALID_ARGUMENT"

    with pytest.raises(Exception) as excinfo:
        fetch(client, "https://example.com/a", max_chars=0)
    assert getattr(excinfo.value, "code", None) == "INVALID_ARGUMENT"
    assert client.calls == []


def test_title_missing_returns_none():
    response = fetch(
        FakeClient(FakeResponse("<html><body><p>no title</p></body></html>")), "https://example.com/"
    )
    assert response.title is None
    assert "no title" in response.content


def test_fetch_response_is_the_shared_contract():
    response = fetch(FakeClient(FakeResponse(article_html())), "https://example.com/a")
    assert isinstance(response, FetchResponse)
    payload = response.to_dict()
    assert payload["url"] == "https://example.com/a"
    assert payload["status"] == 200
    assert payload["content"]


# ---------------------------------------------------------------------------
# JavaScript shell fallback
# ---------------------------------------------------------------------------
SPA_SHELL = "<html><head><title>App</title></head><body><div id='root'></div></body></html>"
SPA_RENDERED = (
    "<html><head><title>Docs</title></head><body><article><h1>Ascend docs</h1><p>"
    + "Body text. " * 40
    + "</p></article></body></html>"
)


def test_empty_shell_is_rendered_through_the_browser():
    calls: list[tuple[str, float]] = []

    def fake_render(url: str, timeout: float) -> str:
        calls.append((url, timeout))
        return SPA_RENDERED

    response = fetch(FakeClient(FakeResponse(SPA_SHELL)), "https://example.com/spa", render=fake_render)

    assert calls == [("https://example.com/final", 30.0)]
    assert "Ascend docs" in response.content
    assert response.title == "Docs"


def test_render_failure_keeps_the_plain_result():
    def boom(url: str, timeout: float) -> str:
        raise RuntimeError("browser exploded")

    response = fetch(FakeClient(FakeResponse(SPA_SHELL)), "https://example.com/spa", render=boom)

    assert response.status == 200
    assert response.content.strip() == ""


def test_rendered_page_shorter_than_the_plain_result_is_ignored():
    page = "<html><body><p>" + "real content " * 30 + "</p></body></html>"

    response = fetch(
        FakeClient(FakeResponse(page)),
        "https://example.com/page",
        render=lambda url, timeout: "<html><body>x</body></html>",
    )

    assert "real content" in response.content


def test_without_a_renderer_the_shell_is_returned_as_is():
    response = fetch(FakeClient(FakeResponse(SPA_SHELL)), "https://example.com/spa")

    assert response.status == 200
    assert response.content.strip() == ""

