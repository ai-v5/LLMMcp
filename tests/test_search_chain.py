"""Offline tests for engine registry and the auto fallback chain."""

from __future__ import annotations

import pytest

from llmmcp.models import SearchError, SearchResponse, SearchResult
from llmmcp.search import chain
from llmmcp.search.base import SearchContext, SearchRequest


class FakeEngine:
    """Engine stub: returns ``results`` or raises ``error``."""

    def __init__(self, name: str, results: list[SearchResult] | None = None, error: Exception | None = None):
        self.name = name
        self._results = results if results is not None else []
        self._error = error
        self.calls = 0

    def is_available(self, ctx) -> bool:  # pragma: no cover - registry only
        return True

    def search(self, request, ctx):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return SearchResponse(query=request.query, engine=self.name, results=list(self._results))


def hit(engine: str, title: str = "ascend npu hit") -> SearchResult:
    """A result that genuinely matches the test query (see ``REQUEST_QUERY``)."""
    return SearchResult(title=title, url=f"https://example.com/{engine}", engine=engine)


def decoy(engine: str) -> SearchResult:
    """A result for a completely different query -- what degraded endpoints return."""
    return SearchResult(title="WhatsApp Web", url="https://web.whatsapp.com/", engine=engine)


@pytest.fixture()
def registry(monkeypatch):
    """Install fake engines into the module registry."""
    engines: dict[str, FakeEngine] = {}

    def install(name: str, results=None, error=None) -> FakeEngine:
        engine = FakeEngine(name, results, error)
        engines[name] = engine
        return engine

    def fake_get_engine(name: str):
        key = (name or "").strip().lower()
        if key not in engines:
            raise SearchError(f"unknown search engine {name!r}", code="SEARCH_UNKNOWN_ENGINE")
        return engines[key]

    monkeypatch.setattr(chain, "get_engine", fake_get_engine)
    return install


def ctx(options=None) -> SearchContext:
    return SearchContext(http=None, render=None, options=options or {})


def request() -> SearchRequest:
    return SearchRequest("ascend npu mfu", max_results=5)


# ------------------------------------------------------------------ chain resolution
def test_resolve_chain_for_explicit_engine() -> None:
    assert chain.resolve_chain("bing", ctx()) == ["bing"]


def test_resolve_chain_auto_uses_default_order() -> None:
    order = chain.resolve_chain("auto", ctx())
    assert order[0] == "google"
    assert "gnews" in order


def test_bing_is_excluded_from_auto_mode() -> None:
    """Bing answers automated traffic with unrelated results on this network."""
    assert "bing" not in chain.resolve_chain("auto", ctx())
    assert "bing" not in chain.DEFAULT_ENGINE_CHAIN


def test_bing_can_be_opted_back_into_auto_mode() -> None:
    order = chain.resolve_chain("auto", ctx({"bing_in_auto": True, "engine_chain": ["google", "bing"]}))
    assert "bing" in order


def test_explicit_bing_engine_still_resolves() -> None:
    assert chain.resolve_chain("bing", ctx()) == ["bing"]


def test_settings_chain_cannot_reintroduce_bing() -> None:
    """settings.engine_chain still lists bing; auto mode must still drop it."""
    order = chain.resolve_chain("auto", ctx({"engine_chain": ["google", "bing", "google_news"]}))
    assert order == ["google", "gnews"]


def test_resolve_chain_prefers_ctx_options() -> None:
    assert chain.resolve_chain("auto", ctx({"engine_chain": ["gnews", "google"]})) == ["gnews", "google"]


def test_resolve_chain_prefers_explicit_chain_argument() -> None:
    assert chain.resolve_chain("auto", ctx({"engine_chain": ["gnews"]}), ["bing"]) == ["bing"]


def test_unknown_engine_is_reported() -> None:
    with pytest.raises(SearchError) as excinfo:
        chain.get_engine("nope")
    assert excinfo.value.code == "SEARCH_UNKNOWN_ENGINE"
    assert "google" in str(excinfo.value)


def test_registry_knows_all_engines() -> None:
    assert set(chain.engines_for_debug()) == {"google", "bing", "gnews"}


def test_google_news_alias_resolves_to_gnews() -> None:
    """settings.engine_chain spells the News engine ``google_news``."""
    assert chain.get_engine("google_news").name == "gnews"
    assert chain.get_engine("gnews").name == "gnews"


def test_available_engines_without_http_offers_bing_and_gnews() -> None:
    # google needs a renderer, so it is unavailable when ctx.render is None.
    available = chain.available_engines(SearchContext(http=object(), render=None, options={}))
    assert "google" not in available
    assert {"bing", "gnews"} <= set(available)


# ------------------------------------------------------------------------ auto mode
def test_auto_returns_first_success(registry) -> None:
    registry("google", [hit("google")])
    registry("bing", [hit("bing")])
    response = chain.search(ctx(), request(), chain=["google", "bing"])
    assert response.engine == "google"
    assert response.results[0].url == "https://example.com/google"
    assert response.warnings == []


def test_auto_falls_back_and_keeps_warnings(registry) -> None:
    straight_google = registry("google", error=SearchError("gated", code="GOOGLE_BLOCKED"))
    registry("bing", [hit("bing")])
    response = chain.search(ctx(), request(), chain=["google", "bing"])
    assert response.engine == "bing"
    assert len(response.results) == 1
    assert straight_google.calls == 1
    assert any("google" in w and "GOOGLE_BLOCKED" in w for w in response.warnings)


def test_empty_results_are_treated_as_failure(registry) -> None:
    registry("google", [])
    registry("bing", [hit("bing")])
    response = chain.search(ctx(), request(), chain=["google", "bing"])
    assert response.engine == "bing"
    assert any("no results" in w for w in response.warnings)


def test_all_engines_failing_raises_with_summary(registry) -> None:
    registry("google", error=SearchError("gated", code="GOOGLE_BLOCKED"))
    registry("bing", error=SearchError("429", code="BING_HTTP_ERROR"))
    registry("gnews", error=SearchError("empty", code="GNEWS_NO_RESULTS"))
    with pytest.raises(SearchError) as excinfo:
        chain.search(ctx(), request(), chain=["google", "bing", "gnews"])
    message = str(excinfo.value)
    assert excinfo.value.code == "SEARCH_ALL_FAILED"
    assert "GOOGLE_BLOCKED" in message
    assert "BING_HTTP_ERROR" in message
    assert "GNEWS_NO_RESULTS" in message
    assert "google, bing, gnews" in message
    assert excinfo.value.hint


def test_unexpected_exception_does_not_abort_the_chain(registry) -> None:
    registry("google", error=RuntimeError("boom"))
    registry("bing", [hit("bing")])
    response = chain.search(ctx(), request(), chain=["google", "bing"])
    assert response.engine == "bing"
    assert any("RuntimeError" in w for w in response.warnings)


def test_elapsed_ms_is_recorded(registry) -> None:
    registry("bing", [hit("bing")])
    response = chain.search(ctx(), request(), engine="bing")
    assert response.elapsed_ms >= 0


def test_explicit_engine_does_not_fall_back(registry) -> None:
    registry("google", error=SearchError("gated", code="GOOGLE_BLOCKED"))
    registry("bing", [hit("bing")])
    with pytest.raises(SearchError) as excinfo:
        chain.search(ctx(), request(), engine="google")
    assert "all search engines failed" in str(excinfo.value)


def test_query_is_preserved(registry) -> None:
    registry("bing", [hit("bing")])
    response = chain.search(ctx(), request(), engine="bing")
    assert response.query == "ascend npu mfu"


def test_response_is_search_response(registry) -> None:
    registry("bing", [hit("bing")])
    response = chain.search(ctx(), request(), engine="bing")
    assert isinstance(response, SearchResponse)
    payload = response.to_dict()
    assert payload["engine"] == "bing"
    assert payload["results"][0]["title"] == "ascend npu hit"


# ------------------------------------------------------------------ relevance guard
def test_unrelated_results_are_rejected_and_chain_continues(registry) -> None:
    """Degraded endpoints answer non-empty but for a different query."""
    registry("bing", [decoy("bing")])
    registry("gnews", [hit("gnews")])
    response = chain.search(ctx(), request(), chain=["bing", "gnews"])
    assert response.engine == "gnews"
    assert any("unrelated to the query" in w for w in response.warnings)


def test_relevance_guard_can_be_disabled(registry) -> None:
    registry("bing", [decoy("bing")])
    response = chain.search(
        ctx({"validate_relevance": False}), request(), chain=["bing", "gnews"]
    )
    assert response.engine == "bing"
    assert response.results[0].title == "WhatsApp Web"


def test_looks_relevant_matching() -> None:
    assert chain.looks_relevant("ascend npu mfu", [hit("x")]) is True
    assert chain.looks_relevant("ascend npu mfu", [decoy("x")]) is False
    # Empty / stopword-only queries cannot be judged, so they pass.
    assert chain.looks_relevant("the a of", [decoy("x")]) is True
