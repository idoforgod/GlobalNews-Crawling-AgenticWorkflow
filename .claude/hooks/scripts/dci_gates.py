#!/usr/bin/env python3
"""
DCI Phase Transition P1 Gate — dci_gates.py

Deterministic decision CLI that the @dci-execution-orchestrator invokes
BEFORE advancing from one phase to the next. The orchestrator reads the
exit code ONLY — never interprets the JSON rationale.

Usage:
    python3 .claude/hooks/scripts/dci_gates.py \\
        --check phase-transition --from preflight --to structural \\
        --run-id dci-2026-04-14-1130 --project-dir .

    python3 .claude/hooks/scripts/dci_gates.py \\
        --check reconcile-reviews --run-id {run_id} --project-dir .

Output: JSON to stdout

Exit codes:
    0 — transition allowed / check PASS
    1 — transition blocked / check FAIL (orchestrator must retry/escalate)
    2 — script error

Checks:
    phase-transition: prerequisites for advancing to the target phase
    reconcile-reviews: Phase 6 three-reviewer consensus arbitration
    finalize: Phase 7 preconditions (all artifacts present)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


PHASES = [
    "preflight", "structural", "graph_style",
    "reasoning", "narrator", "review", "reporting",
]

LAYER_BY_PHASE = {
    "structural": ["L-1_external", "L0_discourse", "L1_semantic",
                   "L1_5_meaning", "L2_relations"],
    "graph_style": ["L3_kg", "L4_cross_document", "L5_psycho_style"],
    "reasoning": ["L6_triadic", "L7_got", "L8_monte_carlo", "L9_metacognitive"],
    "narrator": ["L10_narrator"],
}

# Layers whose failure MUST abort (per failure_policy §Failure Matrix)
MANDATORY_LAYERS = {"L0_discourse", "L6_triadic", "L10_narrator"}


def _emit(result: dict[str, Any], exit_code: int) -> None:
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(exit_code)


def _load_sot_state(project_dir: Path, run_id: str) -> dict[str, Any] | None:
    """Read workflows.dci.* state for this run from SOT (canonical or legacy)."""
    state_path = project_dir / ".claude" / "state.yaml"
    if not state_path.exists():
        return None
    try:
        import yaml
        data = yaml.safe_load(state_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return None
    run_entry = (
        data.get("execution", {})
            .get("runs", {})
            .get(run_id, {})
    )
    workflows = run_entry.get("workflows", {}) if isinstance(run_entry, dict) else {}
    # Canonical
    dci = workflows.get("dci")
    if isinstance(dci, dict):
        return dci
    # Legacy
    legacy = workflows.get("master", {}).get("phases", {}).get("dci")
    if isinstance(legacy, dict):
        return legacy
    return None


def _layer_status(state: dict[str, Any], layer_id: str) -> str | None:
    layers = state.get("layers", {}) if state else {}
    if not isinstance(layers, dict):
        return None
    entry = layers.get(layer_id)
    if isinstance(entry, dict):
        return entry.get("status")
    return None


def _check_phase_transition(
    project_dir: Path, run_id: str, from_phase: str, to_phase: str
) -> tuple[bool, dict[str, Any]]:
    if from_phase not in PHASES:
        return False, {"reason": f"unknown from_phase: {from_phase}"}
    if to_phase not in PHASES:
        return False, {"reason": f"unknown to_phase: {to_phase}"}

    from_idx = PHASES.index(from_phase)
    to_idx = PHASES.index(to_phase)
    if to_idx != from_idx + 1:
        return False, {
            "reason": f"non-sequential transition: {from_phase} → {to_phase}",
        }

    state = _load_sot_state(project_dir, run_id)
    if state is None:
        return False, {"reason": "SOT state for run not found"}

    # Preflight → Structural needs preflight PASS
    if to_phase == "structural":
        pf = state.get("preflight", {})
        if pf.get("status") != "PASS":
            return False, {"reason": "preflight did not PASS", "preflight": pf}

    # Advancing past a layer group requires every mandatory layer completed
    if from_phase in LAYER_BY_PHASE:
        missing = []
        for lid in LAYER_BY_PHASE[from_phase]:
            st = _layer_status(state, lid)
            if lid in MANDATORY_LAYERS and st != "completed":
                missing.append({"layer": lid, "status": st})
        if missing:
            return False, {
                "reason": "mandatory layers not completed",
                "missing": missing,
            }

    # Review → Reporting needs all Phase 6 gates green
    if to_phase == "reporting":
        review = state.get("review", {})
        required = {
            "sg_verdict": "PASS",
            "narrative_parity": "PASS",
            "team_consensus": "PASS",
        }
        for key, expected in required.items():
            if review.get(key) != expected:
                return False, {
                    "reason": f"review.{key} != {expected}",
                    "review": review,
                }

    return True, {"from": from_phase, "to": to_phase}


def _check_reconcile_reviews(project_dir: Path, run_id: str) -> tuple[bool, dict[str, Any]]:
    """Phase 6 arbiter: all 3 reviewer Python backends must exit 0."""
    run_dir = project_dir / "data" / "dci" / "runs" / run_id
    review_dir = run_dir / "phase6"
    required_artifacts = {
        "sg": review_dir / "sg_review.md",
        "evidence": review_dir / "evidence_review.md",
        "narrative": review_dir / "narrative_review.md",
    }
    missing = {k: str(v) for k, v in required_artifacts.items() if not v.exists()}
    if missing:
        return False, {"reason": "reviewer artifacts missing", "missing": missing}

    # Python backends (re-run as smoke check is out of scope here;
    # we trust earlier Phase 6 logs at {run_dir}/phase6/*.json for verdicts)
    verdict_files = {
        "sg": review_dir / "sg_review.json",
        "evidence": review_dir / "evidence_review.json",
        "narrative": review_dir / "narrative_review.json",
    }
    verdicts: dict[str, str] = {}
    for key, p in verdict_files.items():
        if not p.exists():
            return False, {"reason": f"{key} verdict json missing", "path": str(p)}
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            return False, {"reason": f"{key} verdict parse: {exc}"}
        v = obj.get("verdict") or obj.get("valid")
        verdicts[key] = (
            "PASS" if v is True or v == "PASS" else
            ("FAIL" if v is False or v == "FAIL" else str(v))
        )

    consensus = all(v == "PASS" for v in verdicts.values())
    return consensus, {"verdicts": verdicts, "consensus": consensus}


def _check_finalize(project_dir: Path, run_id: str) -> tuple[bool, dict[str, Any]]:
    run_dir = project_dir / "data" / "dci" / "runs" / run_id
    required = [
        "final_report.md",
        "evidence_ledger.jsonl",
        "sg_superhuman_verdict.json",
    ]
    missing = [r for r in required if not (run_dir / r).exists()]
    if missing:
        return False, {"reason": "artifacts missing", "missing": missing}
    return True, {"run_dir": str(run_dir)}


def main() -> None:
    ap = argparse.ArgumentParser(description="DCI phase transition gate")
    ap.add_argument("--check", required=True,
                    choices=["phase-transition", "reconcile-reviews", "finalize"])
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--project-dir", default=".")
    ap.add_argument("--from", dest="from_phase", default=None)
    ap.add_argument("--to", dest="to_phase", default=None)
    args = ap.parse_args()

    project_dir = Path(args.project_dir).resolve()

    if args.check == "phase-transition":
        if not args.from_phase or not args.to_phase:
            _emit({"valid": False, "reason": "--from and --to required"}, 2)
        ok, meta = _check_phase_transition(
            project_dir, args.run_id, args.from_phase, args.to_phase
        )
    elif args.check == "reconcile-reviews":
        ok, meta = _check_reconcile_reviews(project_dir, args.run_id)
    elif args.check == "finalize":
        ok, meta = _check_finalize(project_dir, args.run_id)
    else:
        _emit({"valid": False, "reason": "unknown --check"}, 2)

    _emit({
        "valid": ok, "check": args.check, "run_id": args.run_id, "meta": meta,
    }, 0 if ok else 1)


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
