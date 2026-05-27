#!/usr/bin/env python3
"""
Deny edit/create operations unless every target path is under docs/explanations/
and ends with .md.

Intended use:
- VS Code Copilot hook via PreToolUse
- Best paired with the doc explanation agent

Input:
- JSON from stdin

Output:
- JSON to stdout compatible with VS Code hook protocol
"""

from __future__ import annotations

import json
import logging
import os
import posixpath
import sys
from typing import Iterable, List

# ---------------------------------------------------------------------------
# Logging — writes to stderr AND a rotating log file next to this script.
# Set env var COPILOT_HOOK_LOG=0 to silence file logging.
# ---------------------------------------------------------------------------
_LOG_FILE = os.path.join(os.path.dirname(__file__), "hook-debug.log")
_file_logging_enabled = os.environ.get("COPILOT_HOOK_LOG", "1") != "0"

_handlers: list = [logging.StreamHandler(sys.stderr)]
if _file_logging_enabled:
    _handlers.append(logging.FileHandler(_LOG_FILE, encoding="utf-8"))

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [hook] %(levelname)s %(message)s",
    handlers=_handlers,
)
log = logging.getLogger("check-doc-explanation-writes")


ALLOWED_PREFIX = "docs/explanations/"
ALLOWED_SUFFIX = ".md"
# Include both VS Code internal camelCase names and agent-declared snake_case names.
EDIT_TOOLS = {
    "editFiles",
    "createFile",
    "create_file",
    "replace_string_in_file",
    "multi_replace_string_in_file",
}


def normalize_path(path: str) -> str:
    path = path.replace("\\", "/").strip()
    while "//" in path:
        path = path.replace("//", "/")
    path = posixpath.normpath(path)
    if path == ".":
        return path
    # Strip only leading slashes; lstrip("./") treats args as a char-set, not a prefix.
    return path.lstrip("/")


def extract_paths(tool_name: str, tool_input: dict) -> List[str]:
    """
    Common shapes seen in hook payloads:
    - editFiles: tool_input.files = [...]
    - createFile: tool_input.path = "..."
    Also tolerates filePath / oldUri / newUri / uri.
    """
    paths: List[str] = []

    def add(value):
        if isinstance(value, str) and value.strip():
            paths.append(value)

    if isinstance(tool_input.get("files"), list):
        for item in tool_input["files"]:
            if isinstance(item, str):
                add(item)
            elif isinstance(item, dict):
                add(item.get("path"))
                add(item.get("filePath"))

    add(tool_input.get("path"))
    add(tool_input.get("filePath"))

    for key in ("oldUri", "newUri", "uri"):
        add(tool_input.get(key))

    normalized = []
    for p in paths:
        np = normalize_path(p)
        if np not in ("", "."):
            normalized.append(np)
    return normalized


def is_allowed_markdown_explanation_path(path: str) -> bool:
    # Handles relative paths ("docs/explanation/foo.md") and absolute Windows/Unix paths
    # ("d:/snapdragon/docs/explanation/foo.md", "/workspace/docs/explanation/foo.md").
    # Require ALLOWED_PREFIX to be at the start or immediately after a path separator.
    idx = path.find(ALLOWED_PREFIX)
    if idx == -1:
        return False
    if idx != 0 and path[idx - 1] not in ("/", "\\"):
        return False
    return path.endswith(ALLOWED_SUFFIX)


def allow_response(message: str = "") -> dict:
    out = {"continue": True}
    if message:
        out["systemMessage"] = message
    return out


def deny_response(reason: str, bad_paths: Iterable[str]) -> dict:
    bad_paths = list(bad_paths)
    return {
        "continue": True,
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
            "additionalContext": (
                "Doc Explanation agent may edit only markdown files under docs/explanations/. "
                f"Blocked paths: {', '.join(bad_paths)}"
            ),
        },
        "systemMessage": (
            "Blocked edit: only docs/explanations/*.md files are writable for Doc Explanation agent."
        ),
    }


def ask_response(reason: str) -> dict:
    return {
        "continue": True,
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": reason,
            "additionalContext": "This hook allows edits only under docs/explanations/*.md.",
        },
    }


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception as exc:
        log.warning("Failed to parse hook payload: %s", exc)
        print(json.dumps(allow_response(f"Hook parse warning: {exc}")))
        return 0

    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {}) or {}

    log.debug("--- hook fired ---")
    log.debug("tool_name = %r", tool_name)
    log.debug("tool_input keys = %s", list(tool_input.keys()))

    if tool_name not in EDIT_TOOLS:
        log.debug("tool_name not in EDIT_TOOLS -> allow (pass-through)")
        print(json.dumps(allow_response()))
        return 0

    paths = extract_paths(tool_name, tool_input)
    log.debug("extracted paths = %s", paths)

    if not paths:
        log.warning("No paths extracted -> ask for manual approval")
        print(json.dumps(ask_response(
            "Could not determine edit target paths. Manual approval required."
        )))
        return 0

    bad_paths = [p for p in paths if not is_allowed_markdown_explanation_path(p)]
    log.debug("bad_paths = %s", bad_paths)

    if bad_paths:
        log.warning("DENY: disallowed paths -> %s", bad_paths)
        print(json.dumps(deny_response(
            "Only docs/explanations/*.md files may be edited in this protected flow.",
            bad_paths,
        )))
        return 0

    log.debug("ALLOW: all paths under docs/explanations/*.md")
    print(json.dumps(allow_response()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())