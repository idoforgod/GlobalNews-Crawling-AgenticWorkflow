"""Deduplication engine for the GlobalNews crawling pipeline.

Prevents duplicate articles from entering the analysis pipeline using a
3-level cascade:

    Level 1 — URL normalization (exact match, O(1) hash lookup)
    Level 2 — Title similarity (Jaccard + Levenshtein, O(n) string ops)
    Level 3 — SimHash content fingerprint (Hamming distance ≤ 3 bits)

Each level short-circuits on a match so the more expensive operations are
only reached when cheaper ones produce no verdict.

Persistence: SQLite at data/dedup.sqlite with two tables:
    seen_urls     — normalized URL -> first-seen metadata
    content_hashes — SimHash fingerprint -> article metadata

Thread safety: each method acquires a threading.Lock before any SQLite
write operation.  Reads use separate connection objects created per-call
to avoid cross-thread cursor sharing issues.

Reference: Step 5 Architecture Blueprint, Section 3 (Dedup Engine);
           Step 9 (crawling infrastructure implementation).
"""

from __future__ import annotations

import ctypes
import hashlib
import re
import sqlite3
import threading
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from src.config.constants import DEDUP_SQLITE_PATH
from src.crawling.url_normalizer import URLNormalizer
from src.utils.logging_config import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# SimHash configuration
SIMHASH_BITS: int = 64          # 64-bit fingerprint
SIMHASH_THRESHOLD: int = 10     # Hamming distance ≤ 10 bits → near-duplicate (~84.4% similarity)
# Note: threshold tuned for SHA-256 token hashing (ADR-055). Empirically 10 bits
# captures single-word edits in article-length text while avoiding false positives
# on substantially different content (which scores >15 bits apart).

# Title similarity thresholds
TITLE_JACCARD_THRESHOLD: float = 0.8     # Jaccard on word tokens
TITLE_LEVENSHTEIN_RATIO: float = 0.2    # Normalized edit distance < 0.2

# Minimum body length to compute SimHash (very short bodies are unreliable)
MIN_BODY_LEN_FOR_SIMHASH: int = 50

# CJK Unicode ranges for character-level tokenization
_CJK_RANGES = [
    (0x4E00, 0x9FFF),   # CJK Unified Ideographs
    (0x3400, 0x4DBF),   # CJK Extension A
    (0xAC00, 0xD7AF),   # Hangul Syllables
    (0x3040, 0x309F),   # Hiragana
    (0x30A0, 0x30FF),   # Katakana
]


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DedupResult:
    """Result of a deduplication check.

    Attributes:
        is_duplicate: True if this article is a duplicate of a previously
            seen article.
        reason: Human-readable description of why it was flagged, or "unique"
            if it is not a duplicate.
        match_id: The article_id (or normalized URL) of the matched article,
            if is_duplicate is True.  None otherwise.
        level: The deduplication level that produced the verdict
            (1=URL, 2=Title, 3=SimHash, 0=not duplicate).
        confidence: Similarity score in [0.0, 1.0].  1.0 for exact URL match.
    """
    is_duplicate: bool
    reason: str
    match_id: Optional[str]
    level: int
    confidence: float

    @staticmethod
    def unique() -> "DedupResult":
        """Return a canonical 'not a duplicate' result."""
        return DedupResult(
            is_duplicate=False,
            reason="unique",
            match_id=None,
            level=0,
            confidence=0.0,
        )


# ---------------------------------------------------------------------------
# SimHash implementation
# ---------------------------------------------------------------------------

def _is_cjk(char: str) -> bool:
    """Return True if the character falls in a CJK / Hangul / Kana range."""
    cp = ord(char)
    return any(lo <= cp <= hi for lo, hi in _CJK_RANGES)


def _tokenize(text: str) -> list[str]:
    """Tokenize text into word-level or character-level tokens.

    For text dominated by CJK/Hangul/Kana characters, use character
    bi-grams (character-level shingling) because word boundaries are
    not whitespace-delimited.

    For Latin-script text, use word-level 3-grams (shingling).

    Args:
        text: Raw text, any language mix.

    Returns:
        List of token strings (may contain duplicates for frequency weighting).
    """
    if not text:
        return []

    # Normalize: lowercase, strip punctuation, normalize unicode
    text = unicodedata.normalize("NFKC", text).lower()
    text = re.sub(r"[^\w\s\u3000-\u9fff\uac00-\ud7af\u3040-\u30ff]", " ", text)

    # Detect dominant script: count CJK chars
    cjk_count = sum(1 for c in text if _is_cjk(c))
    total_alpha = sum(1 for c in text if c.isalpha())
    is_cjk_dominant = total_alpha > 0 and (cjk_count / total_alpha) > 0.4

    if is_cjk_dominant:
        # Character-level bi-grams for CJK text
        chars = [c for c in text if not c.isspace()]
        if len(chars) < 2:
            return chars
        return [chars[i] + chars[i + 1] for i in range(len(chars) - 1)]
    else:
        # Word-level 3-grams for Latin text
        words = text.split()
        if len(words) < 3:
            return words
        return [" ".join(words[i:i + 3]) for i in range(len(words) - 2)]


def _token_hash(token: str) -> int:
    """Hash a single token to a 64-bit integer using MD5.

    Args:
        token: String token.

    Returns:
        64-bit unsigned integer.
    """
    digest = hashlib.sha256(token.encode("utf-8")).digest()
    # Take first 8 bytes as a big-endian unsigned 64-bit integer
    return int.from_bytes(digest[:8], "big")


def compute_simhash(text: str) -> int:
    """Compute a 64-bit SimHash fingerprint for the given text.

    Algorithm (Charikar 2002):
        1. Tokenize text into features.
        2. For each feature, compute a 64-bit hash h.
        3. For each bit position i:
               if bit i of h is 1: v[i] += 1
               else:               v[i] -= 1
        4. Final fingerprint: bit i = 1 if v[i] > 0 else 0.

    Args:
        text: Article body text.

    Returns:
        64-bit SimHash as a Python int.  Returns 0 for empty text.
    """
    if not text or len(text.strip()) < MIN_BODY_LEN_FOR_SIMHASH:
        return 0

    tokens = _tokenize(text)
    if not tokens:
        return 0

    v = [0] * SIMHASH_BITS

    for token in tokens:
        h = _token_hash(token)
        for i in range(SIMHASH_BITS):
            # Check bit i (MSB = bit 63)
            bit = (h >> (SIMHASH_BITS - 1 - i)) & 1
            v[i] += 1 if bit else -1

    fingerprint = 0
    for i in range(SIMHASH_BITS):
        if v[i] > 0:
            fingerprint |= 1 << (SIMHASH_BITS - 1 - i)

    return fingerprint


def hamming_distance(a: int, b: int) -> int:
    """Count the number of differing bits between two 64-bit integers.

    Args:
        a: First 64-bit integer.
        b: Second 64-bit integer.

    Returns:
        Number of differing bits (0–64).
    """
    return bin(a ^ b).count("1")


def simhash_similarity(a: int, b: int) -> float:
    """Return similarity score in [0.0, 1.0] based on Hamming distance.

    score = 1.0 - (hamming_distance / SIMHASH_BITS)

    Args:
        a: First SimHash fingerprint.
        b: Second SimHash fingerprint.

    Returns:
        Similarity score. 1.0 = identical, 0.0 = completely different.
    """
    dist = hamming_distance(a, b)
    return 1.0 - (dist / SIMHASH_BITS)


# ---------------------------------------------------------------------------
# Title similarity
# ---------------------------------------------------------------------------

# Common site-name suffixes to strip before comparison
_SITE_SUFFIX_PATTERNS = re.compile(
    r"\s*[\|\-–—»:·•]\s*.{2,60}$",
    re.UNICODE,
)

# Common title prefixes (breaking news labels, etc.)
_TITLE_PREFIX_RE = re.compile(
    r"^(?:\[속보\]|\[단독\]|\[Breaking\]|\[Exclusive\]|\[Update\]|"
    r"\[BREAKING\]|\[EXCLUSIVE\]|속보|단독)\s*",
    re.UNICODE | re.IGNORECASE,
)


def _normalize_title(title: str) -> str:
    """Normalize a title for comparison.

    Steps:
        1. Strip leading/trailing whitespace.
        2. Remove common news prefixes ([속보], [Breaking], etc.).
        3. Remove site-name suffixes ("Title | Site Name").
        4. Lowercase.
        5. Collapse multiple spaces.

    Args:
        title: Raw article title.

    Returns:
        Normalized title string.
    """
    title = title.strip()
    title = _TITLE_PREFIX_RE.sub("", title)
    title = _SITE_SUFFIX_PATTERNS.sub("", title)
    title = title.lower().strip()
    title = re.sub(r"\s+", " ", title)
    return title


def _title_tokens(title: str) -> set[str]:
    """Return a set of word tokens from a normalized title.

    Handles both CJK and Latin text. For CJK, uses individual characters.

    Args:
        title: Normalized title string.

    Returns:
        Set of token strings.
    """
    cjk_count = sum(1 for c in title if _is_cjk(c))
    total_alpha = sum(1 for c in title if c.isalpha())
    is_cjk_dominant = total_alpha > 0 and (cjk_count / total_alpha) > 0.4

    if is_cjk_dominant:
        # Individual characters for CJK
        return {c for c in title if not c.isspace() and c.isalpha()}
    else:
        return set(title.split())


def _levenshtein_distance(s1: str, s2: str) -> int:
    """Compute Levenshtein (edit) distance between two strings.

    Uses the standard Wagner-Fischer DP algorithm.
    O(len(s1) * len(s2)) time and O(min(len(s1), len(s2))) space.

    Args:
        s1: First string.
        s2: Second string.

    Returns:
        Minimum edit operations (insert, delete, substitute) to transform s1 into s2.
    """
    if s1 == s2:
        return 0
    if not s1:
        return len(s2)
    if not s2:
        return len(s1)

    # Keep the shorter string as row to minimize memory
    if len(s1) > len(s2):
        s1, s2 = s2, s1

    n, m = len(s1), len(s2)
    prev = list(range(n + 1))
    curr = [0] * (n + 1)

    for j in range(1, m + 1):
        curr[0] = j
        for i in range(1, n + 1):
            cost = 0 if s1[i - 1] == s2[j - 1] else 1
            curr[i] = min(
                curr[i - 1] + 1,      # insertion
                prev[i] + 1,          # deletion
                prev[i - 1] + cost,   # substitution
            )
        prev, curr = curr, prev

    return prev[n]


def jaccard_similarity(set_a: set[str], set_b: set[str]) -> float:
    """Compute Jaccard coefficient between two sets.

    Args:
        set_a: First set of tokens.
        set_b: Second set of tokens.

    Returns:
        Jaccard similarity in [0.0, 1.0].  0.0 if both sets are empty.
    """
    if not set_a and not set_b:
        return 1.0  # Both empty titles are considered equal
    if not set_a or not set_b:
        return 0.0
    intersection = len(set_a & set_b)
    union = len(set_a | set_b)
    return intersection / union


def titles_are_similar(title_a: str, title_b: str) -> tuple[bool, float]:
    """Determine if two article titles refer to the same story.

    Uses a two-metric approach:
        1. Jaccard similarity on word/character token sets (threshold 0.8).
        2. Normalized Levenshtein distance (threshold 0.2).

    Either metric triggering is sufficient to declare titles similar (OR logic).
    This is intentionally permissive to minimize false negatives.

    Args:
        title_a: First article title (raw or normalized).
        title_b: Second article title (raw or normalized).

    Returns:
        Tuple of (is_similar: bool, confidence: float).
        confidence is in [0.0, 1.0] — the higher of the two metric scores.
    """
    norm_a = _normalize_title(title_a)
    norm_b = _normalize_title(title_b)

    if not norm_a or not norm_b:
        return False, 0.0

    # Exact match after normalization
    if norm_a == norm_b:
        return True, 1.0

    # Jaccard on token sets
    tokens_a = _title_tokens(norm_a)
    tokens_b = _title_tokens(norm_b)
    jacc = jaccard_similarity(tokens_a, tokens_b)

    # Normalized Levenshtein distance
    edit_dist = _levenshtein_distance(norm_a, norm_b)
    max_len = max(len(norm_a), len(norm_b))
    norm_edit = edit_dist / max_len if max_len > 0 else 0.0
    edit_similarity = 1.0 - norm_edit  # convert distance to similarity

    # Prefix matching (one title is a truncated version of the other)
    longer, shorter = (norm_a, norm_b) if len(norm_a) >= len(norm_b) else (norm_b, norm_a)
    prefix_match = (
        len(shorter) > 10
        and longer.startswith(shorter)
        and (len(shorter) / len(longer)) > 0.7
    )

    best_confidence = max(jacc, edit_similarity)

    is_similar = (
        jacc >= TITLE_JACCARD_THRESHOLD
        or norm_edit < TITLE_LEVENSHTEIN_RATIO
        or prefix_match
    )

    return is_similar, best_confidence


# ---------------------------------------------------------------------------
# SQLite persistence
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS seen_urls (
    url_hash     TEXT PRIMARY KEY,  -- SHA-256 of normalized URL (hex)
    normalized_url TEXT NOT NULL,
    source_id    TEXT NOT NULL,
    article_id   TEXT NOT NULL,
    first_seen   TEXT NOT NULL      -- ISO 8601 UTC
);

CREATE TABLE IF NOT EXISTS content_hashes (
    simhash      INTEGER NOT NULL,  -- 64-bit SimHash stored as signed integer
    article_id   TEXT NOT NULL,
    source_id    TEXT NOT NULL,
    title        TEXT NOT NULL,
    seen_at      TEXT NOT NULL,     -- ISO 8601 UTC
    PRIMARY KEY (simhash, article_id)
);

CREATE INDEX IF NOT EXISTS idx_content_hashes_simhash ON content_hashes(simhash);
"""


def _uint64_to_int64(value: int) -> int:
    """Convert unsigned 64-bit integer to signed int64 for SQLite storage.

    SQLite INTEGER columns are signed 64-bit.  Python's SimHash produces an
    unsigned 64-bit int which may exceed SQLite's INTEGER range (max 2^63-1).
    We reinterpret the bit pattern as a signed int64 so storage is lossless.

    Args:
        value: Unsigned 64-bit integer (0 to 2^64-1).

    Returns:
        Signed 64-bit integer (-2^63 to 2^63-1) with identical bit pattern.
    """
    return ctypes.c_int64(value).value


def _int64_to_uint64(value: int) -> int:
    """Reverse of _uint64_to_int64: recover the unsigned bit pattern.

    Args:
        value: Signed 64-bit integer from SQLite.

    Returns:
        Unsigned 64-bit integer.
    """
    return ctypes.c_uint64(value).value


def _sha256_hex(text: str) -> str:
    """Return the SHA-256 hex digest of the given text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _utc_now() -> str:
    """Return current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# DedupEngine
# ---------------------------------------------------------------------------

class DedupEngine:
    """3-level deduplication engine with SQLite-backed persistence.

    Usage:
        engine = DedupEngine()
        result = engine.is_duplicate(
            url="https://example.com/article?utm_source=twitter",
            title="Breaking: Major Event Happens",
            body="The full article body text...",
            source_id="example",
            article_id="uuid-1234",
        )
        if result.is_duplicate:
            print(f"Duplicate at level {result.level}: {result.reason}")

    Thread safety:
        All write operations are protected by an internal threading.Lock.
        Multiple threads can safely call is_duplicate() concurrently.

    Lifecycle:
        Call close() when done to release the database connection, or use
        as a context manager:
            with DedupEngine() as engine:
                ...
    """

    def __init__(
        self,
        db_path: Path = DEDUP_SQLITE_PATH,
        in_memory: bool = False,
    ) -> None:
        """Initialize the engine and open (or create) the SQLite database.

        Args:
            db_path: Path to the SQLite file.  Created if it does not exist.
            in_memory: If True, use an in-memory SQLite database (for testing).
                When in_memory=True, db_path is ignored.
        """
        self._normalizer = URLNormalizer()
        self._lock = threading.Lock()

        if in_memory:
            self._db_path_str = ":memory:"
        else:
            db_path.parent.mkdir(parents=True, exist_ok=True)
            self._db_path_str = str(db_path)

        self._conn = sqlite3.connect(
            self._db_path_str,
            check_same_thread=False,  # We manage thread safety with the lock
        )
        self._conn.row_factory = sqlite3.Row
        self._init_schema()
        logger.info("dedup_engine_initialized", db=self._db_path_str)

    # ------------------------------------------------------------------
    # Context manager support
    # ------------------------------------------------------------------

    def __enter__(self) -> "DedupEngine":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Schema initialization
    # ------------------------------------------------------------------

    def _init_schema(self) -> None:
        """Execute DDL to create tables if they don't exist."""
        with self._lock:
            self._conn.executescript(_SCHEMA_SQL)
            self._conn.commit()

    # ------------------------------------------------------------------
    # Main public interface
    # ------------------------------------------------------------------

    def is_duplicate(
        self,
        url: str,
        title: str,
        body: str,
        source_id: str = "",
        article_id: str = "",
        *,
        check_only: bool = False,
    ) -> DedupResult:
        """Check whether an article is a duplicate of any previously seen article.

        Cascade (short-circuits on first match):
            Level 1 — URL exact match (normalized URL hash lookup).
            Level 2 — Title similarity (Jaccard + Levenshtein).
            Level 3 — SimHash near-duplicate detection (Hamming distance ≤ 3).

        If the article is NOT a duplicate and ``check_only`` is False, it is
        registered in the database so future duplicates are detected.

        Args:
            url: Raw article URL (will be normalized internally).
            title: Article title.
            body: Article body text.
            source_id: Source site identifier (e.g. "chosun").
            article_id: UUID assigned to this article by the caller.
            check_only: If True, only check without registering the article.
                Used by the URL-filtering phase to avoid premature registration
                that would cause the extraction phase to reject the article.

        Returns:
            DedupResult with is_duplicate, reason, match_id, level, confidence.
        """
        # ---- Level 1: URL dedup ----
        try:
            normalized_url = self._normalizer.normalize(url)
        except ValueError:
            logger.warning("dedup_url_normalization_failed", url=url)
            normalized_url = url  # Fallback: use raw URL

        url_result = self._check_url(normalized_url)
        if url_result.is_duplicate:
            return url_result

        # ---- Level 2: Title similarity ----
        title_result = self._check_title(title, source_id)
        if title_result.is_duplicate:
            # Register URL so we don't re-check it again
            if not check_only:
                self._register_url(normalized_url, source_id, article_id)
            return title_result

        # ---- Level 3: SimHash content fingerprint ----
        simhash_result = self._check_simhash(body, source_id)
        if simhash_result.is_duplicate:
            if not check_only:
                self._register_url(normalized_url, source_id, article_id)
            return simhash_result

        # ---- Not a duplicate: register for future checks ----
        if not check_only:
            fingerprint = compute_simhash(body)
            self._register_article(
                normalized_url=normalized_url,
                title=title,
                simhash=fingerprint,
                source_id=source_id,
                article_id=article_id,
            )
        return DedupResult.unique()

    # ------------------------------------------------------------------
    # Batch processing
    # ------------------------------------------------------------------

    def is_duplicate_batch(
        self,
        articles: list[dict],
    ) -> list[DedupResult]:
        """Check a batch of articles for duplicates.

        Articles in the batch are checked against the persistent store AND
        against each other (in-batch dedup). Earlier articles in the batch
        take precedence.

        Args:
            articles: List of dicts with keys: url, title, body,
                source_id (optional), article_id (optional).

        Returns:
            List of DedupResult, one per input article, in the same order.
        """
        results: list[DedupResult] = []
        for art in articles:
            result = self.is_duplicate(
                url=art.get("url", ""),
                title=art.get("title", ""),
                body=art.get("body", ""),
                source_id=art.get("source_id", ""),
                article_id=art.get("article_id", ""),
            )
            results.append(result)
        return results

    # ------------------------------------------------------------------
    # Level 1: URL check
    # ------------------------------------------------------------------

    def _check_url(self, normalized_url: str) -> DedupResult:
        """Check whether the normalized URL is already in seen_urls.

        Args:
            normalized_url: Already-normalized URL string.

        Returns:
            DedupResult with level=1 if duplicate, else level=0 (not duplicate).
        """
        url_hash = _sha256_hex(normalized_url)
        # Thread safety: serialize SQLite access via self._lock.
        with self._lock:
            row = self._conn.execute(
                "SELECT article_id FROM seen_urls WHERE url_hash = ?",
                (url_hash,),
            ).fetchone()

        if row:
            return DedupResult(
                is_duplicate=True,
                reason=f"Exact URL match (normalized): {normalized_url}",
                match_id=row["article_id"],
                level=1,
                confidence=1.0,
            )
        return DedupResult.unique()

    def _register_url(
        self,
        normalized_url: str,
        source_id: str,
        article_id: str,
    ) -> None:
        """Insert a normalized URL into seen_urls.

        Args:
            normalized_url: Already-normalized URL.
            source_id: Source site identifier.
            article_id: Article UUID.
        """
        url_hash = _sha256_hex(normalized_url)
        with self._lock:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO seen_urls
                    (url_hash, normalized_url, source_id, article_id, first_seen)
                VALUES (?, ?, ?, ?, ?)
                """,
                (url_hash, normalized_url, source_id, article_id, _utc_now()),
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # Level 2: Title similarity check
    # ------------------------------------------------------------------

    def _check_title(self, title: str, current_source_id: str) -> DedupResult:
        """Check whether the title is similar to any previously seen title.

        Retrieves all stored titles and compares using Jaccard + Levenshtein.
        This is O(N) in the number of stored articles but titles are short,
        making individual comparisons fast.  For very large stores (> 100k),
        consider an inverted index; for the expected ~6,460 articles/day this
        is well within acceptable latency.

        Args:
            title: Raw article title.
            current_source_id: Source of the candidate article.

        Returns:
            DedupResult with level=2 if duplicate, else level=0.
        """
        if not title or not title.strip():
            return DedupResult.unique()

        # Thread safety: SQLite connection is shared across threads with
        # check_same_thread=False, so ALL access must serialize via self._lock.
        # Materialize results inside the lock before iterating to avoid the
        # cursor being invalidated by a concurrent write in another thread.
        with self._lock:
            rows = self._conn.execute(
                "SELECT simhash, article_id, source_id, title "
                "FROM content_hashes"
            ).fetchall()

        for row in rows:
            row_title = row["title"]
            # Defensive: even though the schema declares title NOT NULL,
            # a corrupted fetch under extreme contention could theoretically
            # return None. Skip any such row rather than crash.
            if row_title is None:
                continue
            is_sim, confidence = titles_are_similar(title, row_title)
            # Trust is_sim from titles_are_similar (already applies Jaccard / Levenshtein
            # / prefix thresholds internally). No additional confidence gate needed here.
            if is_sim:
                return DedupResult(
                    is_duplicate=True,
                    reason=(
                        f"Title similarity ({confidence:.2f}) with article "
                        f"{row['article_id']} from {row['source_id']}"
                    ),
                    match_id=row["article_id"],
                    level=2,
                    confidence=confidence,
                )

        return DedupResult.unique()

    # ------------------------------------------------------------------
    # Level 3: SimHash check
    # ------------------------------------------------------------------

    def _check_simhash(self, body: str, source_id: str) -> DedupResult:
        """Check body text against stored SimHash fingerprints.

        Retrieves all stored fingerprints and computes Hamming distance.
        Fingerprints with distance ≤ SIMHASH_THRESHOLD are near-duplicates.

        Args:
            body: Article body text.
            source_id: Source site identifier.

        Returns:
            DedupResult with level=3 if duplicate, else level=0.
        """
        if not body or len(body.strip()) < MIN_BODY_LEN_FOR_SIMHASH:
            return DedupResult.unique()

        candidate_hash = compute_simhash(body)
        if candidate_hash == 0:
            return DedupResult.unique()

        # Thread safety: serialize SQLite access via self._lock (same
        # rationale as _check_title above).
        with self._lock:
            rows = self._conn.execute(
                "SELECT simhash, article_id, source_id FROM content_hashes"
            ).fetchall()

        best_match_id: Optional[str] = None
        best_confidence: float = 0.0
        best_source: str = ""

        for row in rows:
            # Defensive: skip any row that came back with NULL simhash
            # under extreme contention (shouldn't happen but harmless).
            if row["simhash"] is None:
                continue
            # Recover the unsigned 64-bit fingerprint from the stored signed int64
            stored_hash = _int64_to_uint64(row["simhash"])
            dist = hamming_distance(candidate_hash, stored_hash)
            if dist <= SIMHASH_THRESHOLD:
                sim = simhash_similarity(candidate_hash, stored_hash)
                if sim > best_confidence:
                    best_confidence = sim
                    best_match_id = row["article_id"]
                    best_source = row["source_id"]

        if best_match_id is not None:
            return DedupResult(
                is_duplicate=True,
                reason=(
                    f"SimHash near-duplicate (similarity={best_confidence:.3f}, "
                    f"threshold≤{SIMHASH_THRESHOLD} bits) with {best_match_id} "
                    f"from {best_source}"
                ),
                match_id=best_match_id,
                level=3,
                confidence=best_confidence,
            )

        return DedupResult.unique()

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def _register_article(
        self,
        normalized_url: str,
        title: str,
        simhash: int,
        source_id: str,
        article_id: str,
    ) -> None:
        """Register a unique article in both persistence tables.

        Args:
            normalized_url: Already-normalized URL.
            title: Article title.
            simhash: 64-bit SimHash fingerprint.
            source_id: Source site identifier.
            article_id: Article UUID.
        """
        url_hash = _sha256_hex(normalized_url)
        now = _utc_now()

        with self._lock:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO seen_urls
                    (url_hash, normalized_url, source_id, article_id, first_seen)
                VALUES (?, ?, ?, ?, ?)
                """,
                (url_hash, normalized_url, source_id, article_id, now),
            )
            if simhash != 0:
                # Convert unsigned 64-bit to signed int64 for SQLite storage
                self._conn.execute(
                    """
                    INSERT OR IGNORE INTO content_hashes
                        (simhash, article_id, source_id, title, seen_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (_uint64_to_int64(simhash), article_id, source_id, title, now),
                )
            self._conn.commit()

    # ------------------------------------------------------------------
    # Statistics
    # ------------------------------------------------------------------

    def stats(self) -> dict:
        """Return deduplication statistics from the persistent store.

        Returns:
            Dict with keys:
                total_urls: Number of unique URLs seen.
                total_fingerprints: Number of stored SimHash fingerprints.
        """
        url_count = self._conn.execute(
            "SELECT COUNT(*) FROM seen_urls"
        ).fetchone()[0]
        fp_count = self._conn.execute(
            "SELECT COUNT(*) FROM content_hashes"
        ).fetchone()[0]
        return {
            "total_urls": url_count,
            "total_fingerprints": fp_count,
        }

    # ------------------------------------------------------------------
    # TTL Purge
    # ------------------------------------------------------------------

    def purge_expired(self, ttl_days: int = 90) -> dict[str, int]:
        """Remove entries older than *ttl_days* from both tables.

        Batched DELETE (1 000 rows/batch) to limit WAL growth.
        Thread-safe — acquires ``self._lock`` for the entire operation.

        Args:
            ttl_days: Entries with ``first_seen`` / ``seen_at`` older than
                this many days ago are deleted.  Minimum 7 to prevent
                accidental full purge.

        Returns:
            ``{"urls_purged": int, "hashes_purged": int}``

        Raises:
            ValueError: If *ttl_days* < 7 (safety guard).
        """
        if ttl_days < 7:
            raise ValueError(
                f"TTL too short ({ttl_days} days). "
                "Minimum is 7 to prevent accidental full purge."
            )

        from datetime import datetime, timedelta, timezone

        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=ttl_days)
        ).isoformat()

        url_total = 0
        hash_total = 0
        batch_size = 1000

        with self._lock:
            # Batch-delete seen_urls
            while True:
                cursor = self._conn.execute(
                    "DELETE FROM seen_urls WHERE rowid IN "
                    "(SELECT rowid FROM seen_urls WHERE first_seen < ? LIMIT ?)",
                    (cutoff, batch_size),
                )
                url_total += cursor.rowcount
                if cursor.rowcount < batch_size:
                    break

            # Batch-delete content_hashes
            while True:
                cursor = self._conn.execute(
                    "DELETE FROM content_hashes WHERE rowid IN "
                    "(SELECT rowid FROM content_hashes WHERE seen_at < ? LIMIT ?)",
                    (cutoff, batch_size),
                )
                hash_total += cursor.rowcount
                if cursor.rowcount < batch_size:
                    break

            self._conn.commit()

        if url_total > 0 or hash_total > 0:
            logger.info(
                "dedup_purge_expired",
                ttl_days=ttl_days,
                urls_purged=url_total,
                hashes_purged=hash_total,
            )
        return {"urls_purged": url_total, "hashes_purged": hash_total}

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the database connection and release resources."""
        try:
            self._conn.close()
            logger.info("dedup_engine_closed", db=self._db_path_str)
        except Exception:  # noqa: BLE001
            pass
