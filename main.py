"""GlobalNews Crawling & Analysis System -- Main Entry Point.

A staged monolith that crawls 116 international news sites through
an 8-stage NLP analysis pipeline, producing Parquet/SQLite output
for social trend research.

Usage:
    python main.py --mode crawl --date 2026-02-25
    python main.py --mode analyze --stage 1
    python main.py --mode full --dry-run
    python main.py --help
"""

import argparse
import json
import sys
import time
from datetime import date, datetime
from pathlib import Path


def _check_python_version() -> None:
    """P1 runtime guard — refuse to run on Python 3.14+ (spaCy incompatible).

    spaCy 3.x depends on pydantic v1 which crashes on Python 3.14 due to
    type inference changes. This guard prevents mid-pipeline failures by
    refusing to start on incompatible Python versions.

    Deterministic. No LLM judgment. Runs before any imports.
    """
    # D-7 (9): Python version constraint — sync with:
    #   pyproject.toml requires-python, setup_init.py _check_domain_venv(),
    #   preflight_check.py check_python_version()
    if sys.version_info >= (3, 14):
        print(
            f"ERROR: Python {sys.version_info.major}.{sys.version_info.minor} detected. "
            f"This pipeline requires Python 3.12-3.13 (spaCy pydantic v1 incompatibility).\n"
            f"Run with: .venv/bin/python main.py ...\n"
            f"If .venv does not exist: /opt/homebrew/bin/python3.13 -m venv .venv && "
            f".venv/bin/pip install -r requirements.txt && "
            f".venv/bin/python -m spacy download en_core_web_sm",
            file=sys.stderr,
        )
        sys.exit(1)


_check_python_version()

# Ensure src/ is importable
sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.config.constants import (
    PROJECT_ROOT,
    SOURCES_YAML_PATH,
    PIPELINE_YAML_PATH,
    DATA_RAW_DIR,
    CRAWL_GROUPS,
    RUN_METADATA_PATH,
    ENABLED_DEFAULT,
)
from src.utils.logging_config import setup_logging, get_logger


def _write_run_metadata(
    mode: str,
    target_date: date,
    exit_code: int,
    elapsed_seconds: float,
    *,
    crawl_report: dict | None = None,
    analysis_result: object | None = None,
) -> None:
    """Write run_metadata.json with pipeline execution summary.

    This is the SOT for "what happened in the last run" — used by
    ``--mode status`` and for debugging pipeline issues.

    Args:
        mode: Execution mode (crawl/analyze/full).
        target_date: Target date for the run.
        exit_code: Exit code from the pipeline.
        elapsed_seconds: Total wall time.
        crawl_report: Optional crawl report dict.
        analysis_result: Optional analysis pipeline result object.
    """
    meta: dict = {
        "run_timestamp": datetime.utcnow().isoformat() + "Z",
        "mode": mode,
        "target_date": target_date.isoformat(),
        "exit_code": exit_code,
        "elapsed_seconds": round(elapsed_seconds, 1),
    }

    if crawl_report is not None:
        meta["crawl"] = {
            "total_articles": crawl_report.get("total_articles", 0),
            "sites_attempted": crawl_report.get("total_sites_attempted", 0),
            "sites_failed": crawl_report.get("sites_failed", 0),
        }

    if analysis_result is not None:
        stages_info = {}
        for sn, sr in getattr(analysis_result, "stages", {}).items():
            stages_info[str(sn)] = {
                "name": sr.stage_name,
                "success": sr.success,
                "skipped": sr.skipped,
                "elapsed_seconds": round(sr.elapsed_seconds, 1),
                "article_count": sr.article_count,
            }
        meta["analysis"] = {
            "success": analysis_result.success,
            "stages_completed": analysis_result.stages_completed,
            "stages_failed": analysis_result.stages_failed,
            "stages_skipped": analysis_result.stages_skipped,
            "peak_memory_gb": round(analysis_result.peak_memory_gb, 2),
            "stages": stages_info,
        }

    try:
        RUN_METADATA_PATH.parent.mkdir(parents=True, exist_ok=True)
        RUN_METADATA_PATH.write_text(
            json.dumps(meta, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass  # Non-critical — don't fail the pipeline for metadata


def _validate_date(date_str: str) -> date:
    """Parse and validate a YYYY-MM-DD date string.

    Args:
        date_str: Date string in YYYY-MM-DD format.

    Returns:
        Parsed date object.

    Raises:
        argparse.ArgumentTypeError: If the date string is invalid.
    """
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"Invalid date format: '{date_str}'. Expected YYYY-MM-DD."
        )


def _validate_stage(stage_str: str) -> int:
    """Parse and validate a stage number (1-8).

    Args:
        stage_str: Stage number as string.

    Returns:
        Validated stage number.

    Raises:
        argparse.ArgumentTypeError: If the stage number is invalid.
    """
    try:
        stage = int(stage_str)
    except ValueError:
        raise argparse.ArgumentTypeError(f"Stage must be an integer, got: '{stage_str}'")
    if stage < 1 or stage > 8:
        raise argparse.ArgumentTypeError(f"Stage must be 1-8, got: {stage}")
    return stage


def cmd_crawl(args: argparse.Namespace) -> int:
    """Execute the crawling pipeline for configured news sources.

    Args:
        args: Parsed command-line arguments.

    Returns:
        Exit code (0 = success, 1 = failure).
    """
    logger = get_logger("main.crawl")
    logger.info(
        "Crawl started: date=%s sites=%s groups=%s dry_run=%s",
        args.date, args.sites or "all", args.groups or "all", args.dry_run,
    )

    if args.dry_run:
        logger.info("DRY RUN: Would crawl sites. No actual requests will be made.")
        # Load config to validate it exists and is valid
        from src.utils.config_loader import load_sources_config, get_enabled_sites
        config = load_sources_config()
        enabled = get_enabled_sites()
        logger.info("Would crawl %d enabled sites", len(enabled))
        for sid in sorted(enabled):
            site_cfg = config["sources"][sid]
            logger.info(
                "  %s: %s (group %s, ~%s articles/day)",
                sid, site_cfg['crawl']['primary_method'],
                site_cfg['group'], site_cfg['meta']['daily_article_estimate'],
            )
        return 0

    # Dispatch to the full crawling pipeline (Step 12)
    from src.crawling.pipeline import run_crawl_pipeline

    t0 = time.monotonic()
    crawl_date_str = args.date.strftime("%Y-%m-%d") if args.date else None
    try:
        report = run_crawl_pipeline(
            crawl_date=crawl_date_str,
            sites=args.sites,
            groups=args.groups,
            dry_run=False,
        )
    except KeyboardInterrupt:
        raise
    except Exception as e:
        logger.error("Crawl pipeline failed: %s", e, exc_info=True)
        if not getattr(args, "_skip_metadata", False):
            _write_run_metadata("crawl", args.date, 1, time.monotonic() - t0)
        return 1

    # Exit code: 0 if any articles collected, 1 if total failure
    total = report.get("total_articles", 0)
    failed = report.get("sites_failed", 0)
    attempted = report.get("total_sites_attempted", 0)
    if attempted > 0 and failed == attempted:
        logger.error("All %s sites failed.", attempted)
        if not getattr(args, "_skip_metadata", False):
            _write_run_metadata("crawl", args.date, 1, time.monotonic() - t0, crawl_report=report)
        return 1
    logger.info("Crawl complete: %s articles from %s sites.", total, attempted - failed)
    if not getattr(args, "_skip_metadata", False):
        _write_run_metadata("crawl", args.date, 0, time.monotonic() - t0, crawl_report=report)
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    """Execute the analysis pipeline on crawled articles.

    Dispatches to AnalysisPipeline for the 8-stage NLP pipeline.
    Supports running all stages, a single stage (checkpoint resume),
    or a dry-run configuration check.

    Args:
        args: Parsed command-line arguments.

    Returns:
        Exit code (0 = success, 1 = failure).
    """
    logger = get_logger("main.analyze")
    stages_to_run = list(range(1, 9)) if args.all_stages else ([args.stage] if args.stage else [])

    if not stages_to_run:
        logger.error("Specify --stage N (1-8) or --all-stages")
        return 1

    logger.info(
        "Analysis started: stages=%s date=%s dry_run=%s",
        stages_to_run, args.date or "latest", args.dry_run,
    )

    if args.dry_run:
        logger.info("DRY RUN: Would run analysis stages.")
        from src.analysis.pipeline import AnalysisPipeline, STAGE_NAMES
        analyze_date = args.date.strftime("%Y-%m-%d") if args.date else None
        pipeline = AnalysisPipeline(date=analyze_date)
        for stage_num in stages_to_run:
            name = STAGE_NAMES.get(stage_num, f"Stage {stage_num}")
            missing = pipeline._check_dependencies(stage_num)
            if missing:
                deps_str = ", ".join(f"{p.name}=MISSING" for p in missing)
            else:
                deps_str = "all OK"
            logger.info(f"  Stage {stage_num}: {name} | deps: [{deps_str}]")
        return 0

    # Dispatch to the analysis pipeline (Step 15)
    from src.analysis.pipeline import run_analysis_pipeline

    t0 = time.monotonic()
    analyze_date_str = args.date.strftime("%Y-%m-%d") if args.date else None
    try:
        result = run_analysis_pipeline(
            date=analyze_date_str,
            stages=stages_to_run,
        )
    except KeyboardInterrupt:
        raise
    except Exception as e:
        logger.error("Analysis pipeline failed: %s", e, exc_info=True)
        if not getattr(args, "_skip_metadata", False):
            _write_run_metadata("analyze", args.date, 1, time.monotonic() - t0)
        return 1

    # Print summary
    logger.info(
        "Analysis complete: success=%s completed=%s failed=%s "
        "skipped=%s elapsed=%.1fs peak_memory=%.2f_GB",
        result.success, result.stages_completed, result.stages_failed,
        result.stages_skipped, result.total_elapsed_seconds,
        result.peak_memory_gb,
    )

    # Print per-stage summary
    for stage_num in sorted(result.stages):
        sr = result.stages[stage_num]
        if sr.skipped:
            logger.info("  Stage %s (%s): SKIPPED -- %s", stage_num, sr.stage_name, sr.skip_reason)
        elif sr.success:
            logger.info(
                "  Stage %s (%s): OK (%.1fs, %s articles, %.2f GB)",
                stage_num, sr.stage_name, sr.elapsed_seconds,
                sr.article_count, sr.peak_memory_gb,
            )
        else:
            logger.info(
                "  Stage %s (%s): FAILED -- %s: %s",
                stage_num, sr.stage_name, sr.error_type, sr.error_message[:200],
            )

    # Print final outputs
    if result.final_output_paths:
        logger.info("Final outputs:")
        for name, path in result.final_output_paths.items():
            logger.info("  %s: %s", name, path)

    exit_code = 0 if result.success else 1
    if not getattr(args, "_skip_metadata", False):
        _write_run_metadata("analyze", args.date, exit_code, time.monotonic() - t0, analysis_result=result)
    return exit_code


def cmd_full(args: argparse.Namespace) -> int:
    """Execute the full pipeline: crawl + all 8 analysis stages + insight.

    Workflow:
        Phase 1: Crawl         (cmd_crawl)
        Phase 2: Analyze       (cmd_analyze, all 8 stages, uses rolling window)
        Phase 3: Insight       (cmd_insight, 30-day window, auto-chained)

    Phase 3 is auto-chained so the Workflow A daily run automatically
    refreshes the Workflow B big-data insight outputs. Users no longer need
    to manually run --mode insight. Use --skip-insight to disable Phase 3
    (e.g., for fast crawl-only validation).

    Args:
        args: Parsed command-line arguments.

    Returns:
        Exit code (0 = success, 1 = failure). Crawl/analyze failures abort
        the chain; insight failures are warnings (A still considered OK).
    """
    logger = get_logger("main.full")
    logger.info(
        "Full pipeline started: date=%s dry_run=%s skip_insight=%s",
        args.date, args.dry_run, getattr(args, "skip_insight", False),
    )

    t0 = time.monotonic()

    # Phase 1: Crawl (suppress sub-command metadata writes)
    args._skip_metadata = True
    crawl_result = cmd_crawl(args)
    if crawl_result != 0:
        logger.error("Crawling phase failed, aborting analysis.")
        _write_run_metadata("full", args.date, crawl_result, time.monotonic() - t0)
        return crawl_result

    # Phase 2: Analyze (all stages)
    args.all_stages = True
    args.stage = None
    analyze_result = cmd_analyze(args)

    if analyze_result != 0:
        logger.error("Analysis phase failed, skipping insight pipeline.")
        del args._skip_metadata
        _write_run_metadata("full", args.date, analyze_result, time.monotonic() - t0)
        return analyze_result

    # Phase 3: Insight — auto-chain Workflow B after Workflow A.
    # This fills the gap that previously required a manual
    # `--mode insight` invocation. Skip only on dry-run or explicit opt-out.
    insight_result = 0
    if args.dry_run:
        logger.info("Insight pipeline skipped: dry-run mode")
    elif getattr(args, "skip_insight", False):
        logger.info("Insight pipeline skipped: --skip-insight flag")
    else:
        logger.info(
            "Phase 3: auto-chaining insight pipeline (window=%d, end_date=%s)",
            getattr(args, "window", None) or 30, args.date,
        )
        # Set defaults if cmd_insight needs them
        if not hasattr(args, "end_date") or not args.end_date:
            args.end_date = args.date
        if not hasattr(args, "window") or not args.window:
            args.window = 30
        if not hasattr(args, "module"):
            args.module = None
        try:
            insight_result = cmd_insight(args)
            if insight_result != 0:
                logger.warning(
                    "Insight pipeline returned non-zero (%d); "
                    "Workflow A still considered successful.",
                    insight_result,
                )
                # Degrade to warning: analysis success is what defines
                # Workflow A completion. Insight failure does not abort.
                insight_result = 0
        except Exception as exc:
            logger.warning(
                "Insight pipeline crashed: %s — continuing without insight.",
                exc,
            )

    del args._skip_metadata

    _write_run_metadata("full", args.date, analyze_result, time.monotonic() - t0)
    return analyze_result


def cmd_status(args: argparse.Namespace) -> int:
    """Show pipeline status: configuration summary and data inventory.

    Args:
        args: Parsed command-line arguments.

    Returns:
        Exit code (0 = success).
    """
    logger = get_logger("main.status")

    print("=" * 60)
    print("GlobalNews Crawling & Analysis System -- Status")
    print("=" * 60)

    # Check config files
    print(f"\nConfiguration Files:")
    print(f"  sources.yaml:  {'FOUND' if SOURCES_YAML_PATH.exists() else 'MISSING'} ({SOURCES_YAML_PATH})")
    print(f"  pipeline.yaml: {'FOUND' if PIPELINE_YAML_PATH.exists() else 'MISSING'} ({PIPELINE_YAML_PATH})")

    # Load and summarize sources config
    if SOURCES_YAML_PATH.exists():
        try:
            from src.utils.config_loader import load_sources_config
            config = load_sources_config(validate=False)
            sources = config.get("sources", {})
            # D-7 (13): opt-out pattern — ENABLED_DEFAULT from constants.py (SOT)
            enabled = sum(1 for s in sources.values() if s.get("meta", {}).get("enabled", ENABLED_DEFAULT))
            total_articles = sum(s.get("meta", {}).get("daily_article_estimate", 0) for s in sources.values())
            groups = {}
            for sid, cfg in sources.items():
                g = cfg.get("group", "?")
                groups[g] = groups.get(g, 0) + 1
            print(f"\n  Sites: {len(sources)} total, {enabled} enabled")
            print(f"  Daily article estimate: ~{total_articles}")
            print(f"  Groups: {', '.join(f'{g}({c})' for g, c in sorted(groups.items()))}")
        except Exception as e:
            print(f"  Error loading sources.yaml: {e}")

    # Last run metadata
    if RUN_METADATA_PATH.exists():
        try:
            meta = json.loads(RUN_METADATA_PATH.read_text(encoding="utf-8"))
            print(f"\nLast Run:")
            print(f"  Timestamp: {meta.get('run_timestamp', '?')}")
            print(f"  Mode: {meta.get('mode', '?')}  Date: {meta.get('target_date', '?')}")
            print(f"  Exit code: {meta.get('exit_code', '?')}  Elapsed: {meta.get('elapsed_seconds', '?')}s")
            if "crawl" in meta:
                c = meta["crawl"]
                print(f"  Crawl: {c.get('total_articles', 0)} articles, "
                      f"{c.get('sites_attempted', 0)} sites attempted, "
                      f"{c.get('sites_failed', 0)} failed")
            if "analysis" in meta:
                a = meta["analysis"]
                print(f"  Analysis: {a.get('stages_completed', 0)} completed, "
                      f"{a.get('stages_failed', 0)} failed, "
                      f"{a.get('stages_skipped', 0)} skipped, "
                      f"peak {a.get('peak_memory_gb', '?')} GB")
        except (json.JSONDecodeError, OSError):
            pass

    # Check data directories
    print(f"\nData Directories:")
    for name, path in [
        ("raw", DATA_RAW_DIR),
        ("processed", PROJECT_ROOT / "data" / "processed"),
        ("features", PROJECT_ROOT / "data" / "features"),
        ("analysis", PROJECT_ROOT / "data" / "analysis"),
        ("output", PROJECT_ROOT / "data" / "output"),
        ("insights", PROJECT_ROOT / "data" / "insights"),
    ]:
        exists = path.exists()
        file_count = len(list(path.glob("*"))) if exists else 0
        print(f"  data/{name}/: {'EXISTS' if exists else 'MISSING'} ({file_count} files)")

    # Insight pipeline status
    insight_state_path = PROJECT_ROOT / "data" / "insights" / "insight_state.json"
    if insight_state_path.exists():
        try:
            state = json.loads(insight_state_path.read_text(encoding="utf-8"))
            n_completed = len(state.get("modules_completed", []))
            n_failed = len(state.get("modules_failed", []))
            n_skipped = len(state.get("modules_skipped", []))
            n_total = n_completed + n_failed + n_skipped
            print(f"\nInsight Pipeline (Workflow B):")
            print(f"  Last run: {state.get('last_run', '?')}")
            print(f"  Window: {state.get('last_window', '?')} days ending {state.get('last_end_date', '?')}")
            print(f"  Modules: {n_completed}/{n_total} completed"
                  f"{f', {n_failed} failed' if n_failed else ''}"
                  f"{f', {n_skipped} skipped' if n_skipped else ''}")
            print(f"  Validation: {'PASS' if state.get('validation_passed') else 'FAIL'}")
            print(f"  Output: {state.get('run_id', '?')}")
            cov = state.get("data_coverage", {})
            if cov:
                print(f"  Coverage: {cov.get('available_days', '?')}/{cov.get('window_days', '?')} days "
                      f"({cov.get('coverage_ratio', 0):.0%})")
        except (json.JSONDecodeError, OSError):
            pass

    print()
    return 0


def cmd_insight(args: argparse.Namespace) -> int:
    """Execute the big data insight analytics pipeline.

    Reads accumulated Stage 1-4 outputs across a multi-date window
    to produce structural insights (cross-lingual, geopolitical, economic, etc.).

    Args:
        args: Parsed command-line arguments.

    Returns:
        Exit code (0 = success, 1 = failure).
    """
    logger = get_logger("main.insight")

    end_date = args.end_date or args.date
    window_days = args.window or 30

    logger.info(
        "Insight pipeline started: window=%d days, end_date=%s, module=%s",
        window_days, end_date, args.module or "all",
    )

    from src.insights.pipeline import run_insight_pipeline

    t0 = time.monotonic()
    try:
        result = run_insight_pipeline(
            end_date=end_date,
            window_days=window_days,
            module=args.module,
        )
    except Exception as e:
        logger.error("Insight pipeline failed: %s", e, exc_info=True)
        return 1

    elapsed = time.monotonic() - t0

    # Summary
    logger.info(
        "Insight pipeline %s: %d/%d modules (%.1fs)",
        "COMPLETE" if result.success else "PARTIAL",
        len(result.modules_completed),
        len(result.modules_completed) + len(result.modules_failed) + len(result.modules_skipped),
        elapsed,
    )
    if result.modules_completed:
        logger.info("  Completed: %s", ", ".join(result.modules_completed))
    if result.modules_skipped:
        logger.info("  Skipped: %s", ", ".join(result.modules_skipped))
    if result.modules_failed:
        logger.warning("  Failed: %s", ", ".join(result.modules_failed))
    logger.info(
        "  Validation: %s", "PASS" if result.validation_passed else
        f"FAIL ({len(result.validation_errors)} errors)",
    )
    logger.info("  Output: %s", result.output_dir)

    return 0 if result.success else 1


def cmd_dci(args: argparse.Namespace) -> int:
    """Execute the WF4 Phase 4 Deep Content Intelligence (DCI) layer.

    DCI is an internal Phase of WF4 Master Integration — not a new
    workflow. See research/wf4-dci-design-v0.5.md for the 14-layer
    architecture (L-1 ... L11) and 93-technique registry.

    Phase 0-B (current): this handler validates that the package
    imports cleanly and runs the orchestrator in **dry-run** mode when
    --dry-run is passed. Actual layer implementations land in Phase 1+.

    When invoked without --dry-run in Phase 0-B, the orchestrator
    refuses to proceed unless constants.DCI_ENABLED is flipped to True.
    """
    logger = get_logger("main.dci")

    # Build run_id — user-supplied or deterministic default
    date_str = (
        args.date.isoformat() if hasattr(args.date, "isoformat") else str(args.date)
    )
    run_id = args.run_id or f"dci-{date_str}-{datetime.utcnow():%H%M}"

    logger.info(
        "DCI pipeline start: run_id=%s date=%s dry_run=%s",
        run_id, date_str, args.dry_run,
    )

    # Import deferred until needed — keeps CLI start-up light when mode != dci
    from src.dci.orchestrator import (
        DCIOrchestrator, ArticleCorpus,
        ensure_layers_registered, registered_layer_count, registered_layer_ids,
    )
    from src.config.constants import DCI_ENABLED

    # Trigger layer registration (Phase 0-B: L0/L1/L5/L6 stubs are wired)
    ensure_layers_registered()

    logger.info(
        "DCI registered layers: %d (%s)",
        registered_layer_count(),
        ", ".join(registered_layer_ids()) or "none — Phase 0-B",
    )

    if not args.dry_run and not DCI_ENABLED:
        logger.error(
            "DCI_ENABLED is False (Phase 0-B default). "
            "Set constants.DCI_ENABLED=True or pass --dry-run."
        )
        return 2

    # Phase 1: load real article corpus from W1 output when available.
    # In dry-run mode, we still attempt the load so the PoC surfaces
    # realistic char_coverage metrics; empty corpus is tolerated.
    from src.dci.corpus_loader import load_article_corpus
    corpus, stats = load_article_corpus(date_str)
    logger.info(
        "DCI corpus loaded: %d articles (lines=%d missing_body=%d malformed=%d)",
        stats.articles_yielded, stats.lines_total,
        stats.articles_missing_body, stats.malformed_lines,
    )
    if stats.articles_yielded == 0 and not args.dry_run:
        logger.error(
            "No articles available for real-run at date=%s. "
            "Run --mode crawl first or pass --dry-run.",
            date_str,
        )
        return 2
    orch = DCIOrchestrator(
        project_dir=str(Path(__file__).resolve().parent),
        run_id=run_id,
        dry_run=args.dry_run,
    )
    t0 = time.monotonic()
    result = orch.run(corpus)
    elapsed = time.monotonic() - t0

    logger.info(
        "DCI dry-run complete: layers_completed=%d layers_skipped=%d "
        "sg_decision=%s elapsed=%.3fs",
        len(result.layers_completed), len(result.layers_skipped),
        result.sg_verdict.get("decision", "?"), elapsed,
    )

    # Emit structured summary (for CI smoke + Phase 0-C harness).
    print()
    print("=" * 70)
    print(f"  DCI v0.5 DRY-RUN SUMMARY")
    print("=" * 70)
    print(f"  Run id:               {result.run_id}")
    print(f"  Date:                 {result.date}")
    print(f"  Articles in:          {result.articles_in}")
    print(f"  Layers completed:     {len(result.layers_completed)}")
    print(f"  Layers failed:        {len(result.layers_failed)}")
    print(f"  Layers skipped:       {len(result.layers_skipped)}")
    print(f"  SG-Superhuman:        {result.sg_verdict.get('decision', '?')}")
    print(f"  Elapsed:              {result.elapsed_seconds}s")
    print("=" * 70)

    # Dry-run always returns 0 when layers ran without exception — the
    # SG FAIL is *expected* in Phase 0-B (no implementations registered).
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the CLI.

    Returns:
        Configured ArgumentParser.
    """
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="GlobalNews Crawling & Analysis System -- "
                    "Crawl 116 news sites, analyze through 8-stage NLP pipeline, "
                    "produce Parquet/SQLite output for social trend research.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  python main.py --mode crawl --date 2026-02-25 --dry-run
  python main.py --mode analyze --all-stages
  python main.py --mode full --date 2026-02-25
  python main.py --mode status

Architecture: Staged Monolith (Python 3.12)
Runtime: MacBook M2 Pro 16GB, 20GB memory budget
Sites: 44 international news sources across 7 groups (A-G)
Pipeline: 8 stages, 56 analysis techniques
""",
    )

    parser.add_argument(
        "--version", action="version", version="GlobalNews 0.1.0",
    )

    parser.add_argument(
        "--mode",
        choices=["crawl", "analyze", "full", "status", "insight", "dci"],
        required=True,
        help="Execution mode: crawl (URL discovery + extraction), "
             "analyze (8-stage NLP pipeline), full (crawl + analyze), "
             "insight (big data insight analytics on accumulated data), "
             "dci (WF4 Phase 4 Deep Content Intelligence — v0.5), "
             "status (show pipeline status)",
    )

    parser.add_argument(
        "--date",
        type=_validate_date,
        default=None,
        help="Target date in YYYY-MM-DD format (default: today)",
    )

    parser.add_argument(
        "--sites",
        type=str,
        default=None,
        help="Comma-separated site IDs to crawl (e.g., chosun,donga,yna). "
             "Default: all enabled sites.",
    )

    parser.add_argument(
        "--groups",
        type=str,
        default=None,
        help="Comma-separated group letters to crawl (e.g., A,B,E). "
             "Default: all groups.",
    )

    parser.add_argument(
        "--stage",
        type=_validate_stage,
        default=None,
        help="Specific analysis stage to run (1-8). Use with --mode analyze.",
    )

    parser.add_argument(
        "--all-stages",
        action="store_true",
        default=False,
        help="Run all 8 analysis stages sequentially.",
    )

    parser.add_argument(
        "--window",
        type=int,
        default=None,
        help="Analysis window in days for --mode insight (default: 30). "
             "Common values: 7 (weekly), 30 (monthly), 90 (quarterly).",
    )

    parser.add_argument(
        "--module",
        type=str,
        default=None,
        help="Run a specific insight module only (e.g., m1_crosslingual, m5_geopolitical). "
             "Use with --mode insight.",
    )

    parser.add_argument(
        "--end-date",
        type=_validate_date,
        default=None,
        help="End date for insight analysis window (default: --date or today).",
    )

    parser.add_argument(
        "--skip-insight",
        action="store_true",
        default=False,
        help="Skip auto-chained insight pipeline after --mode full. "
             "By default, --mode full runs Workflow A (crawl + analyze) then "
             "auto-chains Workflow B (insight, 30-day window). Use this flag "
             "to run Workflow A only.",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Validate configuration and show plan without executing.",
    )

    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Console log level (default: INFO).",
    )

    # WF4-DCI v0.5 — Phase 4 Deep Content Intelligence flags
    parser.add_argument(
        "--run-id",
        type=str,
        default=None,
        help="Execution run id (for --mode dci). Default: dci-<date>-<hh:mm>",
    )

    return parser


def main() -> int:
    """Main entry point for the GlobalNews CLI.

    Returns:
        Exit code (0 = success, non-zero = failure).
    """
    parser = build_parser()
    args = parser.parse_args()

    # Set default date to today if not specified
    if args.date is None:
        args.date = date.today()

    # Initialize logging
    setup_logging(console_level=args.log_level)

    # Parse comma-separated sites/groups into lists
    if args.sites:
        args.sites = [s.strip() for s in args.sites.split(",") if s.strip()]
    if args.groups:
        args.groups = [g.strip().upper() for g in args.groups.split(",") if g.strip()]

    # Dispatch to appropriate command handler
    handlers = {
        "crawl": cmd_crawl,
        "analyze": cmd_analyze,
        "full": cmd_full,
        "status": cmd_status,
        "insight": cmd_insight,
        "dci": cmd_dci,
    }

    handler = handlers.get(args.mode)
    if handler is None:
        parser.error(f"Unknown mode: {args.mode}")
        return 1

    try:
        return handler(args)
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
        return 130
    except Exception as e:
        logger = get_logger("main")
        logger.error(f"Unhandled error: {e}", exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
