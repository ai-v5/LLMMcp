"""Independent MCP stdio smoke client.

Spawns an MCP server over stdio, performs the initialize handshake, lists tools
and optionally calls one tool. It intentionally speaks raw newline-delimited
JSON-RPC instead of using an SDK, so it verifies the wire protocol rather than a
client library.

Usage:
    python tools/mcp_smoke.py                       # list tools
    python tools/mcp_smoke.py --call web_fetch --args '{"url": "https://example.com"}'
    python tools/mcp_smoke.py --cmd "python -m llmmcp"
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

PROTOCOL_VERSION = "2024-11-05"


class StdioClient:
    def __init__(self, command: list[str]) -> None:
        environment = dict(os.environ)
        # The server must speak UTF-8 on its stdio channel even on a GBK console.
        environment["PYTHONIOENCODING"] = "utf-8"
        environment["PYTHONUTF8"] = "1"
        self.proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
            env=environment,
        )
        self.next_id = 0

    def request(self, method: str, params: dict | None = None) -> dict:
        self.next_id += 1
        request_id = self.next_id
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()
        assert self.proc.stdout is not None
        while True:
            line = self.proc.stdout.readline()
            if not line:
                stderr = self.proc.stderr.read() if self.proc.stderr else ""
                raise SystemExit(f"server closed stdout. stderr:\n{stderr[-4000:]}")
            line = line.strip()
            if not line:
                continue
            message = json.loads(line)
            if message.get("id") == request_id:
                if "error" in message:
                    raise SystemExit(f"JSON-RPC error on {method}: {message['error']}")
                return message["result"]

    def notify(self, method: str, params: dict | None = None) -> None:
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MCP stdio smoke client")
    parser.add_argument("--cmd", default=None, help="server command; default: current python -m llmmcp")
    parser.add_argument("--call", default=None, help="tool name to call after listing")
    parser.add_argument("--args", default="{}", help="JSON object passed as tool arguments")
    parser.add_argument(
        "--args-file",
        default=None,
        help="path to a UTF-8 JSON file with tool arguments (avoids shell quoting on Windows)",
    )
    parser.add_argument(
        "--timeout", type=float, default=180.0, help="informational tool-call budget in seconds"
    )
    options = parser.parse_args(argv)

    command = options.cmd.split() if options.cmd else [sys.executable, "-m", "llmmcp"]
    client = StdioClient(command)
    started = time.monotonic()
    try:
        init = client.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "llmmcp-smoke", "version": "0.1.0"},
            },
        )
        client.notify("notifications/initialized")
        print(f"initialize ok: server={init.get('serverInfo')} protocol={init.get('protocolVersion')}")
        tools = client.request("tools/list")["tools"]
        print(f"tools ({len(tools)}): {[tool['name'] for tool in tools]}")
        if options.call:
            raw_arguments = options.args
            if options.args_file:
                with open(options.args_file, encoding="utf-8-sig") as handle:
                    raw_arguments = handle.read()
            arguments = json.loads(raw_arguments)
            result = client.request("tools/call", {"name": options.call, "arguments": arguments})
            elapsed = time.monotonic() - started
            print(f"tools/call {options.call} ok in {elapsed:.2f}s, isError={result.get('isError', False)}")
            for block in result.get("content", []):
                if block.get("type") == "text":
                    text = block.get("text", "")
                    print(text[:4000])
            if "structuredContent" in result:
                print("structuredContent keys:", sorted(result["structuredContent"])[:12])
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
