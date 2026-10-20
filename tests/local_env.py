"""Local environment facts that must never enter the repository.

Everything an individual machine, company or account owns - the proxy host, the
absolute install paths, the profile directory - belongs in configuration, not in
committed files. This module reads that local configuration so tests and docs can
stay generic and still exercise real values.

Precedence for each value: environment variable, then `$LLMMCP_CONFIG` (or
`~/.llmmcp/config.json`), then the fallback used by the caller.

Example `~/.llmmcp/config.json`:

    {
      "test_proxy": "http://proxy.example.com:8080",
      "deploy_dir": "C:\\\\Users\\\\me\\\\.dsh\\\\profiles\\\\desktop\\\\deploy\\\\llmmcp"
    }

The file lives outside the repository, so `git status` never shows it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

CONFIG_ENV = "LLMMCP_CONFIG"
DEFAULT_CONFIG = Path.home() / ".llmmcp" / "config.json"

#: Environment variables read for each local value, in precedence order.
LOCAL_ENV_KEYS: dict[str, tuple[str, ...]] = {
    "test_proxy": ("LLMMCP_TEST_PROXY", "LLMMCP_PROXY", "HTTPS_PROXY", "HTTP_PROXY"),
    "deploy_dir": ("LLMMCP_DEPLOY_DIR",),
}


def _load_config_file() -> dict[str, object]:
    raw_path = os.environ.get(CONFIG_ENV)
    path = Path(raw_path).expanduser() if raw_path else DEFAULT_CONFIG
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def local_value(key: str) -> str | None:
    """Return one local value, or ``None`` when the machine has not set it."""

    for name in LOCAL_ENV_KEYS.get(key, (key.upper(),)):
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    config = _load_config_file()
    value = config.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def resolved_label(value: str | None) -> str:
    """Describe where a local value came from without printing the value itself.

    Tests assert on this label so a failure message stays free of machine names
    while still telling a maintainer whether they configured anything at all.
    """

    return "configured" if value else "not-configured"
