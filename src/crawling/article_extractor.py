"""Article content extraction with multi-library fallback chain.

Extracts article fields (title, body, date, author, metadata) from HTML pages
using a cascading extraction strategy:
    1. Fundus (high-precision, ~39 English outlets) -- optional, site-specific
    2. Trafilatura (general-purpose, fast, good boilerplate removal)
    3. Custom CSS selector fallback (per-site selectors from sources.yaml)

For hard-paywall sites (NYT, FT, WSJ, Bloomberg, Le Monde), a browser
renderer is attempted first (fresh browser context, no cookies) to bypass
metered paywalls. If rendering fails or content is still paywalled,
falls back to title-only extraction.

For URLs discovered via external services (Google News, GDELT) that return
403 on direct fetch, a cache/proxy extraction fallback chain is available:
    1. Google AMP CDN: cdn.ampproject.org/c/s/{url_without_scheme}
    2. Google Cache: webcache.googleusercontent.com/search?q=cache:{url}
    3. archive.today: archive.today/newest/{url}

Reference:
    Step 5 Architecture Blueprint, Section 4.2 (RawArticle contract).
    Step 6 Crawling Strategies (per-site extraction configurations).
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from src.crawling.contracts import RawArticle, compute_content_hash
from src.crawling.network_guard import NetworkGuard, FetchResponse
from src.utils.error_handler import ParseError, NetworkError

import logging

logger = logging.getLogger(__name__)

# Minimum body length to consider an extraction successful
MIN_BODY_LENGTH = 100

# Body length below which a paywall-capable site is flagged as truncated
PAYWALL_TRUNCATION_THRESHOLD = 200

# ---------------------------------------------------------------------------
# P1: Paywall text detection — strong/weak pattern classification
# ---------------------------------------------------------------------------
# Compiled once at module load. Patterns are split into STRONG (imperative,
# reader-directed: "Subscribe to unlock") and WEAK (factual, can appear in
# articles about subscriptions: "per month", "$4.99/month").
#
# Detection logic (NO ratio — ratio is structurally flawed for paywall pages
# because nav/footer/title text dilutes the fraction):
#   - strong >= 2 → definitive paywall (regardless of length)
#   - strong >= 1 AND body < 2000 chars → likely paywall
#
# This prevents false positives on real articles about subscription economy
# (which have strong=0, only weak matches) while catching all real paywall
# barrier pages (which always have imperative calls-to-action).
#
# Includes French patterns for Le Monde (1 of the 5 hard-paywall targets).
#
# Reference: Phase 0 V3 finding — FT 1064-char paywall text passed both
# MIN_BODY_LENGTH and the previous ratio-based check (ratio=0.34 < 0.4).
_STRONG_PAYWALL_PHRASES: list[re.Pattern[str]] = [
    re.compile(p, re.IGNORECASE)
    for p in [
        # English — imperative, reader-directed
        r"subscribe\s+to\s+(unlock|read|continue|access)",
        r"sign\s+in\s+to\s+(read|continue|access|view)",
        r"create\s+(an?\s+)?account\s+to",
        r"log\s+in\s+to\s+(read|continue|access)",
        r"this\s+(article|content|story)\s+is\s+(for\s+)?(subscribers?|members?)\s+only",
        r"register\s+to\s+(continue|read|access)",
        r"already\s+a\s+subscriber",
        r"start\s+your\s+(free\s+)?subscription",
        # NOTE: "keep reading.*free", "to continue reading", "want to read more"
        # are intentionally EXCLUDED from STRONG — they appear in normal English
        # writing ("analysts need to continue reading reports") and cause false
        # positives on short real articles. They are in WEAK below.
        # French — Le Monde paywall phrases
        r"r[eé]serv[eé]\s+aux\s+abonn[eé]s",
        r"abonnez[\s-]vous",
        r"connectez[\s-]vous\s+pour",
        r"d[eé]j[aà]\s+abonn[eé]",
        r"acc[eé]dez\s+[aà]\s+(cet|l[''])",
        r"cr[eé]ez?\s+un\s+compte",
    ]
]

_WEAK_PAYWALL_PHRASES: list[re.Pattern[str]] = [
    re.compile(p, re.IGNORECASE)
    for p in [
        # English — ambiguous phrases that appear in normal writing
        r"keep\s+reading.*free",
        r"to\s+continue\s+reading",
        r"want\s+to\s+read\s+more",
        # English — factual, can appear in articles about subscriptions
        r"premium\s+content",
        r"(free\s+)?trial\s+(offer|period|access)",
        r"unlimited\s+access",
        r"per\s+month|per\s+year|billed\s+(monthly|annually|yearly)",
        r"\$\d+[\./]\d{2}\s*(per\s+|/\s*)(month|year|week)",
        r"paywall",
        # French — factual
        r"offre\s+(d['']essai|sp[eé]ciale)",
        r"acc[eè]s\s+(illimit[eé]|num[eé]rique)",
        r"\d+[\s,]*[€]\s*(/|par)\s*(mois|an)",
    ]
]

# Body length below which a single strong match is sufficient.
# Real articles from major outlets are typically 2000+ chars.
_PAYWALL_SHORT_BODY_THRESHOLD = 2000


def is_paywall_body(text: str) -> bool:
    """P1: Deterministic check — is this text paywall/subscription prompt?

    Returns True if the text is likely paywall text rather than article content.
    Uses strong/weak pattern classification (no LLM judgment, no ratio).

    Decision logic:
    - strong matches >= 2 → definitive paywall (any length)
    - strong matches >= 1 AND body < 2000 chars → likely paywall
    - Otherwise → not paywall

    Strong patterns are imperative/reader-directed ("Subscribe to unlock").
    Weak patterns are factual ("per month") and are NOT used in the decision.
    This prevents false positives on articles about subscription economy.
    """
    if not text or len(text) < 50:
        return True  # Too short to be article content

    strong = sum(1 for p in _STRONG_PAYWALL_PHRASES if p.search(text))

    if strong >= 2:
        return True  # 2+ imperative paywall phrases → definitive

    if strong >= 1 and len(text) < _PAYWALL_SHORT_BODY_THRESHOLD:
        return True  # 1 strong phrase in short body → likely paywall

    return False


# ---------------------------------------------------------------------------
# Extraction result container
# ---------------------------------------------------------------------------

class ExtractionResult:
    """Container for extracted article fields before creating a RawArticle.

    This is a mutable container used during the extraction process.
    Once all fields are populated, ``to_raw_article()`` converts it
    to the immutable RawArticle contract.

    Attributes:
        url: Canonical article URL.
        title: Extracted article title.
        body: Extracted article body text.
        published_at: Publication datetime in UTC.
        author: Author name.
        category: Article category/section.
        language: ISO 639-1 language code.
        extraction_method: Which method succeeded ("fundus", "trafilatura", "css").
        confidence: Extraction confidence score (0.0-1.0).
    """

    def __init__(self, url: str = "", language: str = "en") -> None:
        self.url: str = url
        self.title: str = ""
        self.body: str = ""
        self.published_at: datetime | None = None
        self.author: str | None = None
        self.category: str | None = None
        self.language: str = language
        self.extraction_method: str = ""
        self.confidence: float = 0.0

    @property
    def is_complete(self) -> bool:
        """Check if mandatory fields (title + body or title-only) are present."""
        return bool(self.title and self.title.strip())

    @property
    def has_body(self) -> bool:
        """Check if body text was successfully extracted."""
        return bool(self.body and len(self.body.strip()) >= MIN_BODY_LENGTH)

    def to_raw_article(
        self,
        source_id: str,
        source_name: str,
        crawl_tier: int = 1,
        crawl_method: str = "rss",
        is_paywall: bool = False,
    ) -> RawArticle:
        """Convert to the immutable RawArticle contract.

        Args:
            source_id: Site identifier.
            source_name: Human-readable site name.
            crawl_tier: Escalation tier that succeeded.
            crawl_method: Discovery method used.
            is_paywall: Whether this is a paywall-truncated article.

        Returns:
            RawArticle instance.
        """
        body = self.body.strip() if self.body else ""
        is_truncated = is_paywall and len(body) < PAYWALL_TRUNCATION_THRESHOLD

        return RawArticle(
            url=self.url,
            title=self.title.strip(),
            body=body,
            source_id=source_id,
            source_name=source_name,
            language=self.language,
            published_at=self.published_at,
            crawled_at=datetime.now(timezone.utc),
            author=self.author,
            category=self.category,
            content_hash=compute_content_hash(body),
            crawl_tier=crawl_tier,
            crawl_method=crawl_method,
            is_paywall_truncated=is_truncated,
        )


# ---------------------------------------------------------------------------
# Extraction methods
# ---------------------------------------------------------------------------

def _extract_with_trafilatura(html: str, url: str) -> ExtractionResult:
    """Extract article content using Trafilatura.

    Trafilatura is the primary general-purpose extractor. It handles:
    - Boilerplate removal
    - Main content detection
    - Date extraction
    - Author extraction

    Args:
        html: Raw HTML content.
        url: Article URL for context.

    Returns:
        ExtractionResult with extracted fields.
    """
    result = ExtractionResult(url=url)

    try:
        import trafilatura
        from trafilatura.settings import use_config as traf_config
    except ImportError:
        logger.warning("trafilatura_not_available")
        return result

    try:
        # Configure trafilatura for maximum extraction
        config = traf_config()
        config.set("DEFAULT", "EXTRACTION_TIMEOUT", "30")

        extracted = trafilatura.extract(
            html,
            url=url,
            include_comments=False,
            include_tables=True,
            no_fallback=False,
            favor_recall=True,
            config=config,
            output_format="txt",
        )

        if extracted:
            # Unescape HTML entities left by trafilatura (&nbsp; &quot; &rsquo; etc.)
            import html as _html
            result.body = _html.unescape(extracted)
            result.extraction_method = "trafilatura"
            result.confidence = 0.8

        # Extract metadata separately
        metadata = trafilatura.extract_metadata(html, default_url=url)
        if metadata:
            if metadata.title:
                result.title = metadata.title
            if metadata.author:
                result.author = metadata.author
            if metadata.date:
                result.published_at = _parse_date_string(metadata.date)
            if metadata.categories:
                if isinstance(metadata.categories, list):
                    result.category = metadata.categories[0] if metadata.categories else None
                else:
                    result.category = str(metadata.categories)

        # Override published_at with OG/meta tag if available (more reliable
        # than trafilatura's heuristic date extraction).
        og_date = _extract_og_published_date(html)
        if og_date is not None:
            result.published_at = og_date

    except Exception as e:
        logger.warning("trafilatura_extraction_error url=%s error=%s", url, str(e))

    return result


def _extract_with_fundus(url: str, source_id: str) -> ExtractionResult:
    """Extract article content using Fundus.

    Fundus provides high-precision extraction for supported outlets (~39 English
    sites). It uses publisher-specific parsers for accurate field extraction.

    Note: Fundus fetches the page itself, so no pre-fetched HTML is needed.

    Args:
        url: Article URL.
        source_id: Site identifier (used to check if Fundus supports this site).

    Returns:
        ExtractionResult with extracted fields.
    """
    result = ExtractionResult(url=url)

    try:
        from fundus import PublisherCollection, Crawler as FundusCrawler
    except ImportError:
        logger.debug("fundus_not_available")
        return result

    # Fundus only supports specific publishers -- check if this site is supported
    # The actual integration would map source_id to Fundus publisher enum
    # For now, this serves as the integration point for Fundus-supported sites
    try:
        crawler = FundusCrawler(max_workers=1)
        for article in crawler.crawl(url):
            result.title = article.title or ""
            result.body = article.body or ""
            result.published_at = article.publishing_date
            if hasattr(article, "authors") and article.authors:
                result.author = ", ".join(str(a) for a in article.authors)
            result.extraction_method = "fundus"
            result.confidence = 0.95
            break  # Only need the first (and only) article

    except Exception as e:
        logger.debug("fundus_extraction_failed url=%s error=%s", url, str(e))

    return result


def _extract_with_css(
    html: str,
    url: str,
    selectors: dict[str, str],
) -> ExtractionResult:
    """Extract article content using custom CSS selectors.

    This is the fallback extractor when Trafilatura and Fundus both fail.
    It uses site-specific CSS selectors from sources.yaml.

    Args:
        html: Raw HTML content.
        url: Article URL for context.
        selectors: CSS selector mapping with keys:
            - title_css: Selector for title element
            - body_css: Selector for main content container
            - date_css: Selector for publication date
            - author_css: Selector for author name

    Returns:
        ExtractionResult with extracted fields.
    """
    result = ExtractionResult(url=url)

    try:
        from bs4 import BeautifulSoup
    except ImportError:
        logger.error("beautifulsoup4_not_available")
        return result

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception as e:
        logger.warning("css_parse_error url=%s error=%s", url, str(e))
        return result

    # Title extraction with fallback chain
    result.title = _extract_title(soup, selectors.get("title_css", ""))

    # Body extraction
    body_css = selectors.get("body_css", "")
    if body_css:
        body_el = soup.select_one(body_css)
        if body_el:
            # Remove unwanted elements
            for unwanted in body_el.select(
                "script, style, nav, aside, .ad, .advertisement, "
                ".related, .recommended, .comments, .social-share, "
                ".newsletter, footer, .sidebar"
            ):
                unwanted.decompose()

            result.body = body_el.get_text(separator="\n", strip=True)

    # Date extraction
    date_css = selectors.get("date_css", "")
    if date_css:
        date_el = soup.select_one(date_css)
        if date_el:
            date_text = date_el.get("datetime", "") or date_el.get_text(strip=True)
            result.published_at = _parse_date_string(str(date_text))

    # Author extraction
    author_css = selectors.get("author_css", "")
    if author_css:
        author_el = soup.select_one(author_css)
        if author_el:
            result.author = _clean_author(author_el.get_text(strip=True))

    if result.title or result.body:
        result.extraction_method = "css"
        result.confidence = 0.6

    return result


def _extract_with_arc_fusion(html: str, url: str) -> ExtractionResult:
    """Extract article content from Arc Publishing's Fusion.globalContent JSON.

    Arc Publishing (used by Chosun, WaPo, Chicago Tribune, etc.) renders
    articles client-side via JavaScript.  The server-side HTML contains no
    ``<p>`` tags, but the full article data is embedded as a JSON blob:

        ``Fusion.globalContent = { ... };``

    This function parses that JSON to extract title, body, date, author,
    and category — bypassing the empty DOM entirely.

    Args:
        html: Raw HTML content.
        url: Article URL for context.

    Returns:
        ExtractionResult with extracted fields.
    """
    result = ExtractionResult(url=url)

    # Match the Fusion.globalContent JSON assignment.
    # The JSON ends with ``};`` followed by another statement or </script>.
    m = re.search(
        r'Fusion\.globalContent\s*=\s*(\{.+?\})\s*;\s*(?:Fusion|var|window|<)',
        html,
        re.DOTALL,
    )
    if not m:
        return result

    try:
        data = json.loads(m.group(1))
    except (json.JSONDecodeError, ValueError):
        logger.debug("arc_fusion_json_parse_error url=%s", url)
        return result

    # --- Title ---
    headlines = data.get("headlines", {})
    title = headlines.get("basic", "") or ""
    if title:
        result.title = title.strip()

    # --- Body (content_elements) ---
    content_elements = data.get("content_elements", [])
    paragraphs: list[str] = []
    for elem in content_elements:
        if not isinstance(elem, dict):
            continue
        elem_type = elem.get("type", "")
        if elem_type == "text":
            raw_html = elem.get("content", "")
            # Strip HTML tags to get plain text
            plain = re.sub(r"<[^>]+>", "", raw_html).strip()
            if plain:
                paragraphs.append(plain)
        elif elem_type == "header":
            raw_html = elem.get("content", "")
            plain = re.sub(r"<[^>]+>", "", raw_html).strip()
            if plain:
                paragraphs.append(plain)
        elif elem_type == "list":
            for item in elem.get("items", []):
                if isinstance(item, dict):
                    raw_html = item.get("content", "")
                    plain = re.sub(r"<[^>]+>", "", raw_html).strip()
                    if plain:
                        paragraphs.append(f"- {plain}")

    if paragraphs:
        import html as _html
        result.body = _html.unescape("\n\n".join(paragraphs))

    # --- Published date ---
    display_date = data.get("display_date") or data.get("first_publish_date") or ""
    if display_date:
        result.published_at = _parse_date_string(str(display_date))

    # --- Author ---
    credits = data.get("credits", {})
    by_authors = credits.get("by", [])
    if by_authors:
        author_names = []
        for author in by_authors:
            if isinstance(author, dict):
                name = author.get("name", "")
                if name:
                    author_names.append(name)
        if author_names:
            result.author = ", ".join(author_names)

    # --- Category (taxonomy) ---
    taxonomy = data.get("taxonomy", {})
    primary_section = taxonomy.get("primary_section", {})
    section_name = primary_section.get("name", "") or primary_section.get("_id", "")
    if section_name:
        # Clean up section path (e.g., "/politics" -> "politics")
        result.category = section_name.strip("/").strip()
    elif taxonomy.get("tags"):
        tags = taxonomy["tags"]
        if isinstance(tags, list) and tags:
            first_tag = tags[0]
            if isinstance(first_tag, dict):
                result.category = first_tag.get("text", "") or first_tag.get("slug", "")
            elif isinstance(first_tag, str):
                result.category = first_tag

    if result.has_body or result.title:
        result.extraction_method = "arc_fusion"
        result.confidence = 0.9

    return result


def _extract_title(soup: Any, title_css: str = "") -> str:
    """Extract article title using a fallback chain.

    Fallback order:
        1. Custom CSS selector (if provided)
        2. og:title meta tag
        3. <title> tag
        4. First <h1> heading
        5. First heading of any level (h2-h6)

    Args:
        soup: BeautifulSoup object.
        title_css: Optional CSS selector for the title element.

    Returns:
        Extracted title string, or empty string if none found.
    """
    # 1. Custom CSS selector
    if title_css:
        el = soup.select_one(title_css)
        if el:
            text = el.get_text(strip=True)
            if text:
                return text

    # 2. og:title
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        return og["content"].strip()

    # 3. <title> tag
    title_tag = soup.find("title")
    if title_tag and title_tag.string:
        # Strip site name suffix (e.g., " - The New York Times")
        title_text = title_tag.string.strip()
        # Remove common separator patterns at the end
        for sep in (" | ", " - ", " :: ", " >> "):
            if sep in title_text:
                parts = title_text.split(sep)
                if len(parts) >= 2:
                    # Take the longest part (likely the article title)
                    title_text = max(parts, key=len).strip()
                    break
        return title_text

    # 4. First <h1>
    h1 = soup.find("h1")
    if h1:
        text = h1.get_text(strip=True)
        if text:
            return text

    # 5. Any heading
    for level in range(2, 7):
        heading = soup.find(f"h{level}")
        if heading:
            text = heading.get_text(strip=True)
            if text:
                return text

    return ""


def _extract_date_from_html(soup: Any, date_css: str = "") -> datetime | None:
    """Extract publication date from HTML using a fallback chain.

    Fallback order:
        1. Custom CSS selector (if provided)
        2. article:published_time meta tag
        3. og:published_time meta tag (non-standard but common)
        4. datePublished in JSON-LD
        5. <time> element with datetime attribute
        6. Various meta tag patterns

    Args:
        soup: BeautifulSoup object.
        date_css: Optional CSS selector for the date element.

    Returns:
        Parsed datetime in UTC, or None if not found.
    """
    # 1. Custom CSS selector
    if date_css:
        el = soup.select_one(date_css)
        if el:
            date_str = el.get("datetime", "") or el.get("content", "") or el.get_text(strip=True)
            dt = _parse_date_string(str(date_str))
            if dt:
                return dt

    # 2-3. Meta tags
    meta_properties = [
        "article:published_time",
        "og:article:published_time",
        "article:published",
        "og:pubdate",
        "pubdate",
        "date",
        "DC.date.issued",
        "sailthru.date",
    ]
    for prop in meta_properties:
        meta = soup.find("meta", property=prop) or soup.find("meta", attrs={"name": prop})
        if meta and meta.get("content"):
            dt = _parse_date_string(meta["content"])
            if dt:
                return dt

    # 4. JSON-LD datePublished
    for script in soup.find_all("script", type="application/ld+json"):
        if script.string:
            try:
                import json
                ld_data = json.loads(script.string)
                if isinstance(ld_data, list):
                    ld_data = ld_data[0] if ld_data else {}
                date_str = ld_data.get("datePublished", "")
                if date_str:
                    dt = _parse_date_string(date_str)
                    if dt:
                        return dt
            except (json.JSONDecodeError, AttributeError, IndexError, TypeError):
                continue

    # 5. <time> element
    time_el = soup.find("time", datetime=True)
    if time_el:
        dt = _parse_date_string(time_el["datetime"])
        if dt:
            return dt

    return None


def _extract_author_from_html(soup: Any, author_css: str = "") -> str | None:
    """Extract author name from HTML using a fallback chain.

    Fallback order:
        1. Custom CSS selector (if provided)
        2. article:author meta tag
        3. og:author meta tag (non-standard but common)
        4. JSON-LD author field
        5. Byline pattern matching
        6. Various meta tag patterns

    Args:
        soup: BeautifulSoup object.
        author_css: Optional CSS selector.

    Returns:
        Author name string, or None if not found.
    """
    # 1. Custom CSS selector
    if author_css:
        el = soup.select_one(author_css)
        if el:
            text = el.get_text(strip=True)
            if text:
                return _clean_author(text)

    # 2-3. Meta tags
    meta_properties = [
        "article:author",
        "og:article:author",
        "author",
        "DC.creator",
        "sailthru.author",
    ]
    for prop in meta_properties:
        meta = soup.find("meta", property=prop) or soup.find("meta", attrs={"name": prop})
        if meta and meta.get("content"):
            return _clean_author(meta["content"])

    # 4. JSON-LD author
    for script in soup.find_all("script", type="application/ld+json"):
        if script.string:
            try:
                import json
                ld_data = json.loads(script.string)
                if isinstance(ld_data, list):
                    ld_data = ld_data[0] if ld_data else {}
                author_data = ld_data.get("author")
                if isinstance(author_data, str):
                    return _clean_author(author_data)
                elif isinstance(author_data, dict):
                    name = author_data.get("name", "")
                    if name:
                        return _clean_author(name)
                elif isinstance(author_data, list):
                    names = [
                        a.get("name", "") if isinstance(a, dict) else str(a)
                        for a in author_data
                    ]
                    names = [n for n in names if n]
                    if names:
                        return ", ".join(names)
            except (json.JSONDecodeError, AttributeError, IndexError, TypeError):
                continue

    # 5. Byline pattern
    byline_selectors = [
        ".byline", ".author", ".article-author", ".writer",
        "[class*='byline']", "[class*='author']",
        "[rel='author']",
    ]
    for selector in byline_selectors:
        el = soup.select_one(selector)
        if el:
            text = el.get_text(strip=True)
            if text and len(text) < 200:  # sanity check
                return _clean_author(text)

    return None


def _clean_author(text: str) -> str:
    """Clean up an author string.

    Removes common prefixes like "By ", "Author: ", etc.

    Args:
        text: Raw author text.

    Returns:
        Cleaned author name.
    """
    text = text.strip()
    # Remove common prefixes
    for prefix in ("By ", "by ", "BY ", "Author: ", "author: ", "Written by ", "written by "):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
    # Remove trailing dates/timestamps
    text = re.sub(r"\s*\d{4}[-/]\d{2}[-/]\d{2}.*$", "", text)
    return text.strip()


# ---------------------------------------------------------------------------
# OG / meta tag date extraction
# ---------------------------------------------------------------------------

_OG_DATE_PROPERTIES = (
    "article:published_time",
    "article:published",
    "og:published_time",
    "date",
    "pubdate",
    "publishdate",
    "dc.date.issued",
)


def _extract_og_published_date(html: str) -> datetime | None:
    """Extract published date from HTML meta tags (OpenGraph / Dublin Core).

    OG ``article:published_time`` is an industry standard that most news sites
    populate accurately, making it more reliable than trafilatura's heuristic
    date detection which can pick up unrelated date strings on the page.

    Args:
        html: Raw HTML string.

    Returns:
        datetime in UTC, or None if no date meta tag found.
    """
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        return None

    for prop in _OG_DATE_PROPERTIES:
        tag = soup.find("meta", attrs={"property": prop}) or soup.find(
            "meta", attrs={"name": prop}
        )
        if tag and tag.get("content"):
            parsed = _parse_date_string(tag["content"])
            if parsed is not None:
                return parsed

    return None


# ---------------------------------------------------------------------------
# URL-based category extraction
# ---------------------------------------------------------------------------

# URL path segments that typically indicate a news section/category.
# Maps common slug patterns to a normalised category name.
_URL_CATEGORY_PATTERNS: dict[str, str] = {
    "politics": "politics",
    "economy": "economy",
    "finance": "economy",
    "business": "business",
    "society": "society",
    "international": "international",
    "world": "international",
    "global": "international",
    "culture": "culture",
    "entertainment": "entertainment",
    "sports": "sports",
    "sport": "sports",
    "opinion": "opinion",
    "editorial": "opinion",
    "tech": "technology",
    "technology": "technology",
    "science": "science",
    "lifestyle": "lifestyle",
    "health": "health",
    "education": "education",
    "national": "national",
    "us": "us",
    "europe": "europe",
    "asia": "asia",
    "middleeast": "middleeast",
    "africa": "africa",
}


def _extract_category_from_url(url: str) -> str | None:
    """Extract article category/section from the URL path.

    Many news sites encode the section in the URL path, e.g.:
      - ``/arti/society/...`` (Hani)
      - ``/international/us/...`` (Chosun)
      - ``/politics/congress/...`` (generic)

    This function walks the path segments and returns the first segment
    that matches a known category slug.

    Args:
        url: Article URL.

    Returns:
        Normalised category string, or None if no category detected.
    """
    path = urlparse(url).path.lower().strip("/")
    if not path:
        return None

    segments = path.split("/")

    # Skip common non-category prefixes
    skip = {"arti", "article", "articles", "news", "story", "stories", "arc",
            "outboundfeeds", "rss", "www", "en", "ko", "kr"}

    for seg in segments:
        if not seg or seg in skip:
            continue
        # Remove sub-section suffixes like "politics_general" -> "politics"
        base = seg.split("_")[0]
        if base in _URL_CATEGORY_PATTERNS:
            return _URL_CATEGORY_PATTERNS[base]

    return None


# ---------------------------------------------------------------------------
# Date parsing utility
# ---------------------------------------------------------------------------

def _parse_date_string(date_str: str) -> datetime | None:
    """Parse a date string to a UTC datetime.

    Delegates to url_discovery._parse_datetime_string but imported here
    to avoid circular dependencies.

    Args:
        date_str: Date string to parse.

    Returns:
        datetime in UTC, or None if parsing fails.
    """
    if not date_str or not date_str.strip():
        return None

    date_str = date_str.strip()

    # Try Python's fromisoformat first
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        pass

    # Common date patterns
    patterns = [
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
        "%B %d, %Y",
        "%b %d, %Y",
        "%d %B %Y",
        "%d %b %Y",
    ]
    for fmt in patterns:
        try:
            dt = datetime.strptime(date_str, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except (ValueError, TypeError):
            continue

    # RFC 2822
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(date_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError, IndexError):
        pass

    return None


# ---------------------------------------------------------------------------
# Cache / Proxy Extraction Fallback (for 403-blocked URLs)
# ---------------------------------------------------------------------------

# Timeout for cache/proxy fetches (seconds).
# These are external services so we use a moderate timeout.
_CACHE_PROXY_TIMEOUT = 20

# Discovery methods that indicate the URL was found via an external service
# and may not be directly accessible from our IP.
EXTERNAL_DISCOVERY_METHODS = frozenset({"google_news", "gdelt"})


def _fetch_via_google_amp(url: str, timeout: float = _CACHE_PROXY_TIMEOUT) -> str | None:
    """Attempt to fetch article HTML via Google's AMP CDN.

    Google AMP caches article content and serves it from Google's servers,
    bypassing the origin site's WAF. Only works for sites that have AMP
    versions of their articles.

    Constructs: https://cdn.ampproject.org/c/s/{url_without_scheme}

    Args:
        url: Original article URL.
        timeout: HTTP request timeout.

    Returns:
        HTML content string, or None if fetch fails or content is empty.
    """
    from urllib.parse import urlparse as _urlparse
    parsed = _urlparse(url)
    if not parsed.hostname:
        return None

    # Strip scheme, keep host + path + query
    url_without_scheme = url.split("://", 1)[-1]
    amp_url = f"https://cdn.ampproject.org/c/s/{url_without_scheme}"

    return _fetch_cache_url(amp_url, "google_amp", timeout)


def _fetch_via_google_cache(url: str, timeout: float = _CACHE_PROXY_TIMEOUT) -> str | None:
    """Attempt to fetch article HTML via Google's web cache.

    Google Cache stores snapshots of pages from its index. This works
    for pages that Google has recently crawled, even if the origin site
    now blocks our requests.

    Constructs: https://webcache.googleusercontent.com/search?q=cache:{url}

    Args:
        url: Original article URL.
        timeout: HTTP request timeout.

    Returns:
        HTML content string, or None if fetch fails or content is empty.
    """
    import urllib.parse
    encoded_url = urllib.parse.quote(url, safe="")
    cache_url = f"https://webcache.googleusercontent.com/search?q=cache:{encoded_url}"

    return _fetch_cache_url(cache_url, "google_cache", timeout)


def _fetch_via_archive_today(url: str, timeout: float = _CACHE_PROXY_TIMEOUT) -> str | None:
    """Attempt to fetch article HTML via archive.today.

    archive.today (formerly archive.is) creates snapshots of web pages.
    The /newest/ endpoint redirects to the most recent snapshot.

    Constructs: https://archive.today/newest/{url}

    Args:
        url: Original article URL.
        timeout: HTTP request timeout.

    Returns:
        HTML content string, or None if fetch fails or content is empty.
    """
    archive_url = f"https://archive.today/newest/{url}"

    return _fetch_cache_url(archive_url, "archive_today", timeout)


def _fetch_cache_url(
    cache_url: str,
    service_name: str,
    timeout: float,
) -> str | None:
    """Fetch HTML from a cache/proxy URL.

    Uses curl_cffi (with Chrome impersonation) for best compatibility,
    falls back to urllib if curl_cffi is not available.

    IMPORTANT: Does NOT use NetworkGuard -- these are external services
    that should not be affected by the target site's circuit breaker.

    Args:
        cache_url: Full URL of the cache/proxy endpoint.
        service_name: Name for logging (e.g., "google_amp").
        timeout: HTTP request timeout.

    Returns:
        HTML content string, or None on any failure.
    """
    # Try curl_cffi first
    try:
        from curl_cffi import requests as curl_requests
        resp = curl_requests.get(
            cache_url,
            timeout=timeout,
            impersonate="chrome",
            allow_redirects=True,
        )
        if resp.status_code == 200 and resp.text and len(resp.text) > 200:
            logger.info(
                "cache_proxy_fetch_ok service=%s url=%s content_len=%d",
                service_name, cache_url[:100], len(resp.text),
            )
            return resp.text
        logger.debug(
            "cache_proxy_fetch_empty service=%s url=%s status=%s len=%d",
            service_name, cache_url[:100], resp.status_code,
            len(resp.text) if resp.text else 0,
        )
        return None
    except ImportError:
        pass
    except Exception as e:
        logger.debug(
            "cache_proxy_curl_error service=%s url=%s error=%s",
            service_name, cache_url[:100], str(e),
        )

    # Fallback to urllib
    try:
        import urllib.request
        req = urllib.request.Request(
            cache_url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,*/*",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                html = resp.read().decode("utf-8", errors="replace")
                if html and len(html) > 200:
                    logger.info(
                        "cache_proxy_urllib_ok service=%s url=%s len=%d",
                        service_name, cache_url[:100], len(html),
                    )
                    return html
        return None
    except Exception as e:
        logger.debug(
            "cache_proxy_urllib_error service=%s url=%s error=%s",
            service_name, cache_url[:100], str(e),
        )
        return None


def fetch_via_cache_proxies(url: str) -> tuple[str | None, str]:
    """Try to fetch article HTML via cache/proxy services.

    Tries each service in order: Google AMP -> Google Cache -> archive.today.
    Returns on the first successful fetch.

    Args:
        url: Original article URL.

    Returns:
        Tuple of (html_content, service_name) where html_content is the
        fetched HTML or None if all services failed, and service_name is
        the name of the service that succeeded (or "" if none).
    """
    services = [
        (_fetch_via_google_amp, "google_amp"),
        (_fetch_via_google_cache, "google_cache"),
        (_fetch_via_archive_today, "archive_today"),
    ]

    for fetch_fn, name in services:
        try:
            html = fetch_fn(url)
            if html:
                return html, name
        except Exception as e:
            logger.debug(
                "cache_proxy_service_error service=%s url=%s error=%s",
                name, url[:100], str(e),
            )
            continue

    return None, ""


# ---------------------------------------------------------------------------
# ArticleExtractor
# ---------------------------------------------------------------------------

class ArticleExtractor:
    """Orchestrates article content extraction using a multi-library fallback chain.

    Extraction chain: Fundus -> Trafilatura -> CSS selectors (per-site).

    For hard-paywall sites, extraction is limited to title and metadata only.
    The ``is_paywall_truncated`` flag is set when the body is too short.

    Args:
        network_guard: NetworkGuard instance for fetching article pages.
        use_fundus: Whether to attempt Fundus extraction (default True).
        use_trafilatura: Whether to attempt Trafilatura extraction (default True).
    """

    def __init__(
        self,
        network_guard: NetworkGuard,
        use_fundus: bool = True,
        use_trafilatura: bool = True,
        browser_renderer: Any | None = None,
        adaptive_extractor: Any | None = None,
    ) -> None:
        self._guard = network_guard
        self._use_fundus = use_fundus
        self._use_trafilatura = use_trafilatura
        self._browser_renderer = browser_renderer
        self._adaptive_extractor = adaptive_extractor

    def extract(
        self,
        url: str,
        source_id: str,
        site_config: dict[str, Any],
        html: str | None = None,
        title_hint: str | None = None,
        discovered_via: str = "",
    ) -> RawArticle:
        """Extract article content from a URL.

        If ``html`` is provided, it is used directly. Otherwise the page is
        fetched via NetworkGuard. If the direct fetch fails with a 403/block
        error and the URL was discovered via an external service (Google News
        or GDELT), cache/proxy extraction is attempted automatically:
            1. Google AMP CDN
            2. Google Cache
            3. archive.today

        The extraction chain tries each method in order:
        1. Fundus (if available and the site is supported)
        2. Trafilatura (general-purpose)
        3. CSS selectors (site-specific fallback)

        Args:
            url: Article URL.
            source_id: Site identifier for logging and config.
            site_config: Site configuration from sources.yaml.
            html: Pre-fetched HTML content. If None, the page is fetched.
            title_hint: Title from RSS feed to use as fallback.
            discovered_via: How the URL was discovered (e.g., "rss", "google_news",
                "gdelt"). Used to decide whether cache proxy fallback is appropriate.

        Returns:
            RawArticle with extracted content.

        Raises:
            ParseError: If no extraction method can extract the title.
            NetworkError: If the page cannot be fetched and no html is provided
                (and cache proxy fallback either failed or was not attempted).
        """
        extraction = site_config.get("extraction", {})
        is_hard_paywall = extraction.get("paywall_type") == "hard"
        is_title_only = extraction.get("title_only", False)
        source_name = site_config.get("name", source_id)
        language = site_config.get("language", "en")
        charset = extraction.get("charset", "utf-8")
        used_cache_proxy = False
        cache_proxy_service = ""

        # Fetch HTML if not provided
        if html is None:
            try:
                if charset != "utf-8":
                    response = self._guard.fetch_with_encoding(
                        url, site_id=source_id, charset=charset
                    )
                else:
                    response = self._guard.fetch(url, site_id=source_id)
                html = response.text
            except (NetworkError, Exception) as e:
                # If fetch failed and the URL was discovered via an external
                # service (Google News / GDELT), try cache/proxy extraction
                # before giving up. The URL is known to exist (the external
                # service indexed it) but we cannot reach the origin site.
                #
                # For URLs from normal discovery (RSS, sitemap, DOM), we
                # let the error propagate so the anti-block escalation
                # system can handle it through its own retry chain.
                if discovered_via in EXTERNAL_DISCOVERY_METHODS:
                    logger.info(
                        "article_fetch_blocked_trying_cache url=%s source_id=%s "
                        "discovered_via=%s error=%s",
                        url, source_id, discovered_via, str(e),
                    )
                    cached_html, cache_proxy_service = fetch_via_cache_proxies(url)
                    if cached_html:
                        html = cached_html
                        used_cache_proxy = True
                        logger.info(
                            "cache_proxy_extraction_success url=%s source_id=%s "
                            "service=%s content_len=%d",
                            url, source_id, cache_proxy_service, len(html),
                        )
                    else:
                        logger.warning(
                            "cache_proxy_extraction_failed url=%s source_id=%s "
                            "all_services_exhausted=true",
                            url, source_id,
                        )
                        raise NetworkError(
                            f"Direct fetch and all cache proxies failed for {url}",
                            status_code=getattr(e, "status_code", None),
                            url=url,
                        )
                else:
                    logger.error(
                        "article_fetch_failed url=%s source_id=%s error=%s",
                        url, source_id, str(e),
                    )
                    raise

        # For hard-paywall / title-only sites, attempt browser rendering first
        if is_title_only or is_hard_paywall:
            rendered_html = self._try_browser_render(url, source_id)
            if rendered_html is not None:
                # Browser got HTML — run extraction chain on rendered content
                result = self._try_extraction_chain(
                    rendered_html, url, source_id, site_config
                )
                if result.body and len(result.body) >= MIN_BODY_LENGTH and not is_paywall_body(result.body):
                    # Successful extraction from rendered HTML
                    if not result.title and title_hint:
                        result.title = title_hint
                    if result.title:
                        result.language = language
                        if not result.category:
                            result.category = _extract_category_from_url(url)
                        logger.info(
                            "paywall_bypass_success url=%s body_len=%d",
                            url, len(result.body),
                        )
                        return result.to_raw_article(
                            source_id=source_id,
                            source_name=source_name,
                            crawl_tier=3,
                            crawl_method="playwright",
                            is_paywall=True,
                        )
                    # Title extraction failed — fall through to title_only
                else:
                    # Extraction chain failed — try adaptive extractor
                    adaptive_body = self._try_adaptive_extract(
                        rendered_html, source_id
                    )
                    if adaptive_body and len(adaptive_body) >= MIN_BODY_LENGTH and not is_paywall_body(adaptive_body):
                        if not result.title and title_hint:
                            result.title = title_hint
                        title = result.title
                        if not title:
                            try:
                                from bs4 import BeautifulSoup
                                title = _extract_title(BeautifulSoup(rendered_html, "html.parser"))
                            except Exception:
                                pass
                        if title:
                            logger.info(
                                "paywall_adaptive_success url=%s body_len=%d",
                                url, len(adaptive_body),
                            )
                            return RawArticle(
                                url=url,
                                title=title,
                                body=adaptive_body,
                                source_id=source_id,
                                source_name=source_name,
                                language=language,
                                published_at=result.published_at,
                                crawled_at=datetime.now(timezone.utc),
                                author=result.author,
                                category=result.category or _extract_category_from_url(url),
                                content_hash=compute_content_hash(adaptive_body),
                                crawl_tier=5,
                                crawl_method="adaptive",
                                is_paywall_truncated=False,
                            )
                    logger.info(
                        "paywall_bypass_insufficient url=%s body_len=%d",
                        url, len(result.body) if result.body else 0,
                    )
            # Browser rendering failed or content still paywalled — title only
            return self._extract_title_only(
                html, url, source_id, source_name, language, title_hint
            )

        # Try extraction chain
        result = self._try_extraction_chain(html, url, source_id, site_config)

        # Apply title hint from RSS if extraction failed to get title
        if not result.title and title_hint:
            result.title = title_hint

        # Validate mandatory fields
        if not result.title:
            logger.warning("extraction_no_title url=%s source_id=%s", url, source_id)
            raise ParseError(
                f"Failed to extract title from {url}",
                url=url,
            )

        # URL-based category fallback if no category was extracted
        if not result.category:
            result.category = _extract_category_from_url(url)

        # Set language
        result.language = language

        # Detect paywall truncation for soft-metered sites
        is_paywall = extraction.get("paywall_type", "none") != "none"

        # Determine crawl method -- include cache proxy info if used
        crawl_method = discovered_via if discovered_via else "rss"
        if used_cache_proxy and cache_proxy_service:
            crawl_method = f"{crawl_method}+{cache_proxy_service}"

        return result.to_raw_article(
            source_id=source_id,
            source_name=source_name,
            is_paywall=is_paywall,
            crawl_method=crawl_method,
        )

    def _try_extraction_chain(
        self,
        html: str,
        url: str,
        source_id: str,
        site_config: dict[str, Any],
    ) -> ExtractionResult:
        """Try each extraction method in the fallback chain.

        Order: Fundus -> Trafilatura -> CSS selectors.

        Args:
            html: Raw HTML content.
            url: Article URL.
            source_id: Site identifier.
            site_config: Site configuration.

        Returns:
            Best ExtractionResult from the chain.
        """
        best_result = ExtractionResult(url=url)

        # 1. Try Fundus (if enabled)
        if self._use_fundus:
            fundus_result = _extract_with_fundus(url, source_id)
            if fundus_result.has_body and fundus_result.title:
                logger.info(
                    "extraction_success url=%s source_id=%s method=%s confidence=%s",
                    url, source_id, "fundus", fundus_result.confidence,
                )
                return fundus_result
            if fundus_result.title and not best_result.title:
                best_result.title = fundus_result.title

        # 2. Try Trafilatura
        if self._use_trafilatura:
            traf_result = _extract_with_trafilatura(html, url)
            if traf_result.has_body:
                # Merge title if we already have one from Fundus
                if not traf_result.title and best_result.title:
                    traf_result.title = best_result.title
                if traf_result.title:
                    logger.info(
                        "extraction_success",
                        url=url,
                        source_id=source_id,
                        method="trafilatura",
                        confidence=traf_result.confidence,
                    )
                    return traf_result
            # Keep metadata even if body extraction failed
            if traf_result.title and not best_result.title:
                best_result.title = traf_result.title
            if traf_result.published_at and not best_result.published_at:
                best_result.published_at = traf_result.published_at
            if traf_result.author and not best_result.author:
                best_result.author = traf_result.author

        # 2.5. Try Arc Publishing Fusion.globalContent (Chosun, etc.)
        # Only attempt if trafilatura failed to get a body — the JSON blob
        # is embedded in the raw HTML for Arc-powered sites.
        if not best_result.has_body:
            fusion_result = _extract_with_arc_fusion(html, url)
            if fusion_result.has_body:
                # Merge any metadata we already have
                if not fusion_result.title and best_result.title:
                    fusion_result.title = best_result.title
                if not fusion_result.published_at and best_result.published_at:
                    fusion_result.published_at = best_result.published_at
                if not fusion_result.author and best_result.author:
                    fusion_result.author = best_result.author
                if fusion_result.title:
                    logger.info(
                        "extraction_success url=%s source_id=%s method=%s confidence=%s",
                        url, source_id, "arc_fusion", fusion_result.confidence,
                    )
                    return fusion_result
            # Keep any metadata from Fusion even if body extraction failed
            if fusion_result.title and not best_result.title:
                best_result.title = fusion_result.title
            if fusion_result.published_at and not best_result.published_at:
                best_result.published_at = fusion_result.published_at
            if fusion_result.author and not best_result.author:
                best_result.author = fusion_result.author
            if fusion_result.category and not best_result.category:
                best_result.category = fusion_result.category

        # 3. CSS selector fallback
        extraction_cfg = site_config.get("extraction", {})
        selectors = {
            "title_css": extraction_cfg.get("title_css", ""),
            "body_css": extraction_cfg.get("body_css", ""),
            "date_css": extraction_cfg.get("date_css", ""),
            "author_css": extraction_cfg.get("author_css", ""),
        }

        css_result = _extract_with_css(html, url, selectors)

        # Also try general HTML extraction for date/author if CSS missed them
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")

            if not css_result.published_at and not best_result.published_at:
                date_val = _extract_date_from_html(soup, selectors.get("date_css", ""))
                if date_val:
                    css_result.published_at = date_val

            if not css_result.author and not best_result.author:
                author_val = _extract_author_from_html(soup, selectors.get("author_css", ""))
                if author_val:
                    css_result.author = author_val

            # If CSS body extraction failed, try generic content extraction
            if not css_result.has_body:
                # Try article tag
                article_el = soup.find("article")
                if article_el:
                    for unwanted in article_el.select(
                        "script, style, nav, aside, .ad, .advertisement, "
                        ".related, .recommended, .comments, .social-share"
                    ):
                        unwanted.decompose()
                    body_text = article_el.get_text(separator="\n", strip=True)
                    if len(body_text) >= MIN_BODY_LENGTH:
                        css_result.body = body_text
                        css_result.extraction_method = "css_article_tag"
                        css_result.confidence = 0.5
        except ImportError:
            pass
        except Exception as e:
            logger.debug("css_fallback_error", url=url, error=str(e))

        # Merge the best results
        if css_result.has_body or css_result.title:
            if not css_result.title:
                css_result.title = best_result.title
            if not css_result.published_at:
                css_result.published_at = best_result.published_at
            if not css_result.author:
                css_result.author = best_result.author
            return css_result

        # Return whatever we have (may be incomplete)
        if not best_result.extraction_method:
            best_result.extraction_method = "partial"
            best_result.confidence = 0.3

        return best_result

    def _try_browser_render(self, url: str, source_id: str) -> str | None:
        """Attempt to render a URL using the browser renderer.

        Returns rendered HTML or None if renderer is unavailable or fails.
        This is a best-effort attempt — failure is expected and non-fatal.
        """
        if self._browser_renderer is None:
            return None
        try:
            html = self._browser_renderer.render(url, source_id=source_id)
            if html:
                logger.info(
                    "browser_render_ok url=%s source_id=%s len=%d",
                    url, source_id, len(html),
                )
            return html
        except Exception as e:
            logger.debug(
                "browser_render_error url=%s source_id=%s error=%s",
                url, source_id, str(e),
            )
            return None

    def _try_adaptive_extract(self, html: str, source_id: str) -> str | None:
        """Attempt adaptive extraction using CSS selectors and heuristics.

        Returns body text or None if adaptive extractor is unavailable or fails.
        """
        if self._adaptive_extractor is None:
            return None
        try:
            return self._adaptive_extractor.extract_body(html, source_id)
        except Exception as e:
            logger.debug(
                "adaptive_extract_error source_id=%s error=%s",
                source_id, str(e),
            )
            return None

    def _extract_title_only(
        self,
        html: str,
        url: str,
        source_id: str,
        source_name: str,
        language: str,
        title_hint: str | None,
        crawl_method: str = "rss",
    ) -> RawArticle:
        """Extract only title and metadata for hard-paywall sites.

        Args:
            html: Raw HTML content.
            url: Article URL.
            source_id: Site identifier.
            source_name: Human-readable site name.
            language: ISO 639-1 code.
            title_hint: Title from RSS/sitemap.
            crawl_method: How the URL was discovered (passed from caller).

        Returns:
            RawArticle with empty body and is_paywall_truncated=True.
        """
        title = title_hint or ""
        author: str | None = None
        published_at: datetime | None = None
        category: str | None = None

        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")

            if not title:
                title = _extract_title(soup)
            published_at = _extract_date_from_html(soup)
            author = _extract_author_from_html(soup)

        except ImportError:
            pass
        except Exception as e:
            logger.debug("title_only_extraction_error", url=url, error=str(e))

        if not title:
            raise ParseError(
                f"Failed to extract even title from paywall site: {url}",
                url=url,
            )

        logger.info(
            "title_only_extraction",
            url=url,
            source_id=source_id,
            title_length=len(title),
        )

        return RawArticle(
            url=url,
            title=title,
            body="",
            source_id=source_id,
            source_name=source_name,
            language=language,
            published_at=published_at,
            crawled_at=datetime.now(timezone.utc),
            author=author,
            category=category,
            content_hash="",
            crawl_tier=1,
            crawl_method=crawl_method,
            is_paywall_truncated=True,
        )
