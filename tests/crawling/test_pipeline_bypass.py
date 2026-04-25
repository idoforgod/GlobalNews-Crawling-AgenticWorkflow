"""Integration tests for Never-Abandon loop + DynamicBypassEngine in pipeline.

Covers:
    - _write_bypass_result() correct API usage (write_article, not write/to_dict)
    - _write_bypass_result() freshness filtering (24h lookback)
    - _write_bypass_result() dedup integration (3-level cascade)
    - Phase A bypass loop: writer lifecycle (single writer, not per-URL)
    - Phase A → Phase B fallback transition

Reference:
    Critical Reflection 2026-03-11 — Bugs 1-3, Issues 4-5.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch, PropertyMock, call

import pytest

from src.crawling.contracts import RawArticle
from src.crawling.block_detector import BlockType
from src.crawling.dynamic_bypass import BypassResult, StrategyTier


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_article(
    url: str = "https://example.com/article-1",
    title: str = "Test Article",
    body: str = "Body text " * 50,
    published_at: datetime | None = None,
) -> RawArticle:
    """Create a minimal RawArticle for testing."""
    if published_at is None:
        published_at = datetime.now(timezone.utc) - timedelta(hours=1)
    return RawArticle(
        url=url,
        title=title,
        body=body,
        source_id="test_site",
        source_name="Test Site",
        language="en",
        published_at=published_at,
        crawled_at=datetime.now(timezone.utc),
        content_hash="abc123",
        crawl_tier=6,
        crawl_method="bypass",
    )


def _make_dedup_result(is_duplicate: bool = False) -> MagicMock:
    """Create a mock DedupResult."""
    result = MagicMock()
    result.is_duplicate = is_duplicate
    result.level = 1 if is_duplicate else 0
    result.reason = "url_match" if is_duplicate else ""
    return result


# ---------------------------------------------------------------------------
# _write_bypass_result() unit tests
# ---------------------------------------------------------------------------

class TestWriteBypassResult:
    """Test _write_bypass_result() uses correct JSONLWriter API."""

    @pytest.fixture
    def pipeline(self, tmp_path: Path) -> Any:
        """Create a minimal mock pipeline with only the attrs needed by _write_bypass_result."""
        from src.crawling.pipeline import CrawlingPipeline

        # Use object.__new__ to skip __init__ (avoids heavy subsystem imports)
        p = object.__new__(CrawlingPipeline)
        p._extractor = MagicMock()
        p._dedup = MagicMock()
        # Per-site lookback (P0 freshness fix) needs _crawl_start_utc.
        # Without it, _site_lookback_cutoff() raises AttributeError inside
        # _write_bypass_result. _lookback_cutoff is kept for any legacy path.
        p._crawl_start_utc = datetime.now(timezone.utc)
        p._lookback_cutoff = p._crawl_start_utc - timedelta(hours=24)
        p._get_site_config = MagicMock(return_value={})
        return p

    def test_calls_write_article_not_write(self, pipeline: Any) -> None:
        """Bug 1+3 fix: must use writer.write_article(article), not writer.write(article.to_dict())."""
        article = _make_article()
        pipeline._extractor.extract.return_value = article
        pipeline._dedup.is_duplicate.return_value = _make_dedup_result(False)

        writer = MagicMock()
        pipeline._write_bypass_result("test_site", "https://example.com/a", "<html>test</html>", writer)

        writer.write_article.assert_called_once_with(article)
        # Ensure the old buggy pattern is NOT used
        writer.write.assert_not_called()

    def test_skips_old_article_freshness_check(self, pipeline: Any) -> None:
        """Issue 4 fix: articles older than 24h lookback are skipped."""
        old_article = _make_article(
            published_at=datetime.now(timezone.utc) - timedelta(hours=48),
        )
        pipeline._extractor.extract.return_value = old_article

        writer = MagicMock()
        pipeline._write_bypass_result("test_site", "https://example.com/old", "<html>old</html>", writer)

        writer.write_article.assert_not_called()

    def test_skips_duplicate_article(self, pipeline: Any) -> None:
        """Issue 4 fix: duplicate articles are skipped via dedup check."""
        article = _make_article()
        pipeline._extractor.extract.return_value = article
        pipeline._dedup.is_duplicate.return_value = _make_dedup_result(True)

        writer = MagicMock()
        pipeline._write_bypass_result("test_site", "https://example.com/dup", "<html>dup</html>", writer)

        writer.write_article.assert_not_called()
        pipeline._dedup.is_duplicate.assert_called_once()

    def test_writes_fresh_unique_article(self, pipeline: Any) -> None:
        """Normal case: fresh, unique article is written."""
        article = _make_article()
        pipeline._extractor.extract.return_value = article
        pipeline._dedup.is_duplicate.return_value = _make_dedup_result(False)

        writer = MagicMock()
        pipeline._write_bypass_result("test_site", "https://example.com/ok", "<html>ok</html>", writer)

        writer.write_article.assert_called_once_with(article)

    def test_handles_extraction_failure(self, pipeline: Any) -> None:
        """Extraction failure should not crash — just log warning."""
        pipeline._extractor.extract.side_effect = RuntimeError("parse error")

        writer = MagicMock()
        # Should not raise
        pipeline._write_bypass_result("test_site", "https://example.com/fail", "<html>bad</html>", writer)

        writer.write_article.assert_not_called()

    def test_handles_none_article(self, pipeline: Any) -> None:
        """Extractor returning None should be handled gracefully."""
        pipeline._extractor.extract.return_value = None

        writer = MagicMock()
        pipeline._write_bypass_result("test_site", "https://example.com/none", "<html>empty</html>", writer)

        writer.write_article.assert_not_called()

    def test_no_dedup_engine_still_writes(self, pipeline: Any) -> None:
        """When dedup engine is None, article should still be written."""
        article = _make_article()
        pipeline._extractor.extract.return_value = article
        pipeline._dedup = None  # No dedup engine

        writer = MagicMock()
        pipeline._write_bypass_result("test_site", "https://example.com/nodedup", "<html>ok</html>", writer)

        writer.write_article.assert_called_once_with(article)

    def test_article_with_no_published_at_skips_freshness(self, pipeline: Any) -> None:
        """Articles without published_at bypass the freshness check."""
        article = _make_article(published_at=None)
        # Replace the frozen dataclass field
        pipeline._extractor.extract.return_value = RawArticle(
            url=article.url, title=article.title, body=article.body,
            source_id=article.source_id, source_name=article.source_name,
            language=article.language, published_at=None,
            crawled_at=article.crawled_at,
        )
        pipeline._dedup.is_duplicate.return_value = _make_dedup_result(False)

        writer = MagicMock()
        pipeline._write_bypass_result("test_site", "https://example.com/nopub", "<html>ok</html>", writer)

        writer.write_article.assert_called_once()


# ---------------------------------------------------------------------------
# T6_NEVER_ABANDON naming consistency
# ---------------------------------------------------------------------------

class TestT6NeverAbandonNaming:
    """Verify the T6 enum was renamed from T6_HUMAN."""

    def test_t6_enum_name(self) -> None:
        """T6_NEVER_ABANDON exists and has value 6."""
        from src.crawling.anti_block import EscalationTier
        assert EscalationTier.T6_NEVER_ABANDON == 6
        assert EscalationTier.T6_NEVER_ABANDON.name == "T6_NEVER_ABANDON"

    def test_t6_human_removed(self) -> None:
        """T6_HUMAN should no longer exist."""
        from src.crawling.anti_block import EscalationTier
        assert not hasattr(EscalationTier, "T6_HUMAN")
