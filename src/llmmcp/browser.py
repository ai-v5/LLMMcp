"""Headless Chrome/Edge rendering for pages that need JavaScript.

Google's results page answers plain HTTP clients with a JS gate, so a real
browser is the only reliable way to read it. :func:`render` runs the locally
installed Chrome or Edge in ``--headless=new --dump-dom`` mode and returns the
settled DOM.

Operational details that matter on Windows:

* each run gets a throwaway ``--user-data-dir``: a shared profile is locked by
  the first browser instance and the next one exits with code 13;
* the child is spawned with ``CREATE_NO_WINDOW`` so no console window flashes;
* the process is killed on timeout (and on artifact cleanup);
* ``--proxy-server`` is only passed when a proxy was resolved, and it never
  carries credentials (Chromium has no way to use them from the CLI).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .models import LlmmcpError
from .settings import Settings, load_settings, redact_url

#: Sanity limit for the DOM we are willing to return (the Google page is ~2 MB).
MAX_DOM_BYTES = 32 * 1024 * 1024

CANDIDATE_PATHS = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
    r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe",
)

BASE_FLAGS = (
    "--headless=new",
    "--disable-gpu",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-sync",
    "--mute-audio",
    "--hide-scrollbars",
)

#: Process creation flags: never flash a console window on Windows.
CREATE_NO_WINDOW = 0x08000000


def _creation_flags() -> int:
    return CREATE_NO_WINDOW if os.name == "nt" else 0


def candidate_paths() -> tuple[str, ...]:
    """Browser binaries checked, in order, when no path is configured."""

    expanded = []
    for template in CANDIDATE_PATHS:
        path = os.path.expandvars(template)
        if path and "%" not in path:
            expanded.append(path)
    return tuple(expanded)


def find_browser(settings: Settings | None = None) -> str | None:
    """Return the browser executable to drive, or ``None`` when none is usable."""

    override = settings.browser_path if settings is not None else None
    if override:
        return override if Path(override).is_file() else None
    for path in candidate_paths():
        if Path(path).is_file():
            return path
    return None


def browser_info(settings: Settings | None = None) -> dict[str, Any]:
    """Credential-free browser status for ``web_diagnose``."""

    settings = settings or load_settings()
    path = find_browser(settings)
    proxy = browser_proxy(settings.proxy.raw)
    return {
        "path": path,
        "available": path is not None,
        "configured_path": settings.browser_path,
        "candidates": list(candidate_paths()),
        "proxy_arg": proxy,
        "timeout": settings.browser_timeout,
        "headless": "--headless=new",
    }


def browser_proxy(proxy_url: str | None) -> str | None:
    """The ``--proxy-server`` value for *proxy_url*: scheme/host/port only."""

    return redact_url(proxy_url)


def build_command(
    browser: str,
    url: str,
    *,
    profile_dir: str,
    proxy: str | None = None,
    user_agent: str | None = None,
    virtual_time_budget: int | None = None,
    extra_args: Sequence[str] = (),
) -> list[str]:
    """Build the headless ``--dump-dom`` command line (pure, easy to test)."""

    command = [browser, *BASE_FLAGS, f"--user-data-dir={profile_dir}"]
    if user_agent:
        command.append(f"--user-agent={user_agent}")
    proxy_value = browser_proxy(proxy)
    if proxy_value:
        command.append(f"--proxy-server={proxy_value}")
    if virtual_time_budget:
        command.append(f"--virtual-time-budget={int(virtual_time_budget)}")
    command.extend(extra_args)
    command.extend(["--dump-dom", url])
    return command


def render(url: str, timeout: float | None = None, *, settings: Settings | None = None) -> str:
    """Render *url* in a headless browser and return the settled DOM as HTML.

    Raises :class:`~llmmcp.models.LlmmcpError` with ``code=BROWSER_NOT_FOUND``,
    ``BROWSER_TIMEOUT`` or ``BROWSER_FAILED``.
    """

    settings = settings or load_settings()
    budget = settings.browser_timeout_for(timeout)
    browser = find_browser(settings)
    if browser is None:
        raise LlmmcpError(
            "no Chrome or Edge executable found",
            code="BROWSER_NOT_FOUND",
            hint="install Chrome/Edge or set LLMMCP_BROWSER to the executable path",
        )

    last_error: LlmmcpError | None = None
    for attempt in range(2):
        profile_dir = tempfile.mkdtemp(prefix="llmmcp-browser-")
        command = build_command(
            browser,
            url,
            profile_dir=profile_dir,
            proxy=settings.proxy.raw,
            user_agent=settings.user_agent,
            virtual_time_budget=settings.browser_virtual_time_budget,
        )
        try:
            returncode, stdout, stderr = _invoke(command, budget)
            dom = stdout.decode("utf-8", "replace")
            if _looks_like_dom(dom):
                return dom
            last_error = LlmmcpError(
                f"headless browser produced no DOM for {url} (exit={returncode})",
                code="BROWSER_FAILED",
                hint=_stderr_hint(stderr),
            )
        except LlmmcpError as exc:
            # A timeout is not worth doubling the wall clock for; the profile
            # lock ("exit 13") and transient startup failures are.
            if exc.code in ("BROWSER_NOT_FOUND", "BROWSER_TIMEOUT"):
                raise
            last_error = exc
        finally:
            shutil.rmtree(profile_dir, ignore_errors=True)
        if attempt == 0:
            continue
    assert last_error is not None  # noqa: S101 - loop invariant
    raise last_error


def _invoke(command: Sequence[str], timeout: float) -> tuple[int, bytes, bytes]:
    """Run the browser silently, killing the process tree on timeout."""

    process = subprocess.Popen(  # noqa: S603 - fixed binary, argument list, no shell
        list(command),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        creationflags=_creation_flags(),
        close_fds=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(process)
        try:
            process.communicate(timeout=10)
        except Exception:  # pragma: no cover - best effort cleanup
            pass
        raise LlmmcpError(
            f"headless browser timed out after {timeout:.0f}s",
            code="BROWSER_TIMEOUT",
            hint="raise --timeout or LLMMCP_BROWSER_TIMEOUT / --virtual-time-budget",
        ) from None
    if len(stdout) > MAX_DOM_BYTES:
        stdout = stdout[:MAX_DOM_BYTES]
    return int(process.returncode if process.returncode is not None else -1), stdout, stderr


def _kill_tree(process: subprocess.Popen[bytes]) -> None:
    """Kill the browser and any helper processes it spawned."""

    try:
        process.kill()
    except Exception:  # pragma: no cover - already gone
        pass
    if os.name != "nt":
        return
    try:
        subprocess.run(  # noqa: S603 - fixed command, no shell
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_creation_flags(),
            timeout=10,
            check=False,
        )
    except Exception:  # pragma: no cover - taskkill missing is not fatal
        pass


def _looks_like_dom(dom: str) -> bool:
    if not dom or not dom.strip():
        return False
    head = dom[:8192].lower()
    return "<html" in head or "<!doctype" in head[:512] or "<body" in head


def _stderr_hint(stderr: bytes) -> str:
    text = stderr.decode("utf-8", "replace").strip() if stderr else ""
    lowered = text.lower()
    if "user data directory is already in use" in lowered or "processsingleton" in lowered:
        return "the browser profile was locked (exit 13); the next attempt uses a fresh user-data-dir"
    if "proxy" in lowered and "fail" in lowered:
        return "the browser could not reach the proxy: check the resolved proxy in llmmcp diagnose"
    tail = " | ".join(line for line in text.splitlines()[-3:] if line.strip())
    return tail[:400] or "the browser exited without producing a DOM"
