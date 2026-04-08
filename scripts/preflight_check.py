#!/usr/bin/env python3
"""Pre-flight check for GlobalNews Crawling & Analysis System.

Validates that the runtime environment is ready for actual pipeline execution:
  - Python version compatibility
  - Critical dependency availability
  - Configuration file validity
  - Disk space sufficiency
  - Data directory structure

Usage:
    python3 scripts/preflight_check.py --project-dir .
    python3 scripts/preflight_check.py --project-dir . --mode crawl
    python3 scripts/preflight_check.py --project-dir . --mode full --json

Exit codes:
    0 -- All checks passed (or --json mode: always 0, check JSON output)
    1 -- Critical check failed
    2 -- Warning-level issues found (non-blocking)
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any


# ─── Dependency Definitions ───────────────────────────────────────────────

# (module_name, display_name, required_for_crawl, required_for_analysis)
DEPENDENCIES: list[tuple[str, str, bool, bool]] = [
    # Core
    ("yaml", "PyYAML", True, True),
    ("requests", "requests", True, False),
    ("feedparser", "feedparser", True, False),
    ("bs4", "beautifulsoup4", True, False),
    ("lxml", "lxml", True, False),
    ("newspaper", "newspaper3k", True, False),
    ("aiohttp", "aiohttp", True, False),
    # Storage
    ("pyarrow", "pyarrow", True, True),
    # NLP - Crawling (optional: Playwright for Extreme sites)
    ("patchright", "patchright", False, False),  # Optional: Tier 4 only
    # NLP - Analysis
    ("kiwipiepy", "kiwipiepy", False, True),
    ("spacy", "spaCy", False, True),
    ("sentence_transformers", "sentence-transformers", False, True),
    ("sklearn", "scikit-learn", False, True),
    ("hdbscan", "hdbscan", False, True),
    ("torch", "PyTorch", False, True),
    ("transformers", "transformers", False, True),
    ("keybert", "KeyBERT", False, True),
]


def check_python_version() -> dict[str, Any]:
    """Check Python version compatibility."""
    ver = sys.version_info
    version_str = f"{ver.major}.{ver.minor}.{ver.micro}"
    ok = ver.major == 3 and ver.minor >= 12
    warning = None
    # D-7 (9): Python version constraint — sync with:
    #   pyproject.toml requires-python, main.py _check_python_version(),
    #   setup_init.py _check_domain_venv()
    if ver.minor >= 14:
        warning = (
            "Python 3.14 detected. spaCy may not work due to pydantic v1 "
            "incompatibility. Korean analysis (kiwipiepy) is unaffected. "
            "Consider using Python 3.12 or 3.13 for full pipeline support."
        )
    return {
        "check": "python_version",
        "version": version_str,
        "ok": ok,
        "warning": warning,
    }


def check_dependency(module_name: str, display_name: str) -> dict[str, Any]:
    """Check if a single dependency is importable."""
    try:
        mod = importlib.import_module(module_name)
        version = getattr(mod, "__version__", getattr(mod, "VERSION", "unknown"))
        return {
            "name": display_name,
            "module": module_name,
            "status": "ok",
            "version": str(version),
        }
    except Exception as e:
        error_type = type(e).__name__
        return {
            "name": display_name,
            "module": module_name,
            "status": "broken" if error_type != "ModuleNotFoundError" else "missing",
            "error": f"{error_type}: {str(e)[:120]}",
        }


def check_dependencies(mode: str) -> dict[str, Any]:
    """Check all dependencies for the specified mode."""
    results = []
    critical_failures = []
    warnings = []

    for module_name, display_name, req_crawl, req_analysis in DEPENDENCIES:
        result = check_dependency(module_name, display_name)

        # Determine if this dep is required for the requested mode
        required = False
        if mode in ("crawl", "full") and req_crawl:
            required = True
        if mode in ("analyze", "full") and req_analysis:
            required = True

        result["required"] = required

        if result["status"] != "ok" and required:
            critical_failures.append(display_name)
        elif result["status"] != "ok" and not required:
            warnings.append(f"{display_name}: {result['status']}")

        results.append(result)

    return {
        "check": "dependencies",
        "mode": mode,
        "results": results,
        "critical_failures": critical_failures,
        "warnings": warnings,
        "ok": len(critical_failures) == 0,
    }


def check_config_files(project_dir: Path) -> dict[str, Any]:
    """Check that required configuration files exist and are valid."""
    sources_path = project_dir / "data" / "config" / "sources.yaml"
    pipeline_path = project_dir / "data" / "config" / "pipeline.yaml"

    issues = []
    site_count = 0
    enabled_count = 0

    if not sources_path.exists():
        issues.append("data/config/sources.yaml not found")
    else:
        try:
            import yaml

            with open(sources_path) as f:
                config = yaml.safe_load(f)
            sources = config.get("sources", {})
            site_count = len(sources)
            # D-7 (13): opt-out pattern — hardcoded True (standalone script, can't import src).
            # SOT: src/config/constants.py ENABLED_DEFAULT = True
            # Cross-validated by: scripts/validate_enabled_default_sync.py ED6
            enabled_count = sum(
                1
                for s in sources.values()
                if s.get("meta", {}).get("enabled", True)
            )
            if site_count == 0:
                issues.append("sources.yaml has 0 sites defined")
            # P1: validate bot_block_level enum values
            # D-7: VALID_BOT_BLOCK_LEVELS in constants.py (SOT)
            _valid_levels = {"LOW", "MEDIUM", "HIGH"}
            for sid, scfg in sources.items():
                level = (scfg.get("anti_block") or {}).get("bot_block_level", "LOW") or "LOW"
                if level.upper() not in _valid_levels:
                    issues.append(
                        f"site {sid}: invalid bot_block_level '{level}' "
                        f"(must be one of {sorted(_valid_levels)})"
                    )
        except Exception as e:
            issues.append(f"sources.yaml parse error: {e}")

    if not pipeline_path.exists():
        issues.append("data/config/pipeline.yaml not found")

    return {
        "check": "config_files",
        "sources_yaml": sources_path.exists(),
        "pipeline_yaml": pipeline_path.exists(),
        "total_sites": site_count,
        "enabled_sites": enabled_count,
        "issues": issues,
        "ok": len(issues) == 0,
    }


def check_disk_space(project_dir: Path) -> dict[str, Any]:
    """Check available disk space."""
    usage = shutil.disk_usage(project_dir)
    free_gb = usage.free / (1024**3)
    total_gb = usage.total / (1024**3)
    # Need at least 2 GB for crawling + analysis output
    ok = free_gb >= 2.0
    return {
        "check": "disk_space",
        "free_gb": round(free_gb, 1),
        "total_gb": round(total_gb, 1),
        "ok": ok,
        "warning": "Less than 2 GB free" if not ok else None,
    }


def check_data_dirs(project_dir: Path) -> dict[str, Any]:
    """Check and create data directory structure."""
    data_dir = project_dir / "data"
    required_dirs = [
        "raw",
        "processed",
        "features",
        "analysis",
        "output",
        "models",
        "logs",
        "logs/daily",
        "logs/alerts",
        "logs/archive",
        "logs/cron",
        "logs/weekly",
        "config",
    ]

    created = []
    existing = []
    for d in required_dirs:
        full_path = data_dir / d
        if full_path.exists():
            existing.append(d)
        else:
            full_path.mkdir(parents=True, exist_ok=True)
            created.append(d)

    return {
        "check": "data_directories",
        "existing": existing,
        "created": created,
        "ok": True,
    }


def check_spacy_model() -> dict[str, Any]:
    """Check if spaCy English model is available."""
    try:
        import spacy

        nlp = spacy.load("en_core_web_sm")
        return {
            "check": "spacy_model",
            "model": "en_core_web_sm",
            "ok": True,
        }
    except Exception as e:
        return {
            "check": "spacy_model",
            "model": "en_core_web_sm",
            "ok": False,
            "error": str(e)[:200],
            "fix": "python3 -m spacy download en_core_web_sm",
        }


def check_network() -> dict[str, Any]:
    """Basic network connectivity check."""
    try:
        import urllib.request

        urllib.request.urlopen("https://httpbin.org/get", timeout=10)
        return {"check": "network", "ok": True}
    except Exception as e:
        return {
            "check": "network",
            "ok": False,
            "error": str(e)[:120],
        }


def run_preflight(project_dir: Path, mode: str) -> dict[str, Any]:
    """Run all pre-flight checks and return structured results."""
    python_check = check_python_version()
    dep_check = check_dependencies(mode)
    config_check = check_config_files(project_dir)
    disk_check = check_disk_space(project_dir)
    dir_check = check_data_dirs(project_dir)
    network_check = check_network()

    # spaCy model check only if analysis is included and spaCy works
    spacy_check = None
    if mode in ("analyze", "full"):
        spacy_dep = next(
            (r for r in dep_check["results"] if r["module"] == "spacy"), None
        )
        if spacy_dep and spacy_dep["status"] == "ok":
            spacy_check = check_spacy_model()

    # Aggregate
    all_ok = all(
        c["ok"]
        for c in [python_check, dep_check, config_check, disk_check, network_check]
    )

    # Build degradation notes
    degradations = []
    if python_check.get("warning"):
        degradations.append(python_check["warning"])
    spacy_available = (
        spacy_dep["status"] == "ok" if spacy_dep else False
    ) if mode in ("analyze", "full") else True
    if not spacy_available and mode in ("analyze", "full"):
        # Check if .venv exists with working spaCy — provide actionable guidance
        # NOTE: Use .venv/bin/python directly, NOT "source activate"
        # (Claude Code Bash tool does not persist shell state between calls)
        venv_python = project_dir / ".venv" / "bin" / "python"
        if venv_python.is_file():
            degradations.append(
                "spaCy broken in current Python runtime (likely Python 3.14 "
                "pydantic v1 incompatibility). A working .venv exists. "
                "Run with venv directly: .venv/bin/python scripts/preflight_check.py "
                "--project-dir . --mode full --json"
            )
        else:
            degradations.append(
                "spaCy unavailable: English NER and lemmatization will be "
                "disabled. Create a Python 3.13 venv for full pipeline support: "
                "/opt/homebrew/bin/python3.13 -m venv .venv && "
                ".venv/bin/pip install -r requirements.txt && "
                ".venv/bin/python -m spacy download en_core_web_sm"
            )
    patchright_dep = next(
        (r for r in dep_check["results"] if r["module"] == "patchright"), None
    )
    if patchright_dep and patchright_dep["status"] != "ok":
        degradations.append(
            "patchright unavailable: Tier 4 (Extreme difficulty) sites requiring "
            "headless browser will be skipped. RSS/sitemap/DOM sites are unaffected."
        )

    return {
        "readiness": "ready" if all_ok else "blocked",
        "mode": mode,
        "all_ok": all_ok,
        "checks": {
            "python": python_check,
            "dependencies": dep_check,
            "config": config_check,
            "disk": disk_check,
            "directories": dir_check,
            "network": network_check,
            "spacy_model": spacy_check,
        },
        "degradations": degradations,
        "critical_failures": dep_check["critical_failures"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Pre-flight check for GlobalNews system")
    parser.add_argument("--project-dir", required=True, help="Project root directory")
    parser.add_argument(
        "--mode",
        choices=["crawl", "analyze", "full"],
        default="full",
        help="Execution mode to validate for",
    )
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    project_dir = Path(args.project_dir).resolve()
    result = run_preflight(project_dir, args.mode)

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0

    # Human-readable output
    print("=" * 60)
    print("GlobalNews Pre-flight Check")
    print(f"Mode: {args.mode}")
    print("=" * 60)

    # Python
    py = result["checks"]["python"]
    print(f"\n  Python: {py['version']} {'OK' if py['ok'] else 'FAIL'}")
    if py.get("warning"):
        print(f"    WARNING: {py['warning']}")

    # Dependencies
    dep = result["checks"]["dependencies"]
    ok_count = sum(1 for r in dep["results"] if r["status"] == "ok")
    total = len(dep["results"])
    print(f"\n  Dependencies: {ok_count}/{total} available")
    for r in dep["results"]:
        status_icon = "OK" if r["status"] == "ok" else "MISSING" if r["status"] == "missing" else "BROKEN"
        req_mark = " [REQUIRED]" if r["required"] and r["status"] != "ok" else ""
        print(f"    {status_icon:7s} {r['name']}{req_mark}")

    # Config
    cfg = result["checks"]["config"]
    print(f"\n  Config: {cfg['enabled_sites']}/{cfg['total_sites']} sites enabled {'OK' if cfg['ok'] else 'FAIL'}")

    # Disk
    disk = result["checks"]["disk"]
    print(f"\n  Disk: {disk['free_gb']} GB free {'OK' if disk['ok'] else 'LOW'}")

    # Network
    net = result["checks"]["network"]
    print(f"\n  Network: {'OK' if net['ok'] else 'FAIL'}")

    # Degradations
    if result["degradations"]:
        print(f"\n  Degradation Notes:")
        for d in result["degradations"]:
            print(f"    - {d}")

    # Summary
    print()
    print("=" * 60)
    if result["readiness"] == "ready":
        print("RESULT: READY to execute")
    else:
        print("RESULT: BLOCKED")
        print(f"  Critical failures: {', '.join(result['critical_failures'])}")
    print("=" * 60)

    return 0 if result["readiness"] == "ready" else 1


if __name__ == "__main__":
    sys.exit(main())
