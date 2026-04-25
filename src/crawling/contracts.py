"""Data contracts for the Crawling Layer.

Defines the RawArticle dataclass that serves as the output contract
from the Crawling Layer to the Analysis Layer. All crawled articles
are serialized as one JSON object per line in JSONL files at:
    data/raw/YYYY-MM-DD/{source_id}.jsonl

Reference: Step 5 Architecture Blueprint, Section 4.2.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# Phase 0.4: evidence_id derivation for CE1 Evidence Chain.
# _evidence_lib lives in .claude/hooks/scripts/ — import via sys.path insertion.
# The import is guarded: if unavailable (unusual), evidence_id is silently None
# rather than failing the crawler. Downstream validate_evidence_chain.py will
# catch missing IDs at the W1→W2 boundary gate.
_CONTRACTS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_CONTRACTS_DIR))
_HOOKS_SCRIPTS_DIR = os.path.join(
    _PROJECT_ROOT, ".claude", "hooks", "scripts"
)
if _HOOKS_SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _HOOKS_SCRIPTS_DIR)

try:
    from _evidence_lib import generate_evidence_id as _gen_evidence_id, EvidenceIDError
except ImportError:
    _gen_evidence_id = None  # type: ignore
    EvidenceIDError = Exception  # type: ignore


@dataclass(frozen=True)
class RawArticle:
    """Contract: Crawling Layer output -> Analysis Layer input.

    All fields must be populated before serialization. If title is empty
    or url is invalid, the article must be rejected at the boundary.

    Attributes:
        url: Canonical article URL (normalized, tracking params stripped).
        title: Article headline (required, non-empty).
        body: Full article body text. Empty string for paywall-truncated articles.
        source_id: Site identifier matching sources.yaml key (e.g., "chosun").
        source_name: Human-readable site name (e.g., "Chosun Ilbo").
        language: ISO 639-1 language code ("ko", "en", "zh", "ja", etc.).
        published_at: Publication datetime in UTC. None if extraction failed.
        crawled_at: Crawl timestamp in UTC (always present).
        author: Author name. None if unavailable.
        category: Article section/category. None if unavailable.
        content_hash: SHA-256 hash of normalized body text for deduplication.
        crawl_tier: Which escalation tier succeeded (1-5).
            1=RSS/Sitemap, 2=DOM, 3=Playwright, 4=Patchright stealth, 5=Adaptive.
        crawl_method: Extraction method: "rss", "sitemap", "dom", "playwright", "api", "adaptive".
        is_paywall_truncated: True if body is title-only due to hard paywall.
    """

    url: str
    title: str
    body: str
    source_id: str
    source_name: str
    language: str
    published_at: datetime | None
    crawled_at: datetime
    author: str | None = None
    category: str | None = None
    content_hash: str = ""
    crawl_tier: int = 1
    crawl_method: str = "rss"
    is_paywall_truncated: bool = False

    def compute_evidence_id(self) -> str | None:
        """Derive the Evidence Chain ID from immutable canonical fields.

        CE1 (stable 2-field formula): SHA256(normalized_url + "|" + published_at_iso).
        Returns None when published_at is absent (cannot produce a stable ID
        without a timestamp). Downstream validate_evidence_chain.py enforces
        presence at the W1→W2 boundary.
        """
        if self.published_at is None or _gen_evidence_id is None:
            return None
        try:
            return _gen_evidence_id(self.url, self.published_at.isoformat())
        except (EvidenceIDError, ValueError):
            return None

    def to_jsonl_dict(self) -> dict[str, Any]:
        """Serialize for JSONL output. Timestamps as ISO 8601 strings.

        Phase 0.4: includes `evidence_id` for G1 Evidence Chain propagation.
        The field is None for articles without published_at — downstream P1
        validation (scripts/execution/p1/evidence_chain.py --check generate)
        enforces presence at the W1 output boundary.

        Returns:
            Dictionary suitable for json.dumps serialization.
        """
        from urllib.parse import urlparse as _urlparse
        _parsed = _urlparse(self.url)
        source_domain = _parsed.netloc or ""
        return {
            "url": self.url,
            "source_domain": source_domain,
            "title": self.title,
            "body": self.body,
            "source_id": self.source_id,
            "source_name": self.source_name,
            "language": self.language,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "crawled_at": self.crawled_at.isoformat(),
            "author": self.author,
            "category": self.category,
            "content_hash": self.content_hash,
            "crawl_tier": self.crawl_tier,
            "crawl_method": self.crawl_method,
            "is_paywall_truncated": self.is_paywall_truncated,
            "evidence_id": self.compute_evidence_id(),
        }

    def to_jsonl_line(self) -> str:
        """Serialize to a single JSONL line (no trailing newline).

        Returns:
            JSON string suitable for appending to a JSONL file.
        """
        return json.dumps(self.to_jsonl_dict(), ensure_ascii=False)

    @staticmethod
    def from_jsonl_dict(data: dict[str, Any]) -> RawArticle:
        """Deserialize from a JSONL dictionary.

        Args:
            data: Dictionary parsed from a JSONL line.

        Returns:
            RawArticle instance.
        """
        pub_at = data.get("published_at")
        if pub_at and isinstance(pub_at, str):
            pub_at = datetime.fromisoformat(pub_at)

        crawled = data.get("crawled_at", "")
        if isinstance(crawled, str):
            crawled = datetime.fromisoformat(crawled)

        return RawArticle(
            url=data["url"],
            title=data["title"],
            body=data.get("body", ""),
            source_id=data["source_id"],
            source_name=data.get("source_name", ""),
            language=data.get("language", "en"),
            published_at=pub_at,
            crawled_at=crawled,
            author=data.get("author"),
            category=data.get("category"),
            content_hash=data.get("content_hash", ""),
            crawl_tier=data.get("crawl_tier", 1),
            crawl_method=data.get("crawl_method", "rss"),
            is_paywall_truncated=data.get("is_paywall_truncated", False),
        )


def compute_content_hash(body: str) -> str:
    """Compute SHA-256 hash of normalized body text.

    Normalization: lowercase, strip whitespace, collapse multiple spaces.

    Args:
        body: Raw article body text.

    Returns:
        Hex-encoded SHA-256 hash string. Empty string if body is empty.
    """
    if not body or not body.strip():
        return ""
    normalized = " ".join(body.lower().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


@dataclass
class DiscoveredURL:
    """A URL discovered during the URL discovery phase.

    Attributes:
        url: The normalized article URL.
        source_id: Site identifier.
        discovered_via: Discovery method ("rss", "sitemap", "dom", "playwright").
        published_at: Publication date from feed/sitemap. None if not available.
        title_hint: Title extracted from feed entry. None if not available.
        body_hint: Article body from RSS content:encoded/summary. None if not
            available or too short (<200 chars). Used by pipeline to skip HTTP
            fetch for sites whose article pages return 403 but RSS provides
            full content. Max 10KB to prevent memory bloat.
        author_hint: Author name from RSS dc:creator. None if not available.
        priority: Ordering priority (lower = higher priority). Default 0.
    """

    url: str
    source_id: str
    discovered_via: str = "rss"
    published_at: datetime | None = None
    title_hint: str | None = None
    body_hint: str | None = None
    author_hint: str | None = None
    priority: int = 0


@dataclass
class CrawlResult:
    """Result of crawling a single site.

    Attributes:
        source_id: Site identifier.
        articles: Successfully extracted articles.
        discovered_urls: Total URLs discovered.
        extracted_count: Number of articles successfully extracted.
        failed_count: Number of URLs that failed extraction.
        skipped_dedup_count: Number of URLs skipped due to deduplication.
        skipped_freshness_count: Number of articles dropped by the 24h lookback filter.
        elapsed_seconds: Total crawl time for this site.
        tier_used: Highest escalation tier used.
        errors: List of error messages encountered.
    """

    source_id: str
    articles: list[RawArticle] = field(default_factory=list)
    discovered_urls: int = 0
    extracted_count: int = 0
    failed_count: int = 0
    skipped_dedup_count: int = 0
    skipped_freshness_count: int = 0
    elapsed_seconds: float = 0.0
    tier_used: int = 1
    errors: list[str] = field(default_factory=list)
    deadline_yielded: bool = False
    # P1 structured error counts — eliminates string-matching hallucination.
    # Incremented at error creation point (pipeline._crawl_urls), not at judgment point.
    block_count: int = 0       # HTTP 403/451 — permanent bot blocks
    network_error_count: int = 0  # timeout/connection — transient failures
