"""Workspace MCP Server — root-bounded filesystem for one instance.

Spec: specs/001-per-chat-instances/tasks.md T039; plan.md §MCP servers.

Constitution Principle III (bounded authority, pinned at spawn):
  * `--root` is fixed at process start; every path argument is resolved
    via ``os.path.realpath`` and rejected if the chased path does not
    start with the root. Symlinks cannot escape.
  * No tool accepts an `instance_*`, `project_*`, `workspace_*`, or
    `container_*` argument — agents cannot address a different chat's
    workspace (T033 enforces).

Every tool returns text on every path — non-zero exits would crash the
Claude Agent SDK.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import sys
from typing import Iterable

from mcp.server.fastmcp import FastMCP


# ---------------------------------------------------------------------------
# Argv parsing. `--root` is required in production; default `/` only so
# `python -m openclow.mcp_servers.workspace_mcp` can start in a shell for
# schema inspection. Any tool call without a real root returns REFUSED.
# ---------------------------------------------------------------------------


def _parse_argv(argv: list[str]) -> str:
    parser = argparse.ArgumentParser(
        prog="openclow.mcp_servers.workspace_mcp",
        description="Per-instance filesystem access, pinned to --root.",
    )
    parser.add_argument(
        "--root",
        required=False,
        default="",
        help="Absolute path to the instance workspace root.",
    )
    ns, _ = parser.parse_known_args(argv)
    if not ns.root:
        return ""
    # Canonicalise once at startup so every subsequent realpath check is
    # against a stable reference. If the root itself is a symlink, we
    # resolve it here — the agent's tool calls will then be compared
    # against the resolved target, closing the symlink-root loophole.
    return os.path.realpath(ns.root)


_ROOT = _parse_argv(sys.argv[1:])
_ROOT_OK = bool(_ROOT) and os.path.isdir(_ROOT)


mcp = FastMCP("workspace")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_READ_MAX_BYTES = 256 * 1024
_SEARCH_MAX_RESULTS = 500
_SEARCH_MAX_LINE_LEN = 500


def _startup_refused() -> str | None:
    """Return REFUSED text when the module was started without a real root."""
    if _ROOT_OK:
        return None
    return (
        "REFUSED: workspace_mcp started without a valid --root; no "
        "filesystem operation is permitted."
    )


def _resolve(path: str) -> tuple[str | None, str | None]:
    """Resolve `path` under _ROOT. Returns (abs_path, error).

    If the chased realpath does not start with `_ROOT + os.sep` (or equal
    _ROOT for the root itself), returns (None, error). Relative paths
    are joined under _ROOT.
    """
    if not isinstance(path, str) or not path:
        return None, "FAILED: path must be a non-empty string"

    # Treat absolute paths as either already-under-root or invalid; we
    # do not trust the agent to supply the correct root-prefixed path
    # (it doesn't know _ROOT). For ergonomic use, join relative paths
    # under _ROOT.
    if os.path.isabs(path):
        candidate = path
    else:
        candidate = os.path.join(_ROOT, path)

    real = os.path.realpath(candidate)
    if real == _ROOT:
        return real, None
    if not real.startswith(_ROOT + os.sep):
        return None, (
            f"REFUSED: {path!r} resolves outside the workspace root. "
            "Only paths under the instance workspace are permitted."
        )
    return real, None


# ---------------------------------------------------------------------------
# Tools — argument names avoid forbidden substrings (T033).
# ---------------------------------------------------------------------------


@mcp.tool()
async def read_file(path: str) -> str:
    """Read a UTF-8 file under the workspace root. Truncated to 256 KiB."""
    refused = _startup_refused()
    if refused:
        return refused
    abs_path, err = _resolve(path)
    if err:
        return err
    try:
        with open(abs_path, "rb") as f:
            data = f.read(_READ_MAX_BYTES + 1)
    except FileNotFoundError:
        return f"FAILED: {path!r} does not exist"
    except IsADirectoryError:
        return f"FAILED: {path!r} is a directory; use list_dir"
    except PermissionError:
        return f"FAILED: permission denied reading {path!r}"
    except OSError as e:
        return f"FAILED: {type(e).__name__}: {str(e)[:200]}"

    truncated = len(data) > _READ_MAX_BYTES
    body = data[:_READ_MAX_BYTES].decode("utf-8", errors="replace")
    if truncated:
        body += f"\n\n(truncated at {_READ_MAX_BYTES} bytes)"
    return body


@mcp.tool()
async def write_file(path: str, content: str) -> str:
    """Write `content` to a file under the workspace root. Creates parents."""
    refused = _startup_refused()
    if refused:
        return refused
    abs_path, err = _resolve(path)
    if err:
        return err
    try:
        os.makedirs(os.path.dirname(abs_path) or _ROOT, exist_ok=True)
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(content)
    except IsADirectoryError:
        return f"FAILED: {path!r} is a directory"
    except PermissionError:
        return f"FAILED: permission denied writing {path!r}"
    except OSError as e:
        return f"FAILED: {type(e).__name__}: {str(e)[:200]}"
    return f"OK: wrote {len(content)} chars to {path}"


@mcp.tool()
async def edit_file(path: str, old: str, new: str) -> str:
    """Replace one exact occurrence of `old` with `new` inside a file.

    Fails if `old` is absent or appears more than once (force the caller
    to supply enough context to make the match unique, matching the main
    Edit tool's semantics). Non-atomic; a concurrent writer could race.
    """
    refused = _startup_refused()
    if refused:
        return refused
    abs_path, err = _resolve(path)
    if err:
        return err
    try:
        with open(abs_path, "r", encoding="utf-8") as f:
            body = f.read()
    except FileNotFoundError:
        return f"FAILED: {path!r} does not exist"
    except OSError as e:
        return f"FAILED: {type(e).__name__}: {str(e)[:200]}"

    count = body.count(old)
    if count == 0:
        return f"FAILED: {old!r} not found in {path}"
    if count > 1:
        return (
            f"FAILED: {old!r} appears {count} times in {path}; "
            "include more surrounding context to make the match unique."
        )
    new_body = body.replace(old, new, 1)
    try:
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(new_body)
    except OSError as e:
        return f"FAILED: {type(e).__name__}: {str(e)[:200]}"
    return f"OK: edited {path} ({len(body)} → {len(new_body)} chars)"


@mcp.tool()
async def list_dir(path: str = ".") -> str:
    """List entries in a directory under the workspace root."""
    refused = _startup_refused()
    if refused:
        return refused
    abs_path, err = _resolve(path)
    if err:
        return err
    if not os.path.isdir(abs_path):
        return f"FAILED: {path!r} is not a directory"
    try:
        entries = sorted(os.listdir(abs_path))
    except PermissionError:
        return f"FAILED: permission denied listing {path!r}"
    except OSError as e:
        return f"FAILED: {type(e).__name__}: {str(e)[:200]}"

    lines: list[str] = []
    for name in entries:
        full = os.path.join(abs_path, name)
        try:
            if os.path.isdir(full):
                lines.append(f"{name}/")
            else:
                size = os.path.getsize(full)
                lines.append(f"{name}\t{size}")
        except OSError:
            lines.append(f"{name}\t?")
    return "\n".join(lines) or "(empty)"


@mcp.tool()
async def search(pattern: str, path: str = ".") -> str:
    """Plain-substring search over files under the workspace root.

    Returns at most 500 matches, each formatted `relpath:lineno: line`.
    Binary files (those with a NUL byte in the first 4 KiB) are skipped.
    """
    refused = _startup_refused()
    if refused:
        return refused
    if not pattern:
        return "FAILED: pattern must be a non-empty string"
    abs_path, err = _resolve(path)
    if err:
        return err
    if not os.path.isdir(abs_path):
        if os.path.isfile(abs_path):
            return _search_file(abs_path, pattern, single_file=True)
        return f"FAILED: {path!r} not found"

    hits: list[str] = []
    for dirpath, _, filenames in os.walk(abs_path):
        # Skip common noise roots to keep results useful.
        rel_dir = os.path.relpath(dirpath, _ROOT)
        if any(
            seg in {".git", "node_modules", "vendor", "__pycache__", ".venv"}
            for seg in rel_dir.split(os.sep)
        ):
            continue
        for name in filenames:
            full = os.path.join(dirpath, name)
            # Defence in depth: search_file re-resolves; on symlink to
            # outside the root we skip silently rather than error out
            # mid-walk.
            real = os.path.realpath(full)
            if real != _ROOT and not real.startswith(_ROOT + os.sep):
                continue
            for hit in _iter_matches(real, pattern):
                hits.append(hit)
                if len(hits) >= _SEARCH_MAX_RESULTS:
                    hits.append(
                        f"(stopped at {_SEARCH_MAX_RESULTS} matches — "
                        "narrow the pattern or path)"
                    )
                    return "\n".join(hits)
    return "\n".join(hits) or "(no matches)"


def _search_file(abs_path: str, pattern: str, *, single_file: bool) -> str:
    hits = list(_iter_matches(abs_path, pattern))
    return "\n".join(hits) or "(no matches)"


def _iter_matches(abs_path: str, pattern: str) -> Iterable[str]:
    try:
        with open(abs_path, "rb") as f:
            head = f.read(4096)
            if b"\x00" in head:
                return
            body = head + f.read()
    except (OSError, PermissionError):
        return
    try:
        text = body.decode("utf-8", errors="replace")
    except Exception:
        return
    rel = os.path.relpath(abs_path, _ROOT)
    for lineno, line in enumerate(text.splitlines(), start=1):
        if pattern in line:
            snippet = line.strip()[:_SEARCH_MAX_LINE_LEN]
            yield f"{rel}:{lineno}: {snippet}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
