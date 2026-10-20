"""Offline tests for the headless-browser layer (command building and recovery).

Nothing here launches a real browser: ``_invoke`` is monkeypatched, so the tests
assert the command line, the profile lifecycle and the retry policy.
"""

from __future__ import annotations

import os

import pytest
from local_env import local_value

import llmmcp.browser as browser
from llmmcp.models import LlmmcpError
from llmmcp.settings import ProxySettings, Settings

BROWSER = r"C:\Program Files\Google\Chrome\Application\chrome.exe"


def make_settings(**overrides) -> Settings:
    base = {"proxy": ProxySettings(), "browser_path": BROWSER, "browser_timeout": 30.0}
    base.update(overrides)
    return Settings(**base)


def profile_dirs(commands: list[list[str]]) -> list[str]:
    dirs = []
    for command in commands:
        for arg in command:
            if arg.startswith("--user-data-dir="):
                dirs.append(arg.split("=", 1)[1])
    return dirs


# ---------------------------------------------------------------------------
# command construction
# ---------------------------------------------------------------------------
def test_build_command_has_required_flags():
    proxy = local_value("test_proxy") or "http://proxy.example.com:8080"
    command = browser.build_command(
        BROWSER,
        "https://www.google.com/search?q=ascend+npu+mfu&hl=en",
        profile_dir=r"C:\Temp\profile",
        proxy=proxy,
        user_agent="UA/1.0",
        virtual_time_budget=20000,
    )
    assert command[0] == BROWSER
    assert "--headless=new" in command
    assert "--dump-dom" in command
    assert "--disable-gpu" in command
    assert "--no-first-run" in command
    assert "--no-default-browser-check" in command
    assert r"--user-data-dir=C:\Temp\profile" in command
    assert f"--proxy-server={proxy}" in command
    assert "--user-agent=UA/1.0" in command
    assert "--virtual-time-budget=20000" in command
    # The URL is the final argument and is not shell-quoted.
    assert command[-1] == "https://www.google.com/search?q=ascend+npu+mfu&hl=en"


def test_build_command_without_proxy_has_no_proxy_flag():
    command = browser.build_command(BROWSER, "https://example.com/", profile_dir="/tmp/profile")
    assert not any(arg.startswith("--proxy-server=") for arg in command)
    assert not any(arg.startswith("--user-agent=") for arg in command)


def test_proxy_credentials_never_reach_the_browser_command():
    proxy = "http://alice:hunter2@gateway.corp:8080"
    command = browser.build_command(BROWSER, "https://example.com/", profile_dir="/tmp/p", proxy=proxy)
    joined = " ".join(command)
    assert "hunter2" not in joined
    assert "alice" not in joined
    assert "--proxy-server=http://gateway.corp:8080" in command


def test_browser_proxy_redacts():
    assert browser.browser_proxy("http://user:pw@p:8080") == "http://p:8080"
    assert browser.browser_proxy(None) is None


def test_candidate_paths_are_absolute_windows_paths():
    candidates = browser.candidate_paths()
    assert len(candidates) >= 3
    for path in candidates:
        assert path.lower().endswith(("chrome.exe", "msedge.exe"))
        assert "%" not in path


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------
def test_find_browser_honours_configured_path(tmp_path):
    fake = tmp_path / "chrome.exe"
    fake.write_bytes(b"")
    assert browser.find_browser(make_settings(browser_path=str(fake))) == str(fake)


def test_find_browser_rejects_missing_configured_path():
    assert browser.find_browser(make_settings(browser_path=r"C:\nope\chrome.exe")) is None


def test_browser_info_reports_proxy_and_candidates():
    info = browser.browser_info(
        make_settings(proxy=ProxySettings({"https": "http://gw:8080"}, source="config"))
    )
    assert info["proxy_arg"] == "http://gw:8080"
    assert info["configured_path"] == BROWSER
    assert info["headless"] == "--headless=new"
    assert info["candidates"]


# ---------------------------------------------------------------------------
# render(): success, retry, timeout
# ---------------------------------------------------------------------------
def test_render_returns_dom_and_cleans_the_profile(monkeypatch):
    seen: list[list[str]] = []

    def fake_invoke(command, timeout):
        seen.append(list(command))
        return 0, b"<html><body><h1>rendered</h1></body></html>", b""

    monkeypatch.setattr(browser, "_invoke", fake_invoke)
    monkeypatch.setattr(browser, "find_browser", lambda settings=None: BROWSER)

    dom = browser.render("https://example.com/", 12.0, settings=make_settings())
    assert "rendered" in dom
    assert len(seen) == 1
    dirs = profile_dirs(seen)
    assert len(dirs) == 1
    assert not os.path.exists(dirs[0]), "temporary profile must be removed"


def test_render_raises_when_no_browser_is_available(monkeypatch):
    monkeypatch.setattr(browser, "find_browser", lambda settings=None: None)
    with pytest.raises(LlmmcpError) as excinfo:
        browser.render("https://example.com/", settings=make_settings())
    assert excinfo.value.code == "BROWSER_NOT_FOUND"
    assert "LLMMCP_BROWSER" in (excinfo.value.hint or "")


def test_render_retries_once_with_a_fresh_profile_on_exit_13(monkeypatch):
    seen: list[list[str]] = []
    outcomes = [
        (
            13,
            b"",
            b"Failed to create a ProcessSingleton for your profile directory. "
            b"user data directory is already in use",
        ),
        (0, b"<html><body>second attempt</body></html>", b""),
    ]

    def fake_invoke(command, timeout):
        seen.append(list(command))
        return outcomes.pop(0)

    monkeypatch.setattr(browser, "_invoke", fake_invoke)
    monkeypatch.setattr(browser, "find_browser", lambda settings=None: BROWSER)

    dom = browser.render("https://example.com/", 10.0, settings=make_settings())
    assert "second attempt" in dom
    dirs = profile_dirs(seen)
    assert len(dirs) == 2
    assert dirs[0] != dirs[1], "each attempt must use its own user-data-dir"
    assert not any(os.path.exists(path) for path in dirs)


def test_render_fails_after_two_attempts(monkeypatch):
    calls = {"n": 0}

    def fake_invoke(command, timeout):
        calls["n"] += 1
        return 21, b"", b"some chrome error"

    monkeypatch.setattr(browser, "_invoke", fake_invoke)
    monkeypatch.setattr(browser, "find_browser", lambda settings=None: BROWSER)

    with pytest.raises(LlmmcpError) as excinfo:
        browser.render("https://example.com/", settings=make_settings())
    assert excinfo.value.code == "BROWSER_FAILED"
    assert calls["n"] == 2
    assert "some chrome error" in (excinfo.value.hint or "")


def test_render_does_not_retry_a_timeout(monkeypatch):
    calls = {"n": 0}

    def fake_invoke(command, timeout):
        calls["n"] += 1
        raise LlmmcpError("timed out", code="BROWSER_TIMEOUT")

    monkeypatch.setattr(browser, "_invoke", fake_invoke)
    monkeypatch.setattr(browser, "find_browser", lambda settings=None: BROWSER)

    with pytest.raises(LlmmcpError) as excinfo:
        browser.render("https://example.com/", 5.0, settings=make_settings())
    assert excinfo.value.code == "BROWSER_TIMEOUT"
    assert calls["n"] == 1


def test_render_accepts_dom_even_when_the_exit_code_is_noisy(monkeypatch):
    monkeypatch.setattr(
        browser, "_invoke", lambda command, timeout: (1, b"<html><body>ok</body></html>", b"noise")
    )
    monkeypatch.setattr(browser, "find_browser", lambda settings=None: BROWSER)
    assert "ok" in browser.render("https://example.com/", settings=make_settings())


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("dom", "expected"),
    [
        ("<html><body>x</body></html>", True),
        ("<!DOCTYPE html><html>", True),
        ("<body>fragment</body>", True),
        ("", False),
        ("   ", False),
        ("error page without markup", False),
    ],
)
def test_looks_like_dom(dom, expected):
    assert browser._looks_like_dom(dom) is expected


def test_stderr_hint_explains_profile_lock():
    hint = browser._stderr_hint(b"Failed to create a ProcessSingleton for your profile directory")
    assert "profile was locked" in hint


def test_creation_flags_are_windows_only(monkeypatch):
    monkeypatch.setattr(browser.os, "name", "nt")
    assert browser._creation_flags() == 0x08000000
    monkeypatch.setattr(browser.os, "name", "posix")
    assert browser._creation_flags() == 0

