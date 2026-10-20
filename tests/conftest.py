"""Test isolation: the ambient proxy environment must not leak into assertions.

Developers and agents often export ``HTTPS_PROXY`` for the shell while working in
this repository. Every settings test states its own environment, so the ambient
one is removed before each test instead of letting it change the outcome.
"""

from __future__ import annotations

import pytest

PROXY_ENV = (
    "LLMMCP_PROXY",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "https_proxy",
    "http_proxy",
    "all_proxy",
    "no_proxy",
)


@pytest.fixture(autouse=True)
def isolate_proxy_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove proxy variables so each test starts from a known environment."""

    for name in PROXY_ENV:
        monkeypatch.delenv(name, raising=False)
