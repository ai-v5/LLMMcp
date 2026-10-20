"""Offline tests for the HTTP layer: truncation, retries, decoding, proxy plumbing.

A fake ``requests.Session`` keeps these tests hermetic: no sockets, no proxy, no
network.
"""

from __future__ import annotations

import pytest
import requests

from llmmcp.http import HttpClient, HttpError, HttpResponse, detect_encoding
from llmmcp.settings import ProxySettings, Settings


class FakeResponse:
    """The slice of ``requests.Response`` that :class:`HttpClient` touches."""

    def __init__(
        self,
        status: int = 200,
        *,
        url: str = "https://example.com/",
        headers: dict[str, str] | None = None,
        chunks: tuple[bytes, ...] = (b"",),
        raise_on_iter: Exception | None = None,
    ) -> None:
        self.status_code = status
        self.url = url
        self.headers = headers if headers is not None else {"content-type": "text/html; charset=utf-8"}
        self._chunks = list(chunks)
        self._raise = raise_on_iter
        self.closed = False

    def iter_content(self, chunk_size: int = 1):
        if self._raise is not None:
            raise self._raise
        yield from self._chunks

    def close(self) -> None:
        self.closed = True


class FakeSession:
    """Queue-backed ``requests.Session`` stand-in."""

    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = list(outcomes)
        self.headers: dict[str, str] = {}
        self.proxies: dict[str, str] = {}
        self.trust_env = True
        self.verify: object = True
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.closed = False

    def get(self, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append((url, kwargs))
        item = self.outcomes.pop(0)
        if isinstance(item, Exception):
            raise item
        assert isinstance(item, FakeResponse)
        return item

    def close(self) -> None:
        self.closed = True


def make_settings(**overrides) -> Settings:
    base = {
        "proxy": ProxySettings(),
        "tls": "off",
        "timeout": 5.0,
        "max_bytes": 1000,
        "retries": 2,
        "backoff": 0.01,
        "user_agent": "llmmcp-test/1.0",
    }
    base.update(overrides)
    return Settings(**base)


def make_client(outcomes: list[object], **settings_overrides) -> tuple[HttpClient, FakeSession, list[float]]:
    session = FakeSession(outcomes)
    sleeps: list[float] = []
    client = HttpClient(make_settings(**settings_overrides), session=session, sleep=sleeps.append)
    return client, session, sleeps


# ---------------------------------------------------------------------------
# happy path, decoding, truncation
# ---------------------------------------------------------------------------
def test_basic_get_reads_body_and_metadata():
    client, session, sleeps = make_client([FakeResponse(200, chunks=(b"hello ", b"world"))])
    response = client.get("https://example.com/")

    assert isinstance(response, HttpResponse)
    assert response.status == 200
    assert response.status_code == 200
    assert response.ok is True
    assert response.text == "hello world"
    assert response.content == b"hello world"
    assert response.truncated is False
    assert response.final_url == "https://example.com/"
    assert response.encoding == "utf-8"
    assert response.headers["content-type"].startswith("text/html")
    assert response.elapsed_ms >= 0
    assert sleeps == []
    assert session.calls[0][1]["stream"] is True


def test_truncates_to_max_bytes():
    client, _, _ = make_client([FakeResponse(chunks=(b"abcdefghij",))], max_bytes=4)
    response = client.get("https://example.com/")
    assert response.content == b"abcd"
    assert response.text == "abcd"
    assert response.truncated is True


def test_exact_budget_is_not_truncated():
    client, _, _ = make_client([FakeResponse(chunks=(b"abcd",))], max_bytes=4)
    response = client.get("https://example.com/")
    assert response.content == b"abcd"
    assert response.truncated is False


def test_exact_budget_with_more_data_is_truncated():
    client, _, _ = make_client([FakeResponse(chunks=(b"abcdef", b"gh"))], max_bytes=6)
    response = client.get("https://example.com/")
    assert response.content == b"abcdef"
    assert response.truncated is True


def test_per_call_max_bytes_overrides_settings():
    client, _, _ = make_client([FakeResponse(chunks=(b"0123456789",))], max_bytes=1000)
    response = client.get("https://example.com/", max_bytes=3)
    assert response.content == b"012"
    assert response.truncated is True


def test_header_charset_is_used():
    client, _, _ = make_client(
        [FakeResponse(headers={"content-type": "text/plain; charset=iso-8859-1"}, chunks=(b"caf\xe9",))]
    )
    response = client.get("https://example.com/")
    assert response.encoding == "iso-8859-1"
    assert response.text == "café"


def test_meta_charset_is_used_when_header_has_none():
    body = '<meta charset="utf-8"><p>café</p>'.encode()
    client, _, _ = make_client([FakeResponse(headers={"content-type": "text/html"}, chunks=(body,))])
    response = client.get("https://example.com/")
    assert response.encoding == "utf-8"
    assert "café" in response.text


def test_detect_encoding_falls_back_to_utf8():
    assert detect_encoding({"content-type": "text/plain"}, b"plain ascii") == "utf-8"
    assert detect_encoding({"content-type": "text/plain; charset=UTF8"}, b"x") == "utf-8"
    assert detect_encoding({"content-type": "text/html; charset=gb2312"}, b"x") == "gb18030"


# ---------------------------------------------------------------------------
# retries
# ---------------------------------------------------------------------------
def test_retries_on_503_then_succeeds():
    client, session, sleeps = make_client(
        [FakeResponse(503, chunks=(b"busy",)), FakeResponse(200, chunks=(b"ok",))], retries=2
    )
    response = client.get("https://example.com/")
    assert response.status == 200
    assert response.text == "ok"
    assert len(session.calls) == 2
    assert sleeps == [0.01]


def test_retries_on_429():
    client, session, _ = make_client([FakeResponse(429), FakeResponse(200, chunks=(b"fine",))], retries=1)
    assert client.get("https://example.com/").text == "fine"
    assert len(session.calls) == 2


def test_503_is_returned_when_retries_are_exhausted():
    client, session, _ = make_client([FakeResponse(503)], retries=0)
    response = client.get("https://example.com/")
    assert response.status == 503
    assert len(session.calls) == 1


def test_connection_error_is_retried_then_succeeds():
    client, session, sleeps = make_client(
        [requests.exceptions.ConnectionError("boom"), FakeResponse(200, chunks=(b"ok",))], retries=1
    )
    assert client.get("https://example.com/").text == "ok"
    assert len(session.calls) == 2
    assert sleeps == [0.01]


def test_connection_error_is_wrapped_after_retries():
    client, session, _ = make_client(
        [requests.exceptions.ConnectionError("boom"), requests.exceptions.ConnectionError("boom")], retries=1
    )
    with pytest.raises(HttpError) as excinfo:
        client.get("https://example.com/")
    assert excinfo.value.code == "CONNECTION_ERROR"
    assert "proxy" in (excinfo.value.hint or "").lower()
    assert len(session.calls) == 2


def test_timeout_is_wrapped_without_retry_when_retries_zero():
    client, session, _ = make_client([requests.exceptions.ConnectTimeout("slow")], retries=0)
    with pytest.raises(HttpError) as excinfo:
        client.get("https://example.com/")
    assert excinfo.value.code == "TIMEOUT"
    assert len(session.calls) == 1


def test_ssl_error_is_wrapped():
    client, _, _ = make_client([requests.exceptions.SSLError("bad cert")], retries=0)
    with pytest.raises(HttpError) as excinfo:
        client.get("https://example.com/")
    assert excinfo.value.code == "TLS_ERROR"
    assert "LLMMCP_TLS" in (excinfo.value.hint or "")


def test_proxy_error_is_wrapped():
    client, _, _ = make_client([requests.exceptions.ProxyError("proxy refused")], retries=0)
    with pytest.raises(HttpError) as excinfo:
        client.get("https://example.com/")
    assert excinfo.value.code == "PROXY_ERROR"


def test_retry_only_for_transient_exceptions():
    client, session, _ = make_client([requests.exceptions.InvalidSchema("ftp://x")], retries=2)
    with pytest.raises(HttpError) as excinfo:
        client.get("ftp://example.com/")
    assert excinfo.value.code == "UNSUPPORTED_SCHEME"
    assert len(session.calls) == 1


# ---------------------------------------------------------------------------
# proxy plumbing
# ---------------------------------------------------------------------------
def test_proxy_is_applied_per_request_not_on_the_session():
    proxy = ProxySettings(
        proxies={"http": "http://p:8080", "https": "http://p:8080"}, source="env:HTTPS_PROXY"
    )
    client, session, _ = make_client([FakeResponse(200, chunks=(b"x",))], proxy=proxy)
    # A session-level proxy would also cover hosts NO_PROXY excludes, so the
    # mapping is decided per request instead.
    assert session.proxies == {}
    assert session.trust_env is False
    assert session.headers["User-Agent"] == "llmmcp-test/1.0"
    client.get("https://example.com/")
    assert session.calls[0][1]["proxies"] == {"http": "http://p:8080", "https": "http://p:8080"}


def test_proxy_label_never_leaks_credentials():
    proxy = ProxySettings(proxies={"https": "http://alice:hunter2@gateway:8080"}, source="LLMMCP_PROXY")
    client, _, _ = make_client([], proxy=proxy)
    label = client.proxy_label()
    assert label == "http://gateway:8080 (source=LLMMCP_PROXY)"
    assert "hunter2" not in label
    assert "alice" not in label


def test_no_proxy_hosts_bypass_the_proxy():
    proxy = ProxySettings(
        proxies={"http": "http://p:8080", "https": "http://p:8080"},
        source="config",
        no_proxy="internal.example,10.0.0.0/8",
    )
    client, session, _ = make_client([FakeResponse(200, chunks=(b"x",))], proxy=proxy)
    assert client.proxies_for("https://internal.example/api") == {"http": None, "https": None}
    client.get("https://internal.example/api")
    assert session.calls[0][1]["proxies"] == {"http": None, "https": None}


def test_no_proxy_is_not_consulted_when_no_proxy_is_configured():
    client, session, _ = make_client([FakeResponse(200, chunks=(b"x",))])
    assert client.proxies_for("https://example.com/") == {"http": None, "https": None}
    client.get("https://example.com/")
    assert session.calls[0][1]["proxies"] == {"http": None, "https": None}


def test_windows_style_no_proxy_wildcards_bypass_the_proxy():
    """Windows Internet Settings write `127.*`/`10.*`/`<local>`, which requests ignores."""

    proxy = ProxySettings(
        proxies={"http": "http://p:8080", "https": "http://p:8080"},
        source="windows-ie",
        no_proxy="127.*,10.*,*.corp.example,localhost,<local>",
    )
    client, session, _ = make_client([FakeResponse(200, chunks=(b"x",))], proxy=proxy)

    for url in (
        "http://127.0.0.1:8765/market/index.json",
        "http://10.20.30.40/docs",
        "https://svc.corp.example/api",
        "http://localhost:3000/",
        "http://intranet/",
    ):
        assert client.proxies_for(url) == {"http": None, "https": None}, url

    assert client.proxies_for("https://www.google.com/") != {"http": None, "https": None}
    client.get("http://127.0.0.1:8765/market/index.json")
    assert session.calls[0][1]["proxies"] == {"http": None, "https": None}


# ---------------------------------------------------------------------------
# lifecycle / validation
# ---------------------------------------------------------------------------
def test_client_closes_owned_session_only():
    session = FakeSession([FakeResponse()])
    client = HttpClient(make_settings(), session=session)
    client.close()
    assert session.closed is False

    owned = HttpClient(make_settings())
    owned.close()  # closes its own requests.Session without raising


def test_invalid_arguments_are_rejected():
    client, session, _ = make_client([FakeResponse()])
    with pytest.raises(HttpError) as excinfo:
        client.get("https://example.com/", timeout=0)
    assert excinfo.value.code == "INVALID_ARGUMENT"
    with pytest.raises(HttpError) as excinfo:
        client.get("https://example.com/", max_bytes=0)
    assert excinfo.value.code == "INVALID_ARGUMENT"
    assert session.calls == []


def test_allow_redirects_is_forwarded():
    client, session, _ = make_client([FakeResponse(302, headers={"location": "https://example.com/next"})])
    response = client.get("https://example.com/", allow_redirects=False)
    assert response.status == 302
    assert session.calls[0][1]["allow_redirects"] is False
