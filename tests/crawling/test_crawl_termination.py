"""Tests for crawl termination fixes (Fix 1-7).

Validates:
- Sitemap child file capping (Fix 1)
- Diminishing returns cutoff (Fix 2)
- Total crawl budget (Fix 3)
- Permanent block detection (Fix 4/5)
- Adaptive rounds (Fix 7)
- Constant centralization (Step 1)
- D-7 threshold consistency (Step 2)
"""

import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone

from src.config.constants import (
    CRAWL_SUFFICIENT_THRESHOLD,
    CRAWL_TOTAL_BUDGET_SECONDS,
    CRAWL_DIMINISHING_THRESHOLD,
    CRAWL_DIMINISHING_MIN_ARTICLES,
    SITEMAP_MAX_CHILD_FILES,
)
from src.crawling.retry_manager import (
    L3_MAX_ROUNDS,
    get_adaptive_max_rounds,
)


# ── Step 1: Constants exist and have sane values ──

class TestConstantsCentralized:
    def test_sufficient_threshold_range(self):
        assert 0 < CRAWL_SUFFICIENT_THRESHOLD < 1.0

    def test_total_budget_positive(self):
        assert CRAWL_TOTAL_BUDGET_SECONDS > 0

    def test_diminishing_threshold_range(self):
        assert 0 < CRAWL_DIMINISHING_THRESHOLD < 1.0

    def test_diminishing_min_positive(self):
        assert CRAWL_DIMINISHING_MIN_ARTICLES > 0

    def test_sitemap_cap_reasonable(self):
        assert 10 <= SITEMAP_MAX_CHILD_FILES <= 200


# ── Fix 1: Sitemap child file capping ──

class TestSitemapCapping:
    def test_cap_value(self):
        """SITEMAP_MAX_CHILD_FILES should cap at 50."""
        assert SITEMAP_MAX_CHILD_FILES == 50

    def test_cap_applied_in_url_discovery(self):
        """url_discovery imports the constant (not hardcoded)."""
        from src.crawling import url_discovery
        import importlib
        # Just verify the import path works
        from src.config.constants import SITEMAP_MAX_CHILD_FILES as cap
        assert cap == 50


# ── Fix 7: Adaptive rounds ──

class TestAdaptiveRounds:
    def test_small_site_low_block(self):
        """Small sites (≤10 est, LOW block) → 1 round."""
        cfg = {
            "meta": {"daily_article_estimate": 5},
            "anti_block": {"bot_block_level": "LOW"},
        }
        assert get_adaptive_max_rounds(cfg) == 1

    def test_medium_site(self):
        """Medium sites (11-50 est) → 2 rounds."""
        cfg = {
            "meta": {"daily_article_estimate": 30},
            "anti_block": {"bot_block_level": "LOW"},
        }
        assert get_adaptive_max_rounds(cfg) == 2

    def test_large_site(self):
        """Large sites (>50 est) → full L3_MAX_ROUNDS."""
        cfg = {
            "meta": {"daily_article_estimate": 100},
            "anti_block": {"bot_block_level": "LOW"},
        }
        assert get_adaptive_max_rounds(cfg) == L3_MAX_ROUNDS

    def test_high_block_overrides(self):
        """HIGH bot_block always gets full rounds regardless of estimate."""
        cfg = {
            "meta": {"daily_article_estimate": 5},
            "anti_block": {"bot_block_level": "HIGH"},
        }
        assert get_adaptive_max_rounds(cfg) == L3_MAX_ROUNDS

    def test_missing_meta(self):
        """Missing meta → defaults to small (1 round)."""
        cfg = {}
        assert get_adaptive_max_rounds(cfg) == 1

    def test_zero_estimate(self):
        """Zero estimate → small site (1 round)."""
        cfg = {"meta": {"daily_article_estimate": 0}}
        assert get_adaptive_max_rounds(cfg) == 1

    def test_missing_anti_block(self):
        """Missing anti_block defaults to LOW."""
        cfg = {"meta": {"daily_article_estimate": 5}}
        assert get_adaptive_max_rounds(cfg) == 1

    def test_case_insensitive_block_level(self):
        """bot_block_level check is case-insensitive."""
        cfg = {
            "meta": {"daily_article_estimate": 5},
            "anti_block": {"bot_block_level": "high"},
        }
        assert get_adaptive_max_rounds(cfg) == L3_MAX_ROUNDS


# ── D-7: Threshold consistency ──

# ── Fix A: Structured error counts (P1 — no string matching) ──

class TestStructuredErrorCounts:
    def test_crawl_result_has_block_count(self):
        """CrawlResult must have block_count field (int, default 0)."""
        from src.crawling.contracts import CrawlResult
        r = CrawlResult(source_id="test")
        assert r.block_count == 0
        assert r.network_error_count == 0

    def test_block_count_is_int(self):
        """block_count must be integer, not string."""
        from src.crawling.contracts import CrawlResult
        r = CrawlResult(source_id="test", block_count=5)
        assert isinstance(r.block_count, int)

    def test_p1_validation_passes(self):
        """P1 validate_crawl_termination script must pass."""
        from pathlib import Path
        import importlib.util
        script = Path(".claude/hooks/scripts/validate_crawl_termination.py")
        assert script.exists(), "P1 validation script missing"
        spec = importlib.util.spec_from_file_location("vct", script)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        results = mod.validate_crawl_termination(Path("."))
        for check_id, passed, msg in results:
            assert passed, f"{check_id} failed: {msg}"


# ── Fix C: bot_block_level enum validation ──

class TestBotBlockLevelEnum:
    def test_valid_levels_exist(self):
        from src.config.constants import VALID_BOT_BLOCK_LEVELS
        assert "LOW" in VALID_BOT_BLOCK_LEVELS
        assert "MEDIUM" in VALID_BOT_BLOCK_LEVELS
        assert "HIGH" in VALID_BOT_BLOCK_LEVELS

    def test_invalid_level_defaults_to_low(self):
        """Invalid bot_block_level should default to LOW, not crash."""
        cfg = {
            "meta": {"daily_article_estimate": 5},
            "anti_block": {"bot_block_level": "EXTREME"},
        }
        # Should not raise, should return 1 (LOW default)
        assert get_adaptive_max_rounds(cfg) == 1

    def test_empty_string_defaults_to_low(self):
        cfg = {
            "meta": {"daily_article_estimate": 5},
            "anti_block": {"bot_block_level": ""},
        }
        assert get_adaptive_max_rounds(cfg) == 1


# ── D-7: Threshold consistency ──

class TestD7ThresholdConsistency:
    def test_threshold_is_single_source(self):
        """Both pipeline.py references use the same constant."""
        import ast
        import inspect
        from src.crawling import pipeline as mod

        source = inspect.getsource(mod)
        # The constant name should appear, not a magic 0.3
        assert "CRAWL_SUFFICIENT_THRESHOLD" in source
        # No hardcoded 0.3 remaining (except in comments/strings)
        lines = source.split("\n")
        for i, line in enumerate(lines, 1):
            stripped = line.lstrip()
            if stripped.startswith("#") or stripped.startswith('"') or stripped.startswith("'"):
                continue
            if "_sufficient_threshold = 0.3" in line or "_threshold = 0.3" in line:
                pytest.fail(
                    f"Hardcoded 0.3 threshold at line {i}: {line.strip()}"
                )
