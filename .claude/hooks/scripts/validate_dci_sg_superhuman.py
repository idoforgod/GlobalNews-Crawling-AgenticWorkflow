#!/usr/bin/env python3
"""
DCI SG-Superhuman Verdict P1 Validation — validate_dci_sg_superhuman.py

Re-verifies the 10-gate SG-Superhuman verdict produced by
src/dci/sg_superhuman.py. Agents (e.g. @dci-sg-superhuman-auditor)
MUST NOT recompute these — they read this CLI's exit code and quote
its numbers verbatim.

Usage:
    python3 .claude/hooks/scripts/validate_dci_sg_superhuman.py \\
        --run-id dci-2026-04-14-1130 --project-dir .

Output: JSON to stdout
    {"valid": bool, "decision": "PASS|FAIL", "gates": [...], ...}

Exit codes:
    0 — verdict PASS (all 10 gates PASS or whitelisted SKIP)
    1 — verdict FAIL (any mandatory gate FAIL or SKIP)
    2 — script error

Gates (reproduced from src/dci/sg_superhuman.py docstring):
    G1. char_coverage                     (mandatory, =1.00)
    G2. triple_lens_coverage              (mandatory, ≥ threshold)
    G3. llm_body_injection_ratio          (mandatory, =1.00)
    G4. technique_completeness            (mandatory, 93/93)
    G5. nli_verification_pass_rate        (mandatory, ≥ 0.95)
    G6. triadic_consensus_rate            (mandatory, ≥ 0.60)
    G7. adversarial_critic_pass           (mandatory, ≥ 0.90)
    G8. evidence_3layer_complete          (mandatory, =1.00)
    G9. technique_mode_compliance         (mandatory, =1.00)
    G10. uncertainty_quantified           (mandatory, presence)

Validation layers:
    SG-V1: verdict file exists + parseable JSON
    SG-V2: decision field in {"PASS", "FAIL"}
    SG-V3: gates dict non-empty, count == 10 (or declared count)
    SG-V4: each gate has {name, status, value, threshold}
    SG-V5: gate status in {pass, fail, skip}
    SG-V6: arithmetic — each gate's status matches its value vs threshold
    SG-V7: decision consistency — PASS iff every mandatory gate is pass
    SG-V8: no gate is silently skipped without justification
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


MANDATORY_GATES = {
    "char_coverage", "triple_lens_coverage", "llm_body_injection_ratio",
    "technique_completeness", "nli_verification_pass_rate",
    "triadic_consensus_rate", "adversarial_critic_pass",
    "evidence_3layer_complete", "technique_mode_compliance",
    "uncertainty_quantified",
}

EQUALITY_GATES = {"char_coverage", "llm_body_injection_ratio",
                  "evidence_3layer_complete", "technique_mode_compliance"}
EPSILON = 1e-6


def _emit(result: dict[str, Any], exit_code: int) -> None:
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(exit_code)


def _check_arithmetic(gate: dict[str, Any]) -> tuple[bool, str]:
    """SG-V6: Given declared status, value, threshold → does the status hold?"""
    status = gate.get("status")
    if status == "skip":
        return True, ""
    value = gate.get("value")
    threshold = gate.get("threshold")
    name = gate.get("name", "?")
    if value is None or threshold is None:
        return False, f"{name}: value or threshold missing"
    try:
        v = float(value)
        t = float(threshold)
    except (TypeError, ValueError):
        return False, f"{name}: value/threshold not numeric"
    if name in EQUALITY_GATES:
        passes = abs(v - t) <= EPSILON
    else:
        passes = v + EPSILON >= t
    declared_pass = (status == "pass")
    if passes != declared_pass:
        return False, (
            f"{name}: value={v} threshold={t} computed_pass={passes} "
            f"but declared status={status!r}"
        )
    return True, ""


def _validate_verdict(verdict: dict[str, Any]) -> tuple[bool, list[str], dict[str, Any]]:
    violations: list[str] = []
    meta: dict[str, Any] = {}

    # SG-V2: decision enum
    decision = verdict.get("decision")
    if decision not in {"PASS", "FAIL"}:
        violations.append(f"SG-V2: decision {decision!r} not in PASS|FAIL")

    # SG-V3: gates structure
    gates = verdict.get("gates")
    if not isinstance(gates, list) or not gates:
        violations.append("SG-V3: gates missing or not a non-empty list")
        return False, violations, meta

    gate_names = [g.get("name") for g in gates if isinstance(g, dict)]
    meta["gate_count"] = len(gate_names)
    missing = MANDATORY_GATES - set(gate_names)
    if missing:
        violations.append(f"SG-V3: mandatory gates missing: {sorted(missing)}")

    # SG-V4 + V5 + V6
    gate_status: dict[str, str] = {}
    for gate in gates:
        if not isinstance(gate, dict):
            violations.append(f"SG-V4: gate entry not dict: {gate!r}")
            continue
        name = gate.get("name")
        status = gate.get("status")
        if name is None:
            violations.append("SG-V4: gate name missing")
            continue
        if status not in {"pass", "fail", "skip"}:
            violations.append(f"SG-V5: {name}: status {status!r} invalid")
            continue
        gate_status[name] = status
        ok, msg = _check_arithmetic(gate)
        if not ok:
            violations.append(f"SG-V6: {msg}")

    meta["statuses"] = gate_status

    # SG-V7: decision consistency
    mandatory_pass = all(
        gate_status.get(name) == "pass"
        for name in MANDATORY_GATES
    )
    if decision == "PASS" and not mandatory_pass:
        violations.append(
            "SG-V7: decision=PASS but at least one mandatory gate is not pass"
        )
    if decision == "FAIL" and mandatory_pass:
        violations.append(
            "SG-V7: decision=FAIL but every mandatory gate is pass (unexpected)"
        )

    # SG-V8: no unjustified skips
    for name in MANDATORY_GATES:
        if gate_status.get(name) == "skip":
            justification = next(
                (g.get("details", {}).get("skip_reason") for g in gates
                 if g.get("name") == name),
                None,
            )
            if not justification:
                violations.append(
                    f"SG-V8: mandatory gate {name} skipped without justification"
                )

    meta["mandatory_all_pass"] = mandatory_pass
    meta["decision"] = decision
    return not violations, violations, meta


def main() -> None:
    ap = argparse.ArgumentParser(description="DCI SG-Superhuman P1 validator")
    ap.add_argument("--run-id", required=True, help="DCI run id")
    ap.add_argument("--project-dir", default=".", help="Project root")
    ap.add_argument(
        "--verdict-file", default=None,
        help="Explicit verdict path (default: data/dci/runs/{run_id}/sg_superhuman_verdict.json)",
    )
    args = ap.parse_args()

    project_dir = Path(args.project_dir).resolve()
    if args.verdict_file:
        verdict_path = Path(args.verdict_file).resolve()
    else:
        verdict_path = (
            project_dir / "data" / "dci" / "runs" / args.run_id
            / "sg_superhuman_verdict.json"
        )

    if not verdict_path.exists():
        _emit({
            "valid": False,
            "reason": "SG-V1: verdict file not found",
            "path": str(verdict_path),
        }, 1)

    try:
        with verdict_path.open("r", encoding="utf-8") as f:
            verdict = json.load(f)
    except json.JSONDecodeError as exc:
        _emit({
            "valid": False,
            "reason": f"SG-V1: verdict JSON parse error: {exc}",
            "path": str(verdict_path),
        }, 1)

    ok, violations, meta = _validate_verdict(verdict)
    result = {
        "valid": ok,
        "run_id": args.run_id,
        "verdict_path": str(verdict_path),
        "decision": verdict.get("decision"),
        "meta": meta,
        "violations": violations,
    }
    _emit(result, 0 if ok else 1)


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
