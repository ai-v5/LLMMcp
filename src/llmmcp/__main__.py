"""``python -m llmmcp`` -> the CLI (stdio MCP server when no subcommand is given).

The MCP stdio transport is UTF-8 on the wire regardless of the console code page
(mcp 2.x re-wraps the binary buffers itself), but a GBK console would still break
any incidental text write, so stdio is forced to UTF-8 before we start.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence


def force_utf8_stdio() -> None:
    """Reconfigure stdin/stdout/stderr to UTF-8 when they are text streams."""

    for name in ("stdin", "stdout", "stderr"):
        stream = getattr(sys, name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:  # pragma: no cover - redirected/closed stream
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # pragma: no cover - not a reconfigurable text stream
            pass


def main(argv: Sequence[str] | None = None) -> int:
    from .cli import main as cli_main

    force_utf8_stdio()
    return cli_main(list(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    sys.exit(main())
