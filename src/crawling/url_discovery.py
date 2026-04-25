"""3-Tier URL Discovery with External Fallback: RSS/Sitemap, DOM, Playwright,
Google News RSS, GDELT DOC API.

Discovers article URLs from news sites using a tiered fallback strategy:
    - Tier 1: RSS/Atom feeds and XML sitemaps (fastest, ~60-70% coverage)
    - Tier 2: DOM navigation with BeautifulSoup (CSS selectors on listing pages)
    - Tier 3: Playwright/Patchright dynamic rendering (JS-heavy sites)
    - Tier 1.5 (External Fallback): Google News RSS + GDELT DOC API
      Activated automatically when Tiers 1-3 yield insufficient URLs.
      These bypass site WAFs entirely by querying external services.

The discovery pipeline runs Tier 1 -> Tier 2 -> Tier 3 with deduplication at
each stage. If URLs are still below threshold, external fallbacks are tried.

Reference:
    Step 5 Architecture Blueprint, Layer 2 (Crawling Layer).
    Step 6 Crawling Strategies (Per-Site method assignments).
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse, parse_qs, urlencode

from src.crawling.contracts import DiscoveredURL
from src.crawling.network_guard import NetworkGuard
from src.utils.error_handler import ParseError, NetworkError

import logging

logger = logging.getLogger(__name__)

# XML namespaces for sitemap parsing
SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
SITEMAP_NEWS_NS = {"news": "http://www.google.com/schemas/sitemap-news/0.9"}

# Tracking parameters to strip during URL normalization
TRACKING_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "utm_source_platform", "utm_creative_format",
    "fbclid", "gclid", "gclsrc", "msclkid", "dclid",
    "mc_cid", "mc_eid", "oly_enc_id", "oly_anon_id",
    "_ga", "_gl", "_hsenc", "_hsmi", "__s",
    "ref", "referer", "referrer", "source",
    "amp", "amp_js_v", "usqp",
    "icid", "int_cmp", "clickid",
})

# RSS Content Extraction: minimum body length and max size cap
_MIN_RSS_BODY_HINT = 200     # chars — shorter is likely a teaser, not useful
_MAX_RSS_BODY_HINT = 10_000  # chars — prevent memory bloat from tag pages

# HTML tag stripping regex (lightweight alternative to BeautifulSoup for
# RSS content:encoded fields — avoids importing bs4 at discovery time)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_COLLAPSE_RE = re.compile(r"\s+")


def _extract_rss_body_hint(entry: dict) -> str | None:
    """Extract article body text from an RSS feed entry.

    Checks content:encoded first (usually full body), falls back to summary.
    Strips HTML tags and collapses whitespace. Returns None if result is
    shorter than _MIN_RSS_BODY_HINT.

    Args:
        entry: feedparser entry dict.

    Returns:
        Plain-text body string, or None if not substantial enough.
    """
    raw = ""
    # feedparser stores content:encoded in entry.content list
    content_list = entry.get("content", [])
    if content_list and isinstance(content_list, list):
        raw = content_list[0].get("value", "")

    # Fallback to summary/description
    if not raw or len(raw) < _MIN_RSS_BODY_HINT:
        summary = entry.get("summary", "")
        if len(summary) > len(raw):
            raw = summary

    if not raw or len(raw) < _MIN_RSS_BODY_HINT:
        return None

    # Strip HTML tags, then decode HTML entities (&amp; → &, &lt; → <)
    import html as html_mod
    text = _HTML_TAG_RE.sub(" ", raw)
    text = html_mod.unescape(text)
    text = _WHITESPACE_COLLAPSE_RE.sub(" ", text).strip()

    if len(text) < _MIN_RSS_BODY_HINT:
        return None

    # Cap at max to prevent memory bloat (e.g., VNExpress 3000+ tag pages)
    if len(text) > _MAX_RSS_BODY_HINT:
        text = text[:_MAX_RSS_BODY_HINT]

    return text


def _extract_xml_body_hint(
    item: ET.Element,
    ns_prefix: str = "",
) -> str | None:
    """Extract body text from an XML RSS/Atom element.

    For RSS 2.0 <item>: checks <content:encoded>, then <description>.
    For Atom <entry>: checks <content>, then <summary>.

    Args:
        item: XML element (RSS <item> or Atom <entry>).
        ns_prefix: Namespace prefix for Atom elements (e.g., "{http://...}").

    Returns:
        Plain-text body, or None if not substantial.
    """
    raw = ""

    # RSS 2.0: <content:encoded> (full body)
    content_ns = "{http://purl.org/rss/1.0/modules/content/}"
    encoded_el = item.find(f"{content_ns}encoded")
    if encoded_el is not None and encoded_el.text:
        raw = encoded_el.text

    # Atom: <content> or <summary>
    if not raw and ns_prefix:
        content_el = item.find(f"{ns_prefix}content")
        if content_el is not None and content_el.text:
            raw = content_el.text

    # RSS 2.0 fallback: <description>
    if not raw or len(raw) < _MIN_RSS_BODY_HINT:
        desc_tag = f"{ns_prefix}summary" if ns_prefix else "description"
        desc_el = item.find(desc_tag)
        if desc_el is not None and desc_el.text and len(desc_el.text) > len(raw):
            raw = desc_el.text

    if not raw or len(raw) < _MIN_RSS_BODY_HINT:
        return None

    # Strip HTML, decode entities, collapse whitespace, cap size
    import html as html_mod
    text = _HTML_TAG_RE.sub(" ", raw)
    text = html_mod.unescape(text)
    text = _WHITESPACE_COLLAPSE_RE.sub(" ", text).strip()

    if len(text) < _MIN_RSS_BODY_HINT:
        return None
    if len(text) > _MAX_RSS_BODY_HINT:
        text = text[:_MAX_RSS_BODY_HINT]

    return text


# ---------------------------------------------------------------------------
# URL Normalization
# ---------------------------------------------------------------------------

def normalize_url(url: str, base_url: str = "") -> str:
    """Normalize a URL by resolving relative paths, lowercasing host,
    stripping tracking parameters, and sorting remaining query params.

    Args:
        url: The URL to normalize (may be relative).
        base_url: Base URL for resolving relative URLs.

    Returns:
        Normalized absolute URL string, or empty string if URL is invalid.
    """
    if not url or not url.strip():
        return ""

    url = url.strip()

    # Resolve relative URLs
    if base_url and not url.startswith(("http://", "https://")):
        url = urljoin(base_url, url)

    # Must start with http:// or https://
    if not url.startswith(("http://", "https://")):
        return ""

    try:
        parsed = urlparse(url)
    except ValueError:
        return ""

    # Lowercase the hostname
    hostname = (parsed.hostname or "").lower()
    if not hostname:
        return ""

    # Strip www. prefix for consistency
    # (commented out -- some sites use www as a distinct subdomain)
    # hostname = hostname.removeprefix("www.")

    # Strip tracking parameters and sort remaining
    if parsed.query:
        params = parse_qs(parsed.query, keep_blank_values=False)
        filtered = {
            k: v for k, v in params.items()
            if k.lower() not in TRACKING_PARAMS
        }
        # Sort params for consistency
        sorted_query = urlencode(
            {k: v[0] if len(v) == 1 else v for k, v in sorted(filtered.items())},
            doseq=True,
        )
    else:
        sorted_query = ""

    # Strip fragment
    # Normalize path: remove trailing slash on paths (except root)
    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")

    # Reconstruct
    normalized = urlunparse((
        parsed.scheme,
        hostname + (f":{parsed.port}" if parsed.port and parsed.port not in (80, 443) else ""),
        path,
        "",  # params (rarely used)
        sorted_query,
        "",  # fragment stripped
    ))

    return normalized


_SOCIAL_MEDIA_DOMAINS: frozenset[str] = frozenset({
    # Pure social networks — never contain crawlable news articles
    "bsky.app", "x.com", "twitter.com", "t.co",
    "linkedin.com", "facebook.com", "instagram.com",
    "tiktok.com",
})


def is_article_url(url: str, source_url: str = "") -> bool:
    """Heuristic check if a URL is likely an article page vs navigation/category.

    Filters out URLs that are clearly not articles (homepage, category pages,
    image URLs, JS/CSS assets, etc.).

    Args:
        url: The URL to check.
        source_url: The site's base URL for context.

    Returns:
        True if the URL looks like an article URL.
    """
    if not url:
        return False

    parsed = urlparse(url)
    path = parsed.path.lower()

    # Block social media domains — these are profiles/posts, never news articles
    hostname = parsed.hostname or ""
    if any(hostname == d or hostname.endswith("." + d) for d in _SOCIAL_MEDIA_DOMAINS):
        return False

    # Skip non-HTML resources
    non_article_extensions = (
        ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".ico",
        ".css", ".js", ".json", ".xml", ".rss", ".atom",
        ".pdf", ".zip", ".tar", ".gz",
        ".mp3", ".mp4", ".avi", ".mov",
        ".woff", ".woff2", ".ttf", ".eot",
    )
    if any(path.endswith(ext) for ext in non_article_extensions):
        return False

    # Skip common non-article paths
    non_article_paths = (
        "/tag/", "/tags/", "/category/", "/categories/",
        "/author/", "/authors/", "/search",
        "/login", "/signup", "/register", "/subscribe",
        "/about", "/contact", "/privacy", "/terms",
        "/sitemap", "/robots.txt", "/feed", "/rss",
        "/wp-admin", "/wp-login", "/wp-content",
    )
    if any(segment in path for segment in non_article_paths):
        return False

    # WordPress pagination: /page/N/ or /page/N (single integer segment after /page/).
    # Exclude these but NOT date-based /page/YYYYMM/ paths (e.g. globaltimes.cn).
    import re as _re
    if _re.search(r'/page/\d{1,4}(?:/|$)', path):
        return False

    # Very short paths are usually not articles (homepage, section pages)
    # e.g., "/", "/news/", "/politics/"
    # Exception: Ghost CMS / flat-slug CMSes use single-segment paths for articles
    # (e.g. 404media.co/article-slug-here/). Allow if the slug is long enough (≥20 chars)
    # to distinguish from section pages like "/technology/" or "/politics/".
    path_segments = [s for s in path.split("/") if s]
    if len(path_segments) < 2:
        if not path_segments:
            return False
        # Single segment: allow only if slug is long (Ghost-style flat article URL)
        if len(path_segments[0]) < 20:
            return False

    return True


# ---------------------------------------------------------------------------
# Tier 1: RSS/Atom Feed Parser
# ---------------------------------------------------------------------------

class RSSParser:
    """Parse RSS 2.0 and Atom feeds to extract article URLs.

    Uses feedparser library for robust feed parsing. Falls back to
    raw XML parsing if feedparser is not available.

    Args:
        network_guard: NetworkGuard instance for fetching feeds.
    """

    def __init__(self, network_guard: NetworkGuard) -> None:
        self._guard = network_guard

    def parse_feed(
        self,
        feed_url: str,
        source_id: str,
        max_age_days: int = 1,
    ) -> list[DiscoveredURL]:
        """Fetch and parse an RSS/Atom feed.

        Args:
            feed_url: URL of the RSS/Atom feed.
            source_id: Site identifier for rate limiting.
            max_age_days: Only include articles published within this many days.
                Defaults to 1 (24h lookback for daily execution).

        Returns:
            List of DiscoveredURL objects extracted from the feed.

        Raises:
            ParseError: If the feed cannot be parsed.
            NetworkError: If the feed cannot be fetched.
        """
        try:
            import feedparser
        except ImportError:
            logger.warning("feedparser_not_available source_id=%s", source_id)
            return self._parse_feed_raw(feed_url, source_id, max_age_days)

        try:
            response = self._guard.fetch(feed_url, site_id=source_id)
        except NetworkError as e:
            logger.error("rss_fetch_failed url=%s source_id=%s error=%s", feed_url, source_id, str(e))
            raise

        feed = feedparser.parse(response.text)

        if feed.bozo and not feed.entries:
            logger.warning(
                "rss_parse_error url=%s source_id=%s error=%s",
                feed_url, source_id,
                str(feed.bozo_exception) if hasattr(feed, "bozo_exception") else "unknown",
            )

        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
        results: list[DiscoveredURL] = []

        for entry in feed.entries:
            url = entry.get("link", "")
            if not url:
                continue

            normalized = normalize_url(url)
            if not normalized or not is_article_url(normalized):
                continue

            # Extract publication date
            pub_date = self._parse_feed_date(entry)

            # Apply freshness filter
            if pub_date and pub_date < cutoff:
                continue

            title_hint = entry.get("title", None)

            # Extract body from content:encoded or summary (RSS Content Extraction)
            body_hint = _extract_rss_body_hint(entry)
            author_hint = entry.get("author", None)

            results.append(DiscoveredURL(
                url=normalized,
                source_id=source_id,
                discovered_via="rss",
                published_at=pub_date,
                title_hint=title_hint,
                body_hint=body_hint,
                author_hint=author_hint,
                priority=0,
            ))

        logger.info(
            "rss_parsed url=%s source_id=%s entries_total=%s articles_found=%s",
            feed_url, source_id, len(feed.entries), len(results),
        )
        return results

    def _parse_feed_date(self, entry: Any) -> datetime | None:
        """Extract publication date from a feed entry.

        Args:
            entry: feedparser entry object.

        Returns:
            Parsed datetime in UTC, or None if not available.
        """
        # feedparser provides parsed time tuples
        for date_field in ("published_parsed", "updated_parsed", "created_parsed"):
            parsed_time = getattr(entry, date_field, None)
            if parsed_time:
                try:
                    import calendar
                    timestamp = calendar.timegm(parsed_time)
                    return datetime.fromtimestamp(timestamp, tz=timezone.utc)
                except (ValueError, OverflowError, TypeError):
                    continue

        # Fallback to string parsing
        for date_field in ("published", "updated", "created"):
            date_str = entry.get(date_field, "")
            if date_str:
                parsed = _parse_datetime_string(date_str)
                if parsed:
                    return parsed

        return None

    def parse_feed_from_text(
        self,
        xml_text: str,
        source_id: str,
        max_age_days: int = 1,
    ) -> list[DiscoveredURL]:
        """Parse RSS/Atom feed from raw XML text (no network fetch).

        Used by pipeline's bypass discovery fallback when DynamicBypassEngine
        fetches the feed HTML/XML via alternative strategies.

        Args:
            xml_text: Raw XML string of the RSS/Atom feed.
            source_id: Site identifier.
            max_age_days: Freshness filter.

        Returns:
            List of DiscoveredURL objects.
        """
        return self._parse_xml_text(xml_text, source_id, "bypass_rss")

    def _parse_feed_raw(
        self,
        feed_url: str,
        source_id: str,
        max_age_days: int = 1,
    ) -> list[DiscoveredURL]:
        """Fallback RSS parser using raw XML parsing (no feedparser dependency).

        Args:
            feed_url: URL of the RSS feed.
            source_id: Site identifier.
            max_age_days: Freshness filter.

        Returns:
            List of DiscoveredURL objects.
        """
        try:
            response = self._guard.fetch(feed_url, site_id=source_id)
        except NetworkError as e:
            logger.error("rss_raw_fetch_failed url=%s error=%s", feed_url, str(e))
            return []

        return self._parse_xml_text(response.text, source_id, "rss")

    def _parse_xml_text(
        self,
        xml_text: str,
        source_id: str,
        discovered_via: str,
    ) -> list[DiscoveredURL]:
        """Core XML parser for RSS 2.0 and Atom feeds.

        Shared by parse_feed_from_text() and _parse_feed_raw().

        Args:
            xml_text: Raw XML string.
            source_id: Site identifier.
            discovered_via: Discovery method tag for DiscoveredURL.

        Returns:
            List of DiscoveredURL objects.
        """
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as e:
            logger.warning("rss_xml_parse_error source_id=%s error=%s", source_id, str(e))
            return []

        results: list[DiscoveredURL] = []
        # RSS 2.0: <item><link>...<pubDate>...
        for item in root.iter("item"):
            link_el = item.find("link")
            if link_el is None or not link_el.text:
                continue
            url = normalize_url(link_el.text.strip())
            if not url or not is_article_url(url):
                continue

            title_el = item.find("title")
            title_hint = title_el.text.strip() if title_el is not None and title_el.text else None

            pub_el = item.find("pubDate")
            pub_date = None
            if pub_el is not None and pub_el.text:
                pub_date = _parse_datetime_string(pub_el.text.strip())

            # Extract body from <content:encoded> or <description>
            body_hint = _extract_xml_body_hint(item)
            creator_el = item.find("{http://purl.org/dc/elements/1.1/}creator")
            author_hint = (creator_el.text.strip()
                           if creator_el is not None and creator_el.text else None)

            results.append(DiscoveredURL(
                url=url,
                source_id=source_id,
                discovered_via=discovered_via,
                published_at=pub_date,
                title_hint=title_hint,
                body_hint=body_hint,
                author_hint=author_hint,
            ))

        # Atom: <entry><link href="..."><published>...
        for ns_prefix in ("", "{http://www.w3.org/2005/Atom}"):
            for entry in root.iter(f"{ns_prefix}entry"):
                link_el = entry.find(f"{ns_prefix}link")
                href = ""
                if link_el is not None:
                    href = link_el.get("href", "")
                if not href:
                    continue
                url = normalize_url(href)
                if not url or not is_article_url(url):
                    continue

                title_el = entry.find(f"{ns_prefix}title")
                title_hint = title_el.text.strip() if title_el is not None and title_el.text else None

                pub_el = entry.find(f"{ns_prefix}published")
                if pub_el is None:
                    pub_el = entry.find(f"{ns_prefix}updated")
                pub_date = None
                if pub_el is not None and pub_el.text:
                    pub_date = _parse_datetime_string(pub_el.text.strip())

                # Atom content body
                body_hint = _extract_xml_body_hint(entry, ns_prefix=ns_prefix)
                author_el = entry.find(f"{ns_prefix}author")
                author_hint = None
                if author_el is not None:
                    name_el = author_el.find(f"{ns_prefix}name")
                    if name_el is not None and name_el.text:
                        author_hint = name_el.text.strip()

                results.append(DiscoveredURL(
                    url=url,
                    source_id=source_id,
                    discovered_via=discovered_via,
                    published_at=pub_date,
                    title_hint=title_hint,
                    body_hint=body_hint,
                    author_hint=author_hint,
                ))

        logger.info("rss_xml_parsed source_id=%s via=%s articles_found=%s", source_id, discovered_via, len(results))
        return results


# ---------------------------------------------------------------------------
# Tier 1: Sitemap Parser
# ---------------------------------------------------------------------------

class SitemapParser:
    """Parse XML sitemaps (including sitemap index files) to discover article URLs.

    Handles:
        - Standard XML sitemaps (<urlset>)
        - Sitemap index files (<sitemapindex>)
        - Google News sitemaps (with news: namespace)
        - Date-based filtering (lastmod)

    Args:
        network_guard: NetworkGuard instance for fetching sitemaps.
    """

    def __init__(self, network_guard: NetworkGuard) -> None:
        self._guard = network_guard

    def parse_sitemap(
        self,
        sitemap_url: str,
        source_id: str,
        base_url: str = "",
        max_age_days: int = 1,
        max_urls: int = 5000,
        url_pattern: str | None = None,
    ) -> list[DiscoveredURL]:
        """Fetch and parse an XML sitemap, recursing into sitemap indexes.

        Args:
            sitemap_url: URL or path of the sitemap. If relative, resolved against base_url.
            source_id: Site identifier for rate limiting.
            base_url: Base URL for resolving relative sitemap URLs.
            max_age_days: Only include URLs with lastmod within this many days.
            max_urls: Maximum number of URLs to collect (prevents memory issues).
            url_pattern: Optional regex pattern to filter URLs (e.g., "/article/").

        Returns:
            List of DiscoveredURL objects from the sitemap.
        """
        # Resolve relative sitemap URL
        if not sitemap_url.startswith(("http://", "https://")):
            if base_url:
                sitemap_url = urljoin(base_url, sitemap_url)
            else:
                logger.warning("sitemap_relative_url url=%s source_id=%s", sitemap_url, source_id)
                return []

        try:
            response = self._guard.fetch(sitemap_url, site_id=source_id)
        except (NetworkError, Exception) as e:
            logger.error("sitemap_fetch_failed url=%s source_id=%s error=%s", sitemap_url, source_id, str(e))
            return []

        try:
            root = ET.fromstring(response.text)
        except ET.ParseError as e:
            logger.warning("sitemap_parse_error url=%s source_id=%s error=%s", sitemap_url, source_id, str(e))
            return []

        # Strip namespace for easier parsing
        tag = root.tag
        if "}" in tag:
            tag = tag.split("}")[-1]

        if tag == "sitemapindex":
            return self._parse_sitemap_index(
                root, source_id, base_url, max_age_days, max_urls, url_pattern
            )
        elif tag == "urlset":
            return self._parse_urlset(
                root, source_id, max_age_days, max_urls, url_pattern
            )
        else:
            logger.warning("sitemap_unknown_format url=%s root_tag=%s", sitemap_url, root.tag)
            return []

    def parse_sitemap_from_text(
        self,
        xml_text: str,
        source_id: str,
        base_url: str = "",
        max_age_days: int = 1,
        max_urls: int = 5000,
        url_pattern: str | None = None,
    ) -> list[DiscoveredURL]:
        """Parse a sitemap from raw XML text (no network fetch).

        Used by pipeline's bypass discovery fallback when DynamicBypassEngine
        fetches the sitemap via alternative strategies.

        Args:
            xml_text: Raw XML string of the sitemap.
            source_id: Site identifier.
            base_url: Base URL for resolving relative URLs.
            max_age_days: Freshness filter.
            max_urls: Maximum URLs to collect.
            url_pattern: Optional regex filter.

        Returns:
            List of DiscoveredURL objects.
        """
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as e:
            logger.warning("sitemap_text_parse_error source_id=%s error=%s", source_id, str(e))
            return []

        tag = root.tag
        if "}" in tag:
            tag = tag.split("}")[-1]

        if tag == "sitemapindex":
            # Cannot recurse child sitemaps without NetworkGuard — parse only
            # the index for child sitemap URLs (useful for logging/diagnosis).
            logger.info(
                "sitemap_from_text_index source_id=%s — index file, "
                "cannot recurse child sitemaps without network access",
                source_id,
            )
            return []
        elif tag == "urlset":
            return self._parse_urlset(
                root, source_id, max_age_days, max_urls, url_pattern,
            )
        else:
            logger.warning("sitemap_text_unknown_format source_id=%s root_tag=%s", source_id, root.tag)
            return []

    def _parse_sitemap_index(
        self,
        root: ET.Element,
        source_id: str,
        base_url: str,
        max_age_days: int,
        max_urls: int,
        url_pattern: str | None,
    ) -> list[DiscoveredURL]:
        """Parse a sitemap index file and recursively parse child sitemaps.

        Args:
            root: XML root element of the sitemap index.
            source_id: Site identifier.
            base_url: Base URL for resolving.
            max_age_days: Freshness filter.
            max_urls: Maximum total URLs.
            url_pattern: Optional URL filter pattern.

        Returns:
            Aggregated list of DiscoveredURL objects from all child sitemaps.
        """
        results: list[DiscoveredURL] = []
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)

        child_sitemaps: list[tuple[str, datetime | None]] = []

        for sitemap_el in root.iter():
            if sitemap_el.tag.endswith("sitemap"):
                loc_el = None
                lastmod_el = None
                for child in sitemap_el:
                    if child.tag.endswith("loc"):
                        loc_el = child
                    elif child.tag.endswith("lastmod"):
                        lastmod_el = child

                if loc_el is not None and loc_el.text:
                    child_url = loc_el.text.strip()
                    lastmod_dt = None
                    if lastmod_el is not None and lastmod_el.text:
                        lastmod_dt = _parse_datetime_string(lastmod_el.text.strip())

                    # Skip child sitemaps that are too old
                    if lastmod_dt and lastmod_dt < cutoff:
                        continue

                    # L2 heuristic: when lastmod is absent, infer date from URL.
                    # Patterns: sitemap-2024-01.xml, sitemap-202401.xml,
                    #           sitemap/2024/01, post-sitemap-2024-03.xml
                    # If the inferred date is older than cutoff, skip.
                    # If no date pattern matches, let it through (safe fallback).
                    if lastmod_dt is None:
                        url_date = _infer_date_from_sitemap_url(child_url)
                        if url_date is not None and url_date < cutoff:
                            logger.debug(
                                "sitemap_url_date_skip url=%s inferred=%s cutoff=%s",
                                child_url[:120], url_date.isoformat(), cutoff.isoformat(),
                            )
                            continue

                    child_sitemaps.append((child_url, lastmod_dt))

        logger.info(
            "sitemap_index_parsed source_id=%s child_sitemaps=%s",
            source_id, len(child_sitemaps),
        )

        # Parse each child sitemap (most recent first).
        # Cap child sitemaps to prevent sites with 500+ XML files
        # (e.g. n1info_ba) from spending hours scanning the entire archive.
        # Most news sites have <20 child sitemaps; 50 is generous.
        from src.config.constants import SITEMAP_MAX_CHILD_FILES
        MAX_CHILD_SITEMAPS = SITEMAP_MAX_CHILD_FILES
        child_sitemaps.sort(key=lambda x: x[1] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

        if len(child_sitemaps) > MAX_CHILD_SITEMAPS:
            logger.warning(
                "sitemap_index_capped source_id=%s total=%s cap=%s "
                "— scanning most recent %s only",
                source_id, len(child_sitemaps), MAX_CHILD_SITEMAPS, MAX_CHILD_SITEMAPS,
            )
            child_sitemaps = child_sitemaps[:MAX_CHILD_SITEMAPS]

        for child_url, _ in child_sitemaps:
            if len(results) >= max_urls:
                break
            remaining = max_urls - len(results)
            child_results = self.parse_sitemap(
                child_url, source_id, base_url=base_url,
                max_age_days=max_age_days, max_urls=remaining,
                url_pattern=url_pattern,
            )
            results.extend(child_results)

        return results

    def _parse_urlset(
        self,
        root: ET.Element,
        source_id: str,
        max_age_days: int,
        max_urls: int,
        url_pattern: str | None,
    ) -> list[DiscoveredURL]:
        """Parse a standard sitemap <urlset> element.

        Args:
            root: XML root element.
            source_id: Site identifier.
            max_age_days: Freshness filter.
            max_urls: Maximum URLs to collect.
            url_pattern: Optional regex URL filter.

        Returns:
            List of DiscoveredURL objects.
        """
        results: list[DiscoveredURL] = []
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
        compiled_pattern = re.compile(url_pattern) if url_pattern else None

        for url_el in root.iter():
            if not url_el.tag.endswith("url"):
                continue
            if len(results) >= max_urls:
                break

            loc_el = None
            lastmod_el = None
            news_pub_el = None

            for child in url_el:
                tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                if tag == "loc":
                    loc_el = child
                elif tag == "lastmod":
                    lastmod_el = child
                elif tag == "publication_date":
                    news_pub_el = child

            if loc_el is None or not loc_el.text:
                continue

            url = normalize_url(loc_el.text.strip())
            if not url or not is_article_url(url):
                continue

            # Apply URL pattern filter
            if compiled_pattern and not compiled_pattern.search(url):
                continue

            # Extract date (lastmod or news:publication_date)
            pub_date: datetime | None = None
            if news_pub_el is not None and news_pub_el.text:
                pub_date = _parse_datetime_string(news_pub_el.text.strip())
            elif lastmod_el is not None and lastmod_el.text:
                pub_date = _parse_datetime_string(lastmod_el.text.strip())

            # Freshness filter
            if pub_date and pub_date < cutoff:
                continue

            results.append(DiscoveredURL(
                url=url,
                source_id=source_id,
                discovered_via="sitemap",
                published_at=pub_date,
                priority=1,
            ))

        logger.info(
            "sitemap_urlset_parsed source_id=%s articles_found=%s",
            source_id, len(results),
        )
        return results


# ---------------------------------------------------------------------------
# Tier 2: DOM Navigation (BeautifulSoup)
# ---------------------------------------------------------------------------

class DOMNavigator:
    """Extract article URLs from HTML listing pages using CSS selectors.

    Navigates section/category pages and extracts article links using
    configurable CSS selectors from sources.yaml.

    Args:
        network_guard: NetworkGuard instance for fetching pages.
    """

    def __init__(self, network_guard: NetworkGuard) -> None:
        self._guard = network_guard

    def discover_from_page(
        self,
        page_url: str,
        source_id: str,
        article_link_selector: str = "a[href]",
        base_url: str = "",
        max_urls: int = 500,
    ) -> list[DiscoveredURL]:
        """Extract article URLs from a listing page.

        Args:
            page_url: URL of the listing/section page.
            source_id: Site identifier for rate limiting.
            article_link_selector: CSS selector for article links.
            base_url: Base URL for resolving relative links.
            max_urls: Maximum URLs to extract.

        Returns:
            List of DiscoveredURL objects.

        Raises:
            ParseError: If the page HTML cannot be parsed.
        """
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            logger.error("beautifulsoup4_not_available source_id=%s", source_id)
            return []

        try:
            response = self._guard.fetch(page_url, site_id=source_id)
        except NetworkError as e:
            logger.error("dom_fetch_failed url=%s source_id=%s error=%s", page_url, source_id, str(e))
            return []

        effective_base = base_url or str(urlparse(page_url)._replace(path="/", query="", fragment="").geturl())

        try:
            soup = BeautifulSoup(response.text, "html.parser")
        except Exception as e:
            raise ParseError(f"Failed to parse HTML from {page_url}: {e}", url=page_url)

        results: list[DiscoveredURL] = []
        seen: set[str] = set()

        links = soup.select(article_link_selector)

        for link in links:
            href = link.get("href", "")
            if not href or isinstance(href, list):
                continue
            if isinstance(href, list):
                href = href[0]

            url = normalize_url(str(href), base_url=effective_base)
            if not url or url in seen:
                continue
            if not is_article_url(url):
                continue

            seen.add(url)
            results.append(DiscoveredURL(
                url=url,
                source_id=source_id,
                discovered_via="dom",
                priority=2,
            ))

            if len(results) >= max_urls:
                break

        logger.info(
            "dom_links_extracted url=%s source_id=%s total_links=%s article_links=%s",
            page_url, source_id, len(links), len(results),
        )
        return results

    def discover_from_sections(
        self,
        sections: list[str],
        source_id: str,
        base_url: str,
        article_link_selector: str = "a[href]",
        max_urls_per_section: int = 100,
    ) -> list[DiscoveredURL]:
        """Discover article URLs across multiple section pages.

        Args:
            sections: List of section paths (e.g., ["/politics", "/economy"]).
            source_id: Site identifier.
            base_url: Base URL for constructing full section URLs.
            article_link_selector: CSS selector for article links.
            max_urls_per_section: Maximum URLs per section page.

        Returns:
            Deduplicated list of DiscoveredURL objects.
        """
        all_results: list[DiscoveredURL] = []
        seen_urls: set[str] = set()

        for section in sections:
            section_url = urljoin(base_url, section)
            results = self.discover_from_page(
                section_url,
                source_id=source_id,
                article_link_selector=article_link_selector,
                base_url=base_url,
                max_urls=max_urls_per_section,
            )

            for result in results:
                if result.url not in seen_urls:
                    seen_urls.add(result.url)
                    all_results.append(result)

        return all_results


# ---------------------------------------------------------------------------
# Tier 1.5 (External Fallback): Google News RSS Discovery
# ---------------------------------------------------------------------------

# Default timeout for external discovery services (seconds).
# Shorter than NetworkGuard's default because these are simple API calls
# to well-provisioned external services, not crawls of target sites.
_EXTERNAL_DISCOVERY_TIMEOUT = 15

# Default Google News RSS base URL.
GOOGLE_NEWS_RSS_BASE = "https://news.google.com/rss/search"

# Default GDELT DOC API base URL.
GDELT_DOC_API_BASE = "https://api.gdeltproject.org/api/v2/doc/doc"


class GoogleNewsDiscovery:
    """Discover article URLs via Google News RSS search.

    Queries ``news.google.com/rss/search?q=site:{domain}`` which returns
    an RSS feed of recent articles indexed by Google News for the given
    domain. This bypasses the target site's WAF entirely because the
    HTTP request goes to Google's servers, not the news site.

    IMPORTANT: This class intentionally does NOT use NetworkGuard.
    NetworkGuard's circuit breaker / rate limiter is per-site and the whole
    point of this class is to reach Google when the target site blocks us.
    We use feedparser (which fetches via urllib internally) for simplicity
    and to avoid any coupling to the target site's blocking state.

    Args:
        timeout: HTTP timeout in seconds for the Google News request.
        language: Language/region for Google News results (e.g., "en-US").
        country: Country code for Google News (e.g., "US").
    """

    def __init__(
        self,
        timeout: float = _EXTERNAL_DISCOVERY_TIMEOUT,
        language: str = "en-US",
        country: str = "US",
    ) -> None:
        self._timeout = timeout
        self._language = language
        self._country = country

    def discover(
        self,
        domain: str,
        source_id: str,
        max_age_days: int = 1,
        max_results: int = 100,
    ) -> list[DiscoveredURL]:
        """Query Google News RSS for articles from a specific domain.

        Constructs the URL:
            https://news.google.com/rss/search?q=site:{domain}+when:{max_age_days}d
            &hl={language}&gl={country}&ceid={country}:{lang_short}

        Args:
            domain: Target site domain (e.g., "www.chosun.com").
            source_id: Site identifier for the DiscoveredURL objects.
            max_age_days: Time window for the search (default 1 day).
            max_results: Maximum number of URLs to return.

        Returns:
            List of DiscoveredURL objects with discovered_via="google_news".
            Returns empty list on any error (never raises).
        """
        try:
            import feedparser
        except ImportError:
            logger.warning(
                "google_news_discovery_skip reason=feedparser_not_installed "
                "source_id=%s", source_id,
            )
            return []

        # Build Google News RSS search URL
        lang_short = self._language.split("-")[0]
        query = f"site:{domain}+when:{max_age_days}d"
        feed_url = (
            f"{GOOGLE_NEWS_RSS_BASE}?q={query}"
            f"&hl={self._language}&gl={self._country}"
            f"&ceid={self._country}:{lang_short}"
        )

        logger.info(
            "google_news_discovery_start source_id=%s domain=%s url=%s",
            source_id, domain, feed_url,
        )

        try:
            # feedparser handles HTTP fetching internally.
            # We set a socket timeout to avoid hanging indefinitely.
            import socket
            old_timeout = socket.getdefaulttimeout()
            socket.setdefaulttimeout(self._timeout)
            try:
                feed = feedparser.parse(feed_url)
            finally:
                socket.setdefaulttimeout(old_timeout)
        except Exception as e:
            logger.warning(
                "google_news_discovery_fetch_error source_id=%s error=%s",
                source_id, str(e),
            )
            return []

        if feed.bozo and not feed.entries:
            logger.warning(
                "google_news_discovery_parse_error source_id=%s error=%s",
                source_id,
                str(getattr(feed, "bozo_exception", "unknown")),
            )
            return []

        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
        results: list[DiscoveredURL] = []
        seen: set[str] = set()

        for entry in feed.entries:
            if len(results) >= max_results:
                break

            # Google News RSS wraps the real URL in a redirect.
            # The <link> field may be a Google redirect URL or the real URL.
            url = entry.get("link", "")
            if not url:
                continue

            # Google News RSS URLs are often Google redirect URLs like:
            # https://news.google.com/rss/articles/CBMi...
            # The actual source URL is in the <source url="..."> tag, which
            # feedparser exposes as entry.source.href or we can extract from
            # the link itself. For most queries, the link IS the real URL.
            # We also check the "source" field.
            source_href = ""
            if hasattr(entry, "source") and hasattr(entry.source, "href"):
                source_href = entry.source.href

            # Prefer the direct link; fall back to source href
            candidate_urls = [url]
            if source_href and source_href != url:
                candidate_urls.append(source_href)

            for candidate in candidate_urls:
                normalized = normalize_url(candidate)
                if not normalized or normalized in seen:
                    continue
                if not is_article_url(normalized):
                    continue

                # Verify the URL actually belongs to the target domain
                parsed_url = urlparse(normalized)
                url_domain = (parsed_url.hostname or "").lower()
                target_domain = domain.lower()
                if not (url_domain == target_domain or url_domain.endswith(f".{target_domain}")):
                    continue

                seen.add(normalized)

                # Extract publication date
                pub_date = self._parse_entry_date(entry)
                if pub_date and pub_date < cutoff:
                    continue

                title_hint = entry.get("title", None)

                results.append(DiscoveredURL(
                    url=normalized,
                    source_id=source_id,
                    discovered_via="google_news",
                    published_at=pub_date,
                    title_hint=title_hint,
                    priority=3,  # Lower priority than direct RSS/sitemap/DOM
                ))
                break  # Only add one URL per entry

        logger.info(
            "google_news_discovery_complete source_id=%s domain=%s "
            "entries=%s urls_found=%s",
            source_id, domain, len(feed.entries), len(results),
        )
        return results

    def _parse_entry_date(self, entry: Any) -> datetime | None:
        """Extract publication date from a feedparser entry.

        Args:
            entry: feedparser entry object.

        Returns:
            datetime in UTC, or None.
        """
        for date_field in ("published_parsed", "updated_parsed"):
            parsed_time = getattr(entry, date_field, None)
            if parsed_time:
                try:
                    import calendar
                    timestamp = calendar.timegm(parsed_time)
                    return datetime.fromtimestamp(timestamp, tz=timezone.utc)
                except (ValueError, OverflowError, TypeError):
                    continue

        for date_field in ("published", "updated"):
            date_str = entry.get(date_field, "")
            if date_str:
                parsed = _parse_datetime_string(date_str)
                if parsed:
                    return parsed

        return None


# ---------------------------------------------------------------------------
# Tier 1.5 (External Fallback): GDELT DOC API Discovery
# ---------------------------------------------------------------------------

class GDELTDiscovery:
    """Discover article URLs via the GDELT DOC API.

    Queries ``api.gdeltproject.org/api/v2/doc/doc`` for articles from a
    specific domain. GDELT continuously monitors news sources worldwide and
    maintains an extensive index of articles. This bypasses the target site's
    WAF entirely because the request goes to GDELT's API servers.

    IMPORTANT: This class intentionally does NOT use NetworkGuard.
    It uses curl_cffi (or falls back to urllib) to make HTTP requests
    directly to GDELT's API, completely independent of the target site's
    blocking state or circuit breaker.

    Args:
        timeout: HTTP timeout in seconds for the GDELT API request.
        max_records: Maximum records to request from GDELT (up to 250).
        timespan: Time window for the search (e.g., "48h", "24h").
    """

    def __init__(
        self,
        timeout: float = _EXTERNAL_DISCOVERY_TIMEOUT,
        max_records: int = 100,
        timespan: str = "48h",
    ) -> None:
        self._timeout = timeout
        self._max_records = min(max_records, 250)  # GDELT API limit
        self._timespan = timespan

    def discover(
        self,
        domain: str,
        source_id: str,
        max_age_days: int = 1,
        max_results: int = 100,
    ) -> list[DiscoveredURL]:
        """Query GDELT DOC API for articles from a specific domain.

        Constructs the URL:
            https://api.gdeltproject.org/api/v2/doc/doc?
            query=domain:{domain}&mode=artlist&maxrecords={n}
            &format=json&timespan={timespan}

        Args:
            domain: Target site domain (e.g., "bbc.com").
            source_id: Site identifier for DiscoveredURL objects.
            max_age_days: Used to adjust timespan if needed.
            max_results: Maximum URLs to return.

        Returns:
            List of DiscoveredURL objects with discovered_via="gdelt".
            Returns empty list on any error (never raises).
        """
        # Adjust timespan based on max_age_days
        timespan = self._timespan
        if max_age_days > 2:
            timespan = f"{max_age_days * 24}h"

        api_url = (
            f"{GDELT_DOC_API_BASE}?"
            f"query=domain:{domain}"
            f"&mode=artlist"
            f"&maxrecords={self._max_records}"
            f"&format=json"
            f"&timespan={timespan}"
        )

        logger.info(
            "gdelt_discovery_start source_id=%s domain=%s url=%s",
            source_id, domain, api_url,
        )

        json_text = self._fetch_api(api_url, source_id)
        if json_text is None:
            return []

        return self._parse_response(json_text, domain, source_id, max_age_days, max_results)

    def _fetch_api(self, api_url: str, source_id: str) -> str | None:
        """Fetch the GDELT API response, trying curl_cffi first, then urllib.

        Args:
            api_url: Full GDELT API URL.
            source_id: For logging.

        Returns:
            Response text, or None on failure.
        """
        # Try curl_cffi first (better TLS fingerprint, handles edge cases)
        try:
            from curl_cffi import requests as curl_requests
            resp = curl_requests.get(
                api_url,
                timeout=self._timeout,
                impersonate="chrome",
            )
            if resp.status_code == 200:
                return resp.text
            logger.warning(
                "gdelt_discovery_http_error source_id=%s status=%s",
                source_id, resp.status_code,
            )
            return None
        except ImportError:
            pass
        except Exception as e:
            logger.warning(
                "gdelt_discovery_curl_error source_id=%s error=%s",
                source_id, str(e),
            )
            # Fall through to urllib fallback

        # Fallback to urllib (always available)
        try:
            import urllib.request
            import urllib.error
            req = urllib.request.Request(
                api_url,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; NewsCrawler/1.0)",
                    "Accept": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                if resp.status == 200:
                    return resp.read().decode("utf-8", errors="replace")
                logger.warning(
                    "gdelt_discovery_urllib_error source_id=%s status=%s",
                    source_id, resp.status,
                )
                return None
        except Exception as e:
            logger.warning(
                "gdelt_discovery_fetch_error source_id=%s error=%s",
                source_id, str(e),
            )
            return None

    def _parse_response(
        self,
        json_text: str,
        domain: str,
        source_id: str,
        max_age_days: int,
        max_results: int,
    ) -> list[DiscoveredURL]:
        """Parse the GDELT DOC API JSON response.

        GDELT artlist format:
            {"articles": [{"url": "...", "title": "...", "seendate": "20240101T120000Z", ...}, ...]}

        Args:
            json_text: Raw JSON response text.
            domain: Target domain for URL validation.
            source_id: Site identifier.
            max_age_days: Freshness filter.
            max_results: Maximum URLs to return.

        Returns:
            List of DiscoveredURL objects.
        """
        import json as json_mod

        try:
            data = json_mod.loads(json_text)
        except (json_mod.JSONDecodeError, ValueError) as e:
            logger.warning(
                "gdelt_discovery_json_error source_id=%s error=%s",
                source_id, str(e),
            )
            return []

        articles = data.get("articles", [])
        if not isinstance(articles, list):
            logger.warning(
                "gdelt_discovery_unexpected_format source_id=%s type=%s",
                source_id, type(articles).__name__,
            )
            return []

        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
        results: list[DiscoveredURL] = []
        seen: set[str] = set()

        for article in articles:
            if len(results) >= max_results:
                break

            if not isinstance(article, dict):
                continue

            url = article.get("url", "")
            if not url:
                continue

            normalized = normalize_url(url)
            if not normalized or normalized in seen:
                continue
            if not is_article_url(normalized):
                continue

            # Verify domain ownership
            parsed_url = urlparse(normalized)
            url_domain = (parsed_url.hostname or "").lower()
            target_domain = domain.lower()
            if not (url_domain == target_domain or url_domain.endswith(f".{target_domain}")):
                continue

            seen.add(normalized)

            # Parse GDELT's seendate format: "20240101T120000Z"
            pub_date = self._parse_gdelt_date(article.get("seendate", ""))
            if pub_date and pub_date < cutoff:
                continue

            title_hint = article.get("title", None)
            # GDELT sometimes includes HTML entities in titles
            if title_hint:
                try:
                    import html as html_mod
                    title_hint = html_mod.unescape(title_hint)
                except Exception:
                    pass

            results.append(DiscoveredURL(
                url=normalized,
                source_id=source_id,
                discovered_via="gdelt",
                published_at=pub_date,
                title_hint=title_hint,
                priority=4,  # Lower priority than google_news (3)
            ))

        logger.info(
            "gdelt_discovery_complete source_id=%s domain=%s "
            "raw_articles=%s urls_found=%s",
            source_id, domain, len(articles), len(results),
        )
        return results

    @staticmethod
    def _parse_gdelt_date(date_str: str) -> datetime | None:
        """Parse GDELT's seendate format.

        GDELT uses the format ``YYYYMMDDTHHMMSSZ`` (compact ISO 8601).

        Args:
            date_str: Date string from GDELT API.

        Returns:
            datetime in UTC, or None if parsing fails.
        """
        if not date_str:
            return None

        # GDELT compact format: "20240315T143000Z"
        try:
            dt = datetime.strptime(date_str, "%Y%m%dT%H%M%SZ")
            return dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            pass

        # Fallback to general parser
        return _parse_datetime_string(date_str)


# ---------------------------------------------------------------------------
# URL Discovery Pipeline
# ---------------------------------------------------------------------------

class URLDiscovery:
    """Orchestrates the multi-tier URL discovery pipeline with external fallbacks.

    Runs Tier 1 (RSS/Sitemap) -> Tier 2 (DOM) -> Tier 3 (Playwright) with
    deduplication at each stage. If URLs are still below the minimum threshold
    after all configured methods are tried, external fallback services
    (Google News RSS and GDELT DOC API) are automatically activated.

    The external fallbacks bypass site WAFs entirely because they query
    Google/GDELT servers, not the target site. They use their own HTTP
    clients (feedparser/curl_cffi/urllib) independent of NetworkGuard.

    Args:
        network_guard: NetworkGuard instance for HTTP requests.
        min_urls_threshold: Minimum URLs to discover before stopping.
            If a tier produces fewer than this, the next tier is attempted.
        enable_external_fallback: Whether to try Google News / GDELT
            when primary discovery methods yield insufficient URLs.
            Default True.
    """

    def __init__(
        self,
        network_guard: NetworkGuard,
        min_urls_threshold: int = 5,
        enable_external_fallback: bool = True,
    ) -> None:
        self._guard = network_guard
        self._min_threshold = min_urls_threshold
        self._enable_external_fallback = enable_external_fallback
        self._rss_parser = RSSParser(network_guard)
        self._sitemap_parser = SitemapParser(network_guard)
        self._dom_navigator = DOMNavigator(network_guard)
        # External fallback services -- lazy-initialized, do NOT use NetworkGuard
        self._google_news = GoogleNewsDiscovery()
        self._gdelt = GDELTDiscovery()

    def discover(
        self,
        site_config: dict[str, Any],
        source_id: str,
        max_age_days: int = 1,
    ) -> list[DiscoveredURL]:
        """Run the full URL discovery pipeline for a site.

        Executes tiers in order based on the site's configured primary method
        and fallback methods. Deduplicates at each stage. If all configured
        methods yield fewer than ``min_urls_threshold`` URLs and external
        fallback is enabled, Google News RSS and GDELT DOC API are tried
        as automatic fallbacks.

        Flow:
            primary_method -> fallback_methods ->
            IF urls < min_threshold AND enable_external_fallback:
                -> Google News RSS -> GDELT DOC API -> done

        Args:
            site_config: Site configuration from sources.yaml.
            source_id: Site identifier.
            max_age_days: Only include articles published within this many days.

        Returns:
            Deduplicated list of DiscoveredURL objects.
        """
        crawl_config = site_config.get("crawl", {})
        primary_method = crawl_config.get("primary_method", "rss")
        fallback_methods = crawl_config.get("fallback_methods", [])
        base_url = site_config.get("url", "")

        # Build ordered method list
        methods = [primary_method] + [m for m in fallback_methods if m != primary_method]

        all_urls: list[DiscoveredURL] = []
        seen: set[str] = set()

        for method in methods:
            discovered = self._discover_by_method(
                method, site_config, source_id, base_url, max_age_days
            )

            # Deduplicate
            new_urls: list[DiscoveredURL] = []
            for url_obj in discovered:
                if url_obj.url not in seen:
                    seen.add(url_obj.url)
                    new_urls.append(url_obj)
                    all_urls.append(url_obj)

            logger.info(
                "discovery_tier_complete source_id=%s method=%s new_urls=%s total_urls=%s",
                source_id, method, len(new_urls), len(all_urls),
            )

            # If we have enough URLs, stop
            if len(all_urls) >= self._min_threshold:
                break

        # --- External Fallback Tier (Google News + GDELT) ---
        # Activated only when primary/fallback methods found insufficient URLs.
        # These bypass site WAFs by querying external services.
        if len(all_urls) < self._min_threshold and self._enable_external_fallback:
            domain = self._extract_domain(base_url)
            if domain:
                external_urls = self._discover_external(
                    domain, source_id, max_age_days, seen
                )
                all_urls.extend(external_urls)

        logger.info(
            "discovery_complete source_id=%s total_urls=%s methods_tried=%s "
            "external_fallback=%s",
            source_id, len(all_urls), len(methods),
            len(all_urls) >= self._min_threshold or not self._enable_external_fallback,
        )

        return all_urls

    def _discover_external(
        self,
        domain: str,
        source_id: str,
        max_age_days: int,
        seen: set[str],
    ) -> list[DiscoveredURL]:
        """Run external fallback discovery (Google News RSS + GDELT).

        Tries Google News first, then GDELT. Deduplicates against already-seen
        URLs. Stops early if the threshold is met after Google News.

        Args:
            domain: Target site domain.
            source_id: Site identifier.
            max_age_days: Freshness filter.
            seen: Set of already-discovered normalized URLs (mutated in place).

        Returns:
            List of new (deduplicated) DiscoveredURL objects from external sources.
        """
        new_urls: list[DiscoveredURL] = []

        # 1. Google News RSS
        logger.info(
            "external_fallback_start source_id=%s domain=%s service=google_news",
            source_id, domain,
        )
        try:
            gn_urls = self._google_news.discover(
                domain, source_id, max_age_days=max_age_days
            )
            for url_obj in gn_urls:
                if url_obj.url not in seen:
                    seen.add(url_obj.url)
                    new_urls.append(url_obj)
            logger.info(
                "external_fallback_google_news source_id=%s new_urls=%s",
                source_id, len([u for u in gn_urls if u.url in seen]),
            )
        except Exception as e:
            logger.warning(
                "external_fallback_google_news_error source_id=%s error=%s",
                source_id, str(e),
            )

        # Check if we need GDELT
        total_with_external = len(new_urls)  # only new ones from external
        if total_with_external >= self._min_threshold:
            return new_urls

        # 2. GDELT DOC API
        logger.info(
            "external_fallback_continue source_id=%s domain=%s service=gdelt",
            source_id, domain,
        )
        try:
            gdelt_urls = self._gdelt.discover(
                domain, source_id, max_age_days=max_age_days
            )
            for url_obj in gdelt_urls:
                if url_obj.url not in seen:
                    seen.add(url_obj.url)
                    new_urls.append(url_obj)
            logger.info(
                "external_fallback_gdelt source_id=%s new_urls=%s",
                source_id, len([u for u in gdelt_urls if u.url in seen]),
            )
        except Exception as e:
            logger.warning(
                "external_fallback_gdelt_error source_id=%s error=%s",
                source_id, str(e),
            )

        return new_urls

    @staticmethod
    def _extract_domain(base_url: str) -> str:
        """Extract domain from a base URL.

        Args:
            base_url: Site base URL (e.g., "https://www.bbc.com").

        Returns:
            Domain string (e.g., "www.bbc.com"), or empty string if invalid.
        """
        if not base_url:
            return ""
        try:
            parsed = urlparse(base_url)
            return (parsed.hostname or "").lower()
        except Exception:
            return ""

    def parse_feed_from_text(
        self, xml_text: str, source_id: str, max_age_days: int = 1,
    ) -> list[DiscoveredURL]:
        """Parse RSS/Atom feed from raw XML text (no network fetch).

        Public proxy for RSSParser.parse_feed_from_text — avoids callers
        needing to reach into private _rss_parser member.
        """
        return self._rss_parser.parse_feed_from_text(xml_text, source_id, max_age_days)

    def parse_sitemap_from_text(
        self,
        xml_text: str,
        source_id: str,
        base_url: str = "",
        max_age_days: int = 1,
        max_urls: int = 5000,
        url_pattern: str | None = None,
    ) -> list[DiscoveredURL]:
        """Parse sitemap XML from raw text (no network fetch).

        Public proxy for SitemapParser.parse_sitemap_from_text — avoids
        callers needing to reach into private _sitemap_parser member.
        """
        return self._sitemap_parser.parse_sitemap_from_text(
            xml_text, source_id, base_url, max_age_days, max_urls, url_pattern,
        )

    def _discover_by_method(
        self,
        method: str,
        site_config: dict[str, Any],
        source_id: str,
        base_url: str,
        max_age_days: int,
    ) -> list[DiscoveredURL]:
        """Dispatch to the appropriate discovery method.

        Args:
            method: Discovery method name ("rss", "sitemap", "dom", "playwright", "api").
            site_config: Site configuration.
            source_id: Site identifier.
            base_url: Site base URL.
            max_age_days: Freshness filter.

        Returns:
            List of DiscoveredURL objects from this method.
        """
        crawl_config = site_config.get("crawl", {})

        if method == "rss":
            rss_url = crawl_config.get("rss_url", "")
            if not rss_url:
                return []
            if not rss_url.startswith(("http://", "https://")):
                rss_url = urljoin(base_url, rss_url)
            all_results: list[DiscoveredURL] = []
            seen_urls: set[str] = set()
            # Primary RSS feed
            try:
                primary = self._rss_parser.parse_feed(rss_url, source_id, max_age_days)
                for u in primary:
                    if u.url not in seen_urls:
                        seen_urls.add(u.url)
                        all_results.append(u)
            except Exception as e:
                logger.warning("rss_discovery_failed source_id=%s error=%s error_type=%s", source_id, str(e), type(e).__name__)
            # Additional RSS feeds (rss_urls: list of section-specific feeds)
            for extra_url in crawl_config.get("rss_urls", []):
                if not extra_url.startswith(("http://", "https://")):
                    extra_url = urljoin(base_url, extra_url)
                try:
                    extras = self._rss_parser.parse_feed(extra_url, source_id, max_age_days)
                    for u in extras:
                        if u.url not in seen_urls:
                            seen_urls.add(u.url)
                            all_results.append(u)
                    logger.info("rss_extra_feed source_id=%s url=%s new_urls=%s",
                                source_id, extra_url, len([u for u in extras if u.url in seen_urls]))
                except Exception as e:
                    logger.warning("rss_extra_feed_failed source_id=%s url=%s error=%s",
                                   source_id, extra_url, str(e))
            return all_results

        elif method == "sitemap":
            # C-6 fix: support both sitemap_url (singular) and sitemap_urls (plural).
            # Many high-value sites (huffpost, bloomberg, buzzfeed) define multiple
            # specialized sitemaps in sitemap_urls that were previously ignored.
            sitemap_urls_plural = crawl_config.get("sitemap_urls", [])
            sitemap_url_singular = crawl_config.get("sitemap_url", "/sitemap.xml")

            # Build ordered URL list: plural entries first (more specific), then singular
            sitemap_urls_to_try: list[str] = []
            for s_url in sitemap_urls_plural:
                if s_url and s_url not in sitemap_urls_to_try:
                    sitemap_urls_to_try.append(s_url)
            if sitemap_url_singular and sitemap_url_singular not in sitemap_urls_to_try:
                sitemap_urls_to_try.append(sitemap_url_singular)

            all_sitemap_results: list[DiscoveredURL] = []
            seen_urls: set[str] = set()

            for s_url in sitemap_urls_to_try:
                try:
                    results = self._sitemap_parser.parse_sitemap(
                        s_url, source_id, base_url=base_url,
                        max_age_days=max_age_days,
                    )
                    for r in results:
                        if r.url not in seen_urls:
                            seen_urls.add(r.url)
                            all_sitemap_results.append(r)
                except Exception as e:
                    logger.warning(
                        "sitemap_discovery_failed source_id=%s url=%s error=%s",
                        source_id, s_url, str(e),
                    )
                    continue

            return all_sitemap_results

        elif method == "dom":
            # DOM navigation uses sections from config or just the base URL
            sections = crawl_config.get("sections", ["/"])
            article_link_selector = crawl_config.get("article_link_css", "a[href]")
            try:
                return self._dom_navigator.discover_from_sections(
                    sections, source_id, base_url,
                    article_link_selector=article_link_selector,
                )
            except Exception as e:
                logger.warning("dom_discovery_failed source_id=%s error=%s error_type=%s", source_id, str(e), type(e).__name__)
                return []

        elif method == "api":
            # API method is treated as RSS (many API feeds are RSS-compatible)
            rss_url = crawl_config.get("rss_url", "")
            if not rss_url:
                return []
            if not rss_url.startswith(("http://", "https://")):
                rss_url = urljoin(base_url, rss_url)
            try:
                return self._rss_parser.parse_feed(rss_url, source_id, max_age_days)
            except Exception as e:
                logger.warning("api_discovery_failed source_id=%s error=%s error_type=%s", source_id, str(e), type(e).__name__)
                return []

        elif method == "playwright":
            # Playwright discovery is a placeholder -- requires @anti-block-dev integration
            logger.info(
                "playwright_discovery_skipped source_id=%s reason=%s",
                source_id, "Playwright integration not yet available (Step 10 anti-block-dev task)",
            )
            return []

        else:
            logger.warning("unknown_discovery_method method=%s source_id=%s", method, source_id)
            return []


# ---------------------------------------------------------------------------
# Date parsing utilities
# ---------------------------------------------------------------------------

# Common datetime patterns found in RSS feeds, sitemaps, and articles
_DATE_PATTERNS = [
    # ISO 8601 variants
    (r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}", "%Y-%m-%dT%H:%M:%S%z"),
    (r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", "%Y-%m-%dT%H:%M:%SZ"),
    (r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", "%Y-%m-%dT%H:%M:%S"),
    (r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", "%Y-%m-%d %H:%M:%S"),
    (r"\d{4}-\d{2}-\d{2}", "%Y-%m-%d"),
]

_COMPILED_DATE_PATTERNS = [(re.compile(p), fmt) for p, fmt in _DATE_PATTERNS]


# ---------------------------------------------------------------------------
# L2 heuristic: infer date from sitemap URL when <lastmod> is absent.
# Matches: sitemap-2024-01.xml, sitemap-202401.xml, /2024/01/, /2024-03-news
# Returns the LAST DAY of the matched month (conservative — only skip if the
# entire month is before cutoff).
# ---------------------------------------------------------------------------
_SITEMAP_URL_DATE_RE = re.compile(
    r"(?:^|[/\-_.])(\d{4})[\-/]?(\d{2})(?=[/\-_.]|\.xml|$)"
)

# L2 heuristic: Unix epoch timestamp in sitemap query params.
# Matches: ?date_start=1143662400, ?date_end=1710000000
# Uses the larger timestamp (date_end if present, else date_start).
_SITEMAP_UNIX_TS_RE = re.compile(
    r"[?&]date_(?:start|end)=(\d{9,10})(?:&date_(?:start|end)=(\d{9,10}))?"
)


def _infer_date_from_sitemap_url(url: str) -> datetime | None:
    """Extract a date from a sitemap URL path for freshness filtering.

    Looks for YYYY-MM, YYYYMM, or Unix timestamp query params in the URL.
    For YYYY-MM patterns, returns the last day of that month (UTC) so that
    a sitemap is only skipped when its entire month is older than the cutoff.
    For Unix timestamps, returns the datetime of the larger timestamp
    (date_end if present).

    Args:
        url: Child sitemap URL string.

    Returns:
        datetime in UTC, or None if no pattern matched.
    """
    # Try YYYY-MM pattern first
    match = _SITEMAP_URL_DATE_RE.search(url)
    if match is not None:
        try:
            year, month = int(match.group(1)), int(match.group(2))
            if 1990 <= year <= 2100 and 1 <= month <= 12:
                if month == 12:
                    end_of_month = datetime(year + 1, 1, 1, tzinfo=timezone.utc) - timedelta(days=1)
                else:
                    end_of_month = datetime(year, month + 1, 1, tzinfo=timezone.utc) - timedelta(days=1)
                return end_of_month
        except (ValueError, OverflowError):
            pass

    # Try Unix epoch timestamp in query params (e.g. rg.ru sitemap)
    ts_match = _SITEMAP_UNIX_TS_RE.search(url)
    if ts_match is not None:
        try:
            ts1 = int(ts_match.group(1))
            ts2 = int(ts_match.group(2)) if ts_match.group(2) else ts1
            ts = max(ts1, ts2)  # Use the later timestamp
            if 946684800 <= ts <= 2147483647:  # 2000-01-01 to 2038-01-19
                return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (ValueError, OverflowError, OSError):
            pass

    return None


def _parse_datetime_string(date_str: str) -> datetime | None:
    """Parse a datetime string from various common formats.

    Normalizes to UTC. If no timezone info is present, assumes UTC.

    Args:
        date_str: Date string to parse.

    Returns:
        datetime in UTC, or None if parsing fails.
    """
    if not date_str or not date_str.strip():
        return None

    date_str = date_str.strip()

    # Try Python's built-in fromisoformat first (handles most ISO 8601)
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        pass

    # Try compiled patterns
    for pattern, fmt in _COMPILED_DATE_PATTERNS:
        match = pattern.search(date_str)
        if match:
            try:
                dt = datetime.strptime(match.group(), fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc)
            except (ValueError, TypeError):
                continue

    # RFC 2822 format (common in RSS)
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(date_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError, IndexError):
        pass

    return None
