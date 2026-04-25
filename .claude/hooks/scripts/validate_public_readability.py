#!/usr/bin/env python3
"""
Public Narrative P1 Validator — validate_public_readability.py

Enforces 8 deterministic checks (PUB1-PUB8) on a single Public Narrative
layer output. Agents (none exist for this package — narration happens in
``narrator.py`` via Claude CLI subprocess) NEVER compute these themselves.

Usage:
    python3 .claude/hooks/scripts/validate_public_readability.py \\
        --layer L1 --date 2026-04-14 --project-dir .

    # Explicit paths
    python3 .claude/hooks/scripts/validate_public_readability.py \\
        --layer L2 \\
        --report reports/public/2026-04-14/insight.md \\
        --facts-pool reports/public/2026-04-14/facts_pool.json \\
        --project-dir .

Exit codes:
    0 — all 8 checks PASS (or WARNING-only)
    1 — any check FAIL
    2 — script error

Output: JSON with per-check PASS/FAIL details.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _emit(result: dict[str, Any], exit_code: int) -> None:
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(exit_code)


def _default_path(project_dir: Path, date: str, layer: str, lang: str) -> Path:
    filename_en = {
        "L1": "interpretation.md", "L2": "insight.md", "L3": "future.md",
    }
    filename_ko = {
        "L1": "interpretation.ko.md", "L2": "insight.ko.md",
        "L3": "future.ko.md",
    }
    filename = filename_ko if lang == "ko" else filename_en
    return project_dir / "reports" / "public" / date / filename[layer]


def main() -> None:
    ap = argparse.ArgumentParser(description="Public Narrative P1 validator")
    ap.add_argument("--layer", required=True, choices=["L1", "L2", "L3"])
    ap.add_argument("--date", required=True)
    ap.add_argument("--project-dir", default=".")
    ap.add_argument("--report", default=None,
                    help="Override English report path")
    ap.add_argument("--report-ko", default=None,
                    help="Override Korean report path")
    ap.add_argument("--facts-pool", default=None,
                    help="Override facts_pool.json path")
    ap.add_argument("--glossary", default=None,
                    help="Override glossary YAML path")
    ap.add_argument("--skip-parity", action="store_true",
                    help="Skip PUB8 EN↔KO parity (if translation not done yet)")
    args = ap.parse_args()

    project_dir = Path(args.project_dir).resolve()
    public_dir = project_dir / "reports" / "public" / args.date
    report_path = (
        Path(args.report) if args.report
        else _default_path(project_dir, args.date, args.layer, "en")
    )
    report_ko_path = (
        Path(args.report_ko) if args.report_ko
        else _default_path(project_dir, args.date, args.layer, "ko")
    )
    facts_path = (
        Path(args.facts_pool) if args.facts_pool
        else public_dir / "facts_pool.json"
    )
    glossary_path = (
        Path(args.glossary) if args.glossary
        else project_dir / "src" / "public_narrative" / "glossary_simple.yaml"
    )

    # Import validators lazily to keep CLI cold-start light
    sys.path.insert(0, str(project_dir))
    try:
        from src.public_narrative.validators import (
            pub1_file_exists_nonempty, pub2_flesch_kincaid_ok,
            pub3_jargon_ratio_ok, pub4_numbers_parity_ok,
            pub5_markers_whitelist_ok, pub6_required_sections_ok,
            pub7_forbidden_phrases_ok, pub8_en_ko_parity_ok,
            load_glossary,
        )
    except Exception as exc:
        _emit({
            "valid": False, "error": f"import validators failed: {exc}",
        }, 2)

    # PUB1
    r1 = pub1_file_exists_nonempty(report_path)
    if not r1.passed:
        _emit({
            "valid": False, "layer": args.layer, "checks": [r1.to_dict()],
            "reason": "PUB1 failed; skipping downstream checks",
        }, 1)

    text = report_path.read_text(encoding="utf-8")

    # Load auxiliaries
    facts_pool: dict[str, Any] = {}
    if facts_path.exists():
        try:
            facts_pool = json.loads(facts_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            _emit({"valid": False,
                   "error": f"facts_pool parse: {exc}"}, 2)

    glossary = load_glossary(glossary_path)
    allowed_numbers = [
        float(n["value"]) for n in facts_pool.get("numbers", [])
        if isinstance(n.get("value"), (int, float))
    ]
    allowed_markers = list(facts_pool.get("allowed_markers", []))

    # PUB2 — FKGL
    r2 = pub2_flesch_kincaid_ok(text, args.layer)

    # PUB3 — Jargon ratio
    r3 = pub3_jargon_ratio_ok(text, args.layer, glossary["terms"])

    # PUB4 — Numbers parity
    r4 = pub4_numbers_parity_ok(text, allowed_numbers)

    # PUB5 — Marker whitelist
    r5 = pub5_markers_whitelist_ok(text, allowed_markers)

    # PUB6 — Required sections
    r6 = pub6_required_sections_ok(text, args.layer)

    # PUB7 — Forbidden phrases
    r7 = pub7_forbidden_phrases_ok(text, glossary["forbidden_phrases"])

    checks = [r1, r2, r3, r4, r5, r6, r7]

    # PUB8 — EN↔KO structure parity (skippable)
    if args.skip_parity:
        checks.append({"code": "PUB8", "status": "SKIP",
                       "details": {"reason": "--skip-parity set"}})
        ko_exists = None
    elif report_ko_path.exists():
        ko_text = report_ko_path.read_text(encoding="utf-8")
        r8 = pub8_en_ko_parity_ok(text, ko_text)
        checks.append(r8.to_dict())
        ko_exists = True
    else:
        checks.append({"code": "PUB8", "status": "SKIP",
                       "details": {"reason": f"KO not found: {report_ko_path}"}})
        ko_exists = False

    # Aggregate — dict items also participate
    def _status(c):
        if isinstance(c, dict):
            return c.get("status")
        return "PASS" if c.passed else "FAIL"

    def _as_dict(c):
        return c if isinstance(c, dict) else c.to_dict()

    statuses = [_status(c) for c in checks]
    any_fail = any(s == "FAIL" for s in statuses)

    result = {
        "valid": not any_fail,
        "layer": args.layer,
        "date": args.date,
        "report_path": str(report_path),
        "report_ko_path": (
            str(report_ko_path) if ko_exists else None
        ),
        "facts_pool_path": str(facts_path) if facts_path.exists() else None,
        "summary": {
            "total": len(statuses),
            "passed": sum(1 for s in statuses if s == "PASS"),
            "failed": sum(1 for s in statuses if s == "FAIL"),
            "skipped": sum(1 for s in statuses if s == "SKIP"),
        },
        "checks": [_as_dict(c) for c in checks],
    }
    _emit(result, 0 if not any_fail else 1)


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
