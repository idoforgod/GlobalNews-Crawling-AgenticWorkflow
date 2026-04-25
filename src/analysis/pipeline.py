"""Analysis Pipeline Orchestrator: 8-stage sequential pipeline with memory management.

Connects all analysis stages into a unified sequential pipeline:
    Stage 1: Preprocessing     -- JSONL -> Parquet (Kiwi + spaCy tokenization)
    Stage 2: Feature Extraction -- SBERT embeddings, TF-IDF, NER, KeyBERT
    Stage 3: Article Analysis   -- Sentiment, emotion, STEEPS classification
    Stage 4: Aggregation        -- BERTopic, HDBSCAN, NMF/LDA, community detection
    Stage 5: Time Series        -- STL, burst, changepoint, Prophet, wavelet
    Stage 6: Cross Analysis     -- Granger, PCMCI, co-occurrence, cross-lingual
    Stage 7: Signal Classification -- 5-Layer (L1-L5) + novelty detection
    Stage 8: Data Output        -- Parquet merge + SQLite FTS5/vec index

Key design decisions:
    - Sequential execution: 1->2->3->4->5->6->7->8 (no parallelism within stages)
    - Memory management: explicit del + gc.collect() + torch cache clear between stages
    - Atomic stage execution: temp file + rename; failure does not corrupt prior outputs
    - Checkpoint support: can resume from any stage if prior outputs exist on disk
    - Dependency graph: stages 1-4 are strictly sequential; 5-6 can fail independently
      (stage 7 needs 5+6; stage 8 needs all prior outputs)

Memory budget (M2 Pro 16GB, peak <= 5GB at any single stage):
    Stage 1: ~1.0 GB  |  Stage 2: ~2.4 GB  |  Stage 3: ~1.8 GB  |  Stage 4: ~1.5 GB
    Stage 5: ~0.5 GB  |  Stage 6: ~0.8 GB  |  Stage 7: ~0.5 GB  |  Stage 8: ~0.5 GB

CLI: ``python3 main.py --mode analyze --date 2026-02-25 [--stage 3] [--all-stages]``

Reference:
    Step 5 Architecture Blueprint, Section 6 (Pipeline Orchestration).
    Step 7 Analysis Pipeline Design, Section 3 (8-Stage Architecture).
"""

from __future__ import annotations

import gc
import logging
import os
import resource
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.config.constants import (
    ANALYSIS_PARQUET_PATH,
    ARTICLES_PARQUET_PATH,
    ARTICLE_ANALYSIS_PARQUET_PATH,
    CROSS_ANALYSIS_PARQUET_PATH,
    DATA_ANALYSIS_DIR,
    DATA_DIR,
    DATA_FEATURES_DIR,
    DATA_OUTPUT_DIR,
    DATA_PROCESSED_DIR,
    DATA_RAW_DIR,
    EMBEDDINGS_PARQUET_PATH,
    NER_PARQUET_PATH,
    NETWORKS_PARQUET_PATH,
    ROLLING_WINDOW_DAYS,
    SIGNALS_PARQUET_PATH,
    SQLITE_INDEX_PATH,
    TFIDF_PARQUET_PATH,
    TIMESERIES_PARQUET_PATH,
    TOPICS_PARQUET_PATH,
)
from src.utils.error_handler import (
    AnalysisError,
    MemoryLimitError,
    ModelLoadError,
    PipelineStageError,
)
from src.utils.logging_config import get_logger

logger = get_logger(__name__)

# =============================================================================
# Constants
# =============================================================================

# Memory limit: imported from constants (PRD C3 hard limit, D-7 single source)
from src.config.constants import MAX_MEMORY_GB as MEMORY_ABORT_THRESHOLD_GB

# Warning threshold: log a warning if RSS exceeds this (GB)
MEMORY_WARNING_THRESHOLD_GB: float = 5.0

# Stage dependency graph: which prior stage outputs does each stage require?
# Used for checkpoint validation (can we start from stage N?).
STAGE_DEPENDENCIES: dict[int, list[Path]] = {
    1: [],  # Stage 1 reads from raw JSONL (validated separately)
    2: [ARTICLES_PARQUET_PATH],
    3: [ARTICLES_PARQUET_PATH, EMBEDDINGS_PARQUET_PATH, NER_PARQUET_PATH],
    4: [ARTICLES_PARQUET_PATH, EMBEDDINGS_PARQUET_PATH, TFIDF_PARQUET_PATH,
        NER_PARQUET_PATH, ARTICLE_ANALYSIS_PARQUET_PATH],
    5: [ARTICLES_PARQUET_PATH, TOPICS_PARQUET_PATH, ARTICLE_ANALYSIS_PARQUET_PATH],
    6: [TIMESERIES_PARQUET_PATH, TOPICS_PARQUET_PATH,
        ARTICLE_ANALYSIS_PARQUET_PATH, NETWORKS_PARQUET_PATH,
        EMBEDDINGS_PARQUET_PATH, ARTICLES_PARQUET_PATH],
    7: [TOPICS_PARQUET_PATH, TIMESERIES_PARQUET_PATH,
        CROSS_ANALYSIS_PARQUET_PATH, ARTICLE_ANALYSIS_PARQUET_PATH,
        NETWORKS_PARQUET_PATH, EMBEDDINGS_PARQUET_PATH],
    8: [ARTICLES_PARQUET_PATH, EMBEDDINGS_PARQUET_PATH, NER_PARQUET_PATH,
        ARTICLE_ANALYSIS_PARQUET_PATH, TOPICS_PARQUET_PATH,
        SIGNALS_PARQUET_PATH],
}

# Stages that can be skipped without blocking later stages
# (their failure leaves prior outputs intact and non-dependent later stages can proceed)
INDEPENDENT_STAGES: set[int] = {5, 6}

STAGE_NAMES: dict[int, str] = {
    1: "Preprocessing",
    2: "Feature Extraction",
    3: "Article Analysis",
    4: "Aggregation",
    5: "Time Series",
    6: "Cross Analysis",
    7: "Signal Classification",
    8: "Data Output",
}


# =============================================================================
# Data Classes
# =============================================================================

@dataclass
class StageResult:
    """Result of executing a single pipeline stage.

    Attributes:
        stage_number: Stage number (1-8).
        stage_name: Human-readable stage name.
        success: Whether the stage completed without error.
        elapsed_seconds: Wall-clock time for this stage.
        peak_memory_gb: Peak RSS memory during this stage (GB).
        article_count: Number of articles processed (if applicable).
        output_paths: List of output file paths created by this stage.
        error_message: Error description if success is False.
        error_type: Exception class name if an error occurred.
        skipped: Whether the stage was skipped (checkpoint resume or dependency failure).
        skip_reason: Reason for skipping, if applicable.
    """

    stage_number: int = 0
    stage_name: str = ""
    success: bool = False
    elapsed_seconds: float = 0.0
    peak_memory_gb: float = 0.0
    article_count: int = 0
    output_paths: list[str] = field(default_factory=list)
    error_message: str = ""
    error_type: str = ""
    skipped: bool = False
    skip_reason: str = ""


@dataclass
class AnalysisPipelineResult:
    """Aggregate result from the full 8-stage analysis pipeline.

    Attributes:
        success: True if all requested stages completed successfully.
        stages: Per-stage results (keyed by stage number).
        total_elapsed_seconds: Total wall-clock time for the pipeline.
        peak_memory_gb: Maximum RSS observed across all stages.
        stages_completed: List of stage numbers that completed successfully.
        stages_failed: List of stage numbers that failed.
        stages_skipped: List of stage numbers that were skipped.
        final_output_paths: Paths to final output files (from Stage 8).
        date: Target date for this pipeline run.
        started_at: ISO timestamp when the pipeline started.
        finished_at: ISO timestamp when the pipeline finished.
    """

    success: bool = False
    stages: dict[int, StageResult] = field(default_factory=dict)
    total_elapsed_seconds: float = 0.0
    peak_memory_gb: float = 0.0
    stages_completed: list[int] = field(default_factory=list)
    stages_failed: list[int] = field(default_factory=list)
    stages_skipped: list[int] = field(default_factory=list)
    final_output_paths: dict[str, str] = field(default_factory=dict)
    date: str = ""
    started_at: str = ""
    finished_at: str = ""


# =============================================================================
# Memory Monitor
# =============================================================================

class MemoryMonitor:
    """Monitors RSS memory usage and enforces hard limits.

    Uses resource.getrusage() for macOS/Linux RSS tracking.
    Provides methods to check current usage, log it, and abort
    if usage exceeds the configured threshold.
    """

    def __init__(
        self,
        abort_threshold_gb: float = MEMORY_ABORT_THRESHOLD_GB,
        warning_threshold_gb: float = MEMORY_WARNING_THRESHOLD_GB,
    ) -> None:
        self._abort_threshold_gb = abort_threshold_gb
        self._warning_threshold_gb = warning_threshold_gb
        self._peak_gb: float = 0.0

    @staticmethod
    def get_rss_gb() -> float:
        """Get current RSS (Resident Set Size) in GB.

        On macOS, ru_maxrss is in bytes. On Linux, it is in KB.
        This method normalizes to GB.

        Returns:
            Current RSS in gigabytes.
        """
        usage = resource.getrusage(resource.RUSAGE_SELF)
        rss_bytes = usage.ru_maxrss
        # macOS reports bytes; Linux reports kilobytes
        if os.uname().sysname == "Darwin":
            return rss_bytes / (1024 ** 3)
        else:
            return rss_bytes / (1024 ** 2)

    def check_and_log(self, context: str = "") -> float:
        """Check current memory, log it, and enforce limits.

        Args:
            context: Description of what is being measured (e.g., "after_stage_2").

        Returns:
            Current RSS in GB.

        Raises:
            MemoryLimitError: If RSS exceeds the abort threshold.
        """
        current_gb = self.get_rss_gb()
        self._peak_gb = max(self._peak_gb, current_gb)

        if current_gb >= self._abort_threshold_gb:
            msg = (
                f"Memory abort threshold exceeded: {current_gb:.2f} GB "
                f"(limit: {self._abort_threshold_gb:.1f} GB) "
                f"context={context}"
            )
            logger.error(msg)
            raise MemoryLimitError(
                msg,
                current_gb=current_gb,
                limit_gb=self._abort_threshold_gb,
            )

        if current_gb >= self._warning_threshold_gb:
            logger.warning(
                "memory_warning rss=%.2f GB threshold=%.1f GB context=%s",
                current_gb, self._warning_threshold_gb, context,
            )
        else:
            logger.info(
                "memory_check rss=%.2f GB context=%s",
                current_gb, context,
            )

        return current_gb

    @property
    def peak_gb(self) -> float:
        """Return the peak RSS observed so far."""
        return self._peak_gb

    @staticmethod
    def cleanup() -> None:
        """Force garbage collection and clear torch CUDA cache if available.

        This is the standard inter-stage cleanup sequence:
        1. gc.collect() -- reclaim Python objects
        2. torch.cuda.empty_cache() -- release GPU memory (if torch is available)
        """
        gc.collect()
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
            # Also clear MPS cache on Apple Silicon
            if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
                # torch.mps.empty_cache() available in torch >= 2.1
                if hasattr(torch.mps, "empty_cache"):
                    torch.mps.empty_cache()
        except ImportError:
            pass  # torch not installed -- nothing to clear


# =============================================================================
# Analysis Pipeline
# =============================================================================

class AnalysisPipeline:
    """Orchestrates the 8-stage analysis pipeline with memory management.

    Executes stages 1 through 8 in strict sequence, with inter-stage
    memory cleanup, progress logging, and checkpoint support.

    Args:
        data_dir: Root data directory. Defaults to ``data/``.
        date: Target date (YYYY-MM-DD). Defaults to today.
        memory_abort_gb: RSS threshold to abort pipeline (GB).
        memory_warning_gb: RSS threshold to warn (GB).
    """

    def __init__(
        self,
        data_dir: str | Path | None = None,
        date: str | None = None,
        memory_abort_gb: float = MEMORY_ABORT_THRESHOLD_GB,
        memory_warning_gb: float = MEMORY_WARNING_THRESHOLD_GB,
        rolling_window_days: int = ROLLING_WINDOW_DAYS,
    ) -> None:
        self._data_dir = Path(data_dir) if data_dir else DATA_DIR
        self._date = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self._memory = MemoryMonitor(
            abort_threshold_gb=memory_abort_gb,
            warning_threshold_gb=memory_warning_gb,
        )

        # Rolling window configuration for Stage 6/7. Default 30 days.
        # When 0 or negative, rolling window is disabled and stages 6/7
        # fall back to single-day per-date directories (legacy behavior).
        self._rolling_window_days = rolling_window_days

        # Resolve key directories from data_dir — date-partitioned
        # Each daily run writes to its own subdirectory so results accumulate
        # and are available for multi-day (monthly/quarterly/yearly) analysis.
        self._raw_dir = self._data_dir / "raw" / self._date
        self._processed_dir = self._data_dir / "processed" / self._date
        self._features_dir = self._data_dir / "features" / self._date
        self._analysis_dir = self._data_dir / "analysis" / self._date
        self._output_dir = self._data_dir / "output" / self._date

        # Rolling-window workspace for Stage 6/7 (created on-demand).
        self._rolling_dir = (
            self._analysis_dir / f"_rolling_{self._rolling_window_days}d"
        )

    # -----------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------

    def run(
        self,
        stages: list[int] | None = None,
        input_path: str | Path | None = None,
    ) -> AnalysisPipelineResult:
        """Execute the analysis pipeline.

        Args:
            stages: Specific stages to run (e.g., [1,2,3]). If None, run all 1-8.
                    When starting from a stage > 1, validates that prior stage
                    outputs exist on disk (checkpoint support).
            input_path: Override path to JSONL input for Stage 1.
                        If None, defaults to data/raw/{date}/all_articles.jsonl.

        Returns:
            AnalysisPipelineResult with per-stage results and aggregate metrics.
        """
        pipeline_start = time.monotonic()
        started_at = datetime.now(timezone.utc).isoformat()

        stages_to_run = stages if stages else list(range(1, 9))
        stages_to_run = sorted(stages_to_run)

        logger.info(
            "analysis_pipeline_start date=%s stages=%s data_dir=%s",
            self._date, stages_to_run, self._data_dir,
        )

        # Ensure output directories exist
        self._ensure_directories()

        result = AnalysisPipelineResult(
            date=self._date,
            started_at=started_at,
        )

        # Validate checkpoint dependencies for the first requested stage
        first_stage = stages_to_run[0]
        if first_stage > 1:
            missing = self._check_dependencies(first_stage)
            if missing:
                msg = (
                    f"Cannot start from stage {first_stage}: "
                    f"missing prior outputs: {[str(p) for p in missing]}"
                )
                logger.error(msg)
                result.success = False
                result.total_elapsed_seconds = time.monotonic() - pipeline_start
                result.finished_at = datetime.now(timezone.utc).isoformat()
                # Record the first stage as failed with dependency error
                result.stages[first_stage] = StageResult(
                    stage_number=first_stage,
                    stage_name=STAGE_NAMES.get(first_stage, f"Stage {first_stage}"),
                    success=False,
                    error_message=msg,
                    error_type="DependencyError",
                )
                result.stages_failed.append(first_stage)
                return result

        # Execute each stage
        failed_critical = False  # True if a non-independent stage failed

        for stage_num in stages_to_run:
            stage_name = STAGE_NAMES.get(stage_num, f"Stage {stage_num}")

            # Check if this stage should be skipped due to upstream failure
            if failed_critical and stage_num not in INDEPENDENT_STAGES:
                stage_result = StageResult(
                    stage_number=stage_num,
                    stage_name=stage_name,
                    skipped=True,
                    skip_reason="Upstream stage failure",
                )
                result.stages[stage_num] = stage_result
                result.stages_skipped.append(stage_num)
                logger.warning(
                    "stage_skipped stage=%s reason=upstream_failure",
                    stage_num,
                )
                continue

            # For stages 7 (needs 5+6) -- check if both are available
            if stage_num == 7:
                deps_7_missing = self._check_dependencies(7)
                if deps_7_missing:
                    stage_result = StageResult(
                        stage_number=stage_num,
                        stage_name=stage_name,
                        skipped=True,
                        skip_reason=f"Missing dependencies: {[str(p) for p in deps_7_missing]}",
                    )
                    result.stages[stage_num] = stage_result
                    result.stages_skipped.append(stage_num)
                    logger.warning(
                        "stage_skipped stage=%s reason=missing_dependencies deps=%s",
                        stage_num, [str(p) for p in deps_7_missing],
                    )
                    continue

            # Execute the stage
            logger.info(
                "stage_start stage=%s name=%s",
                stage_num, stage_name,
            )

            stage_result = self._run_stage(stage_num, input_path=input_path)
            result.stages[stage_num] = stage_result

            if stage_result.success:
                result.stages_completed.append(stage_num)
                logger.info(
                    "stage_complete stage=%s name=%s elapsed=%.1fs "
                    "articles=%s memory=%.2f_GB outputs=%s",
                    stage_num, stage_name, stage_result.elapsed_seconds,
                    stage_result.article_count, stage_result.peak_memory_gb,
                    stage_result.output_paths,
                )
            else:
                result.stages_failed.append(stage_num)
                logger.error(
                    "stage_failed stage=%s name=%s error_type=%s error=%s",
                    stage_num, stage_name, stage_result.error_type,
                    stage_result.error_message,
                )

                # Mark critical failure for non-independent stages
                if stage_num not in INDEPENDENT_STAGES:
                    failed_critical = True

            # Inter-stage memory cleanup
            self._inter_stage_cleanup(stage_num)

        # Finalize result
        result.total_elapsed_seconds = time.monotonic() - pipeline_start
        result.peak_memory_gb = self._memory.peak_gb
        result.finished_at = datetime.now(timezone.utc).isoformat()

        # Determine overall success: all requested stages completed
        result.success = len(result.stages_failed) == 0 and len(result.stages_skipped) == 0

        # Collect final output paths
        result.final_output_paths = self._collect_final_outputs()

        logger.info(
            "analysis_pipeline_complete success=%s completed=%s failed=%s "
            "skipped=%s total_elapsed=%.1fs peak_memory=%.2f_GB",
            result.success, result.stages_completed, result.stages_failed,
            result.stages_skipped, result.total_elapsed_seconds,
            result.peak_memory_gb,
        )

        return result

    # -----------------------------------------------------------------
    # Stage Execution
    # -----------------------------------------------------------------

    def _run_stage(
        self,
        stage_num: int,
        input_path: str | Path | None = None,
    ) -> StageResult:
        """Execute a single pipeline stage with error handling.

        Each stage is wrapped in try/except to ensure that errors in one
        stage do not propagate to corrupt prior outputs. Stage functions
        are dispatched via _STAGE_RUNNERS.

        Args:
            stage_num: Stage number (1-8).
            input_path: Override JSONL input path for Stage 1.

        Returns:
            StageResult with success/failure, timing, and memory metrics.
        """
        stage_name = STAGE_NAMES.get(stage_num, f"Stage {stage_num}")
        stage_start = time.monotonic()

        # Pre-stage memory check
        try:
            self._memory.check_and_log(f"before_stage_{stage_num}")
        except MemoryLimitError as e:
            return StageResult(
                stage_number=stage_num,
                stage_name=stage_name,
                success=False,
                error_message=str(e),
                error_type="MemoryLimitError",
            )

        try:
            runner = self._get_stage_runner(stage_num)
            stage_output = runner(input_path=input_path)

            # Post-stage memory check
            post_memory = self._memory.check_and_log(f"after_stage_{stage_num}")

            elapsed = time.monotonic() - stage_start

            return StageResult(
                stage_number=stage_num,
                stage_name=stage_name,
                success=True,
                elapsed_seconds=round(elapsed, 2),
                peak_memory_gb=round(post_memory, 3),
                article_count=stage_output.get("article_count", 0),
                output_paths=stage_output.get("output_paths", []),
            )

        except MemoryLimitError as e:
            elapsed = time.monotonic() - stage_start
            return StageResult(
                stage_number=stage_num,
                stage_name=stage_name,
                success=False,
                elapsed_seconds=round(elapsed, 2),
                error_message=str(e),
                error_type="MemoryLimitError",
            )

        except PipelineStageError as e:
            elapsed = time.monotonic() - stage_start
            return StageResult(
                stage_number=stage_num,
                stage_name=stage_name,
                success=False,
                elapsed_seconds=round(elapsed, 2),
                error_message=str(e),
                error_type="PipelineStageError",
            )

        except ModelLoadError as e:
            elapsed = time.monotonic() - stage_start
            return StageResult(
                stage_number=stage_num,
                stage_name=stage_name,
                success=False,
                elapsed_seconds=round(elapsed, 2),
                error_message=str(e),
                error_type="ModelLoadError",
            )

        except FileNotFoundError as e:
            elapsed = time.monotonic() - stage_start
            return StageResult(
                stage_number=stage_num,
                stage_name=stage_name,
                success=False,
                elapsed_seconds=round(elapsed, 2),
                error_message=str(e),
                error_type="FileNotFoundError",
            )

        except AnalysisError as e:
            elapsed = time.monotonic() - stage_start
            return StageResult(
                stage_number=stage_num,
                stage_name=stage_name,
                success=False,
                elapsed_seconds=round(elapsed, 2),
                error_message=str(e),
                error_type=type(e).__name__,
            )

        except Exception as e:
            elapsed = time.monotonic() - stage_start
            logger.error(
                "stage_unexpected_error stage=%s error=%s error_type=%s",
                stage_num, str(e), type(e).__name__,
                exc_info=True,
            )
            return StageResult(
                stage_number=stage_num,
                stage_name=stage_name,
                success=False,
                elapsed_seconds=round(elapsed, 2),
                error_message=f"{type(e).__name__}: {e}",
                error_type=type(e).__name__,
            )

    def _get_stage_runner(self, stage_num: int):
        """Return the appropriate runner method for a stage number.

        Args:
            stage_num: Stage number (1-8).

        Returns:
            Callable that accepts ``input_path`` keyword arg and returns dict.

        Raises:
            ValueError: If stage_num is not 1-8.
        """
        runners = {
            1: self._run_stage1,
            2: self._run_stage2,
            3: self._run_stage3,
            4: self._run_stage4,
            5: self._run_stage5,
            6: self._run_stage6,
            7: self._run_stage7,
            8: self._run_stage8,
        }
        runner = runners.get(stage_num)
        if runner is None:
            raise ValueError(f"Invalid stage number: {stage_num}. Must be 1-8.")
        return runner

    # -----------------------------------------------------------------
    # Individual Stage Runners
    # -----------------------------------------------------------------

    def _run_stage1(self, input_path: str | Path | None = None) -> dict[str, Any]:
        """Run Stage 1: Preprocessing (JSONL -> Parquet).

        Args:
            input_path: Override JSONL directory path.

        Returns:
            Dict with article_count and output_paths.
        """
        from src.analysis.stage1_preprocessing import run_stage1

        input_dir = Path(input_path) if input_path else self._raw_dir
        output_path = self._processed_dir / "articles.parquet"

        logger.info(
            "stage1_run input_dir=%s output_path=%s date=%s",
            input_dir, output_path, self._date,
        )

        table, _intermediates, stats = run_stage1(
            input_dir=input_dir,
            output_path=output_path,
            date=self._date,
            keep_kiwi=False,  # Release Kiwi after Stage 1
        )

        return {
            "article_count": len(table) if table is not None else 0,
            "output_paths": [str(output_path)],
        }

    def _run_stage2(self, input_path: str | Path | None = None) -> dict[str, Any]:
        """Run Stage 2: Feature Extraction (embeddings, TF-IDF, NER, KeyBERT).

        Args:
            input_path: Not used for Stage 2 (reads from Stage 1 output).

        Returns:
            Dict with article_count and output_paths.
        """
        from src.analysis.stage2_features import run_stage2

        articles_path = self._processed_dir / "articles.parquet"
        output_dir = self._features_dir

        logger.info(
            "stage2_run articles_path=%s output_dir=%s",
            articles_path, output_dir,
        )

        metrics = run_stage2(
            articles_path=articles_path,
            output_dir=output_dir,
        )

        return {
            "article_count": metrics.total_articles,
            "output_paths": [
                str(output_dir / "embeddings.parquet"),
                str(output_dir / "tfidf.parquet"),
                str(output_dir / "ner.parquet"),
            ],
        }

    def _run_stage3(self, input_path: str | Path | None = None) -> dict[str, Any]:
        """Run Stage 3: Article Analysis (sentiment, emotion, STEEPS).

        Args:
            input_path: Not used for Stage 3.

        Returns:
            Dict with article_count and output_paths.
        """
        from src.analysis.stage3_article_analysis import run_stage3

        articles_path = self._processed_dir / "articles.parquet"
        features_dir = self._features_dir
        output_path = self._analysis_dir / "article_analysis.parquet"

        logger.info(
            "stage3_run articles_path=%s features_dir=%s output_path=%s",
            articles_path, features_dir, output_path,
        )

        result = run_stage3(
            articles_path=articles_path,
            features_dir=features_dir,
            output_path=output_path,
        )

        # run_stage3 returns a dict with 'output_path', 'elapsed_s', 'stats'
        article_count = 0
        if "stats" in result and isinstance(result["stats"], dict):
            article_count = result["stats"].get("total_articles", 0)

        # Also check for mood_trajectory output
        output_paths = [str(output_path)]
        mood_path = self._analysis_dir / "mood_trajectory.parquet"
        if mood_path.exists():
            output_paths.append(str(mood_path))

        return {
            "article_count": article_count,
            "output_paths": output_paths,
        }

    def _run_stage4(self, input_path: str | Path | None = None) -> dict[str, Any]:
        """Run Stage 4: Aggregation (topics, clusters, communities).

        Args:
            input_path: Not used for Stage 4.

        Returns:
            Dict with article_count and output_paths.
        """
        from src.analysis.stage4_aggregation import run_stage4

        articles_path = self._processed_dir / "articles.parquet"
        features_dir = self._features_dir
        analysis_dir = self._analysis_dir

        logger.info(
            "stage4_run articles_path=%s features_dir=%s analysis_dir=%s",
            articles_path, features_dir, analysis_dir,
        )

        output = run_stage4(
            articles_path=articles_path,
            features_dir=features_dir,
            analysis_dir=analysis_dir,
            output_dir=analysis_dir,
            sbert_model=None,  # Let it load its own; we cleaned up Stage 2 models
            cleanup_after=True,
        )

        output_paths = []
        topics_path = self._analysis_dir / "topics.parquet"
        if topics_path.exists():
            output_paths.append(str(topics_path))
        networks_path = self._analysis_dir / "networks.parquet"
        if networks_path.exists():
            output_paths.append(str(networks_path))
        dtm_path = self._analysis_dir / "dtm.parquet"
        if dtm_path.exists():
            output_paths.append(str(dtm_path))

        # Report number of articles that received topic assignments so the
        # metric reflects actual Stage 4 throughput rather than zero.
        topic_rows = 0
        if topics_path.exists():
            try:
                import pyarrow.parquet as pq
                topic_rows = pq.ParquetFile(topics_path).metadata.num_rows
            except Exception:
                topic_rows = 0

        return {
            "article_count": topic_rows,
            "output_paths": output_paths,
        }

    def _run_stage5(self, input_path: str | Path | None = None) -> dict[str, Any]:
        """Run Stage 5: Time Series Analysis (STL, burst, changepoint, forecast).

        Args:
            input_path: Not used for Stage 5.

        Returns:
            Dict with article_count and output_paths.
        """
        from src.analysis.stage5_timeseries import Stage5TimeseriesAnalyzer

        logger.info(
            "stage5_run articles=%s topics=%s analysis=%s output=%s",
            self._processed_dir / "articles.parquet",
            self._analysis_dir / "topics.parquet",
            self._analysis_dir / "article_analysis.parquet",
            self._analysis_dir / "timeseries.parquet",
        )

        analyzer = Stage5TimeseriesAnalyzer()
        try:
            _table = analyzer.run(
                articles_path=self._processed_dir / "articles.parquet",
                topics_path=self._analysis_dir / "topics.parquet",
                analysis_path=self._analysis_dir / "article_analysis.parquet",
                output_path=self._analysis_dir / "timeseries.parquet",
            )
        finally:
            analyzer.cleanup()

        output_path = self._analysis_dir / "timeseries.parquet"
        return {
            "article_count": 0,
            "output_paths": [str(output_path)] if output_path.exists() else [],
        }

    def _build_rolling_window(self) -> dict[str, Path] | None:
        """Materialize the last N days of parquet inputs into a single
        per-kind file under ``data/analysis/{date}/_rolling_{N}d/``.

        This is the core of the Workflow A rolling-window refactor: Stage
        6 (cross analysis) and Stage 7 (signal classification) previously
        loaded only today's ``data/processed/{date}/articles.parquet``,
        which made causal analysis (Granger/PCMCI) and signal classification
        (L1-L5 data_span_days gates) impossible on single-day slices.

        By concatenating the last N days of parquet files into a single
        file per kind, the downstream analyzer code needs no changes — it
        just sees a fatter parquet with ~30 days of history.

        Returns:
            Dict with keys matching Stage 6/7 inputs (articles/topics/
            analysis/networks/embeddings/timeseries) mapping to the
            concatenated parquet paths, or None if rolling window is
            disabled or there are no historical days available.
        """
        if self._rolling_window_days <= 0:
            return None

        import datetime as _dt
        try:
            end = _dt.datetime.strptime(self._date, "%Y-%m-%d").date()
        except Exception:
            return None

        # Enumerate the last N days ending at self._date (inclusive).
        day_strs: list[str] = []
        for i in range(self._rolling_window_days):
            d = end - _dt.timedelta(days=i)
            day_strs.append(d.strftime("%Y-%m-%d"))
        day_strs.sort()  # chronological

        # For each input kind, collect the existing per-day files.
        import pyarrow as pa
        import pyarrow.parquet as pq

        # (input_kind, source_dir_root, filename)
        kinds = [
            ("articles",     self._data_dir / "processed", "articles.parquet"),
            ("topics",       self._data_dir / "analysis",  "topics.parquet"),
            ("analysis",     self._data_dir / "analysis",  "article_analysis.parquet"),
            ("networks",     self._data_dir / "analysis",  "networks.parquet"),
            ("embeddings",   self._data_dir / "features",  "embeddings.parquet"),
            ("timeseries",   self._data_dir / "analysis",  "timeseries.parquet"),
        ]

        self._rolling_dir.mkdir(parents=True, exist_ok=True)
        result: dict[str, Path] = {}
        summary: dict[str, int] = {}

        for kind, root, fname in kinds:
            tables: list[pa.Table] = []
            days_found: list[str] = []
            for day in day_strs:
                p = root / day / fname
                if not p.exists():
                    continue
                try:
                    tables.append(pq.read_table(str(p)))
                    days_found.append(day)
                except Exception as exc:
                    logger.warning(
                        "rolling_window_skip_file",
                        kind=kind, path=str(p), error=str(exc),
                    )
            if not tables:
                summary[kind] = 0
                continue

            # Schema reconciliation: promote to unified schema
            # (pyarrow will fail if schemas mismatch; use promote_options='default'
            # when available, otherwise fall back to permissive concat).
            try:
                merged = pa.concat_tables(tables, promote_options="default")
            except TypeError:
                # pyarrow < 14: older signature
                try:
                    merged = pa.concat_tables(tables, promote=True)
                except Exception as exc:
                    logger.warning(
                        "rolling_window_concat_fallback",
                        kind=kind, error=str(exc), n_files=len(tables),
                    )
                    # Last resort: use the most recent single file
                    merged = tables[-1]
            except Exception as exc:
                logger.warning(
                    "rolling_window_concat_failed",
                    kind=kind, error=str(exc), n_files=len(tables),
                )
                merged = tables[-1]

            out_path = self._rolling_dir / fname
            pq.write_table(merged, str(out_path), compression="zstd")
            result[kind] = out_path
            summary[kind] = merged.num_rows

        logger.info(
            "rolling_window_built",
            window_days=self._rolling_window_days,
            end_date=self._date,
            days_covered=len([d for d in day_strs
                              if (self._data_dir / "processed" / d /
                                  "articles.parquet").exists()]),
            **{f"{k}_rows": v for k, v in summary.items()},
        )

        if not result:
            return None
        return result

    def _run_stage6(self, input_path: str | Path | None = None) -> dict[str, Any]:
        """Run Stage 6: Cross Analysis (Granger, PCMCI, networks, cross-lingual).

        Uses a rolling N-day window (default 30) instead of single-day inputs
        so that causal analysis and cross-source contradiction detection have
        enough history. Falls back to single-day inputs only if rolling window
        is disabled or no historical data is available.

        Args:
            input_path: Not used for Stage 6.

        Returns:
            Dict with article_count and output_paths.
        """
        from src.analysis.stage6_cross_analysis import run_stage6

        rolling = self._build_rolling_window()
        if rolling is not None:
            logger.info(
                "stage6_run mode=rolling window_days=%d",
                self._rolling_window_days,
            )
            articles_path = rolling.get("articles",
                                        self._processed_dir / "articles.parquet")
            topics_path = rolling.get("topics",
                                      self._analysis_dir / "topics.parquet")
            analysis_path = rolling.get("analysis",
                                        self._analysis_dir / "article_analysis.parquet")
            networks_path = rolling.get("networks",
                                        self._analysis_dir / "networks.parquet")
            embeddings_path = rolling.get("embeddings",
                                          self._features_dir / "embeddings.parquet")
            timeseries_path = rolling.get("timeseries",
                                          self._analysis_dir / "timeseries.parquet")
        else:
            logger.info("stage6_run mode=single_day")
            articles_path = self._processed_dir / "articles.parquet"
            topics_path = self._analysis_dir / "topics.parquet"
            analysis_path = self._analysis_dir / "article_analysis.parquet"
            networks_path = self._analysis_dir / "networks.parquet"
            embeddings_path = self._features_dir / "embeddings.parquet"
            timeseries_path = self._analysis_dir / "timeseries.parquet"

        output = run_stage6(
            timeseries_path=timeseries_path,
            topics_path=topics_path,
            analysis_path=analysis_path,
            networks_path=networks_path,
            embeddings_path=embeddings_path,
            articles_path=articles_path,
            output_dir=self._analysis_dir,
            cleanup_after=True,
        )

        output_path = self._analysis_dir / "cross_analysis.parquet"
        return {
            "article_count": output.total_records,
            "output_paths": [str(output_path)] if output_path.exists() else [],
        }

    def _run_stage7(self, input_path: str | Path | None = None) -> dict[str, Any]:
        """Run Stage 7: Signal Classification (5-Layer hierarchy + novelty).

        Uses the rolling window built by Stage 6 so the 5-Layer classifier
        sees multi-day time series (required for L2/L3/L4 data_span_days
        gates) instead of collapsing to 0 signals on single-day slices.

        Args:
            input_path: Not used for Stage 7.

        Returns:
            Dict with article_count and output_paths.
        """
        from src.analysis.stage7_signals import Stage7SignalClassifier

        # Reuse the rolling window directory from Stage 6 if it was built.
        # The rolling dir has the same filenames (topics.parquet,
        # article_analysis.parquet, networks.parquet, timeseries.parquet)
        # as the per-date analysis_dir, so Stage 7 can just point at it.
        analysis_dir_for_s7 = self._analysis_dir
        features_dir_for_s7 = self._features_dir

        if (self._rolling_window_days > 0 and self._rolling_dir.exists()
                and (self._rolling_dir / "topics.parquet").exists()):
            analysis_dir_for_s7 = self._rolling_dir
            # Use rolling embeddings if present
            if (self._rolling_dir / "embeddings.parquet").exists():
                features_dir_for_s7 = self._rolling_dir

            # Stage 7 reads cross_analysis.parquet from its analysis_dir,
            # but Stage 6 writes it to self._analysis_dir (not the rolling
            # subdir). Symlink it into the rolling dir so Stage 7 finds it.
            cross_path_src = self._analysis_dir / "cross_analysis.parquet"
            cross_path_dst = self._rolling_dir / "cross_analysis.parquet"
            if cross_path_src.exists() and not cross_path_dst.exists():
                try:
                    cross_path_dst.symlink_to(cross_path_src)
                except OSError:
                    # Fall back to a copy if symlinks are restricted.
                    import shutil
                    shutil.copy2(cross_path_src, cross_path_dst)

            logger.info(
                "stage7_run mode=rolling dir=%s",
                analysis_dir_for_s7,
            )
        else:
            logger.info(
                "stage7_run mode=single_day analysis_dir=%s features_dir=%s",
                self._analysis_dir, self._features_dir,
            )

        logger.info(
            "stage7_run output_dir=%s",
            self._output_dir,
        )

        classifier = Stage7SignalClassifier()
        try:
            output = classifier.run(
                analysis_dir=analysis_dir_for_s7,
                features_dir=features_dir_for_s7,
                output_dir=self._output_dir,
            )
        finally:
            classifier.cleanup()

        output_path = self._output_dir / "signals.parquet"
        return {
            "article_count": output.n_signals,
            "output_paths": [str(output_path)] if output_path.exists() else [],
        }

    def _run_stage8(self, input_path: str | Path | None = None) -> dict[str, Any]:
        """Run Stage 8: Data Output (Parquet merge, SQLite index, verification).

        Args:
            input_path: Not used for Stage 8.

        Returns:
            Dict with article_count and output_paths.
        """
        from src.analysis.stage8_output import Stage8OutputBuilder

        logger.info(
            "stage8_run processed=%s analysis=%s features=%s output=%s",
            self._processed_dir, self._analysis_dir,
            self._features_dir, self._output_dir,
        )

        builder = Stage8OutputBuilder(
            output_dir=self._output_dir,
            processed_dir=self._processed_dir,
            analysis_dir=self._analysis_dir,
            features_dir=self._features_dir,
        )
        result = builder.run()

        output_paths = []
        for name in ["analysis.parquet", "signals.parquet", "topics.parquet", "index.sqlite"]:
            path = self._output_dir / name
            if path.exists():
                output_paths.append(str(path))

        article_count = 0
        if isinstance(result, dict):
            article_count = result.get("total_articles", 0)

        return {
            "article_count": article_count,
            "output_paths": output_paths,
        }

    # -----------------------------------------------------------------
    # Inter-Stage Cleanup
    # -----------------------------------------------------------------

    def _inter_stage_cleanup(self, completed_stage: int) -> None:
        """Perform memory cleanup between stages.

        Sequence:
        1. Delete any lingering model references (handled by stage cleanup_after)
        2. gc.collect() to reclaim Python objects
        3. torch.cuda.empty_cache() / torch.mps.empty_cache() if available
        4. Log post-cleanup memory

        Args:
            completed_stage: The stage number that just completed.
        """
        logger.info("inter_stage_cleanup after_stage=%s", completed_stage)

        MemoryMonitor.cleanup()

        try:
            self._memory.check_and_log(f"post_cleanup_stage_{completed_stage}")
        except MemoryLimitError:
            # Log but do not abort between stages; the next stage check will handle it
            logger.warning(
                "memory_high_after_cleanup stage=%s", completed_stage,
            )

    # -----------------------------------------------------------------
    # Dependency Checking
    # -----------------------------------------------------------------

    def _check_dependencies(self, stage_num: int) -> list[Path]:
        """Check if all required input files for a stage exist.

        Uses STAGE_DEPENDENCIES to determine which prior-stage outputs
        must be present on disk before starting a given stage.

        When using a custom data_dir, remaps the default constant paths
        to the custom directory structure.

        Args:
            stage_num: Stage number to check dependencies for.

        Returns:
            List of missing file paths. Empty list means all dependencies met.
        """
        required = STAGE_DEPENDENCIES.get(stage_num, [])
        missing: list[Path] = []

        for dep_path in required:
            # Remap the path from default DATA_DIR to self._data_dir
            actual_path = self._remap_path(dep_path)
            if not actual_path.exists():
                missing.append(actual_path)

        return missing

    def _remap_path(self, default_path: Path) -> Path:
        """Remap a default constant path to the configured data directory.

        For example, if default is /project/data/processed/articles.parquet
        and self._data_dir is /tmp/test/data, returns
        /tmp/test/data/processed/articles.parquet.

        Args:
            default_path: Path using default DATA_DIR from constants.

        Returns:
            Remapped path under self._data_dir.
        """
        try:
            relative = default_path.relative_to(DATA_DIR)
            parts = relative.parts
            # Insert date subdirectory after the category (processed/features/analysis/output)
            date_partitioned = {"processed", "features", "analysis", "output"}
            if len(parts) >= 2 and parts[0] in date_partitioned:
                return self._data_dir / parts[0] / self._date / Path(*parts[1:])
            return self._data_dir / relative
        except ValueError:
            # Path is not under DATA_DIR; return as-is
            return default_path

    # -----------------------------------------------------------------
    # Directory Setup
    # -----------------------------------------------------------------

    def _ensure_directories(self) -> None:
        """Create all required output directories if they do not exist."""
        for d in [
            self._processed_dir,
            self._features_dir,
            self._analysis_dir,
            self._output_dir,
        ]:
            d.mkdir(parents=True, exist_ok=True)

    # -----------------------------------------------------------------
    # Final Output Collection
    # -----------------------------------------------------------------

    def _collect_final_outputs(self) -> dict[str, str]:
        """Collect paths of final output files.

        Returns:
            Dict mapping output name to file path (only for files that exist).
        """
        outputs: dict[str, str] = {}

        candidates = {
            "analysis.parquet": self._output_dir / "analysis.parquet",
            "signals.parquet": self._output_dir / "signals.parquet",
            "topics.parquet": self._output_dir / "topics.parquet",
            "index.sqlite": self._output_dir / "index.sqlite",
        }

        for name, path in candidates.items():
            if path.exists():
                outputs[name] = str(path)

        return outputs


# =============================================================================
# Convenience Function
# =============================================================================

def run_analysis_pipeline(
    data_dir: str | Path | None = None,
    date: str | None = None,
    stages: list[int] | None = None,
    input_path: str | Path | None = None,
    memory_abort_gb: float = MEMORY_ABORT_THRESHOLD_GB,
    memory_warning_gb: float = MEMORY_WARNING_THRESHOLD_GB,
) -> AnalysisPipelineResult:
    """Convenience function to run the analysis pipeline.

    Called from ``main.py cmd_analyze()``.

    Args:
        data_dir: Root data directory. Defaults to ``data/``.
        date: Target date (YYYY-MM-DD). Defaults to today.
        stages: Specific stages to run. Defaults to all (1-8).
        input_path: Override JSONL input path for Stage 1.
        memory_abort_gb: RSS threshold to abort (GB).
        memory_warning_gb: RSS threshold to warn (GB).

    Returns:
        AnalysisPipelineResult with complete pipeline results.
    """
    pipeline = AnalysisPipeline(
        data_dir=data_dir,
        date=date,
        memory_abort_gb=memory_abort_gb,
        memory_warning_gb=memory_warning_gb,
    )
    return pipeline.run(stages=stages, input_path=input_path)
