"""Stage 2 -- Feature Extraction: SBERT embeddings, TF-IDF, NER, KeyBERT.

Transforms preprocessed articles from Stage 1 into dense and sparse feature
representations for downstream analysis stages.

Outputs (three Parquet files):
    data/features/embeddings.parquet  -- SBERT vectors + KeyBERT keywords
    data/features/tfidf.parquet       -- Top-20 TF-IDF terms per article
    data/features/ner.parquet         -- Named entities by type

Techniques implemented:
    T07: SBERT Embeddings      -- sentence-transformers (paraphrase-multilingual-MiniLM-L12-v2)
    T08: TF-IDF                -- sklearn.TfidfVectorizer (max_features=10000, ngram_range=(1,2))
    T09: NER                   -- Multilingual: xlm-roberta-base-ner-hrl; English fallback: spaCy
    T10: KeyBERT               -- keybert.KeyBERT with shared SBERT model
    T12: Word Count/Statistics -- basic corpus stats (logged, not persisted separately)

Memory budget: ~2.4 GB peak (SBERT ~1.1 GB + NER ~0.5 GB + overhead).
Performance target: 1,000 articles in ~6.0 min.

Reference: Step 7 Analysis Pipeline Design, Stage 2 specification.
"""

from __future__ import annotations

import gc
import logging
import os
import re
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

import numpy as np

# ---------------------------------------------------------------------------
# Lazy imports -- heavy libraries loaded only when needed
# ---------------------------------------------------------------------------

_pa = None       # pyarrow
_pq = None       # pyarrow.parquet
_pd = None       # pandas


def _ensure_pyarrow():
    """Lazy-load pyarrow and pyarrow.parquet."""
    global _pa, _pq
    if _pa is None:
        import pyarrow as pa
        import pyarrow.parquet as pq
        _pa = pa
        _pq = pq
    return _pa, _pq


def _ensure_pandas():
    """Lazy-load pandas."""
    global _pd
    if _pd is None:
        import pandas as pd
        _pd = pd
    return _pd


# ---------------------------------------------------------------------------
# Project imports
# ---------------------------------------------------------------------------

from src.config.constants import (
    ARTICLES_PARQUET_PATH,
    DATA_FEATURES_DIR,
    EMBEDDINGS_PARQUET_PATH,
    TFIDF_PARQUET_PATH,
    NER_PARQUET_PATH,
    PARQUET_COMPRESSION,
    PARQUET_COMPRESSION_LEVEL,
    SBERT_MODEL_NAME,
    SBERT_BATCH_SIZE,
    SBERT_EMBEDDING_DIM,
    TFIDF_MAX_FEATURES,
    TFIDF_NGRAM_RANGE,
    NER_BATCH_SIZE,
    NER_MULTILINGUAL_MODEL_NAME,
    SPACY_MODEL_NAME,
    KEYBERT_TOP_N,
    MAX_MEMORY_GB,
)
from src.utils.logging_config import get_analysis_logger
from src.utils.error_handler import (
    ModelLoadError,
    PipelineStageError,
    MemoryLimitError,
)


_raw_logger = get_analysis_logger()


class _StructlogAdapter:
    """Thin adapter that accepts structlog-style keyword arguments.

    When structlog is available, ``get_analysis_logger()`` returns a
    structlog bound logger that natively supports ``logger.info("msg", k=v)``.
    When structlog is *not* installed, the returned object is a stdlib
    ``logging.Logger`` which rejects unexpected kwargs.

    This adapter inspects the returned logger at init time and, if it is a
    stdlib logger, reformats calls so that keyword arguments are appended
    to the message string as ``k=v`` pairs and also passed via ``extra``.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        # structlog bound loggers do NOT have _log as an attribute on the
        # wrapper itself; stdlib loggers do.
        self._is_stdlib = isinstance(inner, logging.Logger)

    def _log(self, level_fn, msg: str, **kwargs: Any) -> None:
        if self._is_stdlib:
            if kwargs:
                kv = " ".join(f"{k}={v}" for k, v in kwargs.items())
                level_fn(f"{msg} [{kv}]", extra=kwargs)
            else:
                level_fn(msg)
        else:
            level_fn(msg, **kwargs)

    def info(self, msg: str, **kwargs: Any) -> None:
        self._log(self._inner.info, msg, **kwargs)

    def warning(self, msg: str, **kwargs: Any) -> None:
        self._log(self._inner.warning, msg, **kwargs)

    def error(self, msg: str, **kwargs: Any) -> None:
        self._log(self._inner.error, msg, **kwargs)

    def debug(self, msg: str, **kwargs: Any) -> None:
        self._log(self._inner.debug, msg, **kwargs)


logger = _StructlogAdapter(_raw_logger)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Default constants (can be overridden per-run via Stage2Config)
_DEFAULT_TFIDF_MIN_DF = 2
_DEFAULT_TFIDF_MAX_DF = 0.95
_DEFAULT_TFIDF_SUBLINEAR_TF = True
_DEFAULT_KEYBERT_DIVERSITY = 0.5
_DEFAULT_KEYBERT_KEYPHRASE_NGRAM_RANGE = (1, 2)
_DEFAULT_NER_TIMEOUT_SECONDS = 30
_DEFAULT_TOP_TFIDF_TERMS = 20

# Entity type mapping from xlm-roberta-base-ner-hrl BIO tags to our schema
_NER_TAG_MAP = {
    "PER": "person",
    "ORG": "org",
    "LOC": "location",
}

# NER quality filter constants (B3: Korean NER garbage entity prevention)
_NER_MAX_ENTITY_LENGTH = 50  # > 50 chars = sentence fragment, not entity
_NER_MIN_ENTITY_LENGTH = 2   # < 2 chars = noise
_NER_MAX_NUMERIC_RATIO = 0.8  # > 80% digits = phone number/date, not entity
# Korean sentence-ending patterns that indicate a full sentence, not an entity name
_NER_KO_SENTENCE_ENDINGS = re.compile(
    r'(거든|니까|습니다|됩니다|입니다|합니다|했다|겠다|된다|한다|세요|네요|어요|아요)$'
)
# Korean postposition/particle patterns inside text indicate a phrase, not an entity.
# Common particles: 을/를 (object), 에/에서 (location), 과/와 (and), 으로 (direction),
# 이/가 (subject when followed by more text).
_NER_KO_PARTICLE_RE = re.compile(
    r'(을|를|에서|에게|으로|과|와|이나|까지|부터|에는|에도|으로서|으로써)'
)


@dataclass
class Stage2Config:
    """Configuration for Stage 2 feature extraction.

    All parameters have sensible defaults aligned with the Step 7 pipeline
    design.  Override only when experimenting or tuning.

    Attributes:
        sbert_model_name: Sentence-BERT model identifier.
        sbert_batch_size: Batch size for SBERT encoding.
        sbert_embedding_dim: Expected embedding dimensionality.
        tfidf_max_features: Vocabulary cap for TF-IDF.
        tfidf_ngram_range: N-gram range for TF-IDF vectorizer.
        tfidf_min_df: Minimum document frequency (absolute or fraction).
        tfidf_max_df: Maximum document frequency (fraction).
        tfidf_sublinear_tf: Use sublinear TF scaling (1 + log(tf)).
        tfidf_top_terms: Number of top TF-IDF terms to store per article.
        ner_model_name: Multilingual NER model name.
        ner_batch_size: Batch size for NER processing.
        ner_timeout_seconds: Per-article NER timeout.
        keybert_top_n: Number of keywords to extract per article.
        keybert_diversity: MMR diversity parameter (0=no diversity, 1=max).
        keybert_ngram_range: Keyphrase n-gram range.
        spacy_model_name: spaCy model for English NER fallback.
        max_memory_gb: Memory ceiling for the stage.
    """

    sbert_model_name: str = SBERT_MODEL_NAME
    sbert_batch_size: int = SBERT_BATCH_SIZE
    sbert_embedding_dim: int = SBERT_EMBEDDING_DIM
    tfidf_max_features: int = TFIDF_MAX_FEATURES
    tfidf_ngram_range: tuple[int, int] = TFIDF_NGRAM_RANGE
    tfidf_min_df: int | float = _DEFAULT_TFIDF_MIN_DF
    tfidf_max_df: float = _DEFAULT_TFIDF_MAX_DF
    tfidf_sublinear_tf: bool = _DEFAULT_TFIDF_SUBLINEAR_TF
    tfidf_top_terms: int = _DEFAULT_TOP_TFIDF_TERMS
    ner_model_name: str = NER_MULTILINGUAL_MODEL_NAME
    ner_batch_size: int = NER_BATCH_SIZE
    ner_timeout_seconds: int = _DEFAULT_NER_TIMEOUT_SECONDS
    keybert_top_n: int = KEYBERT_TOP_N
    keybert_diversity: float = _DEFAULT_KEYBERT_DIVERSITY
    keybert_ngram_range: tuple[int, int] = _DEFAULT_KEYBERT_KEYPHRASE_NGRAM_RANGE
    spacy_model_name: str = SPACY_MODEL_NAME
    max_memory_gb: float = MAX_MEMORY_GB


@dataclass
class Stage2Metrics:
    """Runtime metrics collected during Stage 2 execution.

    Attributes:
        total_articles: Number of articles processed.
        embedding_time_s: Wall time for SBERT encoding.
        tfidf_time_s: Wall time for TF-IDF computation.
        ner_time_s: Wall time for NER extraction.
        keybert_time_s: Wall time for KeyBERT extraction.
        total_time_s: Total wall time for the stage.
        peak_memory_gb: Peak resident memory during execution.
        embedding_failures: Count of articles that fell back to zero-vector.
        ner_failures: Count of articles that skipped NER.
        keybert_failures: Count of articles that fell back to TF-IDF keywords.
        vocab_size_ko: Korean TF-IDF vocabulary size.
        vocab_size_en: English TF-IDF vocabulary size.
        entity_counts: Distribution of entity types across corpus.
    """

    total_articles: int = 0
    embedding_time_s: float = 0.0
    tfidf_time_s: float = 0.0
    ner_time_s: float = 0.0
    keybert_time_s: float = 0.0
    total_time_s: float = 0.0
    peak_memory_gb: float = 0.0
    embedding_failures: int = 0
    ner_failures: int = 0
    keybert_failures: int = 0
    vocab_size_ko: int = 0
    vocab_size_en: int = 0
    entity_counts: dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Memory Tracking Utility
# ---------------------------------------------------------------------------

def _get_memory_gb() -> float:
    """Return current process RSS in GB.

    Falls back to 0.0 on platforms without psutil.
    """
    try:
        import psutil
        process = psutil.Process(os.getpid())
        return process.memory_info().rss / (1024 ** 3)
    except ImportError:
        return 0.0


def _check_memory(limit_gb: float, context: str = "") -> None:
    """Raise MemoryLimitError if current RSS exceeds *limit_gb*.

    Args:
        limit_gb: Memory ceiling in GB.
        context: Description of current operation for the error message.
    """
    current = _get_memory_gb()
    if current > limit_gb:
        raise MemoryLimitError(
            f"Memory limit exceeded during {context}: {current:.2f} GB > {limit_gb} GB",
            current_gb=current,
            limit_gb=limit_gb,
        )


# ---------------------------------------------------------------------------
# SBERT Singleton
# ---------------------------------------------------------------------------

_sbert_instance: Any = None
_sbert_model_name: str | None = None


def get_sbert_model(model_name: str = SBERT_MODEL_NAME) -> Any:
    """Load or return the cached SBERT model (singleton pattern).

    The singleton is shared across KeyBERT and BERTopic (Stage 4) to avoid
    loading the same ~1.1 GB model twice.

    Args:
        model_name: HuggingFace model identifier.

    Returns:
        sentence_transformers.SentenceTransformer instance.

    Raises:
        ModelLoadError: If the model cannot be loaded.
    """
    global _sbert_instance, _sbert_model_name

    if _sbert_instance is not None and _sbert_model_name == model_name:
        return _sbert_instance

    try:
        from sentence_transformers import SentenceTransformer
        logger.info("loading_sbert_model", model=model_name)
        _sbert_instance = SentenceTransformer(model_name)
        _sbert_model_name = model_name
        logger.info(
            "sbert_model_loaded",
            model=model_name,
            embedding_dim=_sbert_instance.get_sentence_embedding_dimension(),
            memory_gb=round(_get_memory_gb(), 2),
        )
        return _sbert_instance
    except Exception as exc:
        raise ModelLoadError(
            f"Failed to load SBERT model '{model_name}': {exc}",
            model_name=model_name,
        ) from exc


def unload_sbert_model() -> None:
    """Unload the SBERT singleton and free memory.

    Only call this after all stages that need SBERT (Stage 2, Stage 4)
    have completed.
    """
    global _sbert_instance, _sbert_model_name
    if _sbert_instance is not None:
        logger.info("unloading_sbert_model", model=_sbert_model_name)
        del _sbert_instance
        _sbert_instance = None
        _sbert_model_name = None
        gc.collect()


# ---------------------------------------------------------------------------
# Text Helpers
# ---------------------------------------------------------------------------

_KOREAN_RE = re.compile(r"[\uac00-\ud7af\u1100-\u11ff\u3130-\u318f]")
_HONORIFIC_RE = re.compile(
    r"^(Mr\.?|Ms\.?|Mrs\.?|Dr\.?|Prof\.?|Sr\.?|Jr\.?)\s+",
    re.IGNORECASE,
)
_WHITESPACE_RE = re.compile(r"\s+")


def _detect_language(text: str) -> str:
    """Heuristic language detection: 'ko' if >=10% Korean characters, else 'en'.

    This is a fast approximation sufficient for routing to per-language
    TF-IDF vectorizers.  Full language detection (langdetect/fasttext) is
    avoided to keep the dependency footprint small.

    Args:
        text: Input text.

    Returns:
        'ko' or 'en'.
    """
    if not text:
        return "en"
    korean_chars = len(_KOREAN_RE.findall(text))
    total_chars = len(text.replace(" ", ""))
    if total_chars == 0:
        return "en"
    return "ko" if korean_chars / total_chars >= 0.10 else "en"


def _is_valid_entity(text: str, lang: str = "en") -> bool:
    """Deterministic quality filter for NER entities (P1 — no LLM judgment).

    Rejects garbage entities produced by multilingual NER on Korean text:
    sentence fragments, overly long strings, numeric-heavy strings.

    Args:
        text: Entity text to validate.
        lang: Language code ("ko" for Korean-specific checks).

    Returns:
        True if entity passes quality filter.
    """
    if not text or len(text) < _NER_MIN_ENTITY_LENGTH:
        return False
    if len(text) > _NER_MAX_ENTITY_LENGTH:
        return False
    # Reject numeric-heavy strings (phone numbers, dates, IDs)
    digit_count = sum(1 for c in text if c.isdigit())
    if len(text) > 0 and digit_count / len(text) > _NER_MAX_NUMERIC_RATIO:
        return False
    # Korean-specific: reject sentence fragments and phrases with particles
    if lang == "ko":
        # Sentence endings (verb/adjective conjugations) indicate a fragment
        if _NER_KO_SENTENCE_ENDINGS.search(text):
            return False
        # Reject phrases containing Korean postpositions/particles
        # (real entity names don't contain 을/를/에서/과/와 etc.)
        if _NER_KO_PARTICLE_RE.search(text):
            return False
        # Reject if text contains spaces and is longer than 20 chars
        # (Korean entity names rarely have spaces and are short)
        if " " in text and len(text) > 20:
            return False
    return True


def _normalize_entity_name(name: str) -> str:
    """Normalize an entity name for deduplication.

    - Strip leading/trailing whitespace
    - Remove honorific prefixes (Mr., Dr., etc.)
    - Collapse internal whitespace

    Args:
        name: Raw entity text.

    Returns:
        Normalized entity string.
    """
    name = name.strip()
    name = _HONORIFIC_RE.sub("", name)
    name = _WHITESPACE_RE.sub(" ", name).strip()
    return name


def _deduplicate_entities(entities: list[str]) -> list[str]:
    """Deduplicate entity list by normalized case-insensitive comparison.

    Retains the first occurrence's casing.  Also merges abbreviated and
    full forms when one is a substring prefix of the other (e.g.,
    "Samsung" vs "Samsung Electronics" -- keeps the longer form).

    Args:
        entities: List of entity names.

    Returns:
        Deduplicated list preserving insertion order.
    """
    if not entities:
        return []

    normalized = [_normalize_entity_name(e) for e in entities]
    seen_lower: dict[str, str] = {}  # lower -> best form
    for norm in normalized:
        if not norm:
            continue
        low = norm.lower()
        if low in seen_lower:
            # Keep the longer form
            if len(norm) > len(seen_lower[low]):
                seen_lower[low] = norm
        else:
            # Check if this is a prefix/suffix of an existing entity
            merged = False
            for existing_low, existing_norm in list(seen_lower.items()):
                if low in existing_low:
                    # Existing is longer -- keep it
                    merged = True
                    break
                if existing_low in low:
                    # New is longer -- replace
                    del seen_lower[existing_low]
                    seen_lower[low] = norm
                    merged = True
                    break
            if not merged:
                seen_lower[low] = norm

    return list(seen_lower.values())


# ---------------------------------------------------------------------------
# Parquet Schema Definitions (explicit, for validation)
# ---------------------------------------------------------------------------

def _embeddings_schema():
    """Return the pyarrow schema for embeddings.parquet."""
    pa, _ = _ensure_pyarrow()
    return pa.schema([
        pa.field("article_id", pa.utf8(), nullable=False),
        pa.field("embedding", pa.list_(pa.float32()), nullable=False),
        pa.field("title_embedding", pa.list_(pa.float32()), nullable=False),
        pa.field("keywords", pa.list_(pa.utf8()), nullable=False),
    ])


def _tfidf_schema():
    """Return the pyarrow schema for tfidf.parquet."""
    pa, _ = _ensure_pyarrow()
    return pa.schema([
        pa.field("article_id", pa.utf8(), nullable=False),
        pa.field("tfidf_top_terms", pa.list_(pa.utf8()), nullable=False),
        pa.field("tfidf_scores", pa.list_(pa.float32()), nullable=False),
    ])


def _ner_schema():
    """Return the pyarrow schema for ner.parquet."""
    pa, _ = _ensure_pyarrow()
    return pa.schema([
        pa.field("article_id", pa.utf8(), nullable=False),
        pa.field("entities_person", pa.list_(pa.utf8()), nullable=False),
        pa.field("entities_org", pa.list_(pa.utf8()), nullable=False),
        pa.field("entities_location", pa.list_(pa.utf8()), nullable=False),
        # Geo Focus (추가 — articles_enriched 핵심 필드)
        pa.field("geo_focus_primary", pa.utf8(), nullable=False),
        pa.field("geo_focus_all", pa.list_(pa.utf8()), nullable=False),
        pa.field("geo_confidence", pa.float32(), nullable=False),
        pa.field("geo_method", pa.utf8(), nullable=False),
        # Signal (추가 — 18-Question Engine 직접 소비)
        pa.field("signal_type", pa.utf8(), nullable=False),
        pa.field("noise_score", pa.float32(), nullable=False),
        pa.field("novelty_score", pa.float32(), nullable=False),
        pa.field("burst_score", pa.float32(), nullable=False),
    ])


# ---------------------------------------------------------------------------
# Component: SBERT Embeddings
# ---------------------------------------------------------------------------

class SBERTEncoder:
    """SBERT embedding generator for article titles and bodies.

    Encodes text into fixed-dimension dense vectors using a
    sentence-transformers model.  All embeddings are L2-normalized
    for direct cosine similarity computation.

    Args:
        config: Stage 2 configuration.
    """

    def __init__(self, config: Stage2Config) -> None:
        self._config = config
        self._model: Any = None

    def load(self) -> None:
        """Load the SBERT model (singleton)."""
        self._model = get_sbert_model(self._config.sbert_model_name)

    @property
    def model(self) -> Any:
        """Return the underlying SentenceTransformer for sharing."""
        return self._model

    def encode_batch(
        self,
        texts: list[str],
        show_progress: bool = True,
    ) -> np.ndarray:
        """Encode a batch of texts into normalized embeddings.

        Args:
            texts: List of text strings.
            show_progress: Show tqdm progress bar.

        Returns:
            numpy array of shape (len(texts), embedding_dim), float32,
            L2-normalized.  Articles with empty text receive zero-vectors.
        """
        if self._model is None:
            raise PipelineStageError(
                "SBERT model not loaded; call load() first",
                stage_name="stage_2_features",
                stage_number=2,
            )

        dim = self._config.sbert_embedding_dim
        zero_vec = np.zeros(dim, dtype=np.float32)

        # Separate empty vs. non-empty for efficient batch encoding
        non_empty_indices = [i for i, t in enumerate(texts) if t and t.strip()]
        non_empty_texts = [texts[i] for i in non_empty_indices]

        embeddings = np.zeros((len(texts), dim), dtype=np.float32)

        if non_empty_texts:
            try:
                raw = self._model.encode(
                    non_empty_texts,
                    batch_size=self._config.sbert_batch_size,
                    show_progress_bar=show_progress,
                    normalize_embeddings=True,
                    convert_to_numpy=True,
                )
                for j, idx in enumerate(non_empty_indices):
                    embeddings[idx] = raw[j].astype(np.float32)
            except Exception as exc:
                logger.error(
                    "sbert_batch_encoding_failed",
                    error=str(exc),
                    batch_size=len(non_empty_texts),
                )
                # All embeddings remain as zero-vectors
                return embeddings

        return embeddings

    def compute_article_embeddings(
        self,
        article_ids: list[str],
        titles: list[str],
        bodies: list[str],
    ) -> dict[str, dict[str, np.ndarray]]:
        """Compute title and body embeddings for all articles.

        The combined ``embedding`` field uses the body embedding when the body
        is non-empty, and falls back to the title embedding for paywall-only
        articles.

        Args:
            article_ids: Article identifiers.
            titles: Article titles (parallel to article_ids).
            bodies: Article bodies (parallel to article_ids; may be empty).

        Returns:
            Dict mapping article_id -> {"embedding": ndarray, "title_embedding": ndarray}.
        """
        logger.info("computing_sbert_embeddings", n_articles=len(article_ids))

        title_embs = self.encode_batch(titles, show_progress=True)
        body_embs = self.encode_batch(bodies, show_progress=True)

        results: dict[str, dict[str, np.ndarray]] = {}
        failures = 0

        for i, aid in enumerate(article_ids):
            title_vec = title_embs[i]
            body_vec = body_embs[i]

            # Use body if non-zero, else fall back to title
            if np.any(body_vec != 0):
                combined = body_vec
            elif np.any(title_vec != 0):
                combined = title_vec
            else:
                combined = np.zeros(self._config.sbert_embedding_dim, dtype=np.float32)
                failures += 1

            results[aid] = {
                "embedding": combined,
                "title_embedding": title_vec,
            }

        if failures:
            logger.warning("sbert_zero_vector_fallbacks", count=failures)

        return results


# ---------------------------------------------------------------------------
# Component: TF-IDF
# ---------------------------------------------------------------------------

class TFIDFExtractor:
    """TF-IDF feature extraction with per-language vectorizers.

    Builds separate TF-IDF models for Korean and English content to
    avoid vocabulary pollution.  Extracts top-N terms per article.

    Args:
        config: Stage 2 configuration.
    """

    def __init__(self, config: Stage2Config) -> None:
        self._config = config
        self._vectorizer_ko: Any = None
        self._vectorizer_en: Any = None

    def _build_vectorizer(self) -> Any:
        """Create a configured TfidfVectorizer."""
        from sklearn.feature_extraction.text import TfidfVectorizer

        return TfidfVectorizer(
            max_features=self._config.tfidf_max_features,
            ngram_range=self._config.tfidf_ngram_range,
            min_df=self._config.tfidf_min_df,
            max_df=self._config.tfidf_max_df,
            sublinear_tf=self._config.tfidf_sublinear_tf,
            dtype=np.float32,
        )

    def fit_transform(
        self,
        article_ids: list[str],
        texts: list[str],
        languages: list[str],
    ) -> dict[str, dict[str, Any]]:
        """Fit TF-IDF vectorizers and extract top terms per article.

        Args:
            article_ids: Article identifiers.
            texts: Full article texts (title + body concatenation).
            languages: Per-article language codes ('ko' or 'en').

        Returns:
            Dict mapping article_id -> {"terms": list[str], "scores": list[float]}.
        """
        logger.info("computing_tfidf", n_articles=len(article_ids))

        # Partition by language
        ko_indices = [i for i, lang in enumerate(languages) if lang == "ko"]
        en_indices = [i for i, lang in enumerate(languages) if lang != "ko"]

        results: dict[str, dict[str, Any]] = {}
        top_n = self._config.tfidf_top_terms

        # Process Korean articles
        if ko_indices:
            ko_texts = [texts[i] for i in ko_indices]
            ko_ids = [article_ids[i] for i in ko_indices]
            ko_results = self._extract_top_terms(ko_texts, ko_ids, top_n, lang="ko")
            results.update(ko_results)

        # Process English articles
        if en_indices:
            en_texts = [texts[i] for i in en_indices]
            en_ids = [article_ids[i] for i in en_indices]
            en_results = self._extract_top_terms(en_texts, en_ids, top_n, lang="en")
            results.update(en_results)

        # Fill any missing (empty text) articles
        for aid in article_ids:
            if aid not in results:
                results[aid] = {"terms": [], "scores": []}

        return results

    def _extract_top_terms(
        self,
        texts: list[str],
        article_ids: list[str],
        top_n: int,
        lang: str,
    ) -> dict[str, dict[str, Any]]:
        """Fit a vectorizer and extract top-N terms per document.

        Args:
            texts: Documents for one language group.
            article_ids: Corresponding article IDs.
            top_n: Number of top terms to return.
            lang: Language code ('ko' or 'en').

        Returns:
            Dict mapping article_id -> {"terms": list[str], "scores": list[float]}.
        """
        from sklearn.feature_extraction.text import TfidfVectorizer

        # Filter out empty texts
        valid_mask = [bool(t and t.strip()) for t in texts]
        valid_texts = [t for t, v in zip(texts, valid_mask) if v]
        valid_ids = [aid for aid, v in zip(article_ids, valid_mask) if v]

        if len(valid_texts) < 2:
            # Need at least 2 documents for meaningful TF-IDF
            results = {}
            for aid in article_ids:
                results[aid] = {"terms": [], "scores": []}
            return results

        vectorizer = self._build_vectorizer()

        # Adaptive min_df: when the partition is small, min_df=2 with
        # max_df=0.95 can create an impossible constraint (max_df * n_docs
        # < min_df).  Fall back to min_df=1 for partitions under 10 docs.
        n_docs = len(valid_texts)
        effective_min_df = self._config.tfidf_min_df
        effective_max_df = self._config.tfidf_max_df
        if isinstance(effective_min_df, int) and effective_min_df >= 2:
            max_doc_count = (
                effective_max_df * n_docs
                if isinstance(effective_max_df, float) and effective_max_df <= 1.0
                else effective_max_df
            )
            if max_doc_count < effective_min_df:
                vectorizer.set_params(min_df=1)
                logger.debug(
                    "tfidf_min_df_adjusted",
                    original=effective_min_df,
                    adjusted=1,
                    n_docs=n_docs,
                )

        try:
            tfidf_matrix = vectorizer.fit_transform(valid_texts)
        except ValueError as exc:
            logger.warning("tfidf_fit_failed", lang=lang, error=str(exc))
            results = {}
            for aid in article_ids:
                results[aid] = {"terms": [], "scores": []}
            return results

        feature_names = vectorizer.get_feature_names_out()
        vocab_size = len(feature_names)

        if lang == "ko":
            self._vectorizer_ko = vectorizer
            logger.info("tfidf_ko_fitted", vocab_size=vocab_size)
        else:
            self._vectorizer_en = vectorizer
            logger.info("tfidf_en_fitted", vocab_size=vocab_size)

        results: dict[str, dict[str, Any]] = {}
        for j, aid in enumerate(valid_ids):
            row = tfidf_matrix[j].toarray().flatten()
            # Get indices sorted by descending TF-IDF score
            top_indices = row.argsort()[::-1][:top_n]
            terms = [str(feature_names[idx]) for idx in top_indices if row[idx] > 0]
            scores = [float(row[idx]) for idx in top_indices if row[idx] > 0]
            results[aid] = {"terms": terms, "scores": scores}

        # Fill invalid articles
        for aid in article_ids:
            if aid not in results:
                results[aid] = {"terms": [], "scores": []}

        return results

    @property
    def vocab_size_ko(self) -> int:
        """Number of features in the Korean TF-IDF vocabulary."""
        if self._vectorizer_ko is None:
            return 0
        return len(self._vectorizer_ko.get_feature_names_out())

    @property
    def vocab_size_en(self) -> int:
        """Number of features in the English TF-IDF vocabulary."""
        if self._vectorizer_en is None:
            return 0
        return len(self._vectorizer_en.get_feature_names_out())


# ---------------------------------------------------------------------------
# Component: Named Entity Recognition
# ---------------------------------------------------------------------------

class NERExtractor:
    """Named Entity Recognition using a multilingual transformer model.

    Primary: Davlan/xlm-roberta-base-ner-hrl (supports Korean, English, and
    many other languages in a single model).

    Fallback: spaCy en_core_web_sm for English-only NER when the
    transformers model is unavailable.

    Entity types extracted: PERSON, ORG, LOCATION (mapped from model-specific
    BIO tags).

    Args:
        config: Stage 2 configuration.
    """

    def __init__(self, config: Stage2Config) -> None:
        self._config = config
        self._pipeline: Any = None
        self._spacy_nlp: Any = None
        self._backend: str = "none"

    def load(self) -> None:
        """Load the NER model with automatic fallback.

        Tries: (1) xlm-roberta NER pipeline, (2) spaCy en_core_web_sm,
        (3) disabled (empty entity lists for all articles).
        """
        # Try multilingual NER pipeline
        try:
            from transformers import pipeline as hf_pipeline, AutoTokenizer
            logger.info("loading_ner_model", model=self._config.ner_model_name)
            # P1 fix: use_fast=False prevents transformers 4.45+ tokenizer
            # conversion bug ('NoneType' has no attribute 'endswith')
            # for XLM-RoBERTa models with sentencepiece vocab.
            _tokenizer = AutoTokenizer.from_pretrained(
                self._config.ner_model_name, use_fast=False,
            )
            self._pipeline = hf_pipeline(
                "ner",
                model=self._config.ner_model_name,
                tokenizer=_tokenizer,
                aggregation_strategy="simple",
                device=-1,  # CPU -- safe default for M2 Pro
            )
            self._backend = "transformers"
            logger.info(
                "ner_model_loaded",
                backend="transformers",
                model=self._config.ner_model_name,
                memory_gb=round(_get_memory_gb(), 2),
            )
            return
        except Exception as exc:
            logger.warning(
                "ner_transformers_fallback",
                error=str(exc),
                fallback="spacy",
            )

        # Try spaCy
        try:
            import spacy
            logger.info("loading_spacy_ner", model=self._config.spacy_model_name)
            self._spacy_nlp = spacy.load(self._config.spacy_model_name)
            self._backend = "spacy"
            logger.info(
                "ner_model_loaded",
                backend="spacy",
                model=self._config.spacy_model_name,
            )
            return
        except Exception as exc:
            logger.warning("ner_spacy_fallback", error=str(exc), fallback="disabled")

        self._backend = "none"
        logger.warning("ner_disabled", reason="no NER model available")

    def extract_batch(
        self,
        article_ids: list[str],
        texts: list[str],
        languages: list[str] | None = None,
    ) -> dict[str, dict[str, list[str]]]:
        """Extract named entities for a batch of articles.

        Args:
            article_ids: Article identifiers.
            texts: Full article texts.
            languages: Per-article language codes (e.g. "ko", "en").
                If None, defaults to "en" for all articles.

        Returns:
            Dict mapping article_id -> {
                "person": list[str],
                "org": list[str],
                "location": list[str],
            }.
        """
        logger.info("extracting_ner", n_articles=len(article_ids), backend=self._backend)

        if languages is None:
            languages = ["en"] * len(article_ids)

        results: dict[str, dict[str, list[str]]] = {}
        empty_entities = {"person": [], "org": [], "location": []}

        if self._backend == "none":
            for aid in article_ids:
                results[aid] = dict(empty_entities)
            return results

        # Process in batches
        batch_size = self._config.ner_batch_size
        for start in range(0, len(article_ids), batch_size):
            end = min(start + batch_size, len(article_ids))
            batch_ids = article_ids[start:end]
            batch_texts = texts[start:end]
            batch_langs = languages[start:end]

            for aid, text, lang in zip(batch_ids, batch_texts, batch_langs):
                if not text or not text.strip():
                    results[aid] = dict(empty_entities)
                    continue

                try:
                    entities = self._extract_single(text, lang=lang)
                    results[aid] = entities
                except Exception as exc:
                    logger.warning(
                        "ner_article_failed",
                        article_id=aid,
                        error=str(exc),
                    )
                    results[aid] = dict(empty_entities)

        return results

    def _extract_single(self, text: str, lang: str = "en") -> dict[str, list[str]]:
        """Extract entities from a single text.

        Args:
            text: Article text.
            lang: Language code for quality filtering ("ko" enables Korean filters).

        Returns:
            Dict with 'person', 'org', 'location' lists.
        """
        persons: list[str] = []
        orgs: list[str] = []
        locations: list[str] = []

        # Truncate very long texts to avoid OOM in NER model.
        # xlm-roberta has 512-token context window.
        # Korean text has ~1.5-2x tokens per char, so use shorter limit
        # to stay within the window and prevent garbled entity extraction.
        max_chars = 2000 if lang == "ko" else 4000
        truncated = text[:max_chars] if len(text) > max_chars else text

        if self._backend == "transformers":
            raw_entities = self._pipeline(truncated)
            for ent in raw_entities:
                entity_group = ent.get("entity_group", "")
                word = ent.get("word", "").strip()
                if not word or len(word) < 2:
                    continue
                # B3: Quality filter — reject garbage entities (P1 deterministic)
                if not _is_valid_entity(word, lang):
                    continue

                mapped_type = _NER_TAG_MAP.get(entity_group, None)
                if mapped_type == "person":
                    persons.append(word)
                elif mapped_type == "org":
                    orgs.append(word)
                elif mapped_type == "location":
                    locations.append(word)

        elif self._backend == "spacy":
            doc = self._spacy_nlp(truncated)
            for ent in doc.ents:
                text_val = ent.text.strip()
                if not text_val or len(text_val) < 2:
                    continue
                if not _is_valid_entity(text_val, lang):
                    continue
                if ent.label_ == "PERSON":
                    persons.append(text_val)
                elif ent.label_ == "ORG":
                    orgs.append(text_val)
                elif ent.label_ in ("GPE", "LOC", "FAC"):
                    locations.append(text_val)

        return {
            "person": _deduplicate_entities(persons),
            "org": _deduplicate_entities(orgs),
            "location": _deduplicate_entities(locations),
        }

    def unload(self) -> None:
        """Release NER model memory."""
        if self._pipeline is not None:
            logger.info("unloading_ner_model", backend="transformers")
            del self._pipeline
            self._pipeline = None
        if self._spacy_nlp is not None:
            logger.info("unloading_ner_model", backend="spacy")
            del self._spacy_nlp
            self._spacy_nlp = None
        self._backend = "none"
        gc.collect()


# ---------------------------------------------------------------------------
# Component: KeyBERT Keyword Extraction
# ---------------------------------------------------------------------------

class KeyBERTExtractor:
    """Keyword extraction using KeyBERT with shared SBERT model.

    Uses Maximal Marginal Relevance (MMR) for keyword diversity.
    Falls back to TF-IDF top terms when KeyBERT fails for an article.

    Args:
        config: Stage 2 configuration.
    """

    def __init__(self, config: Stage2Config) -> None:
        self._config = config
        self._kw_model: Any = None

    def load(self, sbert_model: Any) -> None:
        """Initialize KeyBERT with the shared SBERT model.

        Args:
            sbert_model: Pre-loaded SentenceTransformer instance.

        Raises:
            ModelLoadError: If KeyBERT cannot be initialized.
        """
        try:
            from keybert import KeyBERT
            logger.info("loading_keybert", shared_sbert=True)
            self._kw_model = KeyBERT(model=sbert_model)
            logger.info("keybert_loaded", memory_gb=round(_get_memory_gb(), 2))
        except Exception as exc:
            raise ModelLoadError(
                f"Failed to load KeyBERT: {exc}",
                model_name="keybert",
            ) from exc

    def extract_keywords(
        self,
        article_ids: list[str],
        texts: list[str],
        tfidf_fallback: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, list[str]]:
        """Extract keywords for each article.

        Args:
            article_ids: Article identifiers.
            texts: Full article texts.
            tfidf_fallback: Optional TF-IDF results to use as fallback
                when KeyBERT fails for a specific article.

        Returns:
            Dict mapping article_id -> list of keyword strings.
        """
        logger.info("extracting_keywords", n_articles=len(article_ids))

        results: dict[str, list[str]] = {}
        failures = 0

        for aid, text in zip(article_ids, texts):
            if not text or not text.strip():
                results[aid] = self._get_tfidf_fallback(aid, tfidf_fallback)
                continue

            try:
                if self._kw_model is not None:
                    kws = self._kw_model.extract_keywords(
                        text,
                        keyphrase_ngram_range=self._config.keybert_ngram_range,
                        top_n=self._config.keybert_top_n,
                        use_mmr=True,
                        diversity=self._config.keybert_diversity,
                    )
                    # kws is list of (keyword, score) tuples
                    results[aid] = [kw for kw, _score in kws]
                else:
                    results[aid] = self._get_tfidf_fallback(aid, tfidf_fallback)
                    failures += 1
            except Exception as exc:
                logger.warning(
                    "keybert_article_failed",
                    article_id=aid,
                    error=str(exc),
                )
                results[aid] = self._get_tfidf_fallback(aid, tfidf_fallback)
                failures += 1

        if failures:
            logger.warning("keybert_fallback_count", count=failures)

        return results

    @staticmethod
    def _get_tfidf_fallback(
        article_id: str,
        tfidf_results: dict[str, dict[str, Any]] | None,
    ) -> list[str]:
        """Return TF-IDF top terms as keyword fallback.

        Args:
            article_id: Article identifier.
            tfidf_results: TF-IDF extraction results.

        Returns:
            List of term strings, or empty list.
        """
        if tfidf_results and article_id in tfidf_results:
            return tfidf_results[article_id].get("terms", [])
        return []

    def unload(self) -> None:
        """Release KeyBERT resources (SBERT stays loaded)."""
        if self._kw_model is not None:
            logger.info("unloading_keybert")
            del self._kw_model
            self._kw_model = None
            gc.collect()


# ---------------------------------------------------------------------------
# Stage 2 Orchestrator
# ---------------------------------------------------------------------------

class Stage2FeatureExtractor:
    """Stage 2 pipeline orchestrator: coordinates all feature extractors.

    Loads models sequentially (SBERT -> TF-IDF -> NER -> KeyBERT),
    processes all articles, writes three Parquet output files, then
    unloads NER and KeyBERT (SBERT stays for Stage 4 reuse).

    Usage::

        extractor = Stage2FeatureExtractor()
        metrics = extractor.run(articles_path, output_dir)

    Or use the convenience function::

        metrics = run_stage2()

    Args:
        config: Optional Stage2Config override.
    """

    def __init__(self, config: Stage2Config | None = None) -> None:
        self.config = config or Stage2Config()
        self.metrics = Stage2Metrics()

        # Components (initialized lazily during run)
        self._sbert = SBERTEncoder(self.config)
        self._tfidf = TFIDFExtractor(self.config)
        self._ner = NERExtractor(self.config)
        self._keybert = KeyBERTExtractor(self.config)

    def run(
        self,
        articles_path: Path | str | None = None,
        output_dir: Path | str | None = None,
    ) -> Stage2Metrics:
        """Execute the full Stage 2 pipeline.

        Args:
            articles_path: Path to Stage 1 articles.parquet.
                Defaults to ``ARTICLES_PARQUET_PATH``.
            output_dir: Directory for feature Parquet outputs.
                Defaults to ``DATA_FEATURES_DIR``.

        Returns:
            Stage2Metrics with timing and quality statistics.

        Raises:
            PipelineStageError: If critical failure prevents output generation.
            FileNotFoundError: If articles_path does not exist.
        """
        articles_path = Path(articles_path or ARTICLES_PARQUET_PATH)
        output_dir = Path(output_dir or DATA_FEATURES_DIR)
        output_dir.mkdir(parents=True, exist_ok=True)

        t_start = time.monotonic()

        # ------------------------------------------------------------------
        # 1. Load articles
        # ------------------------------------------------------------------
        logger.info("stage2_loading_articles", path=str(articles_path))
        article_ids, titles, bodies, languages = self._load_articles(articles_path)
        self.metrics.total_articles = len(article_ids)
        logger.info("stage2_articles_loaded", count=len(article_ids))

        if not article_ids:
            logger.warning("stage2_no_articles", path=str(articles_path))
            self._write_empty_outputs(output_dir)
            self.metrics.total_time_s = time.monotonic() - t_start
            return self.metrics

        # Full text = title + body for TF-IDF and KeyBERT
        full_texts = [
            f"{t} {b}".strip() if b else t
            for t, b in zip(titles, bodies)
        ]

        # ------------------------------------------------------------------
        # 2. SBERT Embeddings
        # ------------------------------------------------------------------
        t0 = time.monotonic()
        try:
            self._sbert.load()
            sbert_results = self._sbert.compute_article_embeddings(
                article_ids, titles, bodies,
            )
        except ModelLoadError:
            logger.error("sbert_unavailable_using_zeros")
            dim = self.config.sbert_embedding_dim
            sbert_results = {
                aid: {
                    "embedding": np.zeros(dim, dtype=np.float32),
                    "title_embedding": np.zeros(dim, dtype=np.float32),
                }
                for aid in article_ids
            }
            self.metrics.embedding_failures = len(article_ids)
        self.metrics.embedding_time_s = time.monotonic() - t0
        _check_memory(self.config.max_memory_gb, "after SBERT encoding")

        # ------------------------------------------------------------------
        # 3. TF-IDF
        # ------------------------------------------------------------------
        t0 = time.monotonic()
        tfidf_results = self._tfidf.fit_transform(article_ids, full_texts, languages)
        self.metrics.tfidf_time_s = time.monotonic() - t0
        self.metrics.vocab_size_ko = self._tfidf.vocab_size_ko
        self.metrics.vocab_size_en = self._tfidf.vocab_size_en

        # ------------------------------------------------------------------
        # 4. NER
        # ------------------------------------------------------------------
        t0 = time.monotonic()
        try:
            self._ner.load()
            ner_results = self._ner.extract_batch(article_ids, full_texts, languages)
        except ModelLoadError:
            logger.error("ner_unavailable_using_empty")
            ner_results = {
                aid: {"person": [], "org": [], "location": []}
                for aid in article_ids
            }
            self.metrics.ner_failures = len(article_ids)
        self.metrics.ner_time_s = time.monotonic() - t0

        # Count entity types
        entity_counts: dict[str, int] = {"person": 0, "org": 0, "location": 0}
        for ent_data in ner_results.values():
            for etype in entity_counts:
                entity_counts[etype] += len(ent_data.get(etype, []))
        self.metrics.entity_counts = entity_counts

        _check_memory(self.config.max_memory_gb, "after NER")

        # ------------------------------------------------------------------
        # 4b. Geo Focus Extraction (NER location 재사용 — 모델 재실행 없음)
        # ------------------------------------------------------------------
        t0 = time.monotonic()
        try:
            from src.analysis.geo_focus_extractor import GeoFocusExtractor
            _geo_extractor = GeoFocusExtractor()
            geo_results: dict[str, dict] = {}
            for aid, title, body, lang in zip(
                article_ids, titles, bodies, languages,
            ):
                ner_locs = ner_results.get(aid, {}).get("location", [])
                r = _geo_extractor.extract(
                    title=title, body=body, language=lang,
                    ner_locations=ner_locs,
                )
                geo_results[aid] = r.to_dict()
        except Exception as exc:
            logger.warning("geo_focus_extraction_failed error=%s", exc)
            geo_results = {
                aid: {
                    "geo_focus_primary": "UNKNOWN",
                    "geo_focus_all": [],
                    "geo_confidence": 0.0,
                    "geo_method": "fallback",
                }
                for aid in article_ids
            }
        logger.info(
            "geo_focus_complete elapsed=%.1fs",
            time.monotonic() - t0,
        )

        # ------------------------------------------------------------------
        # 4c. Signal Layer A — 기사 단독 (noise_score, novelty_score, prelim type)
        # ------------------------------------------------------------------
        t0 = time.monotonic()
        try:
            from src.analysis.signal_classifier import SignalClassifier
            _sig_clf = SignalClassifier()
            signal_results: dict[str, dict] = {}
            for aid, title, body, lang in zip(
                article_ids, titles, bodies, languages,
            ):
                geo_d = geo_results.get(aid, {})
                sig_r = _sig_clf.classify(
                    title=title,
                    body=body,
                    language=lang,
                    geo_focus_all=geo_d.get("geo_focus_all", []),
                )
                signal_results[aid] = sig_r.to_dict()
        except Exception as exc:
            logger.warning("signal_layer_a_failed error=%s", exc)
            signal_results = {
                aid: {
                    "signal_type": "UNCLASSIFIED",
                    "noise_score": 0.0,
                    "novelty_score": 0.0,
                    "burst_score": 1.0,
                }
                for aid in article_ids
            }
        logger.info(
            "signal_layer_a_complete elapsed=%.1fs",
            time.monotonic() - t0,
        )

        # ------------------------------------------------------------------
        # 5. KeyBERT Keywords
        # ------------------------------------------------------------------
        t0 = time.monotonic()
        try:
            self._keybert.load(self._sbert.model)
            keyword_results = self._keybert.extract_keywords(
                article_ids, full_texts, tfidf_fallback=tfidf_results,
            )
        except (ModelLoadError, Exception) as exc:
            logger.error("keybert_unavailable_using_tfidf_fallback", error=str(exc))
            keyword_results = {
                aid: tfidf_results.get(aid, {}).get("terms", [])
                for aid in article_ids
            }
            self.metrics.keybert_failures = len(article_ids)
        self.metrics.keybert_time_s = time.monotonic() - t0

        # ------------------------------------------------------------------
        # 6. Write Parquet outputs
        # ------------------------------------------------------------------
        self._write_embeddings_parquet(
            output_dir, article_ids, sbert_results, keyword_results,
        )
        self._write_tfidf_parquet(output_dir, article_ids, tfidf_results)
        self._write_ner_parquet(
            output_dir, article_ids, ner_results, geo_results, signal_results,
        )

        # ------------------------------------------------------------------
        # 7. Cleanup: unload NER and KeyBERT (SBERT stays for Stage 4)
        # ------------------------------------------------------------------
        self._ner.unload()
        self._keybert.unload()
        gc.collect()

        self.metrics.total_time_s = time.monotonic() - t_start
        self.metrics.peak_memory_gb = round(_get_memory_gb(), 2)

        logger.info(
            "stage2_complete",
            total_articles=self.metrics.total_articles,
            total_time_s=round(self.metrics.total_time_s, 2),
            embedding_time_s=round(self.metrics.embedding_time_s, 2),
            tfidf_time_s=round(self.metrics.tfidf_time_s, 2),
            ner_time_s=round(self.metrics.ner_time_s, 2),
            keybert_time_s=round(self.metrics.keybert_time_s, 2),
            peak_memory_gb=self.metrics.peak_memory_gb,
            vocab_ko=self.metrics.vocab_size_ko,
            vocab_en=self.metrics.vocab_size_en,
            entities=self.metrics.entity_counts,
        )

        return self.metrics

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _load_articles(
        path: Path,
    ) -> tuple[list[str], list[str], list[str], list[str]]:
        """Load articles from Parquet and return parallel lists.

        Args:
            path: Path to articles.parquet.

        Returns:
            Tuple of (article_ids, titles, bodies, languages).

        Raises:
            FileNotFoundError: If the Parquet file does not exist.
            PipelineStageError: If required columns are missing.
        """
        if not path.exists():
            raise FileNotFoundError(f"Stage 1 output not found: {path}")

        pa, pq = _ensure_pyarrow()
        table = pq.read_table(str(path))
        schema_names = set(table.schema.names)

        # Validate required columns
        required = {"article_id", "title"}
        missing = required - schema_names
        if missing:
            raise PipelineStageError(
                f"articles.parquet missing required columns: {missing}",
                stage_name="stage_2_features",
                stage_number=2,
            )

        n = table.num_rows
        article_ids = [str(v) for v in table.column("article_id").to_pylist()]
        titles = [str(v) if v is not None else "" for v in table.column("title").to_pylist()]

        if "body" in schema_names:
            bodies = [str(v) if v is not None else "" for v in table.column("body").to_pylist()]
        else:
            bodies = [""] * n

        if "language" in schema_names:
            raw_langs = table.column("language").to_pylist()
            languages = [str(v) if v is not None else "en" for v in raw_langs]
        else:
            # Auto-detect from content
            languages = [_detect_language(f"{t} {b}") for t, b in zip(titles, bodies)]

        return article_ids, titles, bodies, languages

    def _write_embeddings_parquet(
        self,
        output_dir: Path,
        article_ids: list[str],
        sbert_results: dict[str, dict[str, np.ndarray]],
        keyword_results: dict[str, list[str]],
    ) -> None:
        """Write embeddings.parquet with SBERT vectors and keywords."""
        pa, pq = _ensure_pyarrow()
        schema = _embeddings_schema()

        rows = {
            "article_id": [],
            "embedding": [],
            "title_embedding": [],
            "keywords": [],
        }
        for aid in article_ids:
            emb_data = sbert_results.get(aid, {})
            rows["article_id"].append(aid)
            rows["embedding"].append(
                emb_data.get(
                    "embedding",
                    np.zeros(self.config.sbert_embedding_dim, dtype=np.float32),
                ).tolist()
            )
            rows["title_embedding"].append(
                emb_data.get(
                    "title_embedding",
                    np.zeros(self.config.sbert_embedding_dim, dtype=np.float32),
                ).tolist()
            )
            rows["keywords"].append(keyword_results.get(aid, []))

        table = pa.table(rows, schema=schema)
        out_path = output_dir / "embeddings.parquet"
        pq.write_table(
            table,
            str(out_path),
            compression=PARQUET_COMPRESSION,
            compression_level=PARQUET_COMPRESSION_LEVEL,
        )
        logger.info("wrote_embeddings_parquet", path=str(out_path), rows=table.num_rows)

    def _write_tfidf_parquet(
        self,
        output_dir: Path,
        article_ids: list[str],
        tfidf_results: dict[str, dict[str, Any]],
    ) -> None:
        """Write tfidf.parquet with top terms and scores."""
        pa, pq = _ensure_pyarrow()
        schema = _tfidf_schema()

        rows = {
            "article_id": [],
            "tfidf_top_terms": [],
            "tfidf_scores": [],
        }
        for aid in article_ids:
            data = tfidf_results.get(aid, {"terms": [], "scores": []})
            rows["article_id"].append(aid)
            rows["tfidf_top_terms"].append(data.get("terms", []))
            rows["tfidf_scores"].append(
                [float(s) for s in data.get("scores", [])]
            )

        table = pa.table(rows, schema=schema)
        out_path = output_dir / "tfidf.parquet"
        pq.write_table(
            table,
            str(out_path),
            compression=PARQUET_COMPRESSION,
            compression_level=PARQUET_COMPRESSION_LEVEL,
        )
        logger.info("wrote_tfidf_parquet", path=str(out_path), rows=table.num_rows)

    def _write_ner_parquet(
        self,
        output_dir: Path,
        article_ids: list[str],
        ner_results: dict[str, dict[str, list[str]]],
        geo_results: dict[str, dict] | None = None,
        signal_results: dict[str, dict] | None = None,
    ) -> None:
        """Write ner.parquet with entity lists + geo_focus + signal per article."""
        pa, pq = _ensure_pyarrow()
        schema = _ner_schema()
        _geo = geo_results or {}
        _sig = signal_results or {}

        rows = {
            "article_id": [],
            "entities_person": [],
            "entities_org": [],
            "entities_location": [],
            "geo_focus_primary": [],
            "geo_focus_all": [],
            "geo_confidence": [],
            "geo_method": [],
            "signal_type": [],
            "noise_score": [],
            "novelty_score": [],
            "burst_score": [],
        }
        for aid in article_ids:
            data = ner_results.get(aid, {"person": [], "org": [], "location": []})
            geo = _geo.get(aid, {
                "geo_focus_primary": "UNKNOWN",
                "geo_focus_all": [],
                "geo_confidence": 0.0,
                "geo_method": "fallback",
            })
            sig = _sig.get(aid, {
                "signal_type": "UNCLASSIFIED",
                "noise_score": 0.0,
                "novelty_score": 0.0,
                "burst_score": 1.0,
            })
            rows["article_id"].append(aid)
            rows["entities_person"].append(data.get("person", []))
            rows["entities_org"].append(data.get("org", []))
            rows["entities_location"].append(data.get("location", []))
            rows["geo_focus_primary"].append(geo.get("geo_focus_primary", "UNKNOWN"))
            rows["geo_focus_all"].append(geo.get("geo_focus_all", []))
            rows["geo_confidence"].append(float(geo.get("geo_confidence", 0.0)))
            rows["geo_method"].append(geo.get("geo_method", "fallback"))
            rows["signal_type"].append(sig.get("signal_type", "UNCLASSIFIED"))
            rows["noise_score"].append(float(sig.get("noise_score", 0.0)))
            rows["novelty_score"].append(float(sig.get("novelty_score", 0.0)))
            rows["burst_score"].append(float(sig.get("burst_score", 1.0)))

        table = pa.table(rows, schema=schema)
        out_path = output_dir / "ner.parquet"
        pq.write_table(
            table,
            str(out_path),
            compression=PARQUET_COMPRESSION,
            compression_level=PARQUET_COMPRESSION_LEVEL,
        )
        logger.info("wrote_ner_parquet", path=str(out_path), rows=table.num_rows)

    def _write_empty_outputs(self, output_dir: Path) -> None:
        """Write empty (zero-row) Parquet files preserving schemas."""
        pa, pq = _ensure_pyarrow()

        for schema, filename in [
            (_embeddings_schema(), "embeddings.parquet"),
            (_tfidf_schema(), "tfidf.parquet"),
            (_ner_schema(), "ner.parquet"),
        ]:
            table = pa.table(
                {f.name: pa.array([], type=f.type) for f in schema},
                schema=schema,
            )
            out_path = output_dir / filename
            pq.write_table(
                table,
                str(out_path),
                compression=PARQUET_COMPRESSION,
                compression_level=PARQUET_COMPRESSION_LEVEL,
            )
            logger.info("wrote_empty_parquet", path=str(out_path))


# ---------------------------------------------------------------------------
# Convenience function
# ---------------------------------------------------------------------------

def run_stage2(
    articles_path: Path | str | None = None,
    output_dir: Path | str | None = None,
    config: Stage2Config | None = None,
) -> Stage2Metrics:
    """Run Stage 2 feature extraction pipeline.

    Convenience entry point that creates a ``Stage2FeatureExtractor`` and
    invokes ``run()``.

    Args:
        articles_path: Path to Stage 1 articles.parquet.
        output_dir: Output directory for feature Parquet files.
        config: Optional configuration override.

    Returns:
        Stage2Metrics with timing and quality statistics.
    """
    extractor = Stage2FeatureExtractor(config=config)
    return extractor.run(articles_path=articles_path, output_dir=output_dir)
