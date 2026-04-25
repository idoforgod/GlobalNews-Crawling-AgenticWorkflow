#!/usr/bin/env python3
"""PreToolUse hook — block direct Edit/Write on SOT files.

CE7 (4th reflection critical error): fcntl.flock() only protects SOT
mutations performed via sot_manager.py subprocess. Direct Edit/Write
tool invocations bypass the lock entirely, breaking single-writer
isolation guarantees.

This hook physically blocks any Edit/Write operation whose target
resolves to a known SOT filename (state.yaml / state.yml / state.json).
Writes must go through `sot_manager.py --atomic-write` for:

    - fcntl.flock() exclusive locking
    - Actor authorization (_check_write_authorization)
    - Schema validation (validate_sot_schema + validate_execution_section)
    - Atomic check-and-write (TOCTOU-safe)
    - workflow.* hard freeze enforcement

Contract:
    - stdin: JSON from Claude Code PreToolUse hook with `tool_input.file_path`
    - exit 0: allow (not a SOT file)
    - exit 2: block (SOT file detected; stderr carries feedback)
    - stdout: ignored
    - stderr: human-readable feedback for Claude self-correction

Best-effort: malformed stdin, missing fields, or any internal error
defaults to exit 0 (allow) rather than crashing the hook pipeline.
"""

from __future__ import annotations

import json
import os
import sys

# D-7 intentional duplication — must match _execution_lib.py:SOT_FILENAMES,
# _context_lib.py:SOT_FILENAMES, scripts/sot_manager.py:SOT_FILENAMES
SOT_FILENAMES = ("state.yaml", "state.yml", "state.json")

# Feedback message shown to Claude when a direct edit is blocked.
# Must be specific enough that Claude can self-correct.
_FEEDBACK_TEMPLATE = (
    "BLOCKED: Direct Edit/Write on SOT file is forbidden.\n"
    "Target: {path}\n"
    "\n"
    "SOT writes MUST go through scripts/sot_manager.py for:\n"
    "  - fcntl.flock() exclusive locking\n"
    "  - Actor authorization per section path\n"
    "  - Schema validation (E1-E15 + S1-S9)\n"
    "  - workflow.* hard freeze enforcement\n"
    "\n"
    "Use the Bash tool with one of:\n"
    "  python3 scripts/sot_manager.py --read --project-dir .\n"
    "  python3 scripts/sot_manager.py --atomic-write --actor <actor> "
    "--path <dot.path> --value <json-value> --project-dir .\n"
    "  python3 scripts/sot_manager.py --init-execution --actor meta "
    "--project-dir .\n"
    "\n"
    "Valid actors: meta | w1 | w2 | w3 | master"
)


def _normalize(path: str) -> str:
    """Normalize a path for suffix comparison.

    - Strip whitespace
    - Collapse multiple slashes
    - Expand user (~)
    """
    if not path:
        return ""
    path = path.strip()
    path = os.path.expanduser(path)
    # Replace backslashes (Windows-safe) and collapse multiple slashes
    path = path.replace("\\", "/")
    while "//" in path:
        path = path.replace("//", "/")
    return path


def _is_sot_path(path: str) -> bool:
    """Return True if path targets a known SOT file inside a .claude/ dir.

    Matches:
        .claude/state.yaml
        /abs/path/.claude/state.yaml
        project/.claude/state.yml
        ~/workspace/proj/.claude/state.json

    Does NOT match:
        data/state.yaml            (not under .claude/)
        docs/state.yaml            (not under .claude/)
        .claude/context-snapshots/state.yaml  (subdirectory, not direct)
    """
    if not path:
        return False

    normalized = _normalize(path)
    if not normalized:
        return False

    # Check each SOT filename
    for fn in SOT_FILENAMES:
        suffix = f".claude/{fn}"
        # Match exact path or path ending with /.claude/{fn}
        if normalized == suffix or normalized.endswith("/" + suffix):
            return True
        # Also match when given without leading path (e.g., ".claude/state.yaml")
        if normalized == suffix:
            return True
    return False


def main() -> int:
    """Hook entry point. Exit 0 = allow, Exit 2 = block."""
    try:
        raw = sys.stdin.read()
    except Exception:
        return 0  # Best-effort: crash-free on stdin read error

    if not raw or not raw.strip():
        return 0  # Empty stdin → allow (non-Edit/Write context)

    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return 0  # Malformed JSON → allow (best-effort)

    if not isinstance(payload, dict):
        return 0

    tool_input = payload.get("tool_input", {})
    if not isinstance(tool_input, dict):
        return 0

    # Claude Code passes the file path via `file_path` for Edit/Write tools
    file_path = tool_input.get("file_path", "")
    if not isinstance(file_path, str):
        return 0

    if _is_sot_path(file_path):
        print(_FEEDBACK_TEMPLATE.format(path=file_path), file=sys.stderr)
        return 2  # Block with feedback

    return 0


if __name__ == "__main__":
    sys.exit(main())
