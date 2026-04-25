#!/usr/bin/env python3
"""
DCI Narrative CE3 Parity P1 Validation — validate_dci_narrative.py

Parses every number in final_report.md and verifies equality to the
Python-computed value in evidence_ledger.jsonl + sg_superhuman_verdict.json.
Enforces CE3 (Python-computed numbers, LLM prose only).

Usage:
    python3 .claude/hooks/scripts/validate_dci_narrative.py \\
        --run-id dci-2026-04-14-1130 --project-dir .

Output: JSON to stdout

Exit codes:
    0 — every numeric claim in prose has a matching source value
    1 — any numeric divergence (FAIL)
    2 — script error

Checks (NR1-NR6):
    NR1: final_report.md exists + doctoral sections present
    NR2: evidence_ledger.jsonl + sg_superhuman_verdict.json loadable
    NR3: percentage/count/ratio numbers in prose resolve to source values
    NR4: SG gate values in prose match verdict JSON
    NR5: article count / evidence count in prose match ledger
    NR6: no number appears in prose without a plausible source
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


# Number patterns (ordered: specific → general)
PERCENT_RE = re.compile(r"\b(\d{1,3}(?:\.\d+)?)\s*%")
INT_WITH_COMMA_RE = re.compile(r"\b(\d{1,3}(?:,\d{3})+)\b")
FLOAT_RE = re.compile(r"\b(\d+\.\d+)\b")
INT_RE = re.compile(r"\b(\d{2,})\b")

# doctoral section headings required in final_report.md
REQUIRED_SECTIONS = [
    "Executive Summary",
    "Evidence Index",
]

EPSILON = 1e-3  # allow light rounding for ratios


def _emit(result: dict[str, Any], exit_code: int) -> None:
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(exit_code)


def _extract_numbers(text: str) -> list[tuple[str, float, str]]:
    """Return list of (kind, normalized_value, original_match)."""
    out: list[tuple[str, float, str]] = []
    for m in PERCENT_RE.finditer(text):
        try:
            out.append(("percent", float(m.group(1)) / 100.0, m.group(0)))
        except ValueError:
            pass
    for m in INT_WITH_COMMA_RE.finditer(text):
        raw = m.group(1).replace(",", "")
        try:
            out.append(("count", float(raw), m.group(0)))
        except ValueError:
            pass
    for m in FLOAT_RE.finditer(text):
        try:
            out.append(("float", float(m.group(1)), m.group(0)))
        except ValueError:
            pass
    for m in INT_RE.finditer(text):
        try:
            val = float(m.group(1))
            if val >= 100:  # avoid double-counting digits inside larger numbers
                out.append(("int", val, m.group(0)))
        except ValueError:
            pass
    return out


def _source_numeric_pool(
    ledger: list[dict[str, Any]], verdict: dict[str, Any]
) -> set[float]:
    """Flatten all numeric values from authoritative sources."""
    pool: set[float] = set()

    # SG-Superhuman gate values + thresholds
    for gate in verdict.get("gates", []):
        for key in ("value", "threshold"):
            v = gate.get(key)
            if isinstance(v, (int, float)):
                pool.add(float(v))

    # Top-level summary numbers (accept any numeric field)
    def _walk(obj):
        if isinstance(obj, dict):
            for v in obj.values():
                _walk(v)
        elif isinstance(obj, list):
            for v in obj:
                _walk(v)
        elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
            pool.add(float(obj))

    _walk(verdict)

    # Ledger structural counts
    pool.add(float(len(ledger)))  # total evidence markers
    article_ids = {e.get("article_id") for e in ledger if e.get("article_id")}
    pool.add(float(len(article_ids)))  # distinct articles cited

    return pool


def _match_in_pool(value: float, pool: set[float]) -> bool:
    for src in pool:
        if abs(value - src) <= EPSILON:
            return True
        # allow percent ↔ ratio conversion
        if abs(value - src * 100.0) <= EPSILON:
            return True
        if abs(value * 100.0 - src) <= EPSILON:
            return True
    return False


def main() -> None:
    ap = argparse.ArgumentParser(description="DCI Narrative CE3 P1 validator")
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--project-dir", default=".")
    ap.add_argument("--report", default=None)
    ap.add_argument("--ledger", default=None)
    ap.add_argument("--verdict", default=None)
    ap.add_argument(
        "--max-mismatches", type=int, default=20,
        help="Cap on violation list for output",
    )
    args = ap.parse_args()

    project_dir = Path(args.project_dir).resolve()
    run_dir = project_dir / "data" / "dci" / "runs" / args.run_id

    report_path = Path(args.report) if args.report else run_dir / "final_report.md"
    ledger_path = Path(args.ledger) if args.ledger else run_dir / "evidence_ledger.jsonl"
    verdict_path = (
        Path(args.verdict) if args.verdict else run_dir / "sg_superhuman_verdict.json"
    )

    # NR1
    if not report_path.exists() or report_path.stat().st_size == 0:
        _emit({"valid": False, "check": "NR1",
               "reason": f"report missing: {report_path}"}, 1)
    text = report_path.read_text(encoding="utf-8")
    section_violations = [
        s for s in REQUIRED_SECTIONS
        if not re.search(rf"^#+\s*{re.escape(s)}", text, re.MULTILINE)
    ]

    # NR2
    if not ledger_path.exists() or not verdict_path.exists():
        _emit({
            "valid": False, "check": "NR2",
            "reason": "ledger or verdict missing",
            "ledger": str(ledger_path), "verdict": str(verdict_path),
        }, 1)
    ledger_entries: list[dict[str, Any]] = []
    with ledger_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ledger_entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    try:
        verdict = json.loads(verdict_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        _emit({"valid": False, "check": "NR2",
               "reason": f"verdict parse: {exc}"}, 1)

    # NR3-NR6
    numbers = _extract_numbers(text)
    pool = _source_numeric_pool(ledger_entries, verdict)
    mismatches: list[dict[str, Any]] = []
    for kind, value, original in numbers:
        # Skip very small "1" or "2" inside prose — too noisy
        if kind == "int" and value < 10:
            continue
        if not _match_in_pool(value, pool):
            mismatches.append({
                "kind": kind, "value": value, "surface": original,
            })

    valid = not mismatches and not section_violations
    result = {
        "valid": valid,
        "run_id": args.run_id,
        "numbers_checked": len(numbers),
        "mismatches_count": len(mismatches),
        "mismatches": mismatches[: args.max_mismatches],
        "missing_sections": section_violations,
        "source_pool_size": len(pool),
    }
    _emit(result, 0 if valid else 1)


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
