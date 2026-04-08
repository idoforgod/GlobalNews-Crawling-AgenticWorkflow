"""4-Level Retry System for the GlobalNews crawling pipeline.

Implements a hierarchical retry architecture with 4 nested levels:

    Level 1 -- NetworkGuard (innermost): 5 retries with exponential backoff
               per HTTP request. Already implemented in network_guard.py.
               Total: 5 attempts per request.

    Level 2 -- Standard + TotalWar escalation: 2 modes per site.
               Standard mode: normal headers, regular UA rotation.
               TotalWar mode: higher anti-block tier, stealth UA, longer delays.
               If Standard fails, switch to TotalWar for failed URLs.
               Total: 2 strategy attempts.

    Level 3 -- Crawler round: 3 rounds per site.
               Each round processes all remaining (unfetched) URLs.
               Between rounds: increase delays, cycle anti-block tier.
               Total: 3 rounds.

    Level 4 -- Pipeline restart (outermost): 3 full restarts.
               Preserves already-collected articles via dedup/CrawlState.
               On restart: re-discover URLs, skip already-collected.
               Total: 3 restarts.

    Grand total maximum attempts: 5 x 2 x 3 x 3 = 90 per URL.

After all 90 attempts exhausted for a site, Tier 6 escalation writes a
diagnostic report to ``logs/tier6-escalation/{site}-{date}.json`` for
Claude Code interactive analysis.

Reference:
    Step 5 Architecture Blueprint, Section 4a (4-Level Retry Architecture).
    Step 3 Crawling Feasibility (Retry budget calculation).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import IntEnum, unique
from pathlib import Path
from typing import Any

from src.config.constants import PROJECT_ROOT

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Level 1: NetworkGuard retries (already handled in network_guard.py)
L1_MAX_RETRIES = 5

# Level 2: Strategy modes
L2_STRATEGY_COUNT = 2  # Standard + TotalWar

# Level 3: Crawler rounds
L3_MAX_ROUNDS = 3
L3_ROUND_DELAYS = [30.0, 60.0, 120.0]  # seconds between rounds


def get_adaptive_max_rounds(site_cfg: dict) -> int:
    """Return site-specific max rounds based on profile.

    Small sites (≤10 articles/day, LOW block) get 1 round.
    Medium sites (≤50) get 2 rounds. Large or HIGH-block sites get full 3.
    This prevents wasting time on multi-round retries for sites that
    will never yield enough URLs to justify the cost.

    Args:
        site_cfg: Site configuration dict from sources.yaml.

    Returns:
        Adaptive max rounds (1, 2, or L3_MAX_ROUNDS).
    """
    from src.config.constants import VALID_BOT_BLOCK_LEVELS

    daily_est = (site_cfg.get("meta") or {}).get("daily_article_estimate", 0) or 0
    bot_block = ((site_cfg.get("anti_block") or {}).get("bot_block_level", "LOW") or "LOW").upper()

    # P1: validate bot_block_level against enum — no free-form strings
    if bot_block not in VALID_BOT_BLOCK_LEVELS:
        logger.warning(
            "invalid_bot_block_level value=%s — defaulting to LOW", bot_block,
        )
        bot_block = "LOW"

    # HIGH bot-block sites always get full rounds for escalation room
    if bot_block == "HIGH":
        return L3_MAX_ROUNDS
    if daily_est <= 10:
        return 1
    if daily_est <= 50:
        return 2
    return L3_MAX_ROUNDS

# Level 4: Pipeline restarts
L4_MAX_RESTARTS = 3
L4_RESTART_DELAYS = [60.0, 120.0, 300.0]  # seconds between restarts

# Total maximum attempts per URL in the standard 4-level retry
TOTAL_STANDARD_ATTEMPTS = L1_MAX_RETRIES * L2_STRATEGY_COUNT * L3_MAX_ROUNDS * L4_MAX_RESTARTS
assert TOTAL_STANDARD_ATTEMPTS == 90, f"Expected 90 standard attempts, got {TOTAL_STANDARD_ATTEMPTS}"
# Backward compatibility alias
TOTAL_MAX_ATTEMPTS = TOTAL_STANDARD_ATTEMPTS

# ── Crawling Absolute Principle (크롤링 절대 원칙) ──
# After standard 90 attempts exhaust, the Never-Abandon loop takes over.
# It cycles through alternative source strategies (RSS, cache, AMP) with
# exponential backoff (30s → 60s → 120s → ... → max 600s) until success
# or daily time budget exhaustion.
NEVER_ABANDON_MAX_CYCLES = 10  # Hard cap: 10 cycles, then report failure + recommend alternatives
NEVER_ABANDON_BASE_DELAY = 30.0  # Starting delay for persistence loop
NEVER_ABANDON_MAX_DELAY = 600.0  # Max 10-minute delay between cycles
NEVER_ABANDON_BACKOFF_FACTOR = 1.5  # Exponential backoff multiplier

# Alternative source strategies for Never-Abandon loop.
# Names align with DynamicBypassEngine strategy registry
# (src/crawling/dynamic_bypass.py — D-7 Instance 12).
ALTERNATIVE_STRATEGIES = [
    "rotate_user_agent",         # Tier 0: User-Agent string rotation
    "exponential_backoff",       # Tier 0: Exponential delay with jitter
    "rss_feed_fallback",         # Tier 0: RSS/Atom feed (includes Google News RSS)
    "google_cache_fallback",     # Tier 0: Google's cached version of the page
    "amp_version_fallback",      # Tier 0: AMP version (often less protected)
    "gdelt_api_fallback",        # Tier 0: GDELT DOC API for URL discovery
    "curl_cffi_impersonate",     # Tier 1: TLS fingerprint mimicry (JA3/JA4)
    "fingerprint_rotation",      # Tier 1: Rotate all TLS profiles
    "cloudscraper_solve",        # Tier 1: Cloudflare JS challenge solver
    "patchright_stealth",        # Tier 2: Full stealth browser (Chromium)
    "camoufox_stealth",          # Tier 2: Full stealth browser (Firefox)
    "proxy_rotation",            # Tier 3: Proxy pool + TLS mimicry
    "archive_today_fallback",    # Tier 4: archive.today mirror
    "wayback_fallback",          # Tier 4: Internet Archive (last resort)
]

# Tier 6 escalation directory (diagnostic reports)
TIER6_ESCALATION_DIR = PROJECT_ROOT / "logs" / "tier6-escalation"


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

@unique
class StrategyMode(IntEnum):
    """Level 2 strategy modes."""
    STANDARD = 1
    TOTALWAR = 2


@unique
class RetryLevel(IntEnum):
    """Which retry level triggered a retry."""
    L1_NETWORK = 1
    L2_STRATEGY = 2
    L3_CRAWLER_ROUND = 3
    L4_PIPELINE_RESTART = 4


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class RetryAttempt:
    """Record of a single retry attempt at any level.

    Attributes:
        level: Which retry level (1-4).
        attempt_number: Attempt counter within that level.
        strategy_mode: Standard or TotalWar (Level 2).
        round_number: Crawler round (Level 3).
        restart_number: Pipeline restart (Level 4).
        error_type: Exception class name.
        error_message: Error description.
        url: URL being fetched (if applicable).
        site_id: Site identifier.
        timestamp: When the attempt occurred.
        elapsed_seconds: How long the attempt took.
    """
    level: int
    attempt_number: int
    strategy_mode: int = 1
    round_number: int = 1
    restart_number: int = 1
    error_type: str = ""
    error_message: str = ""
    url: str = ""
    site_id: str = ""
    timestamp: str = ""
    elapsed_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Serialize for JSON output."""
        return {
            "level": self.level,
            "attempt_number": self.attempt_number,
            "strategy_mode": self.strategy_mode,
            "round_number": self.round_number,
            "restart_number": self.restart_number,
            "error_type": self.error_type,
            "error_message": self.error_message[:500],
            "url": self.url,
            "site_id": self.site_id,
            "timestamp": self.timestamp,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
        }


@dataclass
class SiteRetryState:
    """Per-site retry tracking across all 4 levels.

    Attributes:
        site_id: Site identifier.
        current_strategy: Current strategy mode (Standard or TotalWar).
        current_round: Current crawler round (1-3).
        current_restart: Current pipeline restart (1-3).
        total_attempts: Total attempts across all levels.
        successful_urls: URLs that were successfully fetched.
        failed_urls: URLs that failed all retries.
        pending_urls: URLs not yet attempted in current round.
        retry_history: Full history of retry attempts.
        exhausted: Whether all retry levels are exhausted.
        tier6_escalated: Whether Tier 6 escalation was triggered.
    """
    site_id: str
    current_strategy: int = StrategyMode.STANDARD
    current_round: int = 1
    current_restart: int = 1
    total_attempts: int = 0
    successful_urls: set[str] = field(default_factory=set)
    failed_urls: set[str] = field(default_factory=set)
    pending_urls: list[str] = field(default_factory=list)
    retry_history: list[RetryAttempt] = field(default_factory=list)
    exhausted: bool = False
    tier6_escalated: bool = False
    # Never-Abandon Persistence Loop (크롤링 절대 원칙)
    never_abandon_cycle: int = 0
    never_abandon_strategy_idx: int = 0
    never_abandon_active: bool = False

    def record_attempt(
        self,
        level: int,
        attempt_number: int,
        url: str = "",
        error_type: str = "",
        error_message: str = "",
        elapsed_seconds: float = 0.0,
    ) -> RetryAttempt:
        """Record a retry attempt at any level.

        Args:
            level: Retry level (1-4).
            attempt_number: Attempt counter within the level.
            url: URL being fetched.
            error_type: Exception class name.
            error_message: Error description.
            elapsed_seconds: Duration of the attempt.

        Returns:
            The recorded RetryAttempt.
        """
        attempt = RetryAttempt(
            level=level,
            attempt_number=attempt_number,
            strategy_mode=self.current_strategy,
            round_number=self.current_round,
            restart_number=self.current_restart,
            error_type=error_type,
            error_message=error_message,
            url=url,
            site_id=self.site_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
            elapsed_seconds=elapsed_seconds,
        )
        self.retry_history.append(attempt)
        self.total_attempts += 1
        return attempt

    @property
    def retry_stats(self) -> dict[str, int]:
        """Aggregate retry counts per level."""
        counts = {f"level{i}": 0 for i in range(1, 5)}
        for attempt in self.retry_history:
            key = f"level{attempt.level}"
            counts[key] = counts.get(key, 0) + 1
        return counts


# ---------------------------------------------------------------------------
# Retry Manager
# ---------------------------------------------------------------------------

class RetryManager:
    """4-level retry orchestration for the crawling pipeline.

    Manages the state machine of nested retries across 4 levels.
    Coordinates escalation between Standard and TotalWar modes,
    crawler rounds, and pipeline restarts.

    Usage:
        manager = RetryManager()
        state = manager.get_state("chosun")

        # After a URL failure:
        action = manager.handle_url_failure(
            "chosun", url, error_type="NetworkError", error_msg="timeout"
        )
        if action == "retry_l2":
            # Switch to TotalWar mode
        elif action == "retry_l3":
            # Start new crawler round
        elif action == "retry_l4":
            # Restart pipeline for this site
        elif action == "exhausted":
            # All retries exhausted, Tier 6 escalation

    Args:
        crawl_date: Date string for this crawl run (YYYY-MM-DD).
    """

    def __init__(self, crawl_date: str = "") -> None:
        self._crawl_date = crawl_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self._states: dict[str, SiteRetryState] = {}

    def get_state(self, site_id: str) -> SiteRetryState:
        """Get or create retry state for a site.

        Args:
            site_id: Site identifier.

        Returns:
            SiteRetryState for the site.
        """
        if site_id not in self._states:
            self._states[site_id] = SiteRetryState(site_id=site_id)
        return self._states[site_id]

    def init_site(
        self,
        site_id: str,
        discovered_urls: list[str],
    ) -> SiteRetryState:
        """Initialize retry state for a site with discovered URLs.

        Args:
            site_id: Site identifier.
            discovered_urls: URLs discovered during URL discovery phase.

        Returns:
            Initialized SiteRetryState.
        """
        state = self.get_state(site_id)
        state.pending_urls = list(discovered_urls)
        return state

    def mark_url_success(self, site_id: str, url: str) -> None:
        """Mark a URL as successfully fetched.

        Args:
            site_id: Site identifier.
            url: Successfully fetched URL.
        """
        state = self.get_state(site_id)
        state.successful_urls.add(url)
        if url in state.failed_urls:
            state.failed_urls.discard(url)

    def handle_url_failure(
        self,
        site_id: str,
        url: str,
        error_type: str = "",
        error_msg: str = "",
        elapsed: float = 0.0,
    ) -> str:
        """Handle a URL fetch failure by recording it.

        Records the failure in the site's retry state. Always returns
        "continue" — escalation decisions are made separately by
        ``should_escalate_to_totalwar()`` and ``should_restart_round()``.

        Args:
            site_id: Site identifier.
            url: Failed URL.
            error_type: Exception class name.
            error_msg: Error description.
            elapsed: Duration of the failed attempt.

        Returns:
            "continue" — always skip this URL and proceed to the next.
        """
        state = self.get_state(site_id)
        state.failed_urls.add(url)

        # Record the attempt
        state.record_attempt(
            level=RetryLevel.L1_NETWORK,
            attempt_number=state.total_attempts + 1,
            url=url,
            error_type=error_type,
            error_message=error_msg,
            elapsed_seconds=elapsed,
        )

        logger.debug(
            "url_failure_recorded site_id=%s url=%s error=%s strategy=%s round=%s restart=%s",
            site_id, url[:80], error_type,
            state.current_strategy, state.current_round, state.current_restart,
        )

        # Default: continue to next URL
        return "continue"

    def should_escalate_to_totalwar(self, site_id: str) -> bool:
        """Check if the site should escalate from Standard to TotalWar mode.

        Conditions for escalation:
        - Currently in Standard mode
        - More than 50% of URLs failed in current round

        Args:
            site_id: Site identifier.

        Returns:
            True if escalation to TotalWar is recommended.
        """
        state = self.get_state(site_id)
        if state.current_strategy >= StrategyMode.TOTALWAR:
            return False

        total_attempted = len(state.successful_urls) + len(state.failed_urls)
        if total_attempted == 0:
            return False

        failure_rate = len(state.failed_urls) / total_attempted
        return failure_rate > 0.5

    def escalate_to_totalwar(self, site_id: str) -> None:
        """Switch a site to TotalWar strategy mode.

        Moves all failed URLs back to pending for retry with TotalWar mode.

        Args:
            site_id: Site identifier.
        """
        state = self.get_state(site_id)
        state.current_strategy = StrategyMode.TOTALWAR
        # Move failed URLs back to pending
        state.pending_urls = list(state.failed_urls)
        state.failed_urls.clear()

        state.record_attempt(
            level=RetryLevel.L2_STRATEGY,
            attempt_number=state.current_strategy,
            error_type="STRATEGY_ESCALATION",
            error_message=f"Escalated to TotalWar mode. {len(state.pending_urls)} URLs to retry.",
        )

        logger.info(
            "strategy_escalated site_id=%s mode=TotalWar pending=%s",
            site_id, len(state.pending_urls),
        )

    def should_start_new_round(self, site_id: str) -> bool:
        """Check if a new crawler round should start.

        Conditions:
        - Current round has processed all pending URLs
        - There are still failed URLs
        - We haven't exceeded the round limit (3 rounds)
        - Both strategies have been tried in current round

        Args:
            site_id: Site identifier.

        Returns:
            True if a new round should start.
        """
        state = self.get_state(site_id)
        if state.current_round >= L3_MAX_ROUNDS:
            return False
        if not state.failed_urls:
            return False
        if not state.pending_urls:
            # All pending processed, but there are failures
            return True
        return False

    def start_new_round(self, site_id: str) -> float:
        """Start a new crawler round.

        Resets strategy to Standard, moves failed URLs to pending,
        and returns the delay to wait before the new round.

        Args:
            site_id: Site identifier.

        Returns:
            Delay in seconds to wait before starting the new round.
        """
        state = self.get_state(site_id)
        state.current_round += 1
        state.current_strategy = StrategyMode.STANDARD
        state.pending_urls = list(state.failed_urls)
        state.failed_urls.clear()

        delay = L3_ROUND_DELAYS[min(state.current_round - 1, len(L3_ROUND_DELAYS) - 1)]

        state.record_attempt(
            level=RetryLevel.L3_CRAWLER_ROUND,
            attempt_number=state.current_round,
            error_type="NEW_ROUND",
            error_message=f"Starting round {state.current_round}. "
                          f"{len(state.pending_urls)} URLs to retry. Delay: {delay}s.",
        )

        logger.info(
            "new_round_started site_id=%s round=%s pending=%s delay=%s",
            site_id, state.current_round, len(state.pending_urls), delay,
        )

        return delay

    def should_restart_pipeline(self, site_id: str) -> bool:
        """Check if the pipeline should be restarted for this site.

        Conditions:
        - All rounds exhausted (round >= L3_MAX_ROUNDS)
        - There are still failed URLs
        - Haven't exceeded the restart limit (3 restarts)

        Args:
            site_id: Site identifier.

        Returns:
            True if a pipeline restart is recommended.
        """
        state = self.get_state(site_id)
        if state.current_restart >= L4_MAX_RESTARTS:
            return False
        if state.current_round < L3_MAX_ROUNDS:
            return False
        if not state.failed_urls:
            return False
        return True

    def restart_pipeline(self, site_id: str) -> float:
        """Restart the pipeline for a site.

        Resets round and strategy counters. Failed URLs are preserved
        for re-discovery. Returns delay before restart.

        Args:
            site_id: Site identifier.

        Returns:
            Delay in seconds to wait before restarting.
        """
        state = self.get_state(site_id)
        state.current_restart += 1
        state.current_round = 1
        state.current_strategy = StrategyMode.STANDARD
        state.pending_urls.clear()
        # failed_urls preserved -- they'll be re-discovered on restart

        delay = L4_RESTART_DELAYS[min(state.current_restart - 1, len(L4_RESTART_DELAYS) - 1)]

        state.record_attempt(
            level=RetryLevel.L4_PIPELINE_RESTART,
            attempt_number=state.current_restart,
            error_type="PIPELINE_RESTART",
            error_message=f"Pipeline restart #{state.current_restart}. "
                          f"{len(state.failed_urls)} failed URLs. Delay: {delay}s.",
        )

        logger.warning(
            "pipeline_restarted site_id=%s restart=%s failed=%s delay=%s",
            site_id, state.current_restart, len(state.failed_urls), delay,
        )

        return delay

    def is_exhausted(self, site_id: str) -> bool:
        """Check if all retry levels are exhausted for a site.

        Args:
            site_id: Site identifier.

        Returns:
            True if no more retries are available.
        """
        state = self.get_state(site_id)
        if state.exhausted:
            return True

        # All rounds exhausted AND all restarts exhausted AND still have failures
        if (state.current_restart >= L4_MAX_RESTARTS
                and state.current_round >= L3_MAX_ROUNDS
                and state.failed_urls
                and not state.pending_urls):
            state.exhausted = True
            return True

        return False

    def escalate_tier6(self, site_id: str) -> Path:
        """Trigger Tier 6 Never-Abandon escalation.

        Writes a diagnostic report AND activates the Never-Abandon
        persistence loop. Unlike the old "human escalation" which gave up,
        this marks the site for continued retry with alternative strategies.

        크롤링 절대 원칙: NEVER abandon a crawl target. After standard 90
        attempts exhaust, activate the persistence loop with alternative
        source strategies (RSS, cache, AMP, etc.).

        Args:
            site_id: Site identifier.

        Returns:
            Path to the escalation report.
        """
        state = self.get_state(site_id)
        state.tier6_escalated = True
        state.never_abandon_active = True
        # Reset exhausted flag — Never-Abandon overrides exhaustion
        state.exhausted = False

        TIER6_ESCALATION_DIR.mkdir(parents=True, exist_ok=True)
        report_path = TIER6_ESCALATION_DIR / f"{site_id}-{self._crawl_date}.json"

        report = {
            "escalation": "tier6_never_abandon",
            "site_id": site_id,
            "crawl_date": self._crawl_date,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "summary": {
                "total_attempts": state.total_attempts,
                "successful_urls": len(state.successful_urls),
                "failed_urls": len(state.failed_urls),
                "final_strategy": state.current_strategy,
                "final_round": state.current_round,
                "final_restart": state.current_restart,
                "retry_stats": state.retry_stats,
            },
            "failed_url_list": sorted(state.failed_urls),
            "retry_history": [a.to_dict() for a in state.retry_history[-100:]],
            "never_abandon": {
                "status": "ACTIVE",
                "max_cycles": NEVER_ABANDON_MAX_CYCLES,
                "strategies": ALTERNATIVE_STRATEGIES,
                "message": (
                    "Standard 90 attempts exhausted. Never-Abandon persistence "
                    "loop activated. System will cycle through alternative "
                    "strategies with exponential backoff until success."
                ),
            },
        }

        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)

        logger.warning(
            "never_abandon_activated site_id=%s total_attempts=%s failed=%s "
            "strategies=%s report=%s",
            site_id, state.total_attempts, len(state.failed_urls),
            len(ALTERNATIVE_STRATEGIES), str(report_path),
        )

        return report_path

    def get_never_abandon_strategy(self, site_id: str) -> tuple[str, float]:
        """Get the next alternative strategy and delay for Never-Abandon loop.

        Cycles through ALTERNATIVE_STRATEGIES with exponential backoff.

        Args:
            site_id: Site identifier.

        Returns:
            Tuple of (strategy_name, delay_seconds).
        """
        state = self.get_state(site_id)
        if not state.never_abandon_active:
            return ("standard", 0.0)

        # Cycle through strategies
        strategy = ALTERNATIVE_STRATEGIES[
            state.never_abandon_strategy_idx % len(ALTERNATIVE_STRATEGIES)
        ]
        state.never_abandon_strategy_idx += 1

        # Exponential backoff
        delay = min(
            NEVER_ABANDON_BASE_DELAY * (NEVER_ABANDON_BACKOFF_FACTOR ** state.never_abandon_cycle),
            NEVER_ABANDON_MAX_DELAY,
        )

        return (strategy, delay)

    def advance_never_abandon_cycle(self, site_id: str) -> bool:
        """Advance to the next Never-Abandon cycle.

        Returns:
            True if more cycles are allowed, False if cap reached.
            At cap, the site is logged for post-crawl failure diagnosis.
        """
        state = self.get_state(site_id)
        if state.never_abandon_cycle >= NEVER_ABANDON_MAX_CYCLES:
            logger.error(
                "never_abandon_cap_reached site_id=%s cycles=%s/%s "
                "— stopping retries, will generate failure report",
                site_id, state.never_abandon_cycle, NEVER_ABANDON_MAX_CYCLES,
            )
            return False  # Stop — record for diagnosis
        state.never_abandon_cycle += 1
        return True

    def get_retry_stats(self) -> dict[str, Any]:
        """Get aggregate retry statistics across all sites.

        Returns:
            Dictionary with per-level retry counts and per-site summaries.
        """
        total_stats = {"level1": 0, "level2": 0, "level3": 0, "level4": 0}
        site_summaries: dict[str, dict[str, Any]] = {}

        for site_id, state in self._states.items():
            stats = state.retry_stats
            for key, count in stats.items():
                total_stats[key] = total_stats.get(key, 0) + count

            site_summaries[site_id] = {
                "total_attempts": state.total_attempts,
                "successful": len(state.successful_urls),
                "failed": len(state.failed_urls),
                "exhausted": state.exhausted,
                "tier6_escalated": state.tier6_escalated,
                "retry_stats": stats,
            }

        return {
            "total_retry_counts": total_stats,
            "sites": site_summaries,
            "total_sites": len(self._states),
            "exhausted_sites": sum(1 for s in self._states.values() if s.exhausted),
            "tier6_escalated_sites": sum(
                1 for s in self._states.values() if s.tier6_escalated
            ),
        }
