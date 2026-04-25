#!/usr/bin/env python3
"""
DCI Evidence Chain P1 Validation — validate_dci_evidence.py

Verifies every [ev:char:{article_id}:{start}-{end}] marker in the final
report resolves to a registered entry in evidence_ledger.jsonl and to a
valid char span in the source article body (CE4 3-layer chain).

Usage:
    python3 .claude/hooks/scripts/validate_dci_evidence.py \\
        --run-id dci-2026-04-14-1130 --project-dir .

Output: JSON to stdout

Exit codes:
    0 — resolution_rate == 1.00 (every marker resolves)
    1 — any marker fails to resolve (FAIL)
    2 — script error

Checks (EV1-EV6):
    EV1: final_report.md exists and non-empty
    EV2: evidence_ledger.jsonl exists + parseable per-line JSON
    EV3: every [ev:xxx] marker in report is present in ledger
    EV4: each ledger entry has the 3-layer trio (article_id, segment_id, char_span)
    EV5: for each resolved marker: char span is within article body length
    EV6: no marker references invented article_id (must be in corpus)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


# CE4 marker: [ev:char:{article_id}:{start}-{end}]
# Also accept legacy [ev:{16-hex}] CE1 style for transitional runs
CE4_RE = re.compile(r"\[ev:char:([a-zA-Z0-9_\-]+):(\d+)-(\d+)\]")
CE1_RE = re.compile(r"\[ev:([0-9a-f]{16})\]")


def _emit(result: dict[str, Any], exit_code: int) -> None:
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(exit_code)


def _load_ledger(path: Path) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Return (marker_key -> entry_dict), errors."""
    entries: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    with path.open("r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                errors.append(f"ledger line {lineno}: {exc}")
                continue
            marker = obj.get("marker")
            if not isinstance(marker, str):
                errors.append(f"ledger line {lineno}: missing 'marker' key")
                continue
            entries[marker] = obj
    return entries, errors


def _load_corpus_articles(
    project_dir: Path, date: str | None, run_id: str
) -> dict[str, str]:
    """Return article_id → body text. Prefers run-local snapshot."""
    snapshot = (
        project_dir / "data" / "dci" / "runs" / run_id
        / "corpus" / "input_snapshot.jsonl"
    )
    candidates: list[Path] = []
    if snapshot.exists():
        candidates.append(snapshot)
    if date:
        candidates.append(project_dir / "data" / "raw" / date / "all_articles.jsonl")

    articles: dict[str, str] = {}
    for path in candidates:
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                aid = obj.get("content_hash") or obj.get("article_id")
                body = obj.get("body", "")
                if isinstance(aid, str) and isinstance(body, str):
                    articles.setdefault(aid, body)
        if articles:
            break
    return articles


def _validate_resolution(
    report_text: str,
    ledger: dict[str, dict[str, Any]],
    articles: dict[str, str],
) -> tuple[int, int, list[dict[str, Any]], list[str]]:
    """Return (total, resolved, unresolved_details, warnings)."""
    markers_ce4 = CE4_RE.findall(report_text)
    markers_ce1 = CE1_RE.findall(report_text)
    warnings: list[str] = []
    if markers_ce1:
        warnings.append(
            f"legacy CE1 markers detected ({len(markers_ce1)}); DCI expects CE4"
        )

    total = len(markers_ce4)
    unresolved: list[dict[str, Any]] = []
    resolved = 0
    for aid, start_s, end_s in markers_ce4:
        start, end = int(start_s), int(end_s)
        marker_key = f"[ev:char:{aid}:{start}-{end}]"
        entry = ledger.get(marker_key)
        if entry is None:
            unresolved.append({
                "marker": marker_key, "reason": "not in ledger",
            })
            continue
        # EV4: 3-layer completeness
        seg_id = entry.get("segment_id")
        if not seg_id:
            unresolved.append({
                "marker": marker_key, "reason": "ledger missing segment_id",
            })
            continue
        # EV5: char span within body
        body = articles.get(aid, "")
        if not body:
            unresolved.append({
                "marker": marker_key, "reason": f"article {aid} not in corpus",
            })
            continue
        if not (0 <= start < end <= len(body)):
            unresolved.append({
                "marker": marker_key,
                "reason": f"char span invalid: body_len={len(body)}",
            })
            continue
        resolved += 1
    return total, resolved, unresolved, warnings


def main() -> None:
    ap = argparse.ArgumentParser(description="DCI Evidence Chain P1 validator")
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--project-dir", default=".")
    ap.add_argument("--report", default=None, help="Override report path")
    ap.add_argument("--date", default=None, help="Corpus date (for article body lookup)")
    args = ap.parse_args()

    project_dir = Path(args.project_dir).resolve()
    run_dir = project_dir / "data" / "dci" / "runs" / args.run_id

    report_path = Path(args.report) if args.report else run_dir / "final_report.md"
    ledger_path = run_dir / "evidence_ledger.jsonl"

    # EV1
    if not report_path.exists() or report_path.stat().st_size == 0:
        _emit({
            "valid": False, "check": "EV1",
            "reason": f"final_report missing/empty: {report_path}",
        }, 1)
    report_text = report_path.read_text(encoding="utf-8")

    # EV2
    if not ledger_path.exists():
        _emit({
            "valid": False, "check": "EV2",
            "reason": f"evidence_ledger missing: {ledger_path}",
        }, 1)
    ledger, load_errors = _load_ledger(ledger_path)
    if load_errors:
        _emit({
            "valid": False, "check": "EV2",
            "parse_errors": load_errors[:20],
        }, 1)

    articles = _load_corpus_articles(project_dir, args.date, args.run_id)

    total, resolved, unresolved, warnings = _validate_resolution(
        report_text, ledger, articles
    )
    resolution_rate = (resolved / total) if total else 1.0
    valid = (total > 0) and (resolution_rate >= 1.0 - 1e-9) and not unresolved

    result = {
        "valid": valid,
        "run_id": args.run_id,
        "report_path": str(report_path),
        "ledger_path": str(ledger_path),
        "markers_total": total,
        "markers_resolved": resolved,
        "resolution_rate": round(resolution_rate, 6),
        "unresolved": unresolved[:50],
        "unresolved_count": len(unresolved),
        "warnings": warnings,
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
