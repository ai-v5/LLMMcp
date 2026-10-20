"""Offline tests for proxy/settings resolution (no registry, no network)."""

from __future__ import annotations

import json

import pytest

from llmmcp.settings import (
    DEFAULT_ENGINE_CHAIN,
    ProxySettings,
    load_settings,
    redact_no_proxy,
    redact_url,
    windows_ie_proxy,
)


class FakeRegistry:
    """Minimal stand-in for ``winreg`` (values default to missing)."""

    HKEY_CURRENT_USER = object()

    def __init__(self, values: dict[str, object]) -> None:
        self.values = values
        self.closed = False

    def OpenKey(self, root: object, path: str) -> str:  # noqa: N802 - winreg spelling
        assert root is self.HKEY_CURRENT_USER
        assert "Internet Settings" in path
        return "handle"

    def QueryValueEx(self, key: str, name: str) -> tuple[object, int]:  # noqa: N802
        if name not in self.values:
            raise FileNotFoundError(name)
        return self.values[name], 1

    def CloseKey(self, key: str) -> None:  # noqa: N802
        self.closed = True


def base_kwargs(**overrides):
    kwargs = {"environ": {}, "config": {}, "use_system_proxy": False}
    kwargs.update(overrides)
    return kwargs


# ---------------------------------------------------------------------------
# defaults and scalar overrides
# ---------------------------------------------------------------------------
def test_defaults_without_proxy():
    settings = load_settings(**base_kwargs())
    assert settings.proxy.enabled is False
    assert settings.proxy.source == "none"
    assert settings.proxy.label() is None
    assert settings.tls == "truststore"
    assert settings.timeout == 30.0
    assert settings.max_bytes == 5 * 1024 * 1024
    assert settings.engine_chain == DEFAULT_ENGINE_CHAIN
    assert settings.google_host == "www.google.com"
    assert settings.browser_path is None
    assert settings.retries == 2


def test_env_overrides_config_and_defaults():
    settings = load_settings(
        environ={"LLMMCP_TIMEOUT": "7.5", "LLMMCP_ENGINES": "bing, google_news", "LLMMCP_TLS": "certifi"},
        config={"timeout": 99, "engine_chain": ["gnews"], "tls": "truststore"},
        use_system_proxy=False,
    )
    assert settings.timeout == 7.5
    assert settings.engine_chain == ("bing", "google_news")
    assert settings.tls == "certifi"


def test_config_file_is_loaded(tmp_path):
    config_file = tmp_path / "config.json"
    config_file.write_text(
        json.dumps({"proxy": "http://cfg-proxy:9000", "timeout": 12, "engine_chain": ["bing"]}),
        encoding="utf-8",
    )
    settings = load_settings(environ={"LLMMCP_CONFIG": str(config_file)}, use_system_proxy=False)
    assert settings.config_loaded is True
    assert settings.config_path == str(config_file)
    assert settings.proxy.source == "config"
    assert settings.proxy.raw == "http://cfg-proxy:9000"
    assert settings.timeout == 12.0
    assert settings.engine_chain == ("bing",)


def test_missing_config_file_is_not_an_error(tmp_path):
    settings = load_settings(environ={"LLMMCP_CONFIG": str(tmp_path / "nope.json")}, use_system_proxy=False)
    assert settings.config_loaded is False
    assert settings.proxy.source == "none"


def test_invalid_tls_mode_rejected():
    with pytest.raises(ValueError, match="tls"):
        load_settings(**base_kwargs(environ={"LLMMCP_TLS": "bogus"}))


# ---------------------------------------------------------------------------
# proxy precedence
# ---------------------------------------------------------------------------
def test_llmmcp_proxy_wins_over_everything():
    settings = load_settings(
        environ={"LLMMCP_PROXY": "http://user:secret@explicit:8080", "HTTPS_PROXY": "http://env-proxy:3128"},
        config={"proxy": "http://cfg-proxy:9000"},
        ie_reader=lambda: {"http": "http://ie-proxy:8888", "https": "http://ie-proxy:8888"},
    )
    assert settings.proxy.source == "LLMMCP_PROXY"
    assert settings.proxy.raw == "http://user:secret@explicit:8080"
    # Both schemes go through the same corporate proxy.
    assert settings.proxy.proxies == {
        "http": "http://user:secret@explicit:8080",
        "https": "http://user:secret@explicit:8080",
    }
    # The label must never leak credentials.
    assert settings.proxy.label() == "http://explicit:8080 (source=LLMMCP_PROXY)"
    assert "secret" not in settings.proxy.label()
    assert "user:" not in settings.proxy.label()


def test_env_proxy_beats_config_and_registry():
    settings = load_settings(
        environ={"HTTPS_PROXY": "http://env-proxy:3128"},
        config={"proxy": "http://cfg-proxy:9000"},
        ie_reader=lambda: {"http": "http://ie-proxy:8888", "https": "http://ie-proxy:8888"},
    )
    assert settings.proxy.source == "env:HTTPS_PROXY"
    # A single https proxy is assumed to serve http too.
    assert settings.proxy.proxies == {"https": "http://env-proxy:3128", "http": "http://env-proxy:3128"}


def test_env_proxy_combines_schemes():
    settings = load_settings(
        **base_kwargs(environ={"HTTP_PROXY": "http://plain:8080", "HTTPS_PROXY": "http://secure:8443"})
    )
    assert settings.proxy.source == "env:HTTPS_PROXY+HTTP_PROXY"
    assert settings.proxy.proxies == {"http": "http://plain:8080", "https": "http://secure:8443"}


def test_all_proxy_fills_both_schemes():
    settings = load_settings(**base_kwargs(environ={"ALL_PROXY": "socks5://all:1080"}))
    assert settings.proxy.source == "env:ALL_PROXY"
    assert settings.proxy.proxies == {"http": "socks5://all:1080", "https": "socks5://all:1080"}


def test_config_proxy_used_when_env_is_empty():
    settings = load_settings(
        config={"proxy": {"https": "http://cfg-secure:8443"}},
        ie_reader=lambda: {"http": "http://ie:1", "https": "http://ie:1"},
    )
    assert settings.proxy.source == "config"
    assert settings.proxy.proxies == {"https": "http://cfg-secure:8443"}


def test_windows_ie_proxy_is_last_resort():
    settings = load_settings(
        use_system_proxy=True,
        ie_reader=lambda: {"http": "http://ie-proxy:8888", "https": "http://ie-proxy:8888"},
    )
    assert settings.proxy.source == "windows-ie"
    assert settings.proxy.raw == "http://ie-proxy:8888"


def test_no_system_proxy_when_disabled():
    settings = load_settings(
        **base_kwargs(
            use_system_proxy=False, ie_reader=lambda: {"http": "http://ie:1", "https": "http://ie:1"}
        )
    )
    assert settings.proxy.enabled is False
    assert settings.proxy.source == "none"


# ---------------------------------------------------------------------------
# NO_PROXY
# ---------------------------------------------------------------------------
def test_no_proxy_precedence():
    config = {"no_proxy": "config.example"}
    system = {"http": "http://p:1", "https": "http://p:1", "no_proxy": "ie.example"}

    verdicts = load_settings(
        environ={"LLMMCP_NO_PROXY": "a.example"}, config=config, ie_reader=lambda: system
    )
    assert verdicts.proxy.no_proxy == "a.example"

    env_only = load_settings(
        environ={"NO_PROXY": "b.example; c.example"}, config=config, ie_reader=lambda: system
    )
    assert env_only.proxy.no_proxy == "b.example,c.example"

    config_only = load_settings(environ={}, config=config, ie_reader=lambda: system)
    assert config_only.proxy.no_proxy == "config.example"

    ie_only = load_settings(environ={}, config={}, ie_reader=lambda: system)
    assert ie_only.proxy.no_proxy == "ie.example"

    nothing = load_settings(**base_kwargs())
    assert nothing.proxy.no_proxy is None


def test_redact_no_proxy_normalises_separators():
    assert redact_no_proxy("a;b, c") == "a,b,c"
    assert redact_no_proxy("") is None
    assert redact_no_proxy(None) is None


# ---------------------------------------------------------------------------
# redaction
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("http://user:pw@proxy.corp:8080", "http://proxy.corp:8080"),
        ("https://proxy.corp:8443/some/path?q=1", "https://proxy.corp:8443"),
        ("proxy.corp:8080", "http://proxy.corp:8080"),
        ("http://proxy.corp", "http://proxy.corp"),
        ("", None),
        (None, None),
    ],
)
def test_redact_url(raw, expected):
    assert redact_url(raw) == expected


def test_proxy_settings_label_hides_userinfo():
    proxy = ProxySettings(proxies={"https": "http://alice:topsecret@gateway:8080"}, source="env:HTTPS_PROXY")
    assert proxy.label() == "http://gateway:8080 (source=env:HTTPS_PROXY)"
    assert "alice" not in proxy.label()
    assert "topsecret" not in proxy.label()


def test_public_dict_is_credential_free():
    settings = load_settings(
        environ={"LLMMCP_PROXY": "http://alice:topsecret@gateway:8080"}, config={}, use_system_proxy=False
    )
    dumped = json.dumps(settings.public_dict())
    assert "topsecret" not in dumped
    assert "alice" not in dumped
    assert "gateway:8080" in dumped


# ---------------------------------------------------------------------------
# Windows Internet Settings parsing
# ---------------------------------------------------------------------------
def test_windows_ie_proxy_per_scheme():
    registry = FakeRegistry(
        {
            "ProxyEnable": 1,
            "ProxyServer": "http=proxy.corp:8081;https=proxy.corp:8443",
            "ProxyOverride": "<local>;*.corp.example",
        }
    )
    result = windows_ie_proxy(registry=registry)
    assert result == {
        "http": "http://proxy.corp:8081",
        "https": "http://proxy.corp:8443",
        "no_proxy": "*.corp.example",
    }
    assert registry.closed is True


def test_windows_ie_proxy_single_endpoint():
    result = windows_ie_proxy(registry=FakeRegistry({"ProxyEnable": 1, "ProxyServer": "proxy.corp:8080"}))
    assert result["http"] == "http://proxy.corp:8080"
    assert result["https"] == "http://proxy.corp:8080"
    assert result["no_proxy"] is None


def test_windows_ie_proxy_disabled_or_missing():
    assert windows_ie_proxy(registry=FakeRegistry({"ProxyEnable": 0, "ProxyServer": "proxy:8080"})) is None
    assert windows_ie_proxy(registry=FakeRegistry({"ProxyEnable": 1})) is None


def test_windows_ie_proxy_reports_pac_without_resolving_it():
    result = windows_ie_proxy(
        registry=FakeRegistry({"ProxyEnable": 1, "AutoConfigURL": "http://pac.corp/proxy.pac"})
    )
    assert result == {
        "http": None,
        "https": None,
        "no_proxy": None,
        "auto_config_url": "http://pac.corp/proxy.pac",
    }
