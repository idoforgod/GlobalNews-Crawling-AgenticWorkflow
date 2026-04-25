#!/usr/bin/env python3
"""
DCI Char Coverage P1 Validation — validate_dci_char_coverage.py

Thin CLI wrapper around src/dci/char_coverage.CharCoverageVerifier.
Enforces the three coverage gates (G1, G2, G3) from SG-Superhuman
without requiring agents to re-derive them.

Usage:
    python3 .claude/hooks/scripts/validate_dci_char_coverage.py \\
        --run-id dci-2026-04-14-1130 --project-dir .

Output: JSON to stdout

Exit codes:
    0 — all three coverage gates PASS
    1 — any gate FAIL
    2 — script error

Checks (CC1-CC4):
    CC1: coverage artifact exists (layers/L10_narrator/coverage_stats.json
         OR sg_superhuman_verdict.json with gate values)
    CC2: char_coverage == 1.00 (G1)
    CC3: triple_lens_coverage ≥ DCI_SG_TRIPLE_LENS_COVERAGE_MIN (G2)
    CC4: llm_body_injection_ratio == 1.00 (G3)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


EPSILON = 1e-6


def _emit(result: dict[str, Any], exit_code: int) -> None:
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(exit_code)


def _load_thresholds(project_dir: Path) -> dict[str, float]:
    sys.path.insert(0, str(project_dir))
    try:
        from src.config import constants as C
    except Exception:
        return {
            "char_coverage": 1.0,
            "triple_lens_coverage": 3.0,
            "llm_body_injection_ratio": 1.0,
        }
    return {
        "char_coverage": float(getattr(C, "DCI_SG_CHAR_COVERAGE_MIN", 1.0)),
        "triple_lens_coverage": float(getattr(C, "DCI_SG_TRIPLE_LENS_COVERAGE_MIN", 3.0)),
        "llm_body_injection_ratio": float(
            getattr(C, "DCI_SG_LLM_BODY_INJECTION_RATIO_MIN", 1.0)
        ),
    }


def _extract_from_verdict(verdict: dict[str, Any]) -> dict[str, float]:
    values: dict[str, float] = {}
    for gate in verdict.get("gates", []):
        name = gate.get("name")
        value = gate.get("value")
        if name in {"char_coverage", "triple_lens_coverage",
                    "llm_body_injection_ratio"} and isinstance(value, (int, float)):
            values[name] = float(value)
    return values


def main() -> None:
    ap = argparse.ArgumentParser(description="DCI Char Coverage P1 validator")
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--project-dir", default=".")
    args = ap.parse_args()

    project_dir = Path(args.project_dir).resolve()
    run_dir = project_dir / "data" / "dci" / "runs" / args.run_id
    thresholds = _load_thresholds(project_dir)

    # Prefer dedicated coverage_stats.json; fall back to SG verdict
    coverage_path = run_dir / "layers" / "L10_narrator" / "coverage_stats.json"
    verdict_path = run_dir / "sg_superhuman_verdict.json"

    values: dict[str, float] = {}
    source: str | None = None
    if coverage_path.exists():
        try:
            values = json.loads(coverage_path.read_text(encoding="utf-8"))
            source = str(coverage_path)
        except json.JSONDecodeError:
            pass
    if not values and verdict_path.exists():
        try:
            verdict = json.loads(verdict_path.read_text(encoding="utf-8"))
            values = _extract_from_verdict(verdict)
            source = str(verdict_path)
        except json.JSONDecodeError:
            pass

    if not values:
        _emit({
            "valid": False, "check": "CC1",
            "reason": "no coverage data source available",
            "tried": [str(coverage_path), str(verdict_path)],
        }, 1)

    violations: list[str] = []

    cc = values.get("char_coverage")
    if cc is None:
        violations.append("CC2: char_coverage missing")
    elif abs(cc - thresholds["char_coverage"]) > EPSILON:
        violations.append(
            f"CC2: char_coverage={cc} != {thresholds['char_coverage']}"
        )

    tlc = values.get("triple_lens_coverage")
    if tlc is None:
        violations.append("CC3: triple_lens_coverage missing")
    elif tlc + EPSILON < thresholds["triple_lens_coverage"]:
        violations.append(
            f"CC3: triple_lens_coverage={tlc} < {thresholds['triple_lens_coverage']}"
        )

    inj = values.get("llm_body_injection_ratio")
    if inj is None:
        violations.append("CC4: llm_body_injection_ratio missing")
    elif abs(inj - thresholds["llm_body_injection_ratio"]) > EPSILON:
        violations.append(
            f"CC4: llm_body_injection_ratio={inj} != {thresholds['llm_body_injection_ratio']}"
        )

    result = {
        "valid": not violations,
        "run_id": args.run_id,
        "source": source,
        "values": values,
        "thresholds": thresholds,
        "violations": violations,
    }
    _emit(result, 0 if not violations else 1)


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
