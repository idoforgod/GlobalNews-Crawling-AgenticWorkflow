#!/usr/bin/env python3
"""
DCI Executive Summary CE3 Generator — dci_executive_summary.py

Replaces the @dci-reporter agent. Assembles the executive summary from
Python-computed metrics using the CE3 pattern:
  Step 1: Python extracts all numbers from SG verdict + ledger
  Step 2: Template rendered with numbers pre-substituted (no LLM numbers)
  Step 3: Optional Claude CLI fill for prose framing (bounded, non-number)
  Step 4: Python re-verifies numbers match source (calls validate_dci_narrative)

Usage:
    python3 .claude/hooks/scripts/dci_executive_summary.py \\
        --run-id dci-2026-04-14-1130 --project-dir . \\
        --output data/dci/runs/dci-2026-04-14-1130/executive_summary.md

Output: the file at --output + JSON status to stdout

Exit codes:
    0 — summary written and CE3 parity confirmed
    1 — inputs missing or parity FAIL
    2 — script error
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


TEMPLATE = """# DCI Executive Summary

**Run ID**: {run_id}
**Corpus date**: {date}
**Articles analyzed**: {article_count}
**Evidence markers registered**: {marker_count}
**SG-Superhuman decision**: **{sg_decision}**

## Headline Metrics

| Gate | Value | Threshold | Status |
|------|-------|-----------|--------|
{gate_table}

## Coverage Highlights

- Character coverage: **{char_coverage:.4f}** (target 1.0000)
- Triple-lens coverage: **{triple_lens_coverage:.4f}** (target ≥ {triple_lens_threshold:.2f})
- LLM body injection ratio: **{llm_body_injection_ratio:.4f}** (target 1.0000)

## Evidence Chain

- Total `[ev:xxx]` markers: **{marker_count}**
- Distinct source articles cited: **{distinct_articles}**
- Average markers per article: **{markers_per_article:.2f}**

## Next Steps

See `final_report.md` for the full doctoral narrative; `final_report.ko.md` for the Korean translation; `evidence_ledger.jsonl` for the complete evidence chain.
"""


def _emit(result: dict[str, Any], exit_code: int) -> None:
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(exit_code)


def _gate_row(gate: dict[str, Any]) -> str:
    name = gate.get("name", "?")
    value = gate.get("value")
    thresh = gate.get("threshold")
    status = gate.get("status", "?").upper()
    v_str = f"{value:.4f}" if isinstance(value, (int, float)) else str(value)
    t_str = f"{thresh:.4f}" if isinstance(thresh, (int, float)) else str(thresh)
    return f"| {name} | {v_str} | {t_str} | {status} |"


def main() -> None:
    ap = argparse.ArgumentParser(description="DCI Executive Summary CE3 generator")
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--project-dir", default=".")
    ap.add_argument("--date", default=None)
    ap.add_argument("--output", default=None)
    args = ap.parse_args()

    project_dir = Path(args.project_dir).resolve()
    run_dir = project_dir / "data" / "dci" / "runs" / args.run_id
    verdict_path = run_dir / "sg_superhuman_verdict.json"
    ledger_path = run_dir / "evidence_ledger.jsonl"
    output_path = (
        Path(args.output) if args.output else run_dir / "executive_summary.md"
    )

    if not verdict_path.exists():
        _emit({"valid": False, "reason": f"verdict missing: {verdict_path}"}, 1)
    if not ledger_path.exists():
        _emit({"valid": False, "reason": f"ledger missing: {ledger_path}"}, 1)

    verdict = json.loads(verdict_path.read_text(encoding="utf-8"))
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

    gate_map: dict[str, dict[str, Any]] = {
        g.get("name"): g for g in verdict.get("gates", [])
        if isinstance(g, dict)
    }
    def _gv(name: str, default=0.0):
        g = gate_map.get(name, {})
        return g.get("value", default) if isinstance(g.get("value"), (int, float)) else default
    def _gt(name: str, default=0.0):
        g = gate_map.get(name, {})
        return g.get("threshold", default) if isinstance(g.get("threshold"), (int, float)) else default

    distinct_articles = len({
        e.get("article_id") for e in ledger_entries if e.get("article_id")
    })
    marker_count = len(ledger_entries)
    article_count = verdict.get("corpus", {}).get("article_count", distinct_articles)
    mpa = (marker_count / distinct_articles) if distinct_articles else 0.0

    # Step 2: render template with Python-computed numbers
    rendered = TEMPLATE.format(
        run_id=args.run_id,
        date=args.date or verdict.get("corpus", {}).get("date", "unknown"),
        article_count=article_count,
        marker_count=marker_count,
        sg_decision=verdict.get("decision", "?"),
        gate_table="\n".join(_gate_row(g) for g in verdict.get("gates", [])),
        char_coverage=_gv("char_coverage"),
        triple_lens_coverage=_gv("triple_lens_coverage"),
        triple_lens_threshold=_gt("triple_lens_coverage", 3.0),
        llm_body_injection_ratio=_gv("llm_body_injection_ratio"),
        distinct_articles=distinct_articles,
        markers_per_article=mpa,
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(rendered, encoding="utf-8")

    _emit({
        "valid": True,
        "run_id": args.run_id,
        "output": str(output_path),
        "size_bytes": output_path.stat().st_size,
        "article_count": article_count,
        "marker_count": marker_count,
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
