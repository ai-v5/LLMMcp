"""Command line entry point.

``python -m llmmcp`` (no arguments) starts the MCP server on stdio, which is what
an MCP client launches. The ``search``/``fetch``/``diagnose`` subcommands exist so
the same runtime can be exercised from a shell and print JSON.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from typing import Any

from . import __version__
from .fetch import fetch
from .models import LlmmcpError
from .search.base import SearchRequest
from .server import Runtime, build_server, diagnose

#: Exit code used when the requested operation failed (the payload is JSON).
EXIT_FAILED = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="llmmcp",
        description="LLMMcp: MCP server for web search and web fetch (proxy aware).",
    )
    parser.add_argument("--version", action="version", version=f"llmmcp {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("serve", help="run the MCP server on stdio (default)")

    search = subparsers.add_parser("search", help="run a web search and print JSON")
    search.add_argument("query", help="search query")
    search.add_argument("--max-results", "-n", type=int, default=8)
    search.add_argument("--engine", default="auto", help="auto (fallback chain) or an engine name")
    search.add_argument("--lang", default=None)
    search.add_argument("--region", default=None)
    search.add_argument("--recency", default=None)
    search.add_argument("--timeout", type=float, default=25.0)

    fetch_cmd = subparsers.add_parser("fetch", help="fetch a URL and print JSON")
    fetch_cmd.add_argument("url")
    fetch_cmd.add_argument("--format", "-f", default="markdown", choices=("markdown", "text", "html"))
    fetch_cmd.add_argument("--max-chars", type=int, default=100000)
    fetch_cmd.add_argument("--timeout", type=float, default=30.0)
    fetch_cmd.add_argument("--include-links", action="store_true")

    diag = subparsers.add_parser("diagnose", help="print proxy/TLS/browser/engine diagnostics")
    diag.add_argument("--no-probe", action="store_true", help="skip the live connectivity probe")

    return parser


def _use_utf8_stdout() -> None:
    """Make JSON output survive a GBK/cp1252 console (raise-on-encode is fatal)."""

    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # pragma: no cover - not a reconfigurable text stream
            pass


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def _fail(exc: LlmmcpError) -> int:
    print(json.dumps({"error": exc.to_dict()}, indent=2, ensure_ascii=False), file=sys.stderr)
    return EXIT_FAILED


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))

    if args.command is None or getattr(args, "command", None) == "serve":
        runtime = Runtime()
        server = build_server(runtime.settings, runtime.client)
        server.run("stdio")
        return 0

    _use_utf8_stdout()
    runtime = Runtime()

    if args.command == "search":
        return _cmd_search(runtime, args)
    if args.command == "fetch":
        return _cmd_fetch(runtime, args)
    if args.command == "diagnose":
        _print_json(diagnose(runtime, probe=not args.no_probe))
        return 0

    parser.print_help()  # pragma: no cover - argparse rejects unknown commands first
    return EXIT_FAILED


def _cmd_search(runtime: Runtime, args: argparse.Namespace) -> int:
    request = SearchRequest(
        query=args.query,
        max_results=max(1, int(args.max_results)),
        lang=args.lang,
        region=args.region,
        recency=args.recency,
        timeout=float(args.timeout),
    )
    try:
        from .search import chain  # noqa: PLC0415 - optional collaborator

        response = chain.search(
            runtime.context(),
            request,
            engine=args.engine,
            chain=list(runtime.settings.engine_chain),
        )
    except ImportError:
        print(
            json.dumps(
                {
                    "error": {
                        "code": "SEARCH_UNAVAILABLE",
                        "message": "llmmcp.search.chain is not installed",
                        "hint": "web_fetch and diagnose work without the search engines",
                    }
                },
                indent=2,
            ),
            file=sys.stderr,
        )
        return EXIT_FAILED
    except LlmmcpError as exc:
        return _fail(exc)
    _print_json(response.to_dict())
    return 0


def _cmd_fetch(runtime: Runtime, args: argparse.Namespace) -> int:
    try:
        response = fetch(
            runtime.client,
            args.url,
            fmt=args.format,
            max_chars=int(args.max_chars),
            timeout=float(args.timeout),
            include_links=bool(args.include_links),
        )
    except LlmmcpError as exc:
        return _fail(exc)
    _print_json(response.to_dict())
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
