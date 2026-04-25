#!/usr/bin/env python3
"""
DCI Retry Budget P1 Gate — dci_retry_budget.py

Deterministic retry counter for DCI gates. Mirrors the design of
validate_retry_budget.py but scoped to DCI gates (structural, reasoning,
narrator, review). Single call enforces both budget and circuit-breaker
transparency — LLM cannot bypass by re-checking its own "progress".

Usage:
    # Before retrying a failed gate
    python3 .claude/hooks/scripts/dci_retry_budget.py \\
        --run-id dci-2026-04-14-1130 --gate reasoning \\
        --check-and-increment --project-dir .

    # Record the outcome of the attempt (after each try)
    python3 .claude/hooks/scripts/dci_retry_budget.py \\
        --run-id ... --gate reasoning --record-attempt \\
        --pacs-score 72 --project-dir .

    # Read-only status
    python3 .claude/hooks/scripts/dci_retry_budget.py \\
        --run-id ... --gate reasoning --status --project-dir .

Output: JSON to stdout

Exit codes:
    0 — retry allowed (or status query)
    1 — retry denied (budget exhausted or circuit breaker OPEN)
    2 — script error

Budget:
    Non-ULW: 10 attempts
    ULW-active: 15 attempts (inherits I-1 Sisyphus Persistence)

Circuit breaker:
    Open if last 3 attempts show non-improving pACS scores
    (Δ ≤ 2 between each pair).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any


DEFAULT_MAX = 10
ULW_MAX = 15
CB_WINDOW = 3
CB_MIN_DELTA = 2.0


def _emit(result: dict[str, Any], exit_code: int) -> None:
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(exit_code)


def _ulw_active(project_dir: Path) -> bool:
    snap = project_dir / ".claude" / "context-snapshots" / "latest.md"
    if not snap.exists():
        return False
    try:
        text = snap.read_text(encoding="utf-8")
    except Exception:
        return False
    return bool(re.search(r"ULW 상태", text))


def _paths(project_dir: Path, run_id: str, gate: str) -> tuple[Path, Path]:
    base = project_dir / "data" / "dci" / "runs" / run_id / "retry-budget"
    base.mkdir(parents=True, exist_ok=True)
    counter = base / f".{gate}-retry-count"
    history = base / f".{gate}-retry-history.jsonl"
    return counter, history


def _read_counter(p: Path) -> int:
    if not p.exists():
        return 0
    try:
        return int(p.read_text(encoding="utf-8").strip() or "0")
    except Exception:
        return 0


def _write_counter(p: Path, value: int) -> None:
    p.write_text(str(value), encoding="utf-8")


def _read_history(p: Path) -> list[dict[str, Any]]:
    if not p.exists():
        return []
    out: list[dict[str, Any]] = []
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _append_history(p: Path, entry: dict[str, Any]) -> None:
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _circuit_breaker_open(history: list[dict[str, Any]]) -> tuple[bool, str]:
    scores = [h.get("pacs_score") for h in history
              if isinstance(h.get("pacs_score"), (int, float))]
    if len(scores) < CB_WINDOW:
        return False, "insufficient history"
    recent = scores[-CB_WINDOW:]
    deltas = [abs(recent[i + 1] - recent[i]) for i in range(len(recent) - 1)]
    if all(d <= CB_MIN_DELTA for d in deltas):
        return True, f"non-improving scores: {recent}"
    return False, "scores diverging — retry worthwhile"


def main() -> None:
    ap = argparse.ArgumentParser(description="DCI retry budget gate")
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--gate", required=True,
                    choices=["preflight", "structural", "graph_style",
                             "reasoning", "narrator", "review", "reporting"])
    ap.add_argument("--project-dir", default=".")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check-and-increment", action="store_true")
    mode.add_argument("--record-attempt", action="store_true")
    mode.add_argument("--status", action="store_true")
    ap.add_argument("--pacs-score", type=float, default=None)
    ap.add_argument("--reason", type=str, default="")
    args = ap.parse_args()

    project_dir = Path(args.project_dir).resolve()
    counter_p, history_p = _paths(project_dir, args.run_id, args.gate)
    ulw = _ulw_active(project_dir)
    max_retries = ULW_MAX if ulw else DEFAULT_MAX

    if args.status:
        _emit({
            "valid": True,
            "run_id": args.run_id, "gate": args.gate,
            "retries_used": _read_counter(counter_p),
            "max_retries": max_retries,
            "ulw_active": ulw,
            "history_count": len(_read_history(history_p)),
        }, 0)

    if args.record_attempt:
        if args.pacs_score is None:
            _emit({"valid": False,
                   "reason": "--pacs-score required for --record-attempt"}, 2)
        entry = {
            "ts": time.time(),
            "gate": args.gate,
            "pacs_score": args.pacs_score,
            "reason": args.reason or None,
        }
        _append_history(history_p, entry)
        _emit({
            "valid": True,
            "run_id": args.run_id, "gate": args.gate,
            "recorded": entry,
            "total_attempts": len(_read_history(history_p)),
        }, 0)

    # --check-and-increment
    used = _read_counter(counter_p)
    if used >= max_retries:
        _emit({
            "valid": False, "can_retry": False,
            "reason": "budget_exhausted",
            "retries_used": used, "max_retries": max_retries,
        }, 1)

    history = _read_history(history_p)
    cb_open, cb_reason = _circuit_breaker_open(history)
    if cb_open:
        _emit({
            "valid": False, "can_retry": False,
            "reason": "circuit_breaker_open",
            "cb_detail": cb_reason,
            "retries_used": used, "max_retries": max_retries,
        }, 1)

    # Budget OK, CB closed → increment and allow
    _write_counter(counter_p, used + 1)
    _emit({
        "valid": True, "can_retry": True,
        "run_id": args.run_id, "gate": args.gate,
        "retries_used": used + 1,
        "max_retries": max_retries,
        "ulw_active": ulw,
    }, 0)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        print(json.dumps({
            "valid": False,
            "error": f"script failure: {type(exc).__name__}: {exc}",
        }))
        sys.exit(2)
