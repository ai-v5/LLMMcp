"""Validate `market/index.json` against DSH plugin-market format 1.

DSH validates a market index with `@deepseek-ai/dsh-plugin-market-index` before it
shows or installs anything. This mirrors the parts an index generator can get
wrong, so a broken index fails here instead of in the Discover tab.

Run: python tools/validate_market_index.py [path ...]
Exit code 0 means every document passed.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

DEFAULT_PATHS = ("market/index.json",)

MAX_PUBLISH_TIMESTAMP = 100_000_000_000
LANGUAGE_ID = re.compile(r"^[a-z]{2,3}(?:-[a-z0-9]{2,8})*$")
PACKAGE_NAME = re.compile(r"^(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*$")
PATH_SEGMENT = re.compile(r"^[A-Za-z0-9._@+-]+$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")
SEMVER = re.compile(
    r"^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
    r"(?:-(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


class Invalid(ValueError):
    """A violated format rule, carrying the JSON path that failed."""

    def __init__(self, path: str, reason: str) -> None:
        super().__init__(f"{path}: {reason}")
        self.path = path
        self.reason = reason


def _object(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise Invalid(path, "must be an object")
    return value


def _string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise Invalid(path, "must be a non-empty string")
    return value


def _localized(value: Any, path: str) -> dict[str, str]:
    if isinstance(value, str):
        return {"en": _string(value, path)}
    text = {key: _string(member, f"{path}.{key}") for key, member in _object(value, path).items()}
    for language in text:
        if not LANGUAGE_ID.match(language):
            raise Invalid(f"{path}.{language}", "must be a lowercase language id")
    if "en" not in text:
        raise Invalid(path, "must contain en")
    return text


def _declarations(value: Any, path: str, *, sort_key: bool) -> dict[str, Any]:
    declared: dict[str, Any] = {}
    for key, member in _object(value, path).items():
        if not key:
            raise Invalid(path, "must not declare an empty id")
        entry = _object(member, f"{path}.{key}")
        item: dict[str, Any] = {"title": _localized(entry.get("title"), f"{path}.{key}.title")}
        if sort_key:
            order = entry.get("order")
            if order not in ("asc", "desc"):
                raise Invalid(f"{path}.{key}.order", 'must be "asc" or "desc"')
            item["order"] = order
        declared[key] = item
    return declared


def _http_url(value: Any, path: str, *, allow_query: bool = False) -> str:
    text = _string(value, path)
    if not re.match(r"^https?://", text):
        raise Invalid(path, "must be an absolute http(s) URL")
    if "://" not in text:
        raise Invalid(path, "must be an absolute http(s) URL")
    authority = text.split("://", 1)[1].split("/", 1)[0]
    if "@" in authority:
        raise Invalid(path, "must not contain credentials")
    if not allow_query and ("?" in text or "#" in text):
        raise Invalid(path, "must not contain a query or fragment")
    return text


def _version(value: Any, path: str) -> dict[str, Any]:
    entry = _object(value, path)
    version = _string(entry.get("version"), f"{path}.version")
    if not SEMVER.match(version):
        raise Invalid(f"{path}.version", "must be an exact SemVer version")
    stamp = entry.get("publishTimestamp")
    if not isinstance(stamp, int) or isinstance(stamp, bool) or not 0 <= stamp < MAX_PUBLISH_TIMESTAMP:
        raise Invalid(f"{path}.publishTimestamp", "must be an integer Unix timestamp in seconds")
    source = _object(entry.get("source"), f"{path}.source")
    kind = source.get("type")
    if kind == "npm":
        parsed: dict[str, Any] = {"type": "npm"}
        if "registry" in source:
            parsed["registry"] = _http_url(source["registry"], f"{path}.source.registry")
    elif kind == "git":
        parsed = {
            "type": "git",
            "url": _http_url(source.get("url"), f"{path}.source.url"),
            "commit": _string(source.get("commit"), f"{path}.source.commit"),
        }
        if not COMMIT.match(parsed["commit"]):
            raise Invalid(f"{path}.source.commit", "must be a lowercase 40-hex-digit commit")
        if parsed["url"].endswith(".git"):
            raise Invalid(f"{path}.source.url", "must omit the .git suffix")
        segments = parsed["url"].split("://", 1)[1].split("/")[1:]
        if len([s for s in segments if s]) < 2:
            raise Invalid(f"{path}.source.url", "must name a repository with at least two path segments")
        if "path" in source:
            directory = _string(source["path"], f"{path}.source.path")
            segments = directory.split("/")
            unsafe = any(seg in (".", "..") or not PATH_SEGMENT.match(seg) for seg in segments)
            if unsafe:
                raise Invalid(f"{path}.source.path", "must be a relative directory of safe segments")
            parsed["path"] = directory
    else:
        raise Invalid(f"{path}.source.type", 'must be "npm" or "git"')
    return {"version": version, "publishTimestamp": stamp, "source": parsed}


def _plugin(value: Any, path: str, metadata: dict[str, Any]) -> dict[str, Any]:
    entry = _object(value, path)
    name = _string(entry.get("name"), f"{path}.name")
    if not PACKAGE_NAME.match(name):
        raise Invalid(f"{path}.name", "must be a valid npm package name")
    for field in ("title", "description"):
        if field in entry:
            _localized(entry[field], f"{path}.{field}")
    for field in ("author",):
        if field in entry:
            _string(entry[field], f"{path}.{field}")
    if "homepage" in entry:
        _http_url(entry["homepage"], f"{path}.homepage", allow_query=True)
    if "repository" in entry:
        repository = _object(entry["repository"], f"{path}.repository")
        _http_url(repository.get("url"), f"{path}.repository.url")
        if "directory" in repository:
            _string(repository["directory"], f"{path}.repository.directory")
    declared = _object(entry.get("metadata", {}), f"{path}.metadata")
    if "category" in declared and declared["category"] not in metadata["categories"]:
        raise Invalid(f"{path}.metadata.category", "references an undeclared category")
    keywords = declared.get("keywords", [])
    if not isinstance(keywords, list) or not all(isinstance(item, str) for item in keywords):
        raise Invalid(f"{path}.metadata.keywords", "must be an array of strings")
    labels = declared.get("labels", [])
    if not isinstance(labels, list):
        raise Invalid(f"{path}.metadata.labels", "must be an array")
    for position, label in enumerate(labels):
        if label not in metadata["labels"]:
            raise Invalid(f"{path}.metadata.labels[{position}]", "references an undeclared label")
    if len(set(labels)) != len(labels):
        raise Invalid(f"{path}.metadata.labels", "must not repeat a label")
    for key in declared.get("sortKeys", {}):
        if key not in metadata["sortKeys"]:
            raise Invalid(f"{path}.metadata.sortKeys.{key}", "references an undeclared sort key")
    versions = entry.get("versions")
    if not isinstance(versions, list) or not versions:
        raise Invalid(f"{path}.versions", "must be a non-empty array")
    parsed_versions = [_version(member, f"{path}.versions[{i}]") for i, member in enumerate(versions)]
    seen: set[str] = set()
    for parsed in parsed_versions:
        if parsed["version"] in seen:
            raise Invalid(f"{path}.versions", f"duplicates {parsed['version']}")
        seen.add(parsed["version"])
    return {"name": name, "versions": parsed_versions}


def validate(document: Any, origin: str = "https://example.com/index.json") -> dict[str, Any]:
    """Validate one parsed index document and return a summary."""

    root = _object(document, "$")
    if root.get("format") != 1:
        raise Invalid("$.format", "must be 1")
    title = _localized(root.get("title"), "$.title")
    if "description" in root:
        _localized(root["description"], "$.description")
    raw_metadata = _object(root.get("metadata", {}), "$.metadata")
    metadata = {
        "categories": _declarations(
            raw_metadata.get("categories", {}), "$.metadata.categories", sort_key=False
        ),
        "labels": _declarations(raw_metadata.get("labels", {}), "$.metadata.labels", sort_key=False),
        "sortKeys": _declarations(
            raw_metadata.get("sortKeys", {}), "$.metadata.sortKeys", sort_key=True
        ),
    }
    plugins_raw = root.get("plugins")
    if not isinstance(plugins_raw, list):
        raise Invalid("$.plugins", "must be an array")
    plugins = [_plugin(member, f"$.plugins[{i}]", metadata) for i, member in enumerate(plugins_raw)]
    names: set[str] = set()
    for plugin in plugins:
        if plugin["name"] in names:
            raise Invalid("$.plugins", f"duplicates {plugin['name']}")
        names.add(plugin["name"])
    return {
        "origin": origin,
        "title": title["en"],
        "plugins": len(plugins),
        "names": sorted(names),
        "versions": sum(len(plugin["versions"]) for plugin in plugins),
    }


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parent.parent
    targets = (argv or list(DEFAULT_PATHS)) or list(DEFAULT_PATHS)
    paths = [root / target if not Path(target).is_absolute() else Path(target) for target in targets]
    failures = 0
    for path in paths:
        try:
            document = json.loads(path.read_text(encoding="utf-8-sig"))
            summary = validate(document)
        except Invalid as error:
            failures += 1
            print(f"{path}: INVALID {error}")
            continue
        except (OSError, json.JSONDecodeError) as error:
            failures += 1
            print(f"{path}: unreadable ({error})")
            continue
        print(
            f"{path}: ok format=1 title={summary['title']!r} "
            f"plugins={summary['plugins']} versions={summary['versions']} names={summary['names']}"
        )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
