"""Configuration resolution for LLMMcp, including the corporate proxy.

One place decides *where* settings come from so every other module stays pure:

* proxy: ``LLMMCP_PROXY`` -> ``HTTPS_PROXY``/``HTTP_PROXY``/``ALL_PROXY`` ->
  config file (``$LLMMCP_CONFIG`` or ``~/.llmmcp/config.json``) -> Windows
  Internet Settings (the IE/WinHTTP proxy).
* ``NO_PROXY``: ``LLMMCP_NO_PROXY`` -> ``NO_PROXY``/``no_proxy`` -> config file
  -> the IE ``ProxyOverride`` list.
* TLS verification: ``truststore`` (default, uses the Windows certificate
  store), ``certifi`` or ``off``.

Every scalar setting can be overridden by an ``LLMMCP_*`` environment variable,
then by the JSON config file, then by the built-in default.

The proxy URL may carry credentials (``http://user:secret@host:port``); the raw
value is only ever handed to the HTTP layer. Anything meant for humans or logs
goes through :func:`redact_url`, which keeps nothing but scheme, host and port.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

# A current desktop Chrome UA: Google answers ordinary desktop UAs far more
# willingly than a "python-requests" one.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

DEFAULT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_TIMEOUT = 30.0
DEFAULT_BROWSER_TIMEOUT = 45.0
DEFAULT_RETRIES = 2
DEFAULT_BACKOFF = 0.5
DEFAULT_GOOGLE_HOST = "www.google.com"
DEFAULT_ENGINE_CHAIN = ("google", "bing", "gnews")
DEFAULT_PROBE_URL = "https://example.com"

TLS_MODES = ("truststore", "certifi", "off")

CONFIG_ENV = "LLMMCP_CONFIG"
DEFAULT_CONFIG_PATH = "~/.llmmcp/config.json"

#: Environment variables consulted for the proxy, after ``LLMMCP_PROXY``.
PROXY_ENV_VARS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")

_SCHEME_OF_ENV_VAR = {
    "HTTPS_PROXY": "https",
    "https_proxy": "https",
    "HTTP_PROXY": "http",
    "http_proxy": "http",
    "ALL_PROXY": "all",
    "all_proxy": "all",
}


# ---------------------------------------------------------------------------
# redaction helpers (the only proxy representation allowed to leave the process)
# ---------------------------------------------------------------------------
def redact_url(url: str | None) -> str | None:
    """Return ``scheme://host:port`` for *url*, dropping userinfo, path and query."""

    if url is None:
        return None
    candidate = str(url).strip()
    if not candidate:
        return None
    if "://" not in candidate:
        # Windows Internet Settings store bare ``host:port`` pairs.
        candidate = "//" + candidate
    try:
        parts = urlsplit(candidate)
        host = parts.hostname or ""
        port = parts.port
    except ValueError:
        return "<unparsable>"
    if not host:
        return "<unparsable>"
    scheme = parts.scheme or "http"
    return f"{scheme}://{host}" + (f":{port}" if port else "")


def redact_no_proxy(value: str | None) -> str | None:
    """Normalise a NO_PROXY string into a comma separated list (``;`` accepted)."""

    if not value:
        return None
    items = [item.strip() for item in str(value).replace(";", ",").split(",")]
    items = [item for item in items if item]
    return ",".join(items) if items else None


@dataclass(frozen=True)
class ProxySettings:
    """Resolved proxy configuration.

    ``proxies`` maps a URL scheme (``http``/``https``) to a proxy URL; those
    values may contain credentials and must not be printed. ``source`` names
    where the configuration came from and is safe to show.
    """

    proxies: Mapping[str, str] = field(default_factory=dict)
    source: str = "none"
    no_proxy: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.proxies)

    @property
    def raw(self) -> str | None:
        for scheme in ("https", "http", "all"):
            value = self.proxies.get(scheme)
            if value:
                return value
        return None

    def label(self) -> str | None:
        """Human readable, credential-free description of the effective proxy."""

        url = redact_url(self.raw)
        if url is None:
            return None
        return f"{url} (source={self.source})"


@dataclass(frozen=True)
class Settings:
    """Immutable, fully resolved runtime configuration."""

    proxy: ProxySettings = field(default_factory=ProxySettings)
    tls: str = "truststore"
    timeout: float = DEFAULT_TIMEOUT
    max_bytes: int = DEFAULT_MAX_BYTES
    user_agent: str = DEFAULT_USER_AGENT
    retries: int = DEFAULT_RETRIES
    backoff: float = DEFAULT_BACKOFF
    browser_path: str | None = None
    browser_timeout: float = DEFAULT_BROWSER_TIMEOUT
    browser_virtual_time_budget: int = 20000
    engine_chain: tuple[str, ...] = DEFAULT_ENGINE_CHAIN
    google_host: str = DEFAULT_GOOGLE_HOST
    probe_url: str = DEFAULT_PROBE_URL
    diagnose_probe: bool = True
    config_path: str | None = None
    config_loaded: bool = False

    # -- derived helpers -------------------------------------------------
    @property
    def proxy_label(self) -> str | None:
        return self.proxy.label()

    def browser_timeout_for(self, timeout: float | None) -> float:
        """Browser render timeout, defaulting to :attr:`browser_timeout`."""

        if timeout is None:
            return self.browser_timeout
        return max(1.0, float(timeout))

    def public_dict(self) -> dict[str, Any]:
        """Credential-free snapshot used by ``web_diagnose``."""

        return {
            "proxy": {
                "enabled": self.proxy.enabled,
                "label": self.proxy.label(),
                "source": self.proxy.source,
                "schemes": sorted(self.proxy.proxies),
                "no_proxy": self.proxy.no_proxy,
            },
            "tls": self.tls,
            "timeout": self.timeout,
            "max_bytes": self.max_bytes,
            "retries": self.retries,
            "user_agent": self.user_agent,
            "browser_path": self.browser_path,
            "browser_timeout": self.browser_timeout,
            "engine_chain": list(self.engine_chain),
            "google_host": self.google_host,
            "config_path": self.config_path,
            "config_loaded": self.config_loaded,
        }


# ---------------------------------------------------------------------------
# config file
# ---------------------------------------------------------------------------
def default_config_path(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    raw = env.get(CONFIG_ENV) or DEFAULT_CONFIG_PATH
    return Path(os.path.expanduser(str(raw)))


def load_config_file(path: Path | None, *, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Read the JSON config file. A missing file is not an error."""

    target = Path(path) if path is not None else default_config_path(environ)
    try:
        with target.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:  # unreadable or invalid JSON
        raise ValueError(f"cannot read config file {target}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"config file {target} must contain a JSON object")
    return data


# ---------------------------------------------------------------------------
# proxy sources
# ---------------------------------------------------------------------------
def env_proxy(environ: Mapping[str, str]) -> tuple[dict[str, str], list[str]]:
    """Collect proxy URLs from the standard environment variables."""

    proxies: dict[str, str] = {}
    used: list[str] = []
    for name in PROXY_ENV_VARS:
        value = environ.get(name)
        if not value or not str(value).strip():
            continue
        url = str(value).strip()
        scheme = _SCHEME_OF_ENV_VAR[name]
        if scheme == "all":
            proxies.setdefault("http", url)
            proxies.setdefault("https", url)
        else:
            proxies.setdefault(scheme, url)
        used.append(name)
    return proxies, used


def config_proxy(config: Mapping[str, Any]) -> tuple[dict[str, str], str | None]:
    """Read the ``proxy``/``no_proxy`` keys of the config file."""

    value = config.get("proxy")
    no_proxy = config.get("no_proxy")
    no_proxy = no_proxy if isinstance(no_proxy, str) and no_proxy.strip() else None

    if isinstance(value, str) and value.strip():
        url = value.strip()
        return {"http": url, "https": url}, no_proxy

    if isinstance(value, Mapping):
        proxies: dict[str, str] = {}
        for scheme in ("http", "https", "all"):
            item = value.get(scheme)
            if isinstance(item, str) and item.strip():
                url = item.strip()
                if scheme == "all":
                    proxies.setdefault("http", url)
                    proxies.setdefault("https", url)
                else:
                    proxies[scheme] = url
        nested = value.get("no_proxy")
        if isinstance(nested, str) and nested.strip():
            no_proxy = nested
        return proxies, no_proxy

    return {}, no_proxy


def windows_ie_proxy(*, registry: Any = None) -> dict[str, Any] | None:
    """Read the WinINET proxy settings for the current user.

    Returns ``None`` when the setting is unavailable or disabled. The result has
    ``http``/``https`` keys (``scheme://host:port``), ``no_proxy`` from
    ``ProxyOverride`` and ``auto_config_url`` when a PAC file is configured (PAC
    is *not* resolved here, only reported).
    """

    if registry is None:
        if os.name != "nt":
            return None
        import winreg  # noqa: PLC0415  (Windows-only import)

        registry = winreg
    try:
        key = registry.OpenKey(
            registry.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        )
    except OSError:  # pragma: no cover - registry unavailable
        return None
    try:
        try:
            enabled = bool(registry.QueryValueEx(key, "ProxyEnable")[0])
        except OSError:
            enabled = False
        try:
            server = registry.QueryValueEx(key, "ProxyServer")[0]
        except OSError:
            server = None
        try:
            override = registry.QueryValueEx(key, "ProxyOverride")[0]
        except OSError:
            override = None
        try:
            auto_config = registry.QueryValueEx(key, "AutoConfigURL")[0]
        except OSError:
            auto_config = None
    finally:
        try:
            registry.CloseKey(key)
        except Exception:  # pragma: no cover - best effort
            pass

    auto_url = str(auto_config) if auto_config else None
    if not enabled or not server:
        if enabled and auto_url:
            return {"http": None, "https": None, "no_proxy": None, "auto_config_url": auto_url}
        return None

    server = str(server).strip()
    proxies: dict[str, str] = {}
    if "=" in server:
        for entry in server.split(";"):
            if "=" not in entry:
                continue
            scheme, _, address = entry.partition("=")
            scheme = scheme.strip().lower()
            address = address.strip()
            if not address or scheme not in ("http", "https"):
                continue
            proxies[scheme] = address if "://" in address else f"http://{address}"
    else:
        url = server if "://" in server else f"http://{server}"
        proxies["http"] = url
        proxies["https"] = url

    if not proxies:
        return None

    no_proxy = None
    if isinstance(override, str) and override.strip():
        items = [item.strip() for item in override.replace(";", ",").split(",") if item.strip()]
        # ``<local>`` means "bypass for intranet hosts"; requests cannot express
        # that, so it is dropped rather than mistaken for a host pattern.
        items = [item for item in items if item != "<local>"]
        no_proxy = ",".join(items) or None

    result: dict[str, Any] = {
        "http": proxies.get("http"),
        "https": proxies.get("https"),
        "no_proxy": no_proxy,
    }
    if auto_url:
        result["auto_config_url"] = auto_url
    return result


def _resolve_no_proxy(
    environ: Mapping[str, str],
    config: Mapping[str, Any],
    system_no_proxy: Callable[[], str | None],
) -> str | None:
    for name in ("LLMMCP_NO_PROXY", "NO_PROXY", "no_proxy"):
        value = environ.get(name)
        if value and str(value).strip():
            return redact_no_proxy(str(value))
    _, config_no_proxy = config_proxy(config)
    if config_no_proxy:
        return redact_no_proxy(config_no_proxy)
    return redact_no_proxy(system_no_proxy())


def resolve_proxy(
    environ: Mapping[str, str],
    config: Mapping[str, Any],
    *,
    use_system_proxy: bool = True,
    ie_reader: Callable[[], dict[str, Any] | None] | None = None,
) -> ProxySettings:
    """Apply the documented proxy precedence and return the chosen proxy."""

    cache: dict[str, dict[str, Any] | None] = {}

    def system() -> dict[str, Any] | None:
        # Read the registry lazily: only when nothing else provided a value.
        if "value" not in cache:
            value: dict[str, Any] | None = None
            if use_system_proxy:
                reader = ie_reader or windows_ie_proxy
                try:
                    value = reader()
                except Exception:  # pragma: no cover - registry failure must not break startup
                    value = None
            cache["value"] = value
        return cache["value"]

    def system_no_proxy() -> str | None:
        settings = system()
        return settings.get("no_proxy") if settings else None

    explicit = environ.get("LLMMCP_PROXY")
    if explicit and str(explicit).strip():
        url = str(explicit).strip()
        return ProxySettings(
            proxies={"http": url, "https": url},
            source="LLMMCP_PROXY",
            no_proxy=_resolve_no_proxy(environ, config, system_no_proxy),
        )

    proxies, used = env_proxy(environ)
    if proxies:
        # A single corporate proxy serves both schemes; without this, URLs using
        # the other scheme would silently bypass the proxy the environment
        # advertises (requests only honours the exact scheme key).
        if "http" not in proxies and "https" in proxies:
            proxies["http"] = proxies["https"]
        if "https" not in proxies and "http" in proxies:
            proxies["https"] = proxies["http"]
        return ProxySettings(
            proxies=proxies,
            source="env:" + "+".join(used),
            no_proxy=_resolve_no_proxy(environ, config, system_no_proxy),
        )

    configured, _ = config_proxy(config)
    if configured:
        return ProxySettings(
            proxies=configured,
            source="config",
            no_proxy=_resolve_no_proxy(environ, config, system_no_proxy),
        )

    settings = system()
    if settings:
        proxies = {
            scheme: settings[scheme]
            for scheme in ("http", "https")
            if isinstance(settings.get(scheme), str) and settings.get(scheme)
        }
        if proxies:
            return ProxySettings(
                proxies=proxies,
                source="windows-ie",
                no_proxy=_resolve_no_proxy(environ, config, system_no_proxy),
            )

    return ProxySettings(proxies={}, source="none", no_proxy=_resolve_no_proxy(environ, config, lambda: None))


# ---------------------------------------------------------------------------
# scalar settings
# ---------------------------------------------------------------------------
def _pick(
    environ: Mapping[str, str], config: Mapping[str, Any], env_name: str, key: str, default: Any
) -> Any:
    value = environ.get(env_name)
    if value is not None and str(value).strip() != "":
        return value
    if key in config and config[key] is not None:
        return config[key]
    return default


def _as_float(value: Any, key: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"settings: {key} must be a number, got {value!r}") from exc
    if number <= 0:
        raise ValueError(f"settings: {key} must be positive, got {number}")
    return number


def _as_int(value: Any, key: str, *, minimum: int = 0) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"settings: {key} must be an integer, got {value!r}") from exc
    if number < minimum:
        raise ValueError(f"settings: {key} must be >= {minimum}, got {number}")
    return number


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    return default


def _as_str_list(value: Any) -> tuple[str, ...]:
    if value is None:
        return DEFAULT_ENGINE_CHAIN
    if isinstance(value, str):
        items = [item.strip() for item in value.split(",")]
    elif isinstance(value, (list, tuple)):
        items = [str(item).strip() for item in value]
    else:
        raise ValueError(f"settings: engine_chain must be a list or comma separated string, got {value!r}")
    items = [item for item in items if item]
    return tuple(items) if items else DEFAULT_ENGINE_CHAIN


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def load_settings(
    *,
    environ: Mapping[str, str] | None = None,
    config: Mapping[str, Any] | None = None,
    config_path: str | os.PathLike[str] | None = None,
    use_system_proxy: bool = True,
    ie_reader: Callable[[], dict[str, Any] | None] | None = None,
) -> Settings:
    """Resolve every setting, applying environment -> config file -> default.

    ``config`` and ``ie_reader`` exist so tests (and ``diagnose``) can inject
    values without touching the real environment or the registry.
    """

    env = dict(os.environ if environ is None else environ)
    if config is None:
        target = Path(config_path) if config_path is not None else default_config_path(env)
        exists = target.is_file()
        config = load_config_file(target, environ=env)
        loaded_path: str | None = str(target)
        loaded = exists
    else:
        config = dict(config)
        loaded_path = str(config_path) if config_path is not None else None
        loaded = True

    proxy = resolve_proxy(env, config, use_system_proxy=use_system_proxy, ie_reader=ie_reader)

    tls_raw = str(_pick(env, config, "LLMMCP_TLS", "tls", "truststore")).strip().lower()
    if tls_raw not in TLS_MODES:
        raise ValueError(f"settings: tls must be one of {', '.join(TLS_MODES)}, got {tls_raw!r}")

    return Settings(
        proxy=proxy,
        tls=tls_raw,
        timeout=_as_float(_pick(env, config, "LLMMCP_TIMEOUT", "timeout", DEFAULT_TIMEOUT), "timeout"),
        max_bytes=_as_int(
            _pick(env, config, "LLMMCP_MAX_BYTES", "max_bytes", DEFAULT_MAX_BYTES), "max_bytes", minimum=1
        ),
        user_agent=str(_pick(env, config, "LLMMCP_USER_AGENT", "user_agent", DEFAULT_USER_AGENT)).strip()
        or DEFAULT_USER_AGENT,
        retries=_as_int(
            _pick(env, config, "LLMMCP_RETRIES", "retries", DEFAULT_RETRIES), "retries", minimum=0
        ),
        backoff=_as_float(_pick(env, config, "LLMMCP_BACKOFF", "backoff", DEFAULT_BACKOFF), "backoff"),
        browser_path=_optional_str(_pick(env, config, "LLMMCP_BROWSER", "browser_path", None)),
        browser_timeout=_as_float(
            _pick(env, config, "LLMMCP_BROWSER_TIMEOUT", "browser_timeout", DEFAULT_BROWSER_TIMEOUT),
            "browser_timeout",
        ),
        browser_virtual_time_budget=_as_int(
            _pick(env, config, "LLMMCP_BROWSER_VIRTUAL_TIME_BUDGET", "browser_virtual_time_budget", 20000),
            "browser_virtual_time_budget",
            minimum=1000,
        ),
        engine_chain=_as_str_list(_pick(env, config, "LLMMCP_ENGINES", "engine_chain", None)),
        google_host=str(_pick(env, config, "LLMMCP_GOOGLE_HOST", "google_host", DEFAULT_GOOGLE_HOST)).strip()
        or DEFAULT_GOOGLE_HOST,
        probe_url=str(_pick(env, config, "LLMMCP_PROBE_URL", "probe_url", DEFAULT_PROBE_URL)).strip()
        or DEFAULT_PROBE_URL,
        diagnose_probe=_as_bool(_pick(env, config, "LLMMCP_DIAGNOSE_PROBE", "diagnose_probe", None), True),
        config_path=loaded_path,
        config_loaded=loaded,
    )
