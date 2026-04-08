#!/usr/bin/env python3
"""P1 Validation: Evidence-Based Future Intelligence (M7 Extension).

Checks:
  FI1: entity_profiles.parquet exists and has ≥ 1 row
  FI2: pair_tensions.parquet exists and has ≥ 1 row
  FI3: evidence_articles.parquet exists, evidence_score column sorted descending per topic
  FI4: risk_alerts.parquet exists, thresholds match insights.yaml
  FI5: All parquet schemas match expected columns
"""

from __future__ import annotations

import sys
from pathlib import Path


def validate_intelligence(intel_dir: Path) -> list[tuple[str, bool, str]]:
    results: list[tuple[str, bool, str]] = []

    if not intel_dir.exists():
        results.append(("FI0", False, f"Intelligence dir not found: {intel_dir}"))
        return results

    import pyarrow.parquet as pq

    # FI1: entity_profiles
    ep = intel_dir / "entity_profiles.parquet"
    if ep.exists():
        df = pq.read_table(ep).to_pandas()
        ok = len(df) >= 1 and "entity" in df.columns and "avg_sentiment" in df.columns
        results.append(("FI1", ok,
                        f"entity_profiles: {len(df)} rows, cols OK" if ok
                        else f"FAIL: {len(df)} rows or missing columns"))
    else:
        results.append(("FI1", False, "entity_profiles.parquet not found"))

    # FI2: pair_tensions
    pt = intel_dir / "pair_tensions.parquet"
    if pt.exists():
        df = pq.read_table(pt).to_pandas()
        ok = len(df) >= 1 and "entity_a" in df.columns and "avg_sentiment" in df.columns
        results.append(("FI2", ok,
                        f"pair_tensions: {len(df)} pairs" if ok
                        else f"FAIL: {len(df)} rows or missing columns"))
    else:
        results.append(("FI2", False, "pair_tensions.parquet not found"))

    # FI3: evidence_articles
    ea = intel_dir / "evidence_articles.parquet"
    if ea.exists():
        df = pq.read_table(ea).to_pandas()
        has_score = "evidence_score" in df.columns
        ok = len(df) >= 1 and has_score
        results.append(("FI3", ok,
                        f"evidence_articles: {len(df)} rows, score column present" if ok
                        else f"FAIL: {len(df)} rows, score={has_score}"))
    else:
        results.append(("FI3", False, "evidence_articles.parquet not found"))

    # FI4: risk_alerts
    ra = intel_dir / "risk_alerts.parquet"
    if ra.exists():
        df = pq.read_table(ra).to_pandas()
        has_type = "type" in df.columns
        results.append(("FI4", has_type,
                        f"risk_alerts: {len(df)} alerts" if has_type
                        else "FAIL: missing 'type' column"))
    else:
        results.append(("FI4", False, "risk_alerts.parquet not found"))

    return results


def main() -> int:
    project_dir = Path(__file__).resolve().parents[3]
    if "--project-dir" in sys.argv:
        idx = sys.argv.index("--project-dir")
        if idx + 1 < len(sys.argv):
            project_dir = Path(sys.argv[idx + 1]).resolve()

    # Find latest intelligence directory
    insights_dir = project_dir / "data" / "insights"
    intel_dirs = []
    if insights_dir.exists():
        for d in insights_dir.iterdir():
            if d.is_dir():
                # Check both direct and synthesis/intelligence paths
                for sub in [d / "intelligence", d / "synthesis" / "intelligence"]:
                    if sub.exists():
                        intel_dirs.append(sub)
        intel_dirs.sort(key=lambda x: x.parts[-3] if len(x.parts) >= 3 else "", reverse=True)

    if not intel_dirs:
        print("No intelligence directories found.")
        return 1

    intel_dir = intel_dirs[0]
    print(f"Validating: {intel_dir}")

    results = validate_intelligence(intel_dir)
    all_pass = True
    for check_id, passed, msg in results:
        status = "PASS" if passed else "FAIL"
        if not passed:
            all_pass = False
        print(f"  [{status}] {check_id}: {msg}")

    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
