"""Crawling Pipeline Orchestrator: end-to-end crawl coordination with 4-level retry.

Integrates all crawling subsystems into a unified pipeline:
    - sources.yaml loading and site iteration
    - Per-site adapter selection via ``get_adapter(site_id)``
    - Circuit breaker checks before site processing
    - 3-Tier URL Discovery (RSS -> Sitemap -> DOM)
    - Article extraction via ArticleExtractor + site adapters
    - 3-level deduplication (URL + Title + SimHash)
    - JSONL output to ``data/raw/YYYY-MM-DD/all_articles.jsonl``
    - 4-level retry system (NetworkGuard x Strategy x Round x Restart)
    - Structured crawl report generation

Pipeline stages (per site):
    1. Load     -- Read site adapter config, init NetworkGuard per site
    2. Iterate  -- Loop through target sites
    3. Select   -- Choose adapter via get_adapter(site_id)
    4. Discover -- Run URL discovery (RSS -> Sitemap -> DOM fallback)
    5. Extract  -- Fetch and extract article content
    6. Dedup    -- Apply URL + content-hash dedup
    7. JSONL    -- Write validated articles to consolidated output

CLI: ``python3 main.py --mode crawl --date 2026-02-25 [--sites chosun,donga] [--groups A,B]``

Reference:
    Step 5 Architecture Blueprint, Section 6 (Pipeline Orchestration).
    Step 3 Crawling Feasibility (4-Level Retry Architecture).
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.config.constants import (
    CRAWL_LOOKBACK_HOURS,
    CRAWL_NEVER_ABANDON,
    DATA_RAW_DIR,
    MAX_ARTICLES_PER_SITE_PER_DAY,
    DEFAULT_RATE_LIMIT_SECONDS,
    ENABLED_DEFAULT,
    DISCOVERY_BYPASS_MAX_ATTEMPTS,
    BYPASS_STATE_PATH,
)
from src.crawling.contracts import RawArticle, CrawlResult, DiscoveredURL, compute_content_hash
from src.crawling.network_guard import NetworkGuard
from src.crawling.url_discovery import URLDiscovery
from src.crawling.article_extractor import ArticleExtractor
from src.crawling.browser_renderer import BrowserRenderer
from src.crawling.adaptive_extractor import AdaptiveExtractor
from src.crawling.crawler import JSONLWriter, CrawlState
from src.crawling.dedup import DedupEngine
from src.crawling.ua_manager import UAManager
from src.crawling.circuit_breaker import CircuitBreakerCoordinator
from src.crawling.anti_block import AntiBlockEngine
from src.crawling.dynamic_bypass import DynamicBypassEngine
from src.crawling.retry_manager import (
    RetryManager,
    StrategyMode,
    L3_MAX_ROUNDS,
    L4_MAX_RESTARTS,
)
from src.crawling.crawl_report import (
    generate_crawl_report,
    print_crawl_summary,
)
from src.utils.config_loader import (
    load_sources_config,
    get_enabled_sites,
    get_site_config,
    get_sites_by_group,
)
from src.utils.error_handler import (
    CrawlError,
    NetworkError,
    ParseError,
    BlockDetectedError,
    RateLimitError,
    CircuitState,
)

logger = logging.getLogger(__name__)

# RSS Content Extraction: minimum body length to skip HTTP fetch
# Cross-ref: url_discovery.py _MIN_RSS_BODY_HINT (same 200-char threshold)
_MIN_RSS_BODY_FOR_EXTRACTION = 200


# ---------------------------------------------------------------------------
# Pipeline Configuration
# ---------------------------------------------------------------------------

DEFAULT_CONCURRENCY = 5  # Max concurrent sites in ThreadPoolExecutor
TOTALWAR_DELAY_MULTIPLIER = 2.0  # Multiply rate limit in TotalWar mode
PER_SITE_TIMEOUT_SECONDS = 300.0  # 5 min cooperative deadline per site


# ---------------------------------------------------------------------------
# Cooperative Deadline — enables per-site timeout without killing threads.
# Threads check deadline.expired at each URL iteration boundary and exit
# cleanly, preserving partial results in CrawlState for the next round.
# ---------------------------------------------------------------------------

@dataclass
class SiteDeadline:
    """Cooperative fairness deadline for per-site crawling.

    The deadline ensures one site does not monopolize a worker thread.
    When expired, the current round exits cleanly — partial results are
    preserved in CrawlState. The site is NOT abandoned; it will be
    re-queued in the next pass with a fresh deadline.

    CRAWL_NEVER_ABANDON: deadline causes "yield to other sites", not "give up".
    """

    _deadline: float
    _timeout_seconds: float

    @staticmethod
    def create(timeout_seconds: float) -> SiteDeadline:
        """Create a deadline from now + timeout_seconds."""
        if timeout_seconds <= 0:
            raise ValueError(
                f"timeout_seconds must be positive, got {timeout_seconds}"
            )
        return SiteDeadline(
            _deadline=time.monotonic() + timeout_seconds,
            _timeout_seconds=timeout_seconds,
        )

    @property
    def expired(self) -> bool:
        """Check if the deadline has passed."""
        return time.monotonic() > self._deadline

    @property
    def remaining(self) -> float:
        """Seconds remaining until deadline (0 if expired)."""
        return max(0.0, self._deadline - time.monotonic())


# ---------------------------------------------------------------------------
# Crawling Pipeline
# ---------------------------------------------------------------------------

class CrawlingPipeline:
    """End-to-end crawling pipeline orchestrator with 4-level retry.

    Coordinates all crawling subsystems to crawl configured news sites,
    extract articles, deduplicate, and produce consolidated JSONL output.

    The pipeline supports:
    - Selective crawling by site IDs or group letters
    - Resume-on-restart via CrawlState persistence
    - 4-level retry with Standard/TotalWar strategy escalation
    - Per-site circuit breakers and anti-block escalation
    - Consolidated output: all articles in one JSONL file per day
    - Structured crawl report generation

    Args:
        crawl_date: Target date string (YYYY-MM-DD).
        output_dir: Base output directory (default: data/raw/).
        sites_filter: Optional list of specific site IDs to crawl.
        groups_filter: Optional list of group letters to crawl.
        max_articles_per_site: Cap on articles per site.
        dry_run: If True, validate config without making requests.
    """

    def __init__(
        self,
        crawl_date: str | None = None,
        output_dir: Path | None = None,
        sites_filter: list[str] | None = None,
        groups_filter: list[str] | None = None,
        max_articles_per_site: int = MAX_ARTICLES_PER_SITE_PER_DAY,
        dry_run: bool = False,
    ) -> None:
        self._date = crawl_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self._output_base = output_dir or DATA_RAW_DIR
        self._output_dir = self._output_base / self._date
        self._sites_filter = sites_filter
        self._groups_filter = groups_filter
        self._max_articles = max_articles_per_site
        self._dry_run = dry_run

        # Core subsystems -- initialized lazily in run()
        self._guard: NetworkGuard | None = None
        self._url_discovery: URLDiscovery | None = None
        self._extractor: ArticleExtractor | None = None
        self._dedup: DedupEngine | None = None
        self._ua_manager: UAManager | None = None
        self._circuit_breakers: CircuitBreakerCoordinator | None = None
        self._anti_block: AntiBlockEngine | None = None
        self._retry_manager: RetryManager | None = None
        self._crawl_state: CrawlState | None = None

        # Results tracking
        self._results: list[CrawlResult] = []
        self._pipeline_start_time: float = 0.0

        # 24-hour lookback cutoff: articles with published_at before this
        # timestamp are dropped after extraction (absolute rule for daily runs).
        self._crawl_start_utc: datetime = datetime.now(timezone.utc)
        self._lookback_cutoff: datetime = (
            self._crawl_start_utc - timedelta(hours=CRAWL_LOOKBACK_HOURS)
        )

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def run(self) -> dict[str, Any]:
        """Execute the full crawling pipeline.

        This is the main entry point. It:
        1. Loads sources.yaml and determines target sites
        2. Initializes all subsystems
        3. Iterates through sites with 4-level retry
        4. Writes consolidated JSONL output
        5. Generates and returns the crawl report

        Returns:
            Crawl report dictionary (also written to disk).
        """
        self._pipeline_start_time = time.monotonic()
        # Refresh the 24h lookback cutoff at actual run time
        self._crawl_start_utc = datetime.now(timezone.utc)
        self._lookback_cutoff = (
            self._crawl_start_utc - timedelta(hours=CRAWL_LOOKBACK_HOURS)
        )

        logger.info(
            "pipeline_start date=%s sites_filter=%s groups_filter=%s dry_run=%s "
            "lookback_cutoff=%s",
            self._date,
            self._sites_filter or "all",
            self._groups_filter or "all",
            self._dry_run,
            self._lookback_cutoff.isoformat(),
        )

        # Step 1: Load configuration and determine target sites
        sources_config = load_sources_config(validate=False)
        target_sites = self._resolve_target_sites(sources_config)

        if not target_sites:
            logger.warning("no_target_sites No sites matched the filter criteria.")
            return generate_crawl_report(
                results=[],
                crawl_date=self._date,
                elapsed_seconds=0.0,
                output_dir=self._output_dir,
            )

        logger.info("target_sites count=%s sites=%s", len(target_sites), list(target_sites.keys()))

        if self._dry_run:
            return self._run_dry(target_sites)

        # Step 2: Initialize subsystems
        self._init_subsystems(sources_config)

        # Step 3: Run the crawl pipeline with Level 4 restart support
        self._results = self._run_with_restarts(target_sites)

        # Step 4: Generate report
        elapsed = time.monotonic() - self._pipeline_start_time
        retry_stats = (
            self._retry_manager.get_retry_stats().get("total_retry_counts", {})
            if self._retry_manager
            else {}
        )

        report = generate_crawl_report(
            results=self._results,
            crawl_date=self._date,
            elapsed_seconds=elapsed,
            retry_stats=retry_stats,
            output_dir=self._output_dir,
        )

        # Print summary to console
        print_crawl_summary(report)

        logger.info(
            "pipeline_complete date=%s articles=%s elapsed=%s",
            self._date, report["total_articles"], round(elapsed, 1),
        )

        return report

    def close(self) -> None:
        """Release all resources held by the pipeline."""
        if self._guard is not None:
            self._guard.close()
        if self._dedup is not None:
            self._dedup.close()

    def __enter__(self) -> CrawlingPipeline:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    # -----------------------------------------------------------------------
    # Initialization
    # -----------------------------------------------------------------------

    def _init_subsystems(self, sources_config: dict[str, Any]) -> None:
        """Initialize all crawling subsystems.

        Args:
            sources_config: Loaded sources.yaml configuration.
        """
        # NetworkGuard -- shared HTTP client
        self._guard = NetworkGuard()

        # URL Discovery
        self._url_discovery = URLDiscovery(self._guard)

        # Browser Renderer (optional — for paywall/JS sites)
        self._browser_renderer: BrowserRenderer | None = None
        try:
            renderer = BrowserRenderer()
            if renderer.is_available():
                self._browser_renderer = renderer
                logger.info("browser_renderer_available engine=patchright/playwright")
            else:
                logger.info("browser_renderer_unavailable — paywall sites will use title-only")
        except Exception as e:
            logger.debug("browser_renderer_init_error error=%s", str(e))

        # Adaptive Extractor (CSS selector fallback for rendered HTML)
        self._adaptive_extractor = AdaptiveExtractor()

        # Article Extractor
        self._extractor = ArticleExtractor(
            self._guard,
            browser_renderer=self._browser_renderer,
            adaptive_extractor=self._adaptive_extractor,
        )

        # Deduplication engine (SQLite-backed)
        self._dedup = DedupEngine()

        # User-Agent manager
        self._ua_manager = UAManager(sources_config=sources_config)

        # Circuit breaker coordinator
        self._circuit_breakers = CircuitBreakerCoordinator()

        # Anti-block engine
        self._anti_block = AntiBlockEngine(auto_load=True)

        # 4-Level retry manager
        self._retry_manager = RetryManager(crawl_date=self._date)

        # Dynamic Bypass Engine (block-type-aware strategy dispatch)
        # Used by the Never-Abandon loop for targeted bypass selection
        self._bypass_engine = DynamicBypassEngine(
            proxy_pool=[],  # Populated from sources.yaml proxy config if available
            enable_browser=self._browser_renderer is not None,
        )

        # Bypass learning state — cross-crawl persistence SOT
        self._bypass_state: dict[str, Any] = self._load_bypass_state()

        # Crawl state (resume support)
        self._crawl_state = CrawlState(self._output_dir)

        logger.info("subsystems_initialized")

    def _resolve_target_sites(
        self,
        sources_config: dict[str, Any],
    ) -> dict[str, dict[str, Any]]:
        """Determine which sites to crawl based on filters.

        Opt-out pattern: sites are included by default unless explicitly
        disabled with ``meta.enabled: false``.  This matches the project
        directive that full-site crawling is the default mode.

        Priority:
        1. --sites filter: explicit site IDs (no enabled check)
        2. --groups filter: non-disabled sites in specified groups
        3. Default: all non-disabled sites

        D-7: ``meta.enabled`` default mirrors ``_SOURCE_DEFAULTS`` in
        ``config_loader.py`` (both default to ``True``).

        Args:
            sources_config: Loaded sources.yaml configuration.

        Returns:
            Dictionary mapping site_id -> site_config for target sites.
        """
        all_sources = sources_config.get("sources", {})
        target: dict[str, dict[str, Any]] = {}

        if self._sites_filter:
            # Explicit site IDs
            for site_id in self._sites_filter:
                if site_id in all_sources:
                    target[site_id] = all_sources[site_id]
                else:
                    logger.warning(
                        "site_not_found site_id=%s available=%s",
                        site_id, sorted(all_sources.keys())[:10],
                    )
        elif self._groups_filter:
            # Sites in specified groups — D-7 (13): ENABLED_DEFAULT from constants.py (SOT)
            for site_id, site_cfg in all_sources.items():
                if site_cfg.get("group") in self._groups_filter:
                    if site_cfg.get("meta", {}).get("enabled", ENABLED_DEFAULT):
                        target[site_id] = site_cfg
        else:
            # All non-disabled sites — D-7 (13): ENABLED_DEFAULT from constants.py (SOT)
            for site_id, site_cfg in all_sources.items():
                if site_cfg.get("meta", {}).get("enabled", ENABLED_DEFAULT):
                    target[site_id] = site_cfg

        return target

    # -----------------------------------------------------------------------
    # Dry Run
    # -----------------------------------------------------------------------

    def _run_dry(self, target_sites: dict[str, dict[str, Any]]) -> dict[str, Any]:
        """Execute a dry run -- validate config and show plan.

        Args:
            target_sites: Sites that would be crawled.

        Returns:
            Dry-run report.
        """
        logger.info("DRY RUN: Validating configuration for %s sites", len(target_sites))

        for site_id, site_cfg in sorted(target_sites.items()):
            method = site_cfg.get("crawl", {}).get("primary_method", "?")
            group = site_cfg.get("group", "?")
            estimate = site_cfg.get("meta", {}).get("daily_article_estimate", 0)
            difficulty = site_cfg.get("meta", {}).get("difficulty_tier", "?")
            logger.info(
                "  %s: %s (Group %s, %s, ~%s articles/day)",
                site_id, method, group, difficulty, estimate,
            )

        total_estimate = sum(
            s.get("meta", {}).get("daily_article_estimate", 0)
            for s in target_sites.values()
        )
        logger.info("DRY RUN complete. Would crawl ~%s articles from %s sites.",
                     total_estimate, len(target_sites))

        return {
            "date": self._date,
            "dry_run": True,
            "total_sites": len(target_sites),
            "estimated_articles": total_estimate,
            "sites": list(target_sites.keys()),
        }

    # -----------------------------------------------------------------------
    # Pipeline Execution (with Level 4 restarts)
    # -----------------------------------------------------------------------

    def _run_with_restarts(
        self,
        target_sites: dict[str, dict[str, Any]],
    ) -> list[CrawlResult]:
        """Run the pipeline with Level 4 restart support.

        On catastrophic failure (network outage, disk full), restarts the
        entire pipeline. Already-collected articles are preserved via
        CrawlState deduplication.

        Args:
            target_sites: Sites to crawl.

        Returns:
            Aggregated CrawlResult list from all restarts.
        """
        all_results: dict[str, CrawlResult] = {}

        for restart in range(1, L4_MAX_RESTARTS + 1):
            logger.info(
                "pipeline_run restart=%s/%s sites=%s",
                restart, L4_MAX_RESTARTS, len(target_sites),
            )

            try:
                results = self._run_single_pass(target_sites, restart)

                # Merge results — P1: completed result replaces yielded result
                for result in results:
                    existing = all_results.get(result.source_id)
                    if existing is None:
                        all_results[result.source_id] = result
                    elif existing.deadline_yielded and not result.deadline_yielded:
                        # Previously yielded, now completed → replace
                        all_results[result.source_id] = result
                    elif result.extracted_count > existing.extracted_count:
                        all_results[result.source_id] = result

                # Check if any sites need re-processing
                sites_needing_restart = self._check_restart_needed(target_sites, all_results)
                if not sites_needing_restart:
                    logger.info("pipeline_all_sites_complete restart=%s", restart)
                    break

                if restart < L4_MAX_RESTARTS:
                    # Apply restart delay
                    assert self._retry_manager is not None
                    for site_id in sites_needing_restart:
                        if self._retry_manager.should_restart_pipeline(site_id):
                            delay = self._retry_manager.restart_pipeline(site_id)
                            logger.info(
                                "pipeline_restart_delay site_id=%s delay=%ss",
                                site_id, delay,
                            )
                            time.sleep(min(delay, 60.0))
                            break  # One delay per restart cycle
                else:
                    # Final restart exhausted -- escalate remaining failures
                    self._escalate_remaining_failures(sites_needing_restart)

            except KeyboardInterrupt:
                logger.warning("pipeline_interrupted restart=%s", restart)
                raise
            except Exception as e:
                logger.error(
                    "pipeline_restart_error restart=%s error=%s error_type=%s",
                    restart, str(e), type(e).__name__,
                )
                if restart >= L4_MAX_RESTARTS:
                    raise

        # CRAWL_NEVER_ABANDON: after L4 restarts + Never-Abandon loop,
        # re-check for incomplete sites and run additional passes.
        # Sites that yielded their deadline get fresh passes with new deadlines.
        # Hard cap: MULTI_PASS_MAX_EXTRA passes to prevent infinite loops.
        from src.config.constants import MULTI_PASS_MAX_EXTRA
        max_extra_passes = MULTI_PASS_MAX_EXTRA  # 10
        pass_number = L4_MAX_RESTARTS
        for _ in range(max_extra_passes):
            incomplete = self._get_incomplete_sites(target_sites, all_results)
            if not incomplete:
                logger.info(
                    "crawl_never_abandon_all_complete total_passes=%s",
                    pass_number,
                )
                break

            pass_number += 1
            logger.info(
                "crawl_never_abandon_pass pass=%s incomplete_sites=%s",
                pass_number, len(incomplete),
            )

            # Build a filtered target_sites dict for incomplete sites only
            incomplete_targets = {
                sid: target_sites[sid] for sid in incomplete
                if sid in target_sites
            }

            try:
                results = self._run_single_pass(incomplete_targets, pass_number)
                # P1: completed result replaces yielded result (same as L4 merge)
                for result in results:
                    existing = all_results.get(result.source_id)
                    if existing is None:
                        all_results[result.source_id] = result
                    elif existing.deadline_yielded and not result.deadline_yielded:
                        all_results[result.source_id] = result
                    elif result.extracted_count > existing.extracted_count:
                        all_results[result.source_id] = result
            except KeyboardInterrupt:
                logger.warning("pipeline_interrupted pass=%s", pass_number)
                raise
            except Exception as e:
                logger.error(
                    "crawl_never_abandon_pass_error pass=%s error=%s",
                    pass_number, str(e)[:200],
                )
                # Backoff before next attempt
                time.sleep(min(pass_number * 30, 300))
        else:
            # Max passes exhausted — generate failure report for remaining sites
            final_incomplete = self._get_incomplete_sites(target_sites, all_results)
            if final_incomplete:
                self._generate_failure_report(final_incomplete, target_sites, all_results)

        return list(all_results.values())

    def _get_incomplete_sites(
        self,
        target_sites: dict[str, dict[str, Any]],
        results: dict[str, CrawlResult],
    ) -> list[str]:
        """Return site IDs that have not completed crawling.

        P1 hallucination prevention: CrawlState is the authoritative
        completion source. mark_site_complete() is only called when
        extracted_count > 0 AND deadline_yielded=False, so CrawlState
        cannot contain false completions.

        Check order (critical — prevents infinite loop):
        1. CrawlState.is_site_complete() → FIRST (authoritative)
        2. deadline_yielded → only if CrawlState says NOT complete
        3. Error-based heuristics → fallback
        """
        assert self._crawl_state is not None
        incomplete: list[str] = []

        for site_id in target_sites:
            # CrawlState is authoritative — if it says complete, the site
            # ran to completion in some pass (no yield). Old results in
            # all_results may have stale deadline_yielded=True from
            # earlier passes, but CrawlState is more recent and reliable.
            if self._crawl_state.is_site_complete(site_id):
                continue

            result = results.get(site_id)

            # P1: deadline_yielded means partial crawl — must resume
            if result is not None and result.deadline_yielded:
                incomplete.append(site_id)
                continue

            # No result at all
            if result is None:
                incomplete.append(site_id)
            elif result.extracted_count == 0 and result.errors:
                # Had errors but no articles — needs retry
                incomplete.append(site_id)

        return incomplete

    def _check_restart_needed(
        self,
        target_sites: dict[str, dict[str, Any]],
        results: dict[str, CrawlResult],
    ) -> list[str]:
        """Check which sites need re-processing.

        A site needs restart if:
        - It was targeted but has no result
        - It has 0 articles AND errors
        - Its retry manager says restart is needed

        Args:
            target_sites: All target sites.
            results: Current results per site.

        Returns:
            List of site IDs that need restart.
        """
        needs_restart: list[str] = []

        for site_id in target_sites:
            result = results.get(site_id)
            if result is None:
                needs_restart.append(site_id)
                continue

            if result.extracted_count == 0 and result.errors:
                assert self._retry_manager is not None
                retry_state = self._retry_manager.get_state(site_id)
                if not retry_state.exhausted:
                    needs_restart.append(site_id)

        return needs_restart

    def _escalate_remaining_failures(self, site_ids: list[str]) -> None:
        """Escalate remaining failed sites to Never-Abandon persistence loop.

        크롤링 절대 원칙: NEVER abandon. After standard 90 attempts exhaust,
        activate the Never-Abandon loop — cycle through alternative source
        strategies (RSS, cache, AMP, etc.) with exponential backoff.

        Args:
            site_ids: Sites that failed all retry levels.
        """
        assert self._retry_manager is not None
        for site_id in site_ids:
            if self._retry_manager.is_exhausted(site_id):
                report_path = self._retry_manager.escalate_tier6(site_id)
                logger.warning(
                    "never_abandon_loop_start site_id=%s report=%s",
                    site_id, str(report_path),
                )

        # Never-Abandon persistence loop for ALL escalated sites
        if not CRAWL_NEVER_ABANDON:
            return

        never_abandon_sites = [
            sid for sid in site_ids
            if self._retry_manager.get_state(sid).never_abandon_active
        ]

        if not never_abandon_sites:
            return

        logger.warning(
            "never_abandon_loop_entering sites=%s",
            [s for s in never_abandon_sites],
        )

        for site_id in never_abandon_sites:
            self._run_never_abandon_loop(site_id)

    def _run_never_abandon_loop(self, site_id: str) -> None:
        """Run the Never-Abandon persistence loop for a single site.

        Uses the DynamicBypassEngine to select targeted bypass strategies
        based on the detected block type. Each cycle tries a different
        strategy from the engine's recommendation, with exponential
        backoff between cycles.

        Strategy selection flow:
        1. Get the last known block type for this site (from BlockDetector)
        2. Ask DynamicBypassEngine for strategies effective against that block
        3. Try strategies in order (cheapest first, learned success rate)
        4. On success → done. On failure → advance cycle, try next strategy.

        Args:
            site_id: Site identifier.
        """
        assert self._retry_manager is not None
        assert self._circuit_breakers is not None

        state = self._retry_manager.get_state(site_id)

        # Get last known block type from anti-block engine's profile
        last_block_type = None
        if self._anti_block is not None:
            profile = self._anti_block.get_profile(site_id)
            if profile.last_block_type:
                from src.crawling.block_detector import BlockType
                try:
                    last_block_type = BlockType(profile.last_block_type)
                except ValueError:
                    pass

        while self._retry_manager.advance_never_abandon_cycle(site_id):
            # strategy_name is for logging only — actual strategy selection
            # is handled by DynamicBypassEngine.try_strategies() in Phase A.
            strategy_name, delay = self._retry_manager.get_never_abandon_strategy(site_id)

            logger.warning(
                "never_abandon_cycle site_id=%s cycle=%s base_strategy=%s "
                "block_type=%s delay=%.1fs",
                site_id, state.never_abandon_cycle, strategy_name,
                last_block_type.value if last_block_type else "unknown", delay,
            )

            # Wait with exponential backoff
            if delay > 0:
                time.sleep(min(delay, 120.0))  # Cap actual sleep for usability

            # Reset BOTH circuit breakers for fresh attempt:
            # 1. Pipeline-level CircuitBreakerCoordinator
            # 2. NetworkGuard's internal per-site circuit breaker
            # BUG FIX: Previously only the coordinator was reset, leaving
            # NetworkGuard's CB in OPEN state. This caused Phase B
            # (_crawl_site_with_retry) to fail immediately on every cycle
            # because NetworkGuard.fetch() raises NetworkError when its
            # own CB is OPEN, independent of the coordinator.
            self._circuit_breakers.reset(site_id)
            assert self._guard is not None
            ng_cb = self._guard._circuit_breakers.get(site_id)
            if ng_cb is not None:
                ng_cb.reset()

            # Phase A: Try DynamicBypassEngine strategies on failed URLs
            # Uses try_all_strategies() (no cap) with start_offset to
            # rotate strategy order across cycles. Each cycle starts from
            # a different strategy instead of always trying curl_cffi first.
            if state.failed_urls and self._bypass_engine is not None:
                bypass_success = False
                recovered_urls: list[str] = []
                output_path = self._output_dir / "all_articles.jsonl"
                try:
                    with JSONLWriter(output_path) as writer:
                        for failed_url in sorted(state.failed_urls):
                            result = self._bypass_engine.try_all_strategies(
                                url=failed_url,
                                block_type=last_block_type,
                                site_id=site_id,
                                timeout=30.0,
                                start_offset=state.never_abandon_strategy_idx,
                            )
                            if result.success:
                                bypass_success = True
                                logger.info(
                                    "never_abandon_bypass_SUCCESS site_id=%s url=%s "
                                    "strategy=%s tier=%s latency=%.0fms",
                                    site_id, failed_url[:80],
                                    result.strategy_name, result.strategy_tier,
                                    result.latency_ms,
                                )
                                self._write_bypass_result(
                                    site_id, failed_url, result.html, writer,
                                )
                                recovered_urls.append(failed_url)
                            else:
                                # Update block type cache if detection changed
                                if result.block_detected:
                                    last_block_type = result.block_detected
                                    self._bypass_engine.update_block_cache(
                                        site_id, result.block_detected,
                                    )
                except Exception as e:
                    logger.warning(
                        "never_abandon_write_error site_id=%s error=%s",
                        site_id, str(e)[:100],
                    )

                for url in recovered_urls:
                    state.failed_urls.discard(url)
                    state.successful_urls.add(url)

                if bypass_success and not state.failed_urls:
                    logger.info(
                        "never_abandon_ALL_SUCCESS site_id=%s cycle=%s "
                        "— all URLs recovered via DynamicBypassEngine",
                        site_id, state.never_abandon_cycle,
                    )
                    state.never_abandon_active = False
                    return  # SUCCESS — mission accomplished

                if bypass_success:
                    logger.info(
                        "never_abandon_partial_success site_id=%s "
                        "remaining_failed=%s",
                        site_id, len(state.failed_urls),
                    )

            # Phase B: Fallback — full pipeline re-crawl with TotalWar
            # Reset BOTH circuit breakers again before Phase B. Phase A
            # failures may have re-tripped the coordinator and NetworkGuard
            # CBs. Without this reset, _crawl_site_with_retry immediately
            # fails because NetworkGuard.fetch() blocks on its OPEN CB.
            self._circuit_breakers.reset(site_id)
            ng_cb_b = self._guard._circuit_breakers.get(site_id)
            if ng_cb_b is not None:
                ng_cb_b.reset()

            state.current_strategy = StrategyMode.TOTALWAR
            state.current_round = 1
            state.exhausted = False

            if state.failed_urls:
                state.pending_urls = sorted(state.failed_urls)
                state.failed_urls = set()

            try:
                site_cfg = self._get_site_config(site_id)
                if site_cfg is None:
                    logger.error("never_abandon_no_config site_id=%s", site_id)
                    break

                output_path = self._output_dir / "all_articles.jsonl"
                with JSONLWriter(output_path, append=True) as writer:
                    result = self._crawl_site_with_retry(site_id, site_cfg, writer)

                if result.extracted_count > 0:
                    logger.info(
                        "never_abandon_SUCCESS site_id=%s cycle=%s "
                        "strategy=totalwar_fallback articles=%s",
                        site_id, state.never_abandon_cycle,
                        result.extracted_count,
                    )
                    state.never_abandon_active = False
                    return  # SUCCESS — mission accomplished
                else:
                    logger.warning(
                        "never_abandon_cycle_failed site_id=%s cycle=%s",
                        site_id, state.never_abandon_cycle,
                    )

            except KeyboardInterrupt:
                raise
            except Exception as e:
                logger.error(
                    "never_abandon_cycle_error site_id=%s cycle=%s error=%s",
                    site_id, state.never_abandon_cycle, str(e)[:200],
                )

        # Safety cap reached — log final status but DO NOT mark as complete.
        # The site will be retried on the next daily run.
        # Also log DynamicBypassEngine stats for post-mortem analysis.
        if self._bypass_engine is not None:
            stats = self._bypass_engine.get_domain_stats(site_id)
            logger.error(
                "never_abandon_safety_cap_reached site_id=%s total_cycles=%s "
                "bypass_stats=%s — site will be retried on next daily run",
                site_id, state.never_abandon_cycle, stats,
            )
        else:
            logger.error(
                "never_abandon_safety_cap_reached site_id=%s total_cycles=%s "
                "— site will be retried on next daily run",
                site_id, state.never_abandon_cycle,
            )

    def _write_bypass_result(
        self,
        site_id: str,
        url: str,
        html: str,
        writer: JSONLWriter,
    ) -> None:
        """Extract article from bypassed HTML and write to JSONL.

        Applies the same validation pipeline as the normal crawl path:
        freshness check (24h lookback) and 3-level dedup (URL/Title/SimHash).

        Args:
            site_id: Site identifier.
            url: Original article URL.
            html: HTML content fetched via bypass strategy.
            writer: JSONL writer instance.
        """
        assert self._extractor is not None

        try:
            site_cfg = self._get_site_config(site_id) or {}
            article = self._extractor.extract(
                url=url,
                source_id=site_id,
                site_config=site_cfg,
                html=html,
            )
            if article is None:
                return

            # Freshness check — same 24h lookback as normal path
            if article.published_at is not None:
                pub_dt = article.published_at
                if pub_dt.tzinfo is None:
                    pub_dt = pub_dt.replace(tzinfo=timezone.utc)
                if pub_dt < self._lookback_cutoff:
                    logger.debug(
                        "bypass_article_outside_24h site_id=%s url=%s published_at=%s",
                        site_id, url[:80], pub_dt.isoformat(),
                    )
                    return

            # 3-level dedup check — same cascade as normal path
            if self._dedup is not None:
                dedup_result = self._dedup.is_duplicate(
                    url=article.url,
                    title=article.title,
                    body=article.body,
                    source_id=site_id,
                    article_id=str(uuid.uuid4()),
                )
                if dedup_result.is_duplicate:
                    logger.debug(
                        "bypass_article_deduped site_id=%s url=%s level=%s reason=%s",
                        site_id, url[:80], dedup_result.level, dedup_result.reason[:80],
                    )
                    return

            writer.write_article(article)
            logger.debug(
                "bypass_article_written site_id=%s url=%s title=%s",
                site_id, url[:80], (article.title or "")[:60],
            )
        except Exception as e:
            logger.warning(
                "bypass_extraction_error site_id=%s url=%s error=%s",
                site_id, url[:80], str(e)[:100],
            )

    def _get_site_config(self, site_id: str) -> dict[str, Any] | None:
        """Get site configuration by site_id.

        Args:
            site_id: Site identifier.

        Returns:
            Site config dict, or None if not found.
        """
        try:
            return get_site_config(site_id)
        except KeyError:
            return None

    def _create_article_from_rss(
        self,
        url_obj: DiscoveredURL,
        site_id: str,
        site_cfg: dict[str, Any],
        full_body: bool = False,
    ) -> RawArticle | None:
        """Create a RawArticle from RSS metadata.

        Unified method replacing duplicated fallback logic. When full_body=True
        and body_hint is available, creates a complete article without HTTP
        fetch. When full_body=False, creates title-only (paywall-truncated).

        Args:
            url_obj: Discovered URL with RSS metadata (title_hint, body_hint).
            site_id: Site identifier.
            site_cfg: Site config from sources.yaml.
            full_body: If True, use body_hint for full content extraction.

        Returns:
            RawArticle, or None if title missing or freshness filter fails.
        """
        if not url_obj.title_hint:
            return None

        source_name = site_cfg.get("name", site_id)
        language = site_cfg.get("language", "en")
        published_at = url_obj.published_at
        if published_at is not None:
            if published_at.tzinfo is None:
                published_at = published_at.replace(tzinfo=timezone.utc)
            if published_at < self._lookback_cutoff:
                return None

        title = url_obj.title_hint.strip()
        body = ""
        is_truncated = True
        crawl_method = "rss"

        if full_body and url_obj.body_hint and len(url_obj.body_hint) >= _MIN_RSS_BODY_FOR_EXTRACTION:
            body = url_obj.body_hint
            is_truncated = False
            crawl_method = "rss_content"

        content_hash = compute_content_hash(body) if body else (
            compute_content_hash(title))

        return RawArticle(
            url=url_obj.url,
            title=title,
            body=body,
            source_id=site_id,
            source_name=source_name,
            language=language,
            published_at=published_at,
            crawled_at=datetime.now(timezone.utc),
            author=url_obj.author_hint,
            crawl_tier=1,
            crawl_method=crawl_method,
            is_paywall_truncated=is_truncated,
            content_hash=content_hash,
        )

    def _create_rss_fallback_article(
        self,
        url_obj: DiscoveredURL,
        site_id: str,
        site_cfg: dict[str, Any],
    ) -> RawArticle | None:
        """Create a title-only article from RSS metadata when page is blocked.

        Thin wrapper around _create_article_from_rss(full_body=False).
        Preserves backward compatibility with existing error handler callsites.
        """
        return self._create_article_from_rss(
            url_obj, site_id, site_cfg, full_body=False)

    # -----------------------------------------------------------------------
    # Single Pass (all sites, with Level 2+3 retry per site)
    # -----------------------------------------------------------------------

    def _run_single_pass(
        self,
        target_sites: dict[str, dict[str, Any]],
        restart_number: int,
    ) -> list[CrawlResult]:
        """Run a single pass through all target sites concurrently.

        Uses ThreadPoolExecutor with cooperative per-site deadlines.
        Each site crawls independently; a slow site cannot block others.
        All thread-unsafe subsystems are protected by locks (Phase 2-3).

        The cooperative deadline ensures threads exit cleanly at URL
        boundaries — no thread killing. Partial results are preserved
        in CrawlState for the next L3/L4 round.

        Args:
            target_sites: Sites to crawl.
            restart_number: Current Level 4 restart number.

        Returns:
            List of CrawlResult, one per site.
        """
        results: list[CrawlResult] = []
        total = len(target_sites)

        # Phase 0: Pre-initialize RetryManager states to prevent race
        # on get_state() during concurrent execution.
        assert self._retry_manager is not None
        for site_id in target_sites:
            self._retry_manager.get_state(site_id)

        # Phase 1: Separate skip-eligible sites from crawl-eligible sites
        assert self._crawl_state is not None
        assert self._circuit_breakers is not None
        site_tasks: list[tuple[str, dict[str, Any]]] = []

        for site_id, site_cfg in target_sites.items():
            # Skip already-completed sites
            if self._crawl_state.is_site_complete(site_id):
                logger.info("site_already_complete site_id=%s", site_id)
                results.append(CrawlResult(source_id=site_id))
                continue

            # Skip disabled sites — D-7 (13): ENABLED_DEFAULT from constants.py (SOT)
            if not site_cfg.get("meta", {}).get("enabled", ENABLED_DEFAULT):
                logger.info("site_disabled site_id=%s", site_id)
                results.append(CrawlResult(source_id=site_id))
                continue

            # Circuit breaker — D-7: CRAWL_NEVER_ABANDON from constants.py (SOT)
            if not self._circuit_breakers.is_allowed(site_id):
                if CRAWL_NEVER_ABANDON:
                    logger.warning(
                        "circuit_breaker_open_force_probe site_id=%s "
                        "state=%s policy=never_abandon",
                        site_id, self._circuit_breakers.get_state(site_id).value,
                    )
                    self._circuit_breakers.force_half_open(site_id)
                else:
                    logger.warning(
                        "circuit_breaker_open site_id=%s state=%s",
                        site_id, self._circuit_breakers.get_state(site_id).value,
                    )
                    results.append(CrawlResult(
                        source_id=site_id,
                        errors=["Circuit breaker OPEN -- site skipped"],
                    ))
                    continue

            site_tasks.append((site_id, site_cfg))

        if not site_tasks:
            return results

        # P1 invariant: each thread must handle a unique site_id.
        # Duplicate site_ids would corrupt per-site RetryManager/CrawlState.
        _site_ids_in_flight = [sid for sid, _ in site_tasks]
        assert len(_site_ids_in_flight) == len(set(_site_ids_in_flight)), (
            f"P1 violation: duplicate site_ids in concurrent dispatch: "
            f"{[s for s in _site_ids_in_flight if _site_ids_in_flight.count(s) > 1]}"
        )

        # Phase 2: Concurrent execution with cooperative deadlines.
        # JSONLWriter is shared across threads (thread-safe via lock).
        # ThreadPoolExecutor.__exit__ calls shutdown(wait=True), which
        # guarantees ALL threads have finished BEFORE writer.close().
        output_path = self._output_dir / "all_articles.jsonl"
        crawl_count = len(site_tasks)

        logger.info(
            "concurrent_crawl_start sites=%s concurrency=%s per_site_timeout=%ss restart=%s",
            crawl_count, DEFAULT_CONCURRENCY, PER_SITE_TIMEOUT_SECONDS, restart_number,
        )

        with JSONLWriter(output_path) as writer:
            with ThreadPoolExecutor(
                max_workers=DEFAULT_CONCURRENCY,
                thread_name_prefix="crawl",
            ) as executor:
                futures: dict[Any, str] = {}

                for idx, (site_id, site_cfg) in enumerate(site_tasks, 1):
                    # H-15 fix: dynamic deadline based on site rate_limit and URL budget.
                    # Sites with higher rate limits need more time per URL.
                    crawl_cfg = site_cfg.get("crawl", {})
                    rate_limit = crawl_cfg.get("rate_limit_seconds", DEFAULT_RATE_LIMIT_SECONDS)
                    # Per-site timeout override from sources.yaml (optional)
                    cfg_timeout = crawl_cfg.get("per_site_timeout")
                    if cfg_timeout is not None:
                        dynamic_timeout = float(cfg_timeout)
                    else:
                        # Budget: rate_limit * expected_urls * 1.5 safety margin + 60s overhead
                        # Capped between PER_SITE_TIMEOUT_SECONDS and 900s (15 min)
                        dynamic_timeout = max(
                            PER_SITE_TIMEOUT_SECONDS,
                            min(rate_limit * 100 * 1.5 + 60, 900.0),
                        )
                    # BUG FIX: Pass timeout_seconds instead of pre-created deadline.
                    # Previously, SiteDeadline was created here at submit time,
                    # causing queued sites to have their deadline expire while
                    # waiting in the ThreadPoolExecutor queue (only 5 workers).
                    # Now the worker creates the deadline at execution time.
                    future = executor.submit(
                        self._crawl_site_worker,
                        site_id, site_cfg, writer, dynamic_timeout, idx, crawl_count, restart_number,
                    )
                    futures[future] = site_id

                # Global safety timeout: 24 hours. Per-site deadlines handle
                # fairness; this is a backstop for catastrophic hangs only.
                total_timeout = 86400.0  # 24 hours

                try:
                    for future in as_completed(futures, timeout=total_timeout):
                        site_id = futures[future]
                        try:
                            result = future.result()
                        except KeyboardInterrupt:
                            raise
                        except Exception as e:
                            logger.error(
                                "site_crawl_fatal site_id=%s error=%s error_type=%s",
                                site_id, str(e), type(e).__name__,
                            )
                            result = CrawlResult(
                                source_id=site_id,
                                errors=[f"Fatal: {type(e).__name__}: {e}"],
                            )

                        results.append(result)

                        logger.info(
                            "crawl_site_complete site=%s articles=%s discovered=%s "
                            "failed=%s elapsed=%ss yielded=%s",
                            site_id, result.extracted_count, result.discovered_urls,
                            result.failed_count, round(result.elapsed_seconds, 1),
                            result.deadline_yielded,
                        )
                except TimeoutError:
                    # 24-hour global timeout — collect partial, re-queue in next pass
                    completed_ids = {r.source_id for r in results}
                    for future, site_id in futures.items():
                        if site_id not in completed_ids:
                            logger.error(
                                "site_crawl_global_timeout site_id=%s — "
                                "will retry in next pass",
                                site_id,
                            )
                            results.append(CrawlResult(
                                source_id=site_id,
                                errors=["Global 24h timeout — deferred to next pass"],
                            ))
                            future.cancel()

        # P1 cross-validation: article count in CrawlResults must match
        # the number of articles actually written to the JSONL writer.
        # This catches any counting inconsistency from concurrent writes.
        concurrent_site_ids = {sid for sid, _ in site_tasks}
        concurrent_extracted = sum(
            r.extracted_count for r in results
            if r.source_id in concurrent_site_ids
        )
        if concurrent_extracted != writer.count:
            logger.error(
                "P1_ARTICLE_COUNT_MISMATCH results_sum=%s writer_count=%s "
                "delta=%s — data integrity may be compromised",
                concurrent_extracted, writer.count,
                concurrent_extracted - writer.count,
            )

        return results

    def _crawl_site_worker(
        self,
        site_id: str,
        site_cfg: dict[str, Any],
        writer: JSONLWriter,
        timeout_seconds: float,
        idx: int,
        total: int,
        restart_number: int,
    ) -> CrawlResult:
        """Thread worker: crawl a single site with deadline.

        Thin wrapper that logs start/end and delegates to
        _crawl_site_with_retry(). Runs in a ThreadPoolExecutor thread.

        The SiteDeadline is created HERE (at execution time), not at
        submit time. This ensures the deadline timer starts when the
        thread actually begins work, not while waiting in the queue.

        Args:
            site_id: Site identifier.
            site_cfg: Site configuration from sources.yaml.
            writer: Shared JSONL writer (thread-safe via lock).
            timeout_seconds: Seconds for cooperative deadline (created at execution time).
            idx: 1-based site index for logging.
            total: Total number of sites being crawled.
            restart_number: Current L4 restart number.

        Returns:
            CrawlResult with crawl statistics.
        """
        # Create deadline at execution time, not submit time.
        deadline = SiteDeadline.create(timeout_seconds)
        logger.info(
            "crawl_site_start site=%s (%s/%s) restart=%s deadline_remaining=%.0fs",
            site_id, idx, total, restart_number, deadline.remaining,
        )
        return self._crawl_site_with_retry(site_id, site_cfg, writer, deadline)

    # -----------------------------------------------------------------------
    # Per-Site Crawl with Level 2+3 Retry
    # -----------------------------------------------------------------------

    def _crawl_site_with_retry(
        self,
        site_id: str,
        site_cfg: dict[str, Any],
        writer: JSONLWriter,
        deadline: SiteDeadline | None = None,
    ) -> CrawlResult:
        """Crawl a single site with Level 2 and Level 3 retry.

        Level 2: Standard mode first, then TotalWar if >50% failure.
        Level 3: Up to 3 rounds, with increasing delays between rounds.

        When a cooperative deadline is set, the method checks expiry at
        each round boundary and propagates it to _crawl_urls(). Deadline
        expiry does NOT mark the site as complete — it is deferred to
        the next L3 round or L4 restart (CRAWL_NEVER_ABANDON compatible).

        Args:
            site_id: Site identifier.
            site_cfg: Site configuration from sources.yaml.
            writer: JSONL writer for article output.
            deadline: Optional cooperative timeout signal.

        Returns:
            CrawlResult with all statistics.
        """
        start_time = time.monotonic()
        result = CrawlResult(source_id=site_id)

        # Configure NetworkGuard for this site
        assert self._guard is not None
        crawl_config = site_cfg.get("crawl", {})
        rate_limit = crawl_config.get("rate_limit_seconds", DEFAULT_RATE_LIMIT_SECONDS)
        jitter = crawl_config.get("jitter_seconds", 0)

        self._guard.configure_site(
            site_id,
            rate_limit_seconds=rate_limit,
            jitter_seconds=jitter,
        )

        # Level 3: Crawler rounds
        assert self._retry_manager is not None

        for round_num in range(1, L3_MAX_ROUNDS + 1):
            # Cooperative deadline check at round boundary — yield to other sites.
            # P1: deadline_yielded flag prevents false completion marking.
            if deadline is not None and deadline.expired:
                result.deadline_yielded = True
                logger.info(
                    "site_deadline_yield site_id=%s round=%s — "
                    "yielding worker to other sites, will resume in next pass",
                    site_id, round_num,
                )
                break

            retry_state = self._retry_manager.get_state(site_id)
            retry_state.current_round = round_num

            logger.info(
                "crawl_round_start site_id=%s round=%s/%s",
                site_id, round_num, L3_MAX_ROUNDS,
            )

            # Phase 1: URL Discovery (internal retry in _discover_urls)
            discovered = self._discover_urls(site_id, site_cfg)
            if not discovered:
                logger.warning(
                    "no_urls_discovered site_id=%s round=%s — "
                    "continuing to next round",
                    site_id, round_num,
                )
                result.errors.append(f"URL discovery failed round {round_num}")
                continue  # try next round, never abandon on first failure

            # Filter out already-processed URLs
            new_urls = self._filter_processed_urls(site_id, discovered)
            if not new_urls:
                logger.info(
                    "all_urls_processed site_id=%s round=%s — "
                    "all %s URLs already seen, site complete",
                    site_id, round_num, len(discovered),
                )
                break  # Intentional: all discovered URLs processed = site genuinely done

            result.discovered_urls = max(result.discovered_urls, len(discovered))

            # Initialize retry state with pending URLs
            self._retry_manager.init_site(
                site_id,
                [u.url for u in new_urls],
            )

            # Level 2: Standard mode
            round_result = self._crawl_urls(
                site_id, site_cfg, new_urls, writer,
                strategy=StrategyMode.STANDARD,
                rate_limit=rate_limit,
                deadline=deadline,
            )
            self._merge_result(result, round_result)

            # Level 2: TotalWar escalation if >50% failure
            if self._retry_manager.should_escalate_to_totalwar(site_id):
                self._retry_manager.escalate_to_totalwar(site_id)
                retry_state = self._retry_manager.get_state(site_id)

                if retry_state.pending_urls:
                    # Re-fetch failed URLs with TotalWar settings
                    failed_url_objs = [
                        url_obj for url_obj in new_urls
                        if url_obj.url in set(retry_state.pending_urls)
                    ]

                    totalwar_result = self._crawl_urls(
                        site_id, site_cfg, failed_url_objs, writer,
                        strategy=StrategyMode.TOTALWAR,
                        rate_limit=rate_limit * TOTALWAR_DELAY_MULTIPLIER,
                        deadline=deadline,
                    )
                    self._merge_result(result, totalwar_result)

            # Check if we need another round
            retry_state = self._retry_manager.get_state(site_id)
            if not retry_state.failed_urls:
                logger.info("all_urls_successful site_id=%s round=%s", site_id, round_num)
                break

            if round_num < L3_MAX_ROUNDS:
                # Delay before next round (deadline-aware)
                if self._retry_manager.should_start_new_round(site_id):
                    delay = self._retry_manager.start_new_round(site_id)
                    max_sleep = min(delay, 30.0)
                    if deadline is not None:
                        max_sleep = min(max_sleep, deadline.remaining)
                    logger.info(
                        "round_delay site_id=%s delay=%ss next_round=%s",
                        site_id, max_sleep, round_num + 1,
                    )
                    if max_sleep > 0:
                        time.sleep(max_sleep)
                else:
                    break

        # Finalize — P1: only mark complete if NOT deadline-yielded.
        # A deadline yield means partial crawl; site must be re-queued.
        assert self._crawl_state is not None
        if result.extracted_count > 0 and not result.deadline_yielded:
            self._crawl_state.mark_site_complete(site_id)
        self._crawl_state.save()

        result.elapsed_seconds = time.monotonic() - start_time

        # Check exhaustion for Tier 6 escalation
        if self._retry_manager.is_exhausted(site_id):
            self._retry_manager.escalate_tier6(site_id)
            result.tier_used = 6

        return result

    # -----------------------------------------------------------------------
    # URL Discovery
    # -----------------------------------------------------------------------

    _DISCOVERY_MAX_RETRIES = 2  # C-2: retry discovery up to 2 times on failure

    def _discover_urls(
        self,
        site_id: str,
        site_cfg: dict[str, Any],
    ) -> list[DiscoveredURL]:
        """Run URL discovery for a site with retry on failure.

        C-2 fix: retries up to _DISCOVERY_MAX_RETRIES times on exception,
        with a short delay between retries (network transients, DNS flaps).

        Uses the 3-tier discovery pipeline: RSS -> Sitemap -> DOM.

        Args:
            site_id: Site identifier.
            site_cfg: Site configuration.

        Returns:
            List of discovered URLs.
        """
        assert self._url_discovery is not None

        discovered: list[DiscoveredURL] = []
        last_error: Exception | None = None

        for attempt in range(1, self._DISCOVERY_MAX_RETRIES + 1):
            try:
                discovered = self._url_discovery.discover(site_cfg, site_id)
                if discovered:
                    break
                # Empty result on first attempt — retry once (RSS might be temporarily empty)
                if attempt < self._DISCOVERY_MAX_RETRIES:
                    logger.info(
                        "discovery_empty_retry site_id=%s attempt=%s",
                        site_id, attempt,
                    )
                    time.sleep(5.0)
            except Exception as e:
                last_error = e
                logger.warning(
                    "discovery_error site_id=%s attempt=%s/%s error=%s error_type=%s",
                    site_id, attempt, self._DISCOVERY_MAX_RETRIES,
                    str(e)[:200], type(e).__name__,
                )
                if attempt < self._DISCOVERY_MAX_RETRIES:
                    time.sleep(5.0)

        if not discovered and last_error is not None:
            logger.error(
                "discovery_failed_all_retries site_id=%s error=%s",
                site_id, str(last_error)[:200],
            )

        # Bypass fallback: when all normal discovery methods fail,
        # try DynamicBypassEngine to fetch RSS/sitemap feeds directly.
        # This handles HTTP 403 blocked sites (Category B).
        if not discovered and self._bypass_engine is not None:
            # Determine block type from the last error (deterministic, not guessed)
            block_type = self._detect_block_type_from_error(last_error)

            discovered = self._discover_via_bypass(
                site_id, site_cfg, block_type,
            )

            if discovered:
                logger.info(
                    "bypass_discovery_success site_id=%s urls=%s block_type=%s",
                    site_id, len(discovered),
                    block_type.value if block_type else "unknown",
                )
                # Update bypass_state SOT for cross-crawl learning
                self._update_bypass_state(site_id, success=True, block_type=block_type)
            else:
                self._update_bypass_state(site_id, success=False, block_type=block_type)

        logger.info(
            "urls_discovered site_id=%s count=%s",
            site_id, len(discovered),
        )
        return discovered

    # -----------------------------------------------------------------------
    # Bypass Discovery (Category B — HTTP 403 blocked sites)
    # -----------------------------------------------------------------------

    def _detect_block_type_from_error(self, error: Exception | None) -> Any:
        """Extract block type from the last discovery error (deterministic).

        Returns a BlockType enum value from dynamic_bypass, or None if
        the error is not block-related. NO guessing — only uses information
        already present in the error object.
        """
        if error is None:
            return None

        from src.crawling.dynamic_bypass import BlockType

        if isinstance(error, BlockDetectedError):
            # BlockDetectedError carries block_type attribute
            bt_str = getattr(error, "block_type", None)
            if bt_str:
                try:
                    return BlockType(bt_str)
                except (ValueError, KeyError):
                    pass
            # Infer from error message (deterministic pattern match)
            msg = str(error).lower()
            if "captcha" in msg:
                return BlockType.CAPTCHA
            if "cloudflare" in msg or "just a moment" in msg:
                return BlockType.JS_CHALLENGE
            # No guessing — return None if block type is not deterministic
            return None

        if isinstance(error, NetworkError):
            status = getattr(error, "status_code", None)
            if status == 403:
                return BlockType.UA_FILTER
            if status == 429:
                return BlockType.RATE_LIMIT

        return None

    def _discover_via_bypass(
        self,
        site_id: str,
        site_cfg: dict[str, Any],
        block_type: Any,
    ) -> list[DiscoveredURL]:
        """Try DynamicBypassEngine to fetch RSS/sitemap feeds when blocked.

        Attempts up to DISCOVERY_BYPASS_MAX_ATTEMPTS strategies on each
        discovery URL (RSS, sitemap). Returns discovered URLs from the
        first successful bypass.

        Args:
            site_id: Site identifier.
            site_cfg: Site configuration from sources.yaml.
            block_type: Detected BlockType or None.

        Returns:
            List of discovered URLs, or empty list on failure.
        """
        assert self._bypass_engine is not None
        assert self._url_discovery is not None

        crawl_config = site_cfg.get("crawl", {})
        base_url = site_cfg.get("url", "")
        feed_urls = self._collect_discovery_urls(crawl_config, base_url)

        if not feed_urls:
            logger.debug("bypass_discovery_no_feeds site_id=%s", site_id)
            return []

        # Check bypass_state for previously learned successful strategy
        site_state = self._bypass_state.get("sites", {}).get(site_id, {})
        preferred_block_type = block_type
        if preferred_block_type is None and site_state.get("last_block_type"):
            from src.crawling.dynamic_bypass import BlockType
            try:
                preferred_block_type = BlockType(site_state["last_block_type"])
            except (ValueError, KeyError):
                pass

        for feed_url in feed_urls:
            bypass_result = self._bypass_engine.try_strategies(
                url=feed_url,
                block_type=preferred_block_type,
                site_id=site_id,
                max_attempts=DISCOVERY_BYPASS_MAX_ATTEMPTS,
                timeout=30.0,
            )

            if not bypass_result.success or len(bypass_result.html) < 500:
                logger.debug(
                    "bypass_discovery_feed_failed site_id=%s url=%s strategy=%s",
                    site_id, feed_url[:80], bypass_result.strategy_name,
                )
                continue

            # Parse the response — deterministic content type detection
            parsed = self._parse_discovery_response(
                feed_url, bypass_result.html, site_id, site_cfg,
            )
            if parsed:
                logger.info(
                    "bypass_discovery_parsed site_id=%s url=%s strategy=%s "
                    "tier=%s urls_found=%s",
                    site_id, feed_url[:80], bypass_result.strategy_name,
                    bypass_result.strategy_tier, len(parsed),
                )
                return parsed

        return []

    @staticmethod
    def _collect_discovery_urls(
        crawl_config: dict[str, Any],
        base_url: str,
    ) -> list[str]:
        """Extract RSS and sitemap URLs from site config.

        Returns an ordered list: RSS first (faster to parse), then sitemaps.
        """
        urls: list[str] = []

        rss_url = crawl_config.get("rss_url", "")
        if rss_url:
            if not rss_url.startswith(("http://", "https://")):
                from urllib.parse import urljoin
                rss_url = urljoin(base_url, rss_url)
            urls.append(rss_url)

        # Sitemap URLs (singular and plural)
        sitemap_urls_plural = crawl_config.get("sitemap_urls", [])
        sitemap_url_singular = crawl_config.get("sitemap_url", "")
        for s_url in sitemap_urls_plural:
            if s_url:
                if not s_url.startswith(("http://", "https://")):
                    from urllib.parse import urljoin
                    s_url = urljoin(base_url, s_url)
                if s_url not in urls:
                    urls.append(s_url)
        if sitemap_url_singular:
            if not sitemap_url_singular.startswith(("http://", "https://")):
                from urllib.parse import urljoin
                sitemap_url_singular = urljoin(base_url, sitemap_url_singular)
            if sitemap_url_singular not in urls:
                urls.append(sitemap_url_singular)

        return urls

    def _parse_discovery_response(
        self,
        feed_url: str,
        html: str,
        site_id: str,
        site_cfg: dict[str, Any],
    ) -> list[DiscoveredURL]:
        """Parse bypass response as RSS, Atom, Sitemap, or HTML.

        Deterministic content-type detection via XML tag inspection.
        No LLM judgment — pure string pattern matching.
        """
        assert self._url_discovery is not None
        stripped = html.strip()

        # Detect XML format by root tag (deterministic)
        if stripped.startswith("<?xml") or stripped.startswith("<"):
            lower = stripped[:500].lower()

            if "<rss" in lower or "<feed" in lower or "<channel>" in lower:
                # RSS/Atom feed
                return self._url_discovery.parse_feed_from_text(
                    html, site_id,
                )

            if "<urlset" in lower or "<sitemapindex" in lower:
                # Sitemap XML
                base_url = site_cfg.get("url", "")
                return self._url_discovery.parse_sitemap_from_text(
                    html, site_id, base_url=base_url,
                )

        # HTML page — try DOM link extraction
        if "<html" in stripped[:500].lower():
            from src.crawling.url_discovery import (
                normalize_url,
                is_article_url,
            )
            import re as _re

            base_url = site_cfg.get("url", "")
            results: list[DiscoveredURL] = []
            seen: set[str] = set()

            for m in _re.finditer(r'href=["\']([^"\']+)["\']', html):
                href = m.group(1)
                if not href.startswith(("http://", "https://")):
                    from urllib.parse import urljoin
                    href = urljoin(base_url, href)
                normalized = normalize_url(href)
                if normalized and is_article_url(normalized) and normalized not in seen:
                    seen.add(normalized)
                    results.append(DiscoveredURL(
                        url=normalized,
                        source_id=site_id,
                        discovered_via="bypass_dom",
                    ))

            return results

        return []

    # -----------------------------------------------------------------------
    # Bypass State SOT — cross-crawl learning persistence
    # -----------------------------------------------------------------------

    @staticmethod
    def _load_bypass_state() -> dict[str, Any]:
        """Load bypass_state.json SOT. Returns empty structure if not found."""
        if BYPASS_STATE_PATH.exists():
            try:
                with open(BYPASS_STATE_PATH, encoding="utf-8") as f:
                    state = json.load(f)
                logger.info(
                    "bypass_state_loaded sites=%s",
                    len(state.get("sites", {})),
                )
                return state
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("bypass_state_load_error error=%s", str(e))
        return {"version": 1, "updated_at": None, "sites": {}}

    def _save_bypass_state(self) -> None:
        """Persist bypass_state.json SOT to disk."""
        self._bypass_state["updated_at"] = datetime.now(timezone.utc).isoformat()
        BYPASS_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(BYPASS_STATE_PATH, "w", encoding="utf-8") as f:
                json.dump(self._bypass_state, f, indent=2, ensure_ascii=False)
        except OSError as e:
            logger.warning("bypass_state_save_error error=%s", str(e))

    def _update_bypass_state(
        self,
        site_id: str,
        success: bool,
        block_type: Any = None,
    ) -> None:
        """Update per-site bypass learning state and persist."""
        sites = self._bypass_state.setdefault("sites", {})
        entry = sites.setdefault(site_id, {})
        entry["discovery_bypass_needed"] = True
        if block_type is not None:
            entry["last_block_type"] = block_type.value if hasattr(block_type, "value") else str(block_type)
        if success:
            entry["last_success_at"] = datetime.now(timezone.utc).isoformat()
        entry["last_attempt_at"] = datetime.now(timezone.utc).isoformat()
        self._save_bypass_state()

    # -----------------------------------------------------------------------
    # Failure Report — post-crawl diagnosis for exhausted sites
    # -----------------------------------------------------------------------

    def _generate_failure_report(
        self,
        failed_site_ids: list[str],
        target_sites: dict[str, dict[str, Any]],
        all_results: dict[str, CrawlResult],
    ) -> None:
        """Generate deterministic failure report + next-crawl recommendations.

        Called when Never-Abandon multi-pass cap is reached. Writes to
        data/raw/YYYY-MM-DD/crawl_exhausted_sites.json.

        For each failed site, records:
        - Last known block type (from bypass_state SOT)
        - Strategies tried and their results
        - Deterministic recommended_action for next crawl
        """
        from src.config.constants import MULTI_PASS_MAX_EXTRA

        report: dict[str, Any] = {
            "date": self._date,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "max_passes_exhausted": MULTI_PASS_MAX_EXTRA,
            "exhausted_sites": [],  # list of per-site dicts (consumer contract)
        }

        for site_id in failed_site_ids:
            result = all_results.get(site_id)
            site_cfg = target_sites.get(site_id, {})

            # Gather evidence from bypass_state SOT
            site_bypass = self._bypass_state.get("sites", {}).get(site_id, {})

            # Determine failure category (deterministic)
            articles = result.extracted_count if result else 0
            discovered = result.discovered_urls if result else 0
            errors = result.errors[:5] if result else []  # First 5 errors
            block_type = site_bypass.get("last_block_type", "unknown")

            if discovered == 0:
                failure_category = "discovery_blocked"
                recommendation = (
                    f"URL discovery completely blocked (block_type={block_type}). "
                    "Next crawl: try T2 browser rendering for discovery, "
                    "or add alternative RSS/sitemap URLs to sources.yaml. "
                    "Consider T4 Wayback Machine as last resort."
                )
            elif articles == 0 and discovered > 0:
                failure_category = "extraction_blocked"
                recommendation = (
                    f"URLs discovered ({discovered}) but extraction failed. "
                    "Next crawl: DynamicBypassEngine should escalate to "
                    "T2 (Patchright/Camoufox) for extraction. "
                    "Check if site requires JS rendering."
                )
            else:
                failure_category = "partial_timeout"
                recommendation = (
                    f"Collected {articles}/{discovered} articles before timeout. "
                    "Next crawl: increase per-site timeout or reduce "
                    "rate_limit_seconds in sources.yaml."
                )

            report["exhausted_sites"].append({
                "site_id": site_id,
                "failure_category": failure_category,
                "block_type": block_type,
                "articles_collected": articles,
                "urls_discovered": discovered,
                "errors_sample": errors,
                "recommendation": recommendation,
                "site_url": site_cfg.get("url", ""),
                "site_difficulty": site_cfg.get("meta", {}).get("difficulty", "unknown"),
            })

            logger.error(
                "crawl_exhausted_site site_id=%s category=%s block_type=%s "
                "articles=%s discovered=%s recommendation=%s",
                site_id, failure_category, block_type,
                articles, discovered, recommendation[:100],
            )

        # Write report
        output_path = self._output_dir / "crawl_exhausted_sites.json"
        try:
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(report, f, indent=2, ensure_ascii=False)
            logger.warning(
                "crawl_exhausted_report_written path=%s sites=%s",
                str(output_path), len(failed_site_ids),
            )
        except OSError as e:
            logger.error("crawl_exhausted_report_error error=%s", str(e))

    def _filter_processed_urls(
        self,
        site_id: str,
        discovered: list[DiscoveredURL],
    ) -> list[DiscoveredURL]:
        """Filter out URLs that have already been processed or deduped.

        Checks against:
        1. CrawlState (URLs processed in previous runs)
        2. DedupEngine (URLs in the dedup database)

        Args:
            site_id: Site identifier.
            discovered: All discovered URLs.

        Returns:
            Filtered list of new URLs to process.
        """
        assert self._crawl_state is not None
        assert self._dedup is not None

        new_urls: list[DiscoveredURL] = []

        for url_obj in discovered:
            # Check CrawlState
            if self._crawl_state.is_url_processed(site_id, url_obj.url):
                continue

            # Check DedupEngine (URL-level only -- fast O(1) check)
            # check_only=True prevents premature URL registration that would
            # cause the extraction phase to reject the article as a duplicate.
            try:
                dedup_result = self._dedup.is_duplicate(
                    url=url_obj.url,
                    title="",
                    body="",
                    source_id=site_id,
                    article_id="",
                    check_only=True,
                )
                if dedup_result.is_duplicate and dedup_result.level == 1:
                    continue
            except Exception:
                pass  # Dedup check failure should not block crawling

            new_urls.append(url_obj)

        filtered_count = len(discovered) - len(new_urls)
        if filtered_count > 0:
            logger.info(
                "urls_filtered site_id=%s total=%s new=%s deduped=%s",
                site_id, len(discovered), len(new_urls), filtered_count,
            )

        return new_urls

    # -----------------------------------------------------------------------
    # Article Extraction
    # -----------------------------------------------------------------------

    def _crawl_urls(
        self,
        site_id: str,
        site_cfg: dict[str, Any],
        urls: list[DiscoveredURL],
        writer: JSONLWriter,
        strategy: int = StrategyMode.STANDARD,
        rate_limit: float = DEFAULT_RATE_LIMIT_SECONDS,
        deadline: SiteDeadline | None = None,
    ) -> CrawlResult:
        """Fetch and extract articles for a list of URLs.

        Args:
            site_id: Site identifier.
            site_cfg: Site configuration.
            urls: URLs to process.
            writer: JSONL writer for output.
            strategy: Current strategy mode (Standard or TotalWar).
            rate_limit: Rate limit for this strategy.
            deadline: Optional cooperative timeout signal.

        Returns:
            CrawlResult with extraction statistics.
        """
        assert self._extractor is not None
        assert self._dedup is not None
        assert self._ua_manager is not None
        assert self._retry_manager is not None
        assert self._crawl_state is not None
        assert self._circuit_breakers is not None
        assert self._guard is not None

        result = CrawlResult(source_id=site_id)
        source_name = site_cfg.get("name", site_id)

        strategy_name = "Standard" if strategy == StrategyMode.STANDARD else "TotalWar"
        logger.info(
            "crawl_urls_start site_id=%s urls=%s strategy=%s",
            site_id, len(urls), strategy_name,
        )

        # Apply TotalWar rate limit override
        if strategy == StrategyMode.TOTALWAR:
            self._guard.configure_site(
                site_id,
                rate_limit_seconds=rate_limit,
                jitter_seconds=rate_limit * 0.3,
            )

        for url_obj in urls:
            # Cooperative deadline check — yield worker at URL boundary.
            # P1: deadline_yielded flag prevents false completion marking.
            if deadline is not None and deadline.expired:
                result.deadline_yielded = True
                logger.info(
                    "site_deadline_yield_in_crawl site_id=%s processed=%s "
                    "remaining_urls=%s — yielding to other sites",
                    site_id, result.extracted_count,
                    len(urls) - (result.extracted_count + result.failed_count),
                )
                break

            # Article cap (5000 default) — safety limit for memory
            if result.extracted_count >= self._max_articles:
                logger.info(
                    "max_articles_reached site_id=%s limit=%s",
                    site_id, self._max_articles,
                )
                break

            # ---- RSS Content Extraction shortcut ----
            # If RSS feed provided full body text, skip HTTP fetch entirely.
            # Must be BEFORE Circuit Breaker check — these sites' article pages
            # return 403, so CB is already OPEN. Fetching would fail.
            if (url_obj.body_hint
                    and len(url_obj.body_hint) >= _MIN_RSS_BODY_FOR_EXTRACTION
                    and url_obj.title_hint):
                article = self._create_article_from_rss(
                    url_obj, site_id, site_cfg, full_body=True)
                if article is not None:
                    article_id = str(uuid.uuid4())
                    dedup_result = self._dedup.is_duplicate(
                        url=article.url, title=article.title,
                        body=article.body, source_id=site_id,
                        article_id=article_id)
                    if dedup_result.is_duplicate:
                        result.skipped_dedup_count += 1
                    else:
                        writer.write_article(article)
                        result.extracted_count += 1
                        logger.info(
                            "rss_content_extracted url=%s site_id=%s words=%d",
                            url_obj.url[:80], site_id,
                            len(article.body.split()))
                    self._circuit_breakers.record_success(site_id)
                    self._retry_manager.mark_url_success(site_id, url_obj.url)
                    continue
            # ---- END RSS Content Extraction shortcut ----

            # Circuit breaker — D-7: CRAWL_NEVER_ABANDON from constants.py
            if not self._circuit_breakers.is_allowed(site_id):
                if CRAWL_NEVER_ABANDON:
                    self._circuit_breakers.force_half_open(site_id)
                    logger.warning(
                        "circuit_breaker_force_half_open site_id=%s — "
                        "CRAWL_NEVER_ABANDON active, retrying",
                        site_id,
                    )
                else:
                    logger.warning("circuit_breaker_tripped site_id=%s", site_id)
                    result.errors.append("Circuit breaker opened during crawl")
                    break

            # Apply UA rotation
            ua_string = self._ua_manager.get_ua(site_id)

            # Attempt extraction
            try:
                article = self._extractor.extract(
                    url=url_obj.url,
                    source_id=site_id,
                    site_config=site_cfg,
                    title_hint=url_obj.title_hint,
                    discovered_via=url_obj.discovered_via,
                )

                # Override crawl_method from discovery
                # RawArticle is frozen, so we create a new one with the right method.
                # Preserve non-discovery crawl_methods set by ArticleExtractor:
                #   "adaptive" = browser render + adaptive CSS selector extraction
                #   "playwright" = browser render + standard extraction chain
                # Overwriting these with discovered_via would lose paywall bypass tracking.
                effective_method = (
                    article.crawl_method
                    if article.crawl_method in ("adaptive", "playwright")
                    else url_obj.discovered_via
                )
                article = RawArticle(
                    url=article.url,
                    title=article.title,
                    body=article.body,
                    source_id=article.source_id,
                    source_name=article.source_name,
                    language=article.language,
                    published_at=article.published_at,
                    crawled_at=article.crawled_at,
                    author=article.author,
                    category=article.category,
                    content_hash=article.content_hash,
                    crawl_tier=article.crawl_tier,
                    crawl_method=effective_method,
                    is_paywall_truncated=article.is_paywall_truncated,
                )

                # 24-hour lookback filter (absolute rule for daily runs).
                # Drop articles published before the cutoff even if they
                # passed the coarser URL-discovery date filter.
                # NOTE: article.published_at is datetime|None (not str).
                if article.published_at is not None:
                    pub_dt = article.published_at
                    if pub_dt.tzinfo is None:
                        pub_dt = pub_dt.replace(tzinfo=timezone.utc)
                    if pub_dt < self._lookback_cutoff:
                        result.skipped_freshness_count += 1
                        # Extraction succeeded — record success so circuit
                        # breakers and retry manager stay accurate.
                        self._retry_manager.mark_url_success(site_id, url_obj.url)
                        self._circuit_breakers.record_success(site_id)
                        logger.debug(
                            "article_outside_24h site_id=%s url=%s published_at=%s cutoff=%s",
                            site_id,
                            url_obj.url[:80],
                            pub_dt.isoformat(),
                            self._lookback_cutoff.isoformat(),
                        )
                        continue

                # Content-level dedup check
                article_id = str(uuid.uuid4())
                dedup_result = self._dedup.is_duplicate(
                    url=article.url,
                    title=article.title,
                    body=article.body,
                    source_id=site_id,
                    article_id=article_id,
                )

                if dedup_result.is_duplicate:
                    result.skipped_dedup_count += 1
                    logger.debug(
                        "article_deduped site_id=%s url=%s level=%s reason=%s",
                        site_id,
                        url_obj.url[:80], dedup_result.level, dedup_result.reason[:80],
                    )
                else:
                    # Write to JSONL
                    writer.write_article(article)
                    # H-16 fix: don't keep full article bodies in memory.
                    # Articles are already persisted to JSONL; keeping them in
                    # result.articles wastes ~1.2 GB for 116 sites at scale.
                    # extracted_count is the authoritative counter.
                    result.extracted_count += 1

                # Mark success
                self._retry_manager.mark_url_success(site_id, url_obj.url)
                self._circuit_breakers.record_success(site_id)

            except ParseError as e:
                result.failed_count += 1
                result.errors.append(f"Parse: {url_obj.url}: {e}")
                self._retry_manager.handle_url_failure(
                    site_id, url_obj.url,
                    error_type="ParseError", error_msg=str(e),
                )
                logger.warning(
                    "extraction_parse_error url=%s site_id=%s error=%s",
                    url_obj.url[:80], site_id, str(e)[:200],
                )

            except NetworkError as e:
                # For title_only sites with RSS metadata, save title-only article
                if (site_cfg.get("extraction", {}).get("title_only", False)
                        and url_obj.title_hint
                        and url_obj.discovered_via in ("rss", "google_news")):
                    article = self._create_rss_fallback_article(
                        url_obj, site_id, site_cfg)
                    if article is not None:
                        article_id = str(uuid.uuid4())
                        dedup_result = self._dedup.is_duplicate(
                            url=article.url, title=article.title,
                            body=article.body, source_id=site_id,
                            article_id=article_id)
                        if not dedup_result.is_duplicate:
                            writer.write_article(article)
                            result.extracted_count += 1
                            logger.info(
                                "rss_fallback_saved url=%s site_id=%s title=%s",
                                url_obj.url[:80], site_id,
                                (article.title or "")[:60])
                            self._circuit_breakers.record_success(site_id)
                            continue
                result.failed_count += 1
                result.errors.append(f"Network: {url_obj.url}: {e}")
                self._retry_manager.handle_url_failure(
                    site_id, url_obj.url,
                    error_type="NetworkError", error_msg=str(e),
                )
                self._circuit_breakers.record_failure(site_id, "network_error")
                logger.warning(
                    "extraction_network_error url=%s site_id=%s error=%s",
                    url_obj.url[:80], site_id, str(e)[:200],
                )

            except BlockDetectedError as e:
                # For title_only sites with RSS metadata, save title-only article
                # even when the article page is blocked (403/WAF).
                if (site_cfg.get("extraction", {}).get("title_only", False)
                        and url_obj.title_hint
                        and url_obj.discovered_via in ("rss", "google_news")):
                    article = self._create_rss_fallback_article(
                        url_obj, site_id, site_cfg)
                    if article is not None:
                        article_id = str(uuid.uuid4())
                        dedup_result = self._dedup.is_duplicate(
                            url=article.url, title=article.title,
                            body=article.body, source_id=site_id,
                            article_id=article_id)
                        if not dedup_result.is_duplicate:
                            writer.write_article(article)
                            result.extracted_count += 1
                            logger.info(
                                "rss_fallback_saved url=%s site_id=%s title=%s",
                                url_obj.url[:80], site_id,
                                (article.title or "")[:60])
                            self._circuit_breakers.record_success(site_id)
                            continue
                result.failed_count += 1
                result.tier_used = max(result.tier_used, 2)
                result.errors.append(f"Blocked: {url_obj.url}: {e}")
                self._retry_manager.handle_url_failure(
                    site_id, url_obj.url,
                    error_type="BlockDetectedError", error_msg=str(e),
                )
                self._circuit_breakers.record_failure(site_id, e.block_type)
                logger.warning(
                    "extraction_blocked url=%s site_id=%s block_type=%s",
                    url_obj.url[:80], site_id, e.block_type,
                )

            except RateLimitError as e:
                result.failed_count += 1
                result.errors.append(f"RateLimit: {url_obj.url}: {e}")
                self._retry_manager.handle_url_failure(
                    site_id, url_obj.url,
                    error_type="RateLimitError", error_msg=str(e),
                )
                # C-11 fix: record circuit breaker failure for rate limits
                self._circuit_breakers.record_failure(site_id, "rate_limit")
                logger.warning(
                    "extraction_rate_limited url=%s site_id=%s",
                    url_obj.url[:80], site_id,
                )
                # Respect rate limit -- wait
                if e.retry_after:
                    time.sleep(min(e.retry_after, 60.0))

            except Exception as e:
                result.failed_count += 1
                result.errors.append(f"Unexpected: {url_obj.url}: {type(e).__name__}: {e}")
                self._retry_manager.handle_url_failure(
                    site_id, url_obj.url,
                    error_type=type(e).__name__, error_msg=str(e),
                )
                # H-21 fix: record circuit breaker failure for generic exceptions
                self._circuit_breakers.record_failure(site_id, "unexpected_error")
                logger.error(
                    "extraction_unexpected url=%s site_id=%s error=%s error_type=%s",
                    url_obj.url[:80], site_id, str(e)[:200], type(e).__name__,
                )

            # Mark URL as processed (for CrawlState resume)
            self._crawl_state.mark_url_processed(site_id, url_obj.url)

        return result

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    @staticmethod
    def _merge_result(target: CrawlResult, source: CrawlResult) -> None:
        """Merge a source CrawlResult into a target.

        Adds statistics from source into target without creating a new object.

        Args:
            target: Result to merge into.
            source: Result to merge from.
        """
        target.articles.extend(source.articles)
        target.extracted_count += source.extracted_count
        target.failed_count += source.failed_count
        target.skipped_dedup_count += source.skipped_dedup_count
        target.skipped_freshness_count += source.skipped_freshness_count
        target.tier_used = max(target.tier_used, source.tier_used)
        target.errors.extend(source.errors)
        # P1: deadline_yielded propagates upward (sticky True)
        if source.deadline_yielded:
            target.deadline_yielded = True


# ---------------------------------------------------------------------------
# Entry point for main.py
# ---------------------------------------------------------------------------

def run_crawl_pipeline(
    crawl_date: str | None = None,
    sites: list[str] | None = None,
    groups: list[str] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Convenience function for running the crawling pipeline.

    Called from ``main.py cmd_crawl()``.

    Args:
        crawl_date: Target date (YYYY-MM-DD), defaults to today.
        sites: Optional list of site IDs to crawl.
        groups: Optional list of group letters to crawl.
        dry_run: If True, validate config without making requests.

    Returns:
        Crawl report dictionary.
    """
    with CrawlingPipeline(
        crawl_date=crawl_date,
        sites_filter=sites,
        groups_filter=groups,
        dry_run=dry_run,
    ) as pipeline:
        return pipeline.run()
