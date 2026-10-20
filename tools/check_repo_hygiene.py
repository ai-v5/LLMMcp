"""Fail when a commit would publish local machine, company or account details.

The repository is intended to be publishable: code, tests and generic docs only.
This check scans every tracked file for the shapes that leak a local environment -
absolute user paths, corporate e-mail addresses, private proxy hosts, committed
DSH profile patches - and exits non-zero when it finds one.

Run it before publishing, or from CI:

    python tools/check_repo_hygiene.py

Placeholders such as ``http://user:secret@host:8080`` are intentional redaction
fixtures, so credential placement alone is not reported; a real-looking password
is. Allow listed lines live next to the rule that needs them.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

#: Documented placeholders that appear in fixtures and docstrings on purpose.
PLACEHOLDER_PASSWORDS = frozenset(
    {"user", "pw", "secret", "hunter2", "topsecret", "password", "pass", "***", "xxx", "changeme"}
)

#: (name, compiled pattern) pairs. A finding names the kind of detail that leaked.
RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "absolute-windows-user-path",
        re.compile(r"[A-Za-z]:\\{1,2}Users\\{1,2}(?P<user>[^\\\s\"']+)"),
    ),
    (
        "absolute-posix-home-path",
        re.compile(r"(?:^|[\s\"'(])/(?:home|Users)/(?P<user>[^/\s\"')]+)"),
    ),
    (
        "company-email-address",
        re.compile(r"[\w.+-]+@(?:huawei|hisilicon)[a-z.]*\.[a-z]{2,}", re.I),
    ),
    (
        "internal-proxy-or-gateway-host",
        re.compile(r"(?:proxysg|proxyhk)\.[a-z0-9.-]+", re.I),
    ),
    (
        "committed-profile-patch",
        re.compile(r"cordis\.patch\.ya?ml\.bak-[0-9A-Za-z]+"),
    ),
    (
        "real-looking-credential-in-url",
        re.compile(r"[a-z][a-z0-9+.-]*://[^/\s:@\"']+:(?P<secret>[^/\s@\"']+)@[^/\s\"']+"),
    ),
)

#: Reviewed exceptions: the line's *text* is the documentation of the rule itself.
ALLOWED: dict[str, frozenset[str]] = {
    ".gitignore": frozenset({"committed-profile-patch"}),
    "tools/check_repo_hygiene.py": frozenset(name for name, _ in RULES),
    "docs/local-setup.md": frozenset({"absolute-windows-user-path", "absolute-posix-home-path"}),
}

SKIP_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".zip", ".whl", ".svg",
}


def tracked_files(root: Path) -> list[Path]:
    """Every file Git tracks, as absolute paths."""

    output = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        capture_output=True,
        check=True,
    ).stdout
    return [root / name for name in output.decode("utf-8").split("\0") if name]


def _is_placeholder(secret: str) -> bool:
    return secret.lower() in PLACEHOLDER_PASSWORDS


def _hits(name: str, pattern: re.Pattern[str], line: str) -> bool:
    for match in pattern.finditer(line):
        if name == "real-looking-credential-in-url":
            secret = match.group("secret")
            if _is_placeholder(secret) or secret.startswith("${"):
                continue
            return True
        if name in ("absolute-windows-user-path", "absolute-posix-home-path"):
            # `C:\Users\<name>` style documentation is a placeholder, not a leak.
            if match.group("user").startswith("<"):
                continue
        return True
    return False


def scan(root: Path) -> list[tuple[str, Path, int, str]]:
    """Return ``(rule, path, line_number, line)`` for every finding."""

    findings: list[tuple[str, Path, int, str]] = []
    for path in tracked_files(root):
        if path.suffix.lower() in SKIP_SUFFIXES or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        relative = path.relative_to(root).as_posix()
        allowed = ALLOWED.get(relative, frozenset())
        for number, line in enumerate(text.splitlines(), start=1):
            for name, pattern in RULES:
                if name in allowed:
                    continue
                if _hits(name, pattern, line):
                    findings.append((name, path, number, line.strip()))
    return findings


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parent.parent
    findings = scan(root)
    if not findings:
        print(f"hygiene: clean ({len(tracked_files(root))} tracked files)")
        return 0
    print(f"hygiene: {len(findings)} finding(s) must not be published:")
    for name, path, number, line in findings:
        location = path.relative_to(root).as_posix()
        print(f"  {location}:{number}: [{name}] {line[:120]}")
    print("\nMove the value into local configuration (see docs/local-setup.md).")
    return 1


if __name__ == "__main__":
    sys.exit(main())
