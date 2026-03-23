#!/usr/bin/env python3
"""Site Registry Sync Validator — P1 hallucination prevention.

Cross-validates all hardcoded site lists across the codebase to ensure
no list falls out of sync after expansion (e.g., 44 → 121 → 116).

Checks:
    RS1: All 5 hardcoded lists have identical domain sets (after normalization)
    RS2: Group-level counts are consistent across lists
    RS3: Runtime SOT (sources.yaml) matches hardcoded canonical list
    RS4: Total site count matches expected (116)

Usage:
    python3 scripts/validate_site_registry_sync.py --project-dir .
    python3 scripts/validate_site_registry_sync.py --project-dir . --require-sot

JSON output to stdout. Exit code 0 if valid, 1 if invalid.

This is a P1 deterministic script: no LLM inference, pure set operations.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
from typing import Any


# ---------------------------------------------------------------------------
# Domain normalization
# ---------------------------------------------------------------------------

# Known aliases: map variant → canonical form
_DOMAIN_ALIASES: dict[str, str] = {
    "huffpost.com": "huffingtonpost.com",
    "taiwannews.com.tw": "taiwannews.com",
}

# Subdomain prefixes to strip (order matters — longest first)
_STRIP_PREFIXES = (
    "www.", "en.", "e.", "news.", "digital.", "mongolia.",
    "edition.",
)


def normalize_domain(domain: str) -> str:
    """Normalize a domain to its canonical bare form.

    1. Lowercase
    2. Strip known subdomain prefixes (www., en., e., news., digital., etc.)
    3. Resolve known aliases (huffpost.com → huffingtonpost.com)

    This function is the single source of truth for domain comparison
    across all site registry scripts.
    """
    d = domain.strip().lower()
    # Strip prefixes iteratively (handles "www.en.example.com")
    changed = True
    while changed:
        changed = False
        for prefix in _STRIP_PREFIXES:
            if d.startswith(prefix):
                d = d[len(prefix):]
                changed = True
    # Apply aliases
    d = _DOMAIN_ALIASES.get(d, d)
    return d


# ---------------------------------------------------------------------------
# Source extractors (parse Python files without importing them)
# ---------------------------------------------------------------------------

def _extract_from_extract_site_urls(project_dir: str) -> dict[str, set[str]]:
    """Extract domains and groups from extract_site_urls.py _CANONICAL_SITES."""
    path = os.path.join(project_dir, "scripts", "extract_site_urls.py")
    if not os.path.isfile(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    # Parse the tuple list: ("domain", "lang", "GROUP", "label")
    groups: dict[str, set[str]] = {}
    for match in re.finditer(
        r'\(\s*"([^"]+)"\s*,\s*"[^"]*"\s*,\s*"([A-J])"\s*,\s*"[^"]*"\s*\)',
        content,
    ):
        domain, group = match.group(1), match.group(2)
        groups.setdefault(group, set()).add(normalize_domain(domain))
    return groups


def _extract_from_split_sites(project_dir: str) -> dict[str, set[str]]:
    """Extract domains and groups from split_sites_by_group.py _SITES."""
    path = os.path.join(project_dir, "scripts", "split_sites_by_group.py")
    if not os.path.isfile(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    groups: dict[str, set[str]] = {}
    # Match dict entries: {"domain": "xxx", ... "group": "X", ...}
    for match in re.finditer(
        r'"domain"\s*:\s*"([^"]+)"[^}]*"group"\s*:\s*"([A-J])"',
        content,
    ):
        domain, group = match.group(1), match.group(2)
        groups.setdefault(group, set()).add(normalize_domain(domain))
    return groups


def _extract_from_validate_coverage(project_dir: str) -> set[str]:
    """Extract domains from validate_site_coverage.py _FALLBACK_SITES."""
    path = os.path.join(project_dir, "scripts", "validate_site_coverage.py")
    if not os.path.isfile(path):
        return set()
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    domains = set()
    # Match quoted strings in list: "www.chosun.com", "www.nytimes.com"
    in_fallback = False
    for line in content.splitlines():
        if "_FALLBACK_SITES" in line and "=" in line:
            in_fallback = True
            continue
        if in_fallback:
            if line.strip() == "]":
                break
            for match in re.finditer(r'"([^"]+)"', line):
                d = match.group(1)
                if "." in d and not d.startswith("#"):
                    domains.add(normalize_domain(d))
    return domains


def _extract_from_distribute(project_dir: str) -> dict[str, set[str]]:
    """Extract domains from distribute_sites_to_teams.py _FALLBACK_GROUPS."""
    path = os.path.join(project_dir, "scripts", "distribute_sites_to_teams.py")
    if not os.path.isfile(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    groups: dict[str, set[str]] = {}
    # Parse group-by-group: "kr-major": [...], "kr-tech": [...], etc.
    # Map team keys back to group letters
    team_to_groups = {
        "kr-major": ["A", "B", "C"],
        "kr-tech": ["D"],
        "english": ["E"],
        "multilingual": ["F", "G", "H", "I", "J"],
    }

    # Extract all domains with their team key
    current_team = None
    for line in content.splitlines():
        # Detect team key: "kr-major": [
        team_match = re.match(r'\s*"(kr-major|kr-tech|english|multilingual)"\s*:', line)
        if team_match:
            current_team = team_match.group(1)
            continue
        if current_team and '"domain"' in line:
            domain_match = re.search(r'"domain"\s*:\s*"([^"]+)"', line)
            if domain_match:
                d = normalize_domain(domain_match.group(1))
                # Assign to the team's group letters
                for g in team_to_groups.get(current_team, []):
                    groups.setdefault(g, set()).add(d)
        if current_team and line.strip() == "],":
            current_team = None

    return groups


def _extract_from_sources_yaml(project_dir: str) -> dict[str, set[str]] | None:
    """Extract domains from runtime SOT (data/config/sources.yaml)."""
    sot_path = os.path.join(project_dir, "data", "config", "sources.yaml")
    if not os.path.isfile(sot_path):
        return None
    try:
        import yaml
    except ImportError:
        return None
    with open(sot_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    sources = data.get("sources", {})
    if not isinstance(sources, dict):
        return None

    groups: dict[str, set[str]] = {}
    for _sid, cfg in sources.items():
        if not isinstance(cfg, dict):
            continue
        url = cfg.get("url", "")
        group = cfg.get("group", "")
        domain = re.sub(r"^https?://", "", str(url)).rstrip("/").split("/")[0]
        if domain and group:
            groups.setdefault(group, set()).add(normalize_domain(domain))
    return groups if groups else None


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

# Expected group counts (A-J)
# Updated 2026-03-23: +3 sites (npr, upi, pbsnewshour in Group E),
# 9 sites disabled (bloomberg, wsj, marketwatch, latimes, euractiv,
# lefigaro, liberation, ouestfrance, icelandmonitor).
# Counts include all sites (enabled + disabled).
_EXPECTED_COUNTS: dict[str, int] = {
    "A": 5, "B": 3, "C": 2, "D": 8,
    "E": 23, "F": 22, "G": 39,
    "H": 4, "I": 9, "J": 4,
}
_EXPECTED_TOTAL = 119  # 116 original + 3 replacement sites


def validate_sync(project_dir: str, require_sot: bool = False) -> dict[str, Any]:
    """Cross-validate all site registries.

    Returns JSON-serializable result dict with valid/invalid status.
    """
    result: dict[str, Any] = {
        "valid": True,
        "checks": {},
        "errors": [],
        "warnings": [],
        "details": {},
    }

    # --- Load all sources ---
    extract_groups = _extract_from_extract_site_urls(project_dir)
    split_groups = _extract_from_split_sites(project_dir)
    coverage_domains = _extract_from_validate_coverage(project_dir)
    distribute_groups = _extract_from_distribute(project_dir)
    sot_groups = _extract_from_sources_yaml(project_dir)

    # Flatten grouped sources to domain sets for cross-comparison
    extract_all = set()
    for ds in extract_groups.values():
        extract_all |= ds
    split_all = set()
    for ds in split_groups.values():
        split_all |= ds
    distribute_all = set()
    for ds in distribute_groups.values():
        distribute_all |= ds

    sources = {
        "extract_site_urls": extract_all,
        "split_sites_by_group": split_all,
        "validate_site_coverage": coverage_domains,
        "distribute_sites_to_teams": distribute_all,
    }

    # --- RS1: Cross-validate domain sets ---
    rs1_ok = True
    rs1_details: list[str] = []
    source_names = list(sources.keys())
    for i in range(len(source_names)):
        for j in range(i + 1, len(source_names)):
            name_a, name_b = source_names[i], source_names[j]
            set_a, set_b = sources[name_a], sources[name_b]
            if not set_a or not set_b:
                continue
            only_in_a = set_a - set_b
            only_in_b = set_b - set_a
            if only_in_a or only_in_b:
                rs1_ok = False
                if only_in_a:
                    rs1_details.append(
                        f"In {name_a} but not in {name_b}: {sorted(only_in_a)}"
                    )
                if only_in_b:
                    rs1_details.append(
                        f"In {name_b} but not in {name_a}: {sorted(only_in_b)}"
                    )
    result["checks"]["RS1_cross_validate"] = "PASS" if rs1_ok else "FAIL"
    if not rs1_ok:
        result["valid"] = False
        for d in rs1_details:
            result["errors"].append(f"RS1: {d}")
    result["details"]["rs1_source_counts"] = {
        name: len(domains) for name, domains in sources.items()
    }

    # --- RS2: Group-level count consistency ---
    rs2_ok = True
    rs2_details: list[str] = []
    grouped_sources = {
        "extract_site_urls": extract_groups,
        "split_sites_by_group": split_groups,
    }
    for src_name, groups in grouped_sources.items():
        for group_letter, expected_count in _EXPECTED_COUNTS.items():
            actual = len(groups.get(group_letter, set()))
            if actual != expected_count:
                rs2_ok = False
                rs2_details.append(
                    f"{src_name} Group {group_letter}: expected {expected_count}, got {actual}"
                )
    result["checks"]["RS2_group_counts"] = "PASS" if rs2_ok else "FAIL"
    if not rs2_ok:
        result["valid"] = False
        for d in rs2_details:
            result["errors"].append(f"RS2: {d}")

    # --- RS3: SOT matches canonical ---
    if sot_groups is not None:
        sot_all = set()
        for ds in sot_groups.values():
            sot_all |= ds
        only_in_sot = sot_all - extract_all
        only_in_canonical = extract_all - sot_all
        rs3_ok = not only_in_sot and not only_in_canonical
        result["checks"]["RS3_sot_matches_canonical"] = "PASS" if rs3_ok else "FAIL"
        if not rs3_ok:
            result["valid"] = False
            if only_in_sot:
                result["errors"].append(
                    f"RS3: In SOT but not in canonical: {sorted(only_in_sot)}"
                )
            if only_in_canonical:
                result["errors"].append(
                    f"RS3: In canonical but not in SOT: {sorted(only_in_canonical)}"
                )
        result["details"]["sot_count"] = len(sot_all)
    elif require_sot:
        result["checks"]["RS3_sot_matches_canonical"] = "FAIL"
        result["valid"] = False
        result["errors"].append("RS3: --require-sot specified but sources.yaml not found/parseable")
    else:
        result["checks"]["RS3_sot_matches_canonical"] = "SKIP"
        result["warnings"].append("RS3: sources.yaml not available — SOT check skipped")

    # --- RS4: Total count ---
    rs4_ok = True
    for src_name, domains in sources.items():
        if domains and len(domains) != _EXPECTED_TOTAL:
            rs4_ok = False
            result["errors"].append(
                f"RS4: {src_name} has {len(domains)} sites, expected {_EXPECTED_TOTAL}"
            )
    result["checks"]["RS4_total_count"] = "PASS" if rs4_ok else "FAIL"
    if not rs4_ok:
        result["valid"] = False

    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Site Registry Sync Validator — P1 hallucination prevention"
    )
    parser.add_argument("--project-dir", required=True, help="Project root directory")
    parser.add_argument(
        "--require-sot", action="store_true",
        help="Fail if runtime SOT (sources.yaml) is not available"
    )
    args = parser.parse_args()

    project_dir = os.path.abspath(args.project_dir)
    result = validate_sync(project_dir, require_sot=args.require_sot)

    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    print()
    sys.exit(0 if result["valid"] else 1)


if __name__ == "__main__":
    main()
