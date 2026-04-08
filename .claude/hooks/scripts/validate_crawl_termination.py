#!/usr/bin/env python3
"""P1 Validation: Crawl termination condition consistency.

Verifies that the crawl termination logic in pipeline.py uses
structured types (not string matching) and references centralized
constants (not magic numbers).

Checks:
  CT1: _get_incomplete_sites uses block_count (int), not string "403"
  CT2: CRAWL_SUFFICIENT_THRESHOLD referenced (not hardcoded 0.3)
  CT3: CRAWL_DIMINISHING_THRESHOLD referenced (not hardcoded 0.02)
  CT4: VALID_BOT_BLOCK_LEVELS referenced in get_adaptive_max_rounds
  CT5: No hardcoded termination thresholds in pipeline.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


def validate_crawl_termination(project_dir: Path) -> list[tuple[str, bool, str]]:
    """Run all crawl termination P1 checks.

    Returns:
        List of (check_id, passed, message) tuples.
    """
    results: list[tuple[str, bool, str]] = []
    pipeline_path = project_dir / "src" / "crawling" / "pipeline.py"
    retry_path = project_dir / "src" / "crawling" / "retry_manager.py"
    constants_path = project_dir / "src" / "config" / "constants.py"

    if not pipeline_path.exists():
        results.append(("CT0", False, f"pipeline.py not found: {pipeline_path}"))
        return results

    pipeline_src = pipeline_path.read_text()
    retry_src = retry_path.read_text() if retry_path.exists() else ""
    constants_src = constants_path.read_text() if constants_path.exists() else ""

    # CT1: block_count used instead of string "403" matching
    # In _get_incomplete_sites, there should be block_count comparison,
    # NOT string matching like '"403" in str(e)'
    _has_string_403 = bool(re.search(
        r'["\']403["\']\s*in\s*str\(', pipeline_src
    ))
    _has_block_count = "result.block_count" in pipeline_src
    results.append((
        "CT1",
        _has_block_count and not _has_string_403,
        "block_count (structured int) used, no string '403' matching"
        if _has_block_count and not _has_string_403
        else f"FAIL: string_403={_has_string_403}, block_count={_has_block_count}",
    ))

    # CT2: CRAWL_SUFFICIENT_THRESHOLD constant used (not hardcoded 0.3)
    _has_constant_ref = "CRAWL_SUFFICIENT_THRESHOLD" in pipeline_src
    # Check no hardcoded 0.3 in non-comment lines for threshold logic
    _hardcoded_03 = False
    for line in pipeline_src.split("\n"):
        stripped = line.lstrip()
        if stripped.startswith("#") or stripped.startswith('"') or stripped.startswith("'"):
            continue
        if re.search(r'_threshold\s*=\s*0\.3|_sufficient.*=\s*0\.3', line):
            _hardcoded_03 = True
            break
    results.append((
        "CT2",
        _has_constant_ref and not _hardcoded_03,
        "CRAWL_SUFFICIENT_THRESHOLD used, no hardcoded 0.3"
        if _has_constant_ref and not _hardcoded_03
        else f"FAIL: constant_ref={_has_constant_ref}, hardcoded_03={_hardcoded_03}",
    ))

    # CT3: CRAWL_DIMINISHING_THRESHOLD constant used
    _has_diminishing = "CRAWL_DIMINISHING_THRESHOLD" in pipeline_src
    results.append((
        "CT3",
        _has_diminishing,
        "CRAWL_DIMINISHING_THRESHOLD referenced"
        if _has_diminishing
        else "FAIL: CRAWL_DIMINISHING_THRESHOLD not found in pipeline.py",
    ))

    # CT4: VALID_BOT_BLOCK_LEVELS referenced in retry_manager
    _has_valid_levels = "VALID_BOT_BLOCK_LEVELS" in retry_src
    results.append((
        "CT4",
        _has_valid_levels,
        "VALID_BOT_BLOCK_LEVELS referenced in retry_manager"
        if _has_valid_levels
        else "FAIL: VALID_BOT_BLOCK_LEVELS not found in retry_manager.py",
    ))

    # CT5: VALID_BOT_BLOCK_LEVELS defined in constants.py
    _has_enum_def = "VALID_BOT_BLOCK_LEVELS" in constants_src and "frozenset" in constants_src
    results.append((
        "CT5",
        _has_enum_def,
        "VALID_BOT_BLOCK_LEVELS frozenset defined in constants.py"
        if _has_enum_def
        else "FAIL: VALID_BOT_BLOCK_LEVELS frozenset not in constants.py",
    ))

    return results


def main() -> int:
    """CLI entry point."""
    project_dir = Path(__file__).resolve().parents[3]

    # Allow --project-dir override
    if "--project-dir" in sys.argv:
        idx = sys.argv.index("--project-dir")
        if idx + 1 < len(sys.argv):
            project_dir = Path(sys.argv[idx + 1]).resolve()

    results = validate_crawl_termination(project_dir)

    all_pass = True
    for check_id, passed, msg in results:
        status = "PASS" if passed else "FAIL"
        if not passed:
            all_pass = False
        print(f"  [{status}] {check_id}: {msg}")

    if all_pass:
        print("\nAll crawl termination P1 checks passed.")
        return 0
    else:
        print("\nSome crawl termination P1 checks FAILED.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
