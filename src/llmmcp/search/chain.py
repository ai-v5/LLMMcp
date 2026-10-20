"""Engine registry and the automatic fallback chain.

``auto`` mode walks the configured engine order, treats "engine answered but
produced nothing" as a failure, accumulates one warning per failed engine, and
only raises once every candidate is exhausted -- so a single blocked source
never blinds the caller.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterable, Sequence
from typing import Any

from ..models import SearchError, SearchResponse
from .base import SearchContext, SearchRequest
from .bing import BingEngine
from .gnews import GoogleNewsEngine
from .google import GoogleEngine

#: Fallback order used when neither an explicit chain nor settings are available.
#: Fallback order when nothing else is configured. ``bing`` is deliberately
#: absent: from this corporate egress it often answers with results belonging
#: to a different query, so it is only used when requested explicitly.
DEFAULT_ENGINE_CHAIN: tuple[str, ...] = ("google", "gnews")

_ENGINE_FACTORIES = {
    "google": GoogleEngine,
    "bing": BingEngine,
    "gnews": GoogleNewsEngine,
}

#: Accepted aliases so ``settings.engine_chain`` can spell the News engine either way.
_ENGINE_ALIASES = {
    "google_news": "gnews",
    "google-news": "gnews",
    "news": "gnews",
    "googlenews": "gnews",
}


def _settings_chain() -> list[str]:
    """Best-effort read of ``settings.engine_chain`` without a hard dependency.

    The search layer must stay importable on its own (tests, CLI, MCP bootstrap),
    so a missing or partially built settings module degrades to the default
    order instead of exploding.
    """
    try:  # pragma: no cover - depends on the core package being present
        from .. import settings as settings_mod
    except Exception:
        return list(DEFAULT_ENGINE_CHAIN)

    for attr in ("engine_chain", "ENGINE_CHAIN", "DEFAULT_ENGINE_CHAIN"):
        value = getattr(settings_mod, attr, None)
        if isinstance(value, (list, tuple)) and value:
            return [str(item) for item in value]

    loader = getattr(settings_mod, "load_settings", None) or getattr(settings_mod, "get_settings", None)
    if callable(loader):
        try:  # pragma: no cover - depends on the core package being present
            loaded = loader()
            value = getattr(loaded, "engine_chain", None)
            if isinstance(value, (list, tuple)) and value:
                return [str(item) for item in value]
        except Exception:
            pass
    return list(DEFAULT_ENGINE_CHAIN)


def _canonical(name: str) -> str:
    """Map an engine name or alias onto its canonical registry key."""
    key = (name or "").strip().lower().replace(" ", "_")
    return _ENGINE_ALIASES.get(key, key)


def get_engine(name: str):
    """Return the engine instance registered under ``name`` (aliases accepted)."""
    key = _canonical(name)
    factory = _ENGINE_FACTORIES.get(key)
    if factory is None:
        known = ", ".join(sorted(_ENGINE_FACTORIES) + sorted(_ENGINE_ALIASES))
        raise SearchError(
            f"unknown search engine {name!r} (available: {known})",
            code="SEARCH_UNKNOWN_ENGINE",
            hint=f"Pass engine='auto' or one of: {known}.",
        )
    return factory()


_STOPWORDS = frozenset(
    {"the", "a", "an", "of", "for", "and", "or", "to", "in", "on", "is", "are",
     "what", "how", "why", "with", "vs", "about", "latest", "best"}
)


def _query_tokens(text: str) -> set[str]:
    return {tok for tok in re.findall(r"[a-z0-9]+", (text or "").lower())
            if len(tok) >= 2 and tok not in _STOPWORDS}


def looks_relevant(query: str, results: Sequence[Any]) -> bool:
    """Cheap sanity check that a response actually answers the query.

    Some search endpoints degrade under automation from a given egress IP and
    answer with results for a *different* query. Those responses parse fine and
    are non-empty, so the only cheap discriminator is whether any result mentions
    a query term at all. A false negative (legit results with no literal query
    word) only costs a fallback to the next engine, so the check errs towards
    tolerance: one matching result is enough.
    """
    wanted = _query_tokens(query)
    if not wanted:
        return True
    for item in results:
        parts = (getattr(item, "title", ""), getattr(item, "url", ""),
                 getattr(item, "snippet", ""))
        haystack = " ".join(parts).lower()
        if any(token in haystack for token in wanted):
            return True
    return False


def available_engines(ctx: SearchContext) -> list[str]:
    """Names of engines whose collaborators are present in ``ctx``."""
    names: list[str] = []
    for name, factory in _ENGINE_FACTORIES.items():
        engine = factory()
        checker = getattr(engine, "is_available", None)
        if checker is None or checker(ctx):
            names.append(name)
    return names


def resolve_chain(engine: str, ctx: SearchContext, chain: Sequence[str] | None = None) -> list[str]:
    """Work out which engine names to try, in order.

    An explicit ``engine`` or ``chain`` is taken literally. ``auto`` follows the
    caller's ``engine_chain`` option, then settings, then the default order; in
    that automatic path ``bing`` is dropped because this network makes it answer
    with unrelated results (pass ``options["bing_in_auto"] = True`` to opt in).
    """
    if chain:
        return [_canonical(str(item)) for item in chain if str(item).strip()]
    requested = (engine or "auto").strip().lower()
    if requested and requested != "auto":
        return [_canonical(requested)]

    configured = ctx.options.get("engine_chain") if ctx.options else None
    if isinstance(configured, (list, tuple)) and configured:
        order = [_canonical(str(item)) for item in configured if str(item).strip()]
    else:
        order = [_canonical(item) for item in _settings_chain()]

    if not (ctx.options or {}).get("bing_in_auto"):
        order = [name for name in order if name != "bing"]
    return order


def search(
    ctx: SearchContext,
    request: SearchRequest,
    engine: str = "auto",
    chain: Sequence[str] | None = None,
) -> SearchResponse:
    """Run ``request`` against one engine or the fallback chain."""
    started = time.perf_counter()
    order = resolve_chain(engine, ctx, chain)
    if not order:
        raise SearchError(
            "no search engine configured",
            code="SEARCH_NO_ENGINE",
            hint="Pass engine='google'/'bing'/'gnews' or set an engine chain.",
        )

    warnings: list[str] = []
    for name in order:
        candidate = get_engine(name)
        try:
            response = candidate.search(request, ctx)
        except SearchError as exc:
            warnings.append(f"{name}: {exc.code}: {exc}")
            continue
        except Exception as exc:  # a buggy engine must not break the fallback
            warnings.append(f"{name}: {type(exc).__name__}: {exc}")
            continue

        if not response.results:
            warnings.append(f"{name}: returned no results")
            continue

        check = ctx.options.get("validate_relevance", True)
        if check and not looks_relevant(request.query, response.results):
            # Degraded endpoints answer non-empty but for a different query; a
            # silently wrong answer is worse than falling through to the next one.
            warnings.append(
                f"{name}: returned {len(response.results)} results unrelated to the query"
            )
            continue

        return SearchResponse(
            query=response.query or request.query,
            engine=response.engine or name,
            results=list(response.results),
            answer=response.answer,
            warnings=warnings + list(response.warnings or []),
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )

    tried = ", ".join(order)
    detail = "; ".join(warnings) if warnings else "no diagnostics captured"
    raise SearchError(
        f"all search engines failed ({tried}): {detail}",
        code="SEARCH_ALL_FAILED",
        hint="Check proxy/TLS settings and whether the headless browser is available, then retry.",
    )


def engines_for_debug() -> Iterable[str]:
    """Names of every registered engine (diagnostics helper)."""
    return tuple(sorted(_ENGINE_FACTORIES))
