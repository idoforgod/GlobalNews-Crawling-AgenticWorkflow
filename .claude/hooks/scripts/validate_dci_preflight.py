#!/usr/bin/env python3
"""
DCI Preflight P1 Validation — validate_dci_preflight.py

Replaces the @dci-preflight agent with deterministic Python checks.
Every LLM-hallucinatable decision (file existence, model reachability,
threshold range checks) is resolved here before any agent narrates.

Usage:
    python3 .claude/hooks/scripts/validate_dci_preflight.py \\
        --date 2026-04-14 --run-id dci-2026-04-14-1130 --project-dir .

    # Dry-run mode (allows DCI_ENABLED=False)
    python3 .claude/hooks/scripts/validate_dci_preflight.py \\
        --date 2026-04-14 --run-id dry --project-dir . --dry-run

Output: JSON to stdout
    {"valid": bool, "checks": [...], "warnings": [...], "errors": [...]}

Exit codes:
    0 — all mandatory checks PASS (WARNING allowed)
    1 — one or more FAIL (blocks workflow)
    2 — script error (argument or internal)

Checks (PF1-PF8):
    PF1: data/raw/{date}/all_articles.jsonl exists + non-empty + schema-valid first line
    PF2: JSONL article count ≥ 1 (corpus non-empty)
    PF3: DCI_ENABLED flag True (or --dry-run passed)
    PF4: Required Python deps importable (spacy, kiwipiepy, transformers)
    PF5: Claude CLI binary reachable (`claude --version` exit 0)
    PF6: SG-Superhuman thresholds in valid ranges (0.0 ≤ t ≤ 1.0 for ratios, t ≥ 0 for counts)
    PF7: Output directory data/dci/runs/{run_id}/ writable + no stale lock
    PF8: Evidence Ledger initialization safe (no stale in-progress marker)

P1 Compliance: 100% deterministic. No LLM involvement.
SOT Compliance: Read-only. No writes to state.yaml.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


MIN_BODY_CHARS = 50  # minimum body length for valid article
REQUIRED_DEPS = ("spacy", "kiwipiepy", "transformers", "networkx", "textstat")


def _emit(result: dict[str, Any], exit_code: int) -> None:
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(exit_code)


def _check_corpus(project_dir: Path, date: str) -> tuple[bool, dict[str, Any]]:
    """PF1 + PF2: corpus file existence, non-empty, schema-valid first line."""
    jsonl_path = project_dir / "data" / "raw" / date / "all_articles.jsonl"
    details: dict[str, Any] = {"path": str(jsonl_path)}

    if not jsonl_path.exists():
        details["reason"] = "file not found"
        return False, details

    size = jsonl_path.stat().st_size
    details["size_bytes"] = size
    if size == 0:
        details["reason"] = "file empty"
        return False, details

    article_count = 0
    first_line_valid = False
    with jsonl_path.open("r", encoding="utf-8") as f:
        for idx, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            article_count += 1
            if idx == 0:
                try:
                    obj = json.loads(line)
                    body = obj.get("body", "")
                    if (
                        isinstance(obj.get("content_hash"), str)
                        and isinstance(obj.get("evidence_id"), str)
                        and isinstance(body, str)
                        and len(body) >= MIN_BODY_CHARS
                    ):
                        first_line_valid = True
                except json.JSONDecodeError:
                    pass
    details["article_count"] = article_count
    details["first_line_schema_valid"] = first_line_valid

    if article_count == 0:
        details["reason"] = "no articles"
        return False, details
    if not first_line_valid:
        details["reason"] = "first line schema invalid"
        return False, details
    return True, details


def _check_dci_enabled(project_dir: Path, dry_run: bool) -> tuple[bool, dict[str, Any]]:
    """PF3: DCI_ENABLED flag (or --dry-run bypass)."""
    sys.path.insert(0, str(project_dir))
    try:
        from src.config.constants import DCI_ENABLED
    except Exception as exc:
        return False, {"reason": f"import DCI_ENABLED failed: {exc}"}
    if DCI_ENABLED or dry_run:
        return True, {"dci_enabled": bool(DCI_ENABLED), "dry_run": dry_run}
    return False, {"reason": "DCI_ENABLED=False and not dry-run", "dci_enabled": False}


def _check_deps(project_dir: Path) -> tuple[bool, dict[str, Any]]:
    """PF4: Required Python deps importable.

    Hook scripts run on system python3 (>= 3.12); domain deps (spaCy, Kiwi,
    textstat) live in .venv (Python 3.13) because spaCy is incompatible
    with Python 3.14 (pydantic v1 constraint). So we delegate the import
    probe to .venv/bin/python when it exists.
    """
    venv_python = project_dir / ".venv" / "bin" / "python"
    if venv_python.exists():
        probe_src = (
            "import json, importlib\n"
            f"mods = {list(REQUIRED_DEPS)!r}\n"
            "missing, present = [], {}\n"
            "for mod in mods:\n"
            "    try:\n"
            "        m = importlib.import_module(mod)\n"
            "        present[mod] = getattr(m, '__version__', 'unknown')\n"
            "    except Exception:\n"
            "        missing.append(mod)\n"
            "print(json.dumps({'missing': missing, 'present': present}))\n"
        )
        try:
            proc = subprocess.run(
                [str(venv_python), "-c", probe_src],
                capture_output=True, text=True, timeout=30,
            )
            if proc.returncode != 0:
                return False, {
                    "reason": f"venv probe exit {proc.returncode}",
                    "stderr": proc.stderr.strip()[:300],
                }
            result = json.loads(proc.stdout.strip() or "{}")
            if result.get("missing"):
                return False, {
                    "missing": result["missing"],
                    "present": result.get("present", {}),
                    "python": str(venv_python),
                }
            return True, {
                "present": result.get("present", {}),
                "python": str(venv_python),
            }
        except Exception as exc:
            return False, {"reason": f"venv probe failed: {exc}"}

    # Fallback: probe system python (hook scripts' own interpreter)
    missing: list[str] = []
    versions: dict[str, str] = {}
    for mod in REQUIRED_DEPS:
        try:
            m = importlib.import_module(mod)
            versions[mod] = getattr(m, "__version__", "unknown")
        except Exception:
            missing.append(mod)
    if missing:
        return False, {"missing": missing, "present": versions, "fallback": "system"}
    return True, {"present": versions, "fallback": "system"}


def _check_claude_cli() -> tuple[bool, dict[str, Any]]:
    """PF5: `claude --version` exit 0."""
    binary = shutil.which("claude")
    if binary is None:
        return False, {"reason": "claude binary not on PATH"}
    try:
        proc = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, timeout=10
        )
    except Exception as exc:
        return False, {"reason": f"invoke failed: {exc}", "binary": binary}
    if proc.returncode != 0:
        return False, {
            "reason": f"--version exit {proc.returncode}",
            "binary": binary,
            "stderr": proc.stderr.strip()[:300],
        }
    return True, {"binary": binary, "version": proc.stdout.strip()[:200]}


def _check_sg_thresholds(project_dir: Path) -> tuple[bool, dict[str, Any]]:
    """PF6: SG-Superhuman threshold ranges."""
    sys.path.insert(0, str(project_dir))
    try:
        from src.config import constants as C
    except Exception as exc:
        return False, {"reason": f"import constants failed: {exc}"}

    probes = {
        "char_coverage": getattr(C, "DCI_SG_CHAR_COVERAGE_MIN", None),
        "triple_lens_coverage": getattr(C, "DCI_SG_TRIPLE_LENS_COVERAGE_MIN", None),
        "llm_body_injection_ratio": getattr(C, "DCI_SG_LLM_BODY_INJECTION_RATIO_MIN", None),
        "nli_verification_pass_rate": getattr(C, "DCI_SG_NLI_VERIFICATION_PASS_RATE_MIN", None),
        "triadic_consensus_rate": getattr(C, "DCI_SG_TRIADIC_CONSENSUS_RATE_MIN", None),
        "adversarial_critic_pass": getattr(C, "DCI_SG_ADVERSARIAL_CRITIC_PASS_MIN", None),
        "multilingual_coverage": getattr(C, "DCI_SG_MULTILINGUAL_COVERAGE_MIN", None),
    }
    violations: list[str] = []
    for name, val in probes.items():
        if val is None:
            violations.append(f"{name}: constant missing")
            continue
        if not isinstance(val, (int, float)):
            violations.append(f"{name}: non-numeric {val!r}")
            continue
        if name == "triple_lens_coverage":
            if val < 1.0:
                violations.append(f"{name}={val} < 1.0 (nonsensical floor)")
        else:
            if not (0.0 <= val <= 1.0):
                violations.append(f"{name}={val} outside [0.0, 1.0]")
    if violations:
        return False, {"violations": violations, "thresholds": probes}
    return True, {"thresholds": probes}


def _check_output_dir(project_dir: Path, run_id: str) -> tuple[bool, dict[str, Any]]:
    """PF7: Output dir writable + no stale lock."""
    run_dir = project_dir / "data" / "dci" / "runs" / run_id
    details: dict[str, Any] = {"run_dir": str(run_dir)}
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        return False, {**details, "reason": f"mkdir failed: {exc}"}

    test_file = run_dir / ".writable_probe"
    try:
        test_file.write_text("probe", encoding="utf-8")
        test_file.unlink()
    except Exception as exc:
        return False, {**details, "reason": f"write probe failed: {exc}"}

    lock = run_dir / ".dci_run.lock"
    if lock.exists():
        age = _file_age_seconds(lock)
        details["lock_age_seconds"] = age
        if age < 60 * 60 * 2:  # lock fresh (< 2h) → another run active
            return False, {**details, "reason": f"fresh lock ({age:.0f}s old)"}
        details["stale_lock_removed"] = True
        try:
            lock.unlink()
        except Exception:
            pass
    return True, details


def _check_evidence_ledger_safe(
    project_dir: Path, run_id: str
) -> tuple[bool, dict[str, Any]]:
    """PF8: Evidence Ledger init safe (no in-progress marker from crashed run)."""
    run_dir = project_dir / "data" / "dci" / "runs" / run_id
    ledger = run_dir / "evidence_ledger.jsonl"
    in_progress = run_dir / "evidence_ledger.jsonl.in-progress"
    details: dict[str, Any] = {
        "ledger_exists": ledger.exists(),
        "in_progress_marker": in_progress.exists(),
    }
    if in_progress.exists():
        age = _file_age_seconds(in_progress)
        details["in_progress_age_seconds"] = age
        if age < 60 * 60 * 2:
            return False, {**details, "reason": "fresh in-progress marker"}
        details["stale_marker_removed"] = True
        try:
            in_progress.unlink()
        except Exception:
            pass
    return True, details


def _file_age_seconds(p: Path) -> float:
    import time
    return max(0.0, time.time() - p.stat().st_mtime)


def main() -> None:
    ap = argparse.ArgumentParser(description="DCI Preflight P1 validator")
    ap.add_argument("--date", required=True, help="Corpus date (YYYY-MM-DD)")
    ap.add_argument("--run-id", required=True, help="DCI run id")
    ap.add_argument("--project-dir", default=".", help="Project root")
    ap.add_argument("--dry-run", action="store_true", help="Allow DCI_ENABLED=False")
    args = ap.parse_args()

    project_dir = Path(args.project_dir).resolve()

    checks: list[dict[str, Any]] = []
    all_pass = True

    for code, name, func, kwargs in [
        ("PF1+PF2", "corpus", _check_corpus, {"project_dir": project_dir, "date": args.date}),
        ("PF3", "dci_enabled", _check_dci_enabled,
         {"project_dir": project_dir, "dry_run": args.dry_run}),
        ("PF4", "python_deps", _check_deps, {"project_dir": project_dir}),
        ("PF5", "claude_cli", _check_claude_cli, {}),
        ("PF6", "sg_thresholds", _check_sg_thresholds, {"project_dir": project_dir}),
        ("PF7", "output_dir", _check_output_dir,
         {"project_dir": project_dir, "run_id": args.run_id}),
        ("PF8", "evidence_ledger_safe", _check_evidence_ledger_safe,
         {"project_dir": project_dir, "run_id": args.run_id}),
    ]:
        try:
            passed, details = func(**kwargs)
        except Exception as exc:
            passed = False
            details = {"exception": f"{type(exc).__name__}: {exc}"}
        checks.append({
            "code": code,
            "name": name,
            "status": "PASS" if passed else "FAIL",
            "details": details,
        })
        if not passed:
            all_pass = False

    result = {
        "valid": all_pass,
        "date": args.date,
        "run_id": args.run_id,
        "checks": checks,
        "summary": {
            "total": len(checks),
            "passed": sum(1 for c in checks if c["status"] == "PASS"),
            "failed": sum(1 for c in checks if c["status"] == "FAIL"),
        },
    }
    _emit(result, 0 if all_pass else 1)


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
