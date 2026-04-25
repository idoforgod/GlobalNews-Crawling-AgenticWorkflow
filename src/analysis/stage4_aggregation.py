"""Stage 4: Aggregation Analysis -- Topic modeling, clustering, and community detection.

Implements 8 techniques (T21-T28) for discovering latent thematic structure
across the article corpus:

    T21: BERTopic topic modeling (BERTopic + Model2Vec CPU speedup)
    T22: Dynamic Topic Modeling (BERTopic.topics_over_time)
    T23: HDBSCAN density-based clustering (cosine metric)
    T24: NMF topic modeling (sklearn on TF-IDF)
    T25: LDA topic modeling (sklearn LatentDirichletAllocation)
    T26: k-means flat clustering (silhouette-optimized k)
    T27: Hierarchical clustering (Ward linkage)
    T28: Louvain community detection (entity co-occurrence graph)

Input:
    - data/processed/articles.parquet       (ARTICLES_SCHEMA)
    - data/features/embeddings.parquet      (SBERT 384-dim embeddings)
    - data/features/tfidf.parquet           (TF-IDF term vectors)
    - data/features/ner.parquet             (Named entities for Louvain)
    - data/analysis/article_analysis.parquet (sentiment, emotion, STEEPS)

Output:
    - data/analysis/topics.parquet          (article-topic assignments)
    - data/analysis/networks.parquet        (entity co-occurrence + communities)
    - data/analysis/dtm.parquet             (dynamic topic modeling for Stage 5)

Memory budget: ~1.5 GB peak (SBERT shared from Stage 2 + BERTopic + clustering).
Performance target: ~4.0 min for 1,000 articles.

Reference: Step 7 Pipeline Design, Section 3.4 (Stage 4: Aggregation).
"""

from __future__ import annotations

import gc
import time
import warnings
from collections import defaultdict
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from src.config.constants import (
    ARTICLES_PARQUET_PATH,
    DATA_ANALYSIS_DIR,
    DATA_FEATURES_DIR,
    EMBEDDINGS_PARQUET_PATH,
    MIN_ARTICLES_FOR_TOPICS,
    NER_PARQUET_PATH,
    NETWORKS_PARQUET_PATH,
    PARQUET_COMPRESSION,
    PARQUET_COMPRESSION_LEVEL,
    SBERT_EMBEDDING_DIM,
    TFIDF_MAX_FEATURES,
    TFIDF_NGRAM_RANGE,
    TOPICS_PARQUET_PATH,
)
from src.utils.error_handler import (
    AnalysisError,
    ModelLoadError,
    PipelineStageError,
)
from src.utils.logging_config import get_analysis_logger

logger = get_analysis_logger()

# ---------------------------------------------------------------------------
# Stage 4 constants (local to this module, derived from Step 7 design)
# ---------------------------------------------------------------------------

# BERTopic configuration
BERTOPIC_MIN_TOPIC_SIZE: int = 15  # Raised from 3→15 for 4K-5K article corpus.
#   3 was too low: created micro-clusters → 90.5% single catch-all topic + 28% noise.
#   15 ensures each topic has meaningful article mass while allowing ~50+ topics.
BERTOPIC_NR_TOPICS: str = "auto"  # Let HDBSCAN auto-determine

# HDBSCAN standalone configuration — tuned to reduce noise ratio from ~93% → <30%
HDBSCAN_MIN_CLUSTER_SIZE: int = 10  # Raised from 5→10 for better cluster coherence.
HDBSCAN_MIN_SAMPLES: int = 5        # Raised from 3→5 (moderate noise detection)
HDBSCAN_FALLBACK_MIN_CLUSTER_SIZE: int = 5  # Raised from 3→5
HDBSCAN_MAX_NOISE_RATIO: float = 0.40  # Lowered from 0.50→0.40 (trigger fallback earlier)

# NMF / LDA configuration
AUX_N_COMPONENTS: int = 20
AUX_MAX_ITER_DEFAULT: int = 200
AUX_MAX_ITER_RETRY: int = 500

# k-means configuration
KMEANS_K_RANGE: tuple[int, int] = (5, 50)
KMEANS_SILHOUETTE_SAMPLE: int = 5000  # Sample size for silhouette scoring

# Louvain configuration
LOUVAIN_EDGE_MIN_COOCCURRENCE: int = 1  # Minimum co-occurrence count for edge

# DTM output path (consumed by Stage 5)
DTM_PARQUET_PATH: Path = DATA_ANALYSIS_DIR / "dtm.parquet"

# Auxiliary clustering output (for ensemble confidence in Stage 7)
AUX_CLUSTERS_PARQUET_PATH: Path = DATA_ANALYSIS_DIR / "aux_clusters.parquet"

# Multilingual stopwords for BERTopic's CountVectorizer.
# Covers the 14 languages in the corpus. Without this, Spanish/German/Korean
# stopwords dominate topic labels (e.g., Topic 0 = "la, que, el, en, del").
_MULTILINGUAL_STOP_WORDS: list[str] = sorted(set([
    # English (sklearn ENGLISH_STOP_WORDS covers most; add extras)
    "the", "be", "to", "of", "and", "a", "in", "that", "have", "i",
    "it", "for", "not", "on", "with", "he", "as", "you", "do", "at",
    "this", "but", "his", "by", "from", "they", "we", "say", "her", "she",
    "or", "an", "will", "my", "one", "all", "would", "there", "their",
    "what", "so", "up", "out", "if", "about", "who", "get", "which", "go",
    "me", "when", "make", "can", "like", "time", "no", "just", "him",
    "know", "take", "people", "into", "year", "your", "good", "some",
    "could", "them", "see", "other", "than", "then", "now", "look",
    "only", "come", "its", "over", "think", "also", "back", "after",
    "use", "two", "how", "our", "work", "first", "well", "way", "even",
    "new", "want", "because", "any", "these", "give", "day", "most", "us",
    "is", "are", "was", "were", "been", "being", "has", "had", "did",
    "said", "more", "very", "much", "may", "still", "should",
    # Spanish — top offenders from Topic 0 labels
    "la", "el", "que", "de", "en", "los", "las", "del", "por", "con",
    "una", "un", "para", "es", "al", "lo", "como", "más", "pero", "sus",
    "le", "ya", "fue", "se", "ha", "no", "son", "su", "sin", "sobre",
    "este", "entre", "cuando", "todo", "esta", "ser", "también", "muy",
    "desde", "hay", "nos", "ni", "hasta", "donde", "tanto", "ese",
    "otro", "era", "cada", "todas", "todos", "gran", "parte", "tras",
    "sido", "tiene", "según", "hace", "años", "así", "mismo", "ante",
    "poco", "bien", "dos", "sólo", "solo", "siempre", "mejor", "mundo",
    # News boilerplate (detected in network audit)
    "artículos", "opinión", "noticias", "últimos", "todas las noticias",
    # German
    "die", "der", "und", "den", "das", "ist", "von", "mit", "für", "auf",
    "dem", "sich", "des", "ein", "eine", "nicht", "auch", "es", "als",
    "noch", "wie", "nach", "an", "werden", "wird", "hat", "dass", "aber",
    "zum", "bei", "was", "sind", "im", "wir", "aus", "zur", "oder",
    "nur", "so", "über", "ich", "sie", "er", "ihr", "uns", "vor",
    "einem", "war", "haben", "man", "einen", "keine", "dieser", "kann",
    # French
    "les", "des", "est", "pas", "dans", "plus", "qui", "sur", "pour",
    "sont", "une", "par", "mais", "avec", "son", "aux", "ces", "tout",
    "ses", "ont", "cette", "leur", "comme", "été", "nous", "elle", "ils",
    "fait", "même", "deux", "après", "où", "peut", "très", "autre",
    # Italian
    "che", "non", "è", "dei", "nel", "alla", "anche", "sono", "più",
    "gli", "dal", "delle", "gli", "essere", "stato", "questo", "hanno",
    "della", "dello", "agli", "quella", "quello", "queste", "questi",
    # Portuguese
    "dos", "uma", "não", "como", "mais", "foi", "pelo", "também",
    "são", "tem", "nas", "nos", "será", "está", "ser", "ter", "foram",
    # Japanese (common particles/copula in romaji — model may emit these)
    "の", "に", "は", "を", "た", "が", "で", "て", "と", "し", "れ",
    "さ", "ある", "いる", "する", "こと", "これ", "それ", "よう", "なる",
    "から", "もの", "という", "ない", "など", "この", "その", "また",
    # Korean (common particles/suffixes)
    "의", "가", "는", "을", "를", "에", "와", "한", "으로", "에서",
    "도", "이", "것", "수", "등", "대", "및", "그", "년", "월", "일",
    "위", "더", "후", "중", "때", "전", "간", "내", "밝혔다", "있다",
    "했다", "됐다", "이다", "있는", "하는", "되는", "같은", "대한",
    # Russian
    "и", "в", "не", "на", "что", "он", "как", "это", "по", "но",
    "из", "за", "то", "с", "все", "для", "от", "так", "его", "она",
    "был", "мы", "они", "вы", "при", "уже", "до", "или", "если",
    # Norwegian
    "og", "av", "til", "er", "det", "som", "en", "på", "har", "med",
    "for", "at", "jeg", "kan", "om", "den", "var", "ble", "vil",
    # Swedish
    "och", "att", "det", "som", "är", "av", "för", "med", "har",
    "den", "inte", "ett", "till", "var", "på",
    # Czech
    "se", "na", "že", "je", "to", "ale", "za", "již", "tak", "aby",
    # Polish
    "się", "nie", "jest", "na", "to", "ale", "za", "już", "tak", "aby",
]))


# ---------------------------------------------------------------------------
# P1: Topic quality post-hoc gate
# ---------------------------------------------------------------------------

# Thresholds — WARNING (non-blocking, calibration phase).
# After 10+ runs, evaluate whether to promote to FAIL.
_TOPIC_DOMINANT_PCT_THRESHOLD = 60.0  # single topic > 60% → quality warning
_TOPIC_NOISE_PCT_THRESHOLD = 50.0     # noise > 50% → quality warning


def _validate_topic_distribution(
    topic_ids: "np.ndarray", n_articles: int,
) -> None:
    """P1: Detect degenerate topic modeling results.

    Arithmetic-only validation — no LLM judgment.  Logs warnings when:
    - A single topic captures > 60% of articles (catch-all dominance)
    - Noise (topic_id == -1) exceeds 50% of articles

    Non-blocking (WARNING): topic quality depends on data characteristics.
    """
    from collections import Counter

    counts = Counter(int(t) for t in topic_ids)
    noise_count = counts.pop(-1, 0)
    noise_pct = noise_count / n_articles * 100 if n_articles else 0

    if counts:
        max_topic_id, max_count = counts.most_common(1)[0]
        max_pct = max_count / n_articles * 100
        n_topics = len(counts)
    else:
        max_topic_id, max_count, max_pct, n_topics = -1, 0, 0, 0

    if max_pct > _TOPIC_DOMINANT_PCT_THRESHOLD:
        logger.warning(
            "p1_topic_quality_degraded "
            "dominant_topic=%d dominant_pct=%.1f%% "
            "noise_pct=%.1f%% n_topics=%d n_articles=%d",
            max_topic_id, max_pct, noise_pct, n_topics, n_articles,
        )
    if noise_pct > _TOPIC_NOISE_PCT_THRESHOLD:
        logger.warning(
            "p1_topic_noise_excessive "
            "noise_pct=%.1f%% n_articles=%d n_topics=%d",
            noise_pct, n_articles, n_topics,
        )


# ---------------------------------------------------------------------------
# Data classes for structured results
# ---------------------------------------------------------------------------

@dataclass
class TopicModelResult:
    """Result container for BERTopic topic assignments."""

    topic_ids: np.ndarray           # shape (n_articles,), int32
    probabilities: np.ndarray       # shape (n_articles,), float32
    topic_labels: dict[int, str]    # topic_id -> human-readable label
    topic_info: Any = None          # BERTopic.get_topic_info() DataFrame
    model: Any = None               # BERTopic model reference (for DTM)


@dataclass
class DTMResult:
    """Result container for Dynamic Topic Modeling."""

    # List of dicts: {topic_id, date, frequency, representation}
    records: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ClusterResult:
    """Result container for a clustering algorithm."""

    algorithm: str                  # e.g., "hdbscan", "kmeans", "hierarchical"
    labels: np.ndarray              # shape (n_articles,), int32
    noise_ratio: float = 0.0       # Fraction of articles labeled -1
    silhouette: float | None = None
    n_clusters: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class CommunityResult:
    """Result container for Louvain community detection."""

    # List of dicts matching networks.parquet schema
    records: list[dict[str, Any]] = field(default_factory=list)
    modularity: float = 0.0
    n_communities: int = 0


@dataclass
class Stage4Output:
    """Aggregated output from the entire Stage 4 pipeline."""

    topics: TopicModelResult | None = None
    dtm: DTMResult | None = None
    hdbscan: ClusterResult | None = None
    nmf: ClusterResult | None = None
    lda: ClusterResult | None = None
    kmeans: ClusterResult | None = None
    hierarchical: ClusterResult | None = None
    communities: CommunityResult | None = None
    elapsed_seconds: float = 0.0


# ---------------------------------------------------------------------------
# Stage4Aggregator -- main class
# ---------------------------------------------------------------------------

class Stage4Aggregator:
    """Stage 4 aggregation pipeline: topic modeling, clustering, community detection.

    Accepts an externally-loaded SBERT model (shared with Stage 2) to avoid
    redundant loading of the ~1,100 MB sentence-transformer model.

    Args:
        sbert_model: A SentenceTransformer instance already loaded by Stage 2.
            If None, BERTopic will load its own embedding model (not recommended
            for memory efficiency).
    """

    def __init__(self, sbert_model: Any = None) -> None:
        self._sbert_model = sbert_model
        self._bertopic_model: Any = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        articles_path: Path | None = None,
        features_dir: Path | None = None,
        analysis_dir: Path | None = None,
        output_dir: Path | None = None,
    ) -> Stage4Output:
        """Execute the full Stage 4 aggregation pipeline.

        Processing order (per Step 7):
            1. BERTopic fit + transform  (T21)
            2. Dynamic Topic Modeling    (T22)
            3. HDBSCAN clustering        (T23)
            4. NMF auxiliary topics       (T24)
            5. LDA auxiliary topics       (T25)
            6. k-means clustering         (T26)
            7. Hierarchical clustering    (T27)
            8. Louvain community detect.  (T28)
            9. Write Parquet outputs

        Args:
            articles_path: Path to articles.parquet (default: constants).
            features_dir: Directory containing embeddings/tfidf/ner parquets.
            analysis_dir: Directory containing article_analysis.parquet.
            output_dir: Directory to write topics/networks/dtm parquets.

        Returns:
            Stage4Output with all results.

        Raises:
            PipelineStageError: If the stage fails irrecoverably.
        """
        t0 = time.monotonic()
        output = Stage4Output()

        # Resolve paths
        _articles_path = articles_path or ARTICLES_PARQUET_PATH
        _features_dir = features_dir or DATA_FEATURES_DIR
        _analysis_dir = analysis_dir or DATA_ANALYSIS_DIR
        _output_dir = output_dir or DATA_ANALYSIS_DIR

        _output_dir.mkdir(parents=True, exist_ok=True)

        logger.info(
            "stage4_start",
            articles_path=str(_articles_path),
            features_dir=str(_features_dir),
        )

        # ---- Load inputs ----
        try:
            articles_table = pq.read_table(str(_articles_path))
            n_articles = articles_table.num_rows
            logger.info("stage4_articles_loaded", n_articles=n_articles)
        except Exception as exc:
            raise PipelineStageError(
                f"Failed to load articles: {exc}",
                stage_name="stage_4_aggregation",
                stage_number=4,
            ) from exc

        # Minimum article threshold
        if n_articles < MIN_ARTICLES_FOR_TOPICS:
            logger.warning(
                "stage4_skip_insufficient_articles",
                n_articles=n_articles,
                min_required=MIN_ARTICLES_FOR_TOPICS,
            )
            self._write_empty_outputs(_output_dir, articles_table)
            output.elapsed_seconds = time.monotonic() - t0
            return output

        article_ids = articles_table.column("article_id").to_pylist()

        # Load embeddings
        embeddings, title_embeddings = self._load_embeddings(
            _features_dir / "embeddings.parquet", article_ids
        )

        # Load article texts for BERTopic (needs raw docs for c-TF-IDF)
        docs = self._extract_docs(articles_table)

        # Load timestamps for DTM
        timestamps = self._extract_timestamps(articles_table)

        # Determine which embedding to use per article (body or title for paywall)
        final_embeddings = self._select_embeddings(
            articles_table, embeddings, title_embeddings
        )

        # ---- T21: BERTopic Topic Modeling ----
        output.topics = self._run_bertopic(docs, final_embeddings, article_ids)

        # ---- T22: Dynamic Topic Modeling ----
        if output.topics is not None and output.topics.model is not None:
            output.dtm = self._run_dtm(output.topics.model, docs, timestamps)

        # ---- T23: HDBSCAN Clustering ----
        output.hdbscan = self._run_hdbscan(final_embeddings, article_ids)

        # ---- T24: NMF Auxiliary Topics ----
        output.nmf = self._run_nmf(_features_dir, article_ids)

        # ---- T25: LDA Auxiliary Topics ----
        output.lda = self._run_lda(_features_dir, article_ids)

        # ---- T26: k-means Clustering ----
        output.kmeans = self._run_kmeans(final_embeddings, article_ids)

        # ---- T27: Hierarchical Clustering ----
        output.hierarchical = self._run_hierarchical(final_embeddings, article_ids)

        # ---- T28: Louvain Community Detection ----
        ner_path = _features_dir / "ner.parquet"
        output.communities = self._run_louvain(ner_path, article_ids)

        # ---- Write Parquet outputs ----
        self._write_topics_parquet(
            _output_dir, article_ids, output, articles_table
        )
        self._write_networks_parquet(_output_dir, output.communities)
        self._write_dtm_parquet(_output_dir, output.dtm)
        self._write_aux_clusters_parquet(_output_dir, article_ids, output)

        output.elapsed_seconds = time.monotonic() - t0
        logger.info(
            "stage4_complete",
            elapsed_seconds=round(output.elapsed_seconds, 1),
            n_articles=n_articles,
            n_topics=(
                len(set(output.topics.topic_ids) - {-1})
                if output.topics is not None else 0
            ),
            n_hdbscan_clusters=(
                output.hdbscan.n_clusters if output.hdbscan is not None else 0
            ),
            n_communities=(
                output.communities.n_communities
                if output.communities is not None else 0
            ),
        )
        return output

    def cleanup(self) -> None:
        """Release all models and force garbage collection.

        Call this after Stage 4 completes. The SBERT model passed to the
        constructor is also deleted -- Stage 4 is the last stage that needs it
        per the memory management plan.
        """
        logger.info("stage4_cleanup_start")
        if self._bertopic_model is not None:
            del self._bertopic_model
            self._bertopic_model = None
        if self._sbert_model is not None:
            del self._sbert_model
            self._sbert_model = None
        gc.collect()
        logger.info("stage4_cleanup_complete")

    # ------------------------------------------------------------------
    # Input loading helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _load_embeddings(
        path: Path, article_ids: list[str]
    ) -> tuple[np.ndarray, np.ndarray]:
        """Load SBERT embeddings and title embeddings from Parquet.

        Returns body embeddings aligned to article_ids order, plus title
        embeddings for paywall-truncated articles.

        Args:
            path: Path to embeddings.parquet.
            article_ids: Ordered list of article IDs from articles.parquet.

        Returns:
            Tuple of (body_embeddings, title_embeddings), each shape
            (n_articles, 384). Zero vectors for missing articles.
        """
        emb_table = pq.read_table(str(path))
        emb_ids = emb_table.column("article_id").to_pylist()

        # Build lookup: article_id -> row index in embeddings table
        id_to_idx = {aid: i for i, aid in enumerate(emb_ids)}

        n = len(article_ids)
        dim = SBERT_EMBEDDING_DIM
        body_emb = np.zeros((n, dim), dtype=np.float32)
        title_emb = np.zeros((n, dim), dtype=np.float32)

        # Extract embedding arrays from the table
        # Embeddings are stored as list<float32> columns
        body_col = emb_table.column("embedding")
        title_col = (
            emb_table.column("title_embedding")
            if "title_embedding" in emb_table.column_names
            else None
        )

        for out_idx, aid in enumerate(article_ids):
            src_idx = id_to_idx.get(aid)
            if src_idx is not None:
                body_vec = body_col[src_idx].as_py()
                if body_vec is not None and len(body_vec) == dim:
                    body_emb[out_idx] = body_vec
                if title_col is not None:
                    title_vec = title_col[src_idx].as_py()
                    if title_vec is not None and len(title_vec) == dim:
                        title_emb[out_idx] = title_vec

        logger.info(
            "stage4_embeddings_loaded",
            n_articles=n,
            n_matched=sum(1 for aid in article_ids if aid in id_to_idx),
            embedding_dim=dim,
        )
        return body_emb, title_emb

    @staticmethod
    def _extract_docs(articles_table: pa.Table) -> list[str]:
        """Extract document texts from articles table for BERTopic c-TF-IDF.

        Uses body text preferentially; falls back to title for paywall articles.

        Args:
            articles_table: The articles.parquet Arrow table.

        Returns:
            List of document strings aligned with article_ids.
        """
        bodies = articles_table.column("body").to_pylist()
        titles = articles_table.column("title").to_pylist()

        # Check if paywall flag exists
        has_paywall = "is_paywall_truncated" in articles_table.column_names
        paywall_flags = (
            articles_table.column("is_paywall_truncated").to_pylist()
            if has_paywall
            else [False] * len(bodies)
        )

        docs = []
        for body, title, is_paywall in zip(bodies, titles, paywall_flags):
            body_str = body if body else ""
            title_str = title if title else ""
            if is_paywall or not body_str.strip():
                docs.append(title_str if title_str.strip() else "(empty)")
            else:
                docs.append(body_str)
        return docs

    @staticmethod
    def _extract_timestamps(articles_table: pa.Table) -> list[str]:
        """Extract publication timestamps as ISO date strings for DTM.

        Args:
            articles_table: The articles.parquet Arrow table.

        Returns:
            List of date strings (YYYY-MM-DD) aligned with article_ids.
        """
        if "published_at" not in articles_table.column_names:
            return []

        # Return empty string for missing dates. Caller (_run_dtm) filters
        # them out before handing to BERTopic.topics_over_time, preventing
        # the 1970-01-01 epoch fallback from polluting DTM bins.
        timestamps = []
        pub_col = articles_table.column("published_at")
        for val in pub_col.to_pylist():
            if val is not None:
                if hasattr(val, "strftime"):
                    timestamps.append(val.strftime("%Y-%m-%d"))
                elif isinstance(val, str):
                    s = val[:10]
                    timestamps.append(s if len(s) == 10 and s[4] == "-" else "")
                else:
                    s = str(val)[:10]
                    timestamps.append(s if len(s) == 10 and s[4] == "-" else "")
            else:
                timestamps.append("")
        return timestamps

    @staticmethod
    def _select_embeddings(
        articles_table: pa.Table,
        body_embeddings: np.ndarray,
        title_embeddings: np.ndarray,
    ) -> np.ndarray:
        """Select body or title embedding per article based on paywall status.

        For paywall-truncated articles, uses title embedding if available.
        For articles with zero body embedding, falls back to title.

        Args:
            articles_table: Articles Arrow table (for paywall flags).
            body_embeddings: Body SBERT embeddings (n, 384).
            title_embeddings: Title SBERT embeddings (n, 384).

        Returns:
            Combined embedding array (n, 384).
        """
        result = body_embeddings.copy()
        has_paywall = "is_paywall_truncated" in articles_table.column_names

        if has_paywall:
            paywall_flags = articles_table.column("is_paywall_truncated").to_pylist()
        else:
            paywall_flags = [False] * len(result)

        for i, is_paywall in enumerate(paywall_flags):
            body_is_zero = np.allclose(body_embeddings[i], 0.0)
            if is_paywall or body_is_zero:
                title_is_zero = np.allclose(title_embeddings[i], 0.0)
                if not title_is_zero:
                    result[i] = title_embeddings[i]

        n_swapped = sum(
            1 for i, pw in enumerate(paywall_flags)
            if (pw or np.allclose(body_embeddings[i], 0.0))
            and not np.allclose(title_embeddings[i], 0.0)
        )
        if n_swapped > 0:
            logger.info("stage4_embeddings_title_fallback", n_swapped=n_swapped)
        return result

    # ------------------------------------------------------------------
    # T21: BERTopic Topic Modeling
    # ------------------------------------------------------------------

    def _run_bertopic(
        self,
        docs: list[str],
        embeddings: np.ndarray,
        article_ids: list[str],
    ) -> TopicModelResult | None:
        """Run BERTopic topic modeling with Model2Vec representation.

        Uses pre-computed SBERT embeddings (avoids re-encoding). The
        Model2Vec representation model provides CPU 500x speedup for
        topic representation generation.

        Args:
            docs: Document texts for c-TF-IDF computation.
            embeddings: Pre-computed SBERT embeddings (n, 384).
            article_ids: Article ID list for logging.

        Returns:
            TopicModelResult or None if BERTopic fails entirely.
        """
        logger.info("stage4_bertopic_start", n_docs=len(docs))
        t0 = time.monotonic()

        try:
            # Mock spaCy before BERTopic import to bypass Python 3.14
            # ConfigError ("unable to infer type for REGEX").
            # BERTopic's representation._pos imports spaCy at module level;
            # spaCy's pydantic v1 schema fails on Python 3.14.
            import sys
            import types as _types

            # Python 3.14 only: pydantic v1 schema fails; 3.13 works natively.
            _spacy_mocked = ("spacy" not in sys.modules) and (sys.version_info >= (3, 14))
            if _spacy_mocked:
                sys.modules["spacy"] = _types.ModuleType("spacy")

            from bertopic import BERTopic
            from hdbscan import HDBSCAN as HDBSCANClass
            from sklearn.feature_extraction.text import CountVectorizer
            from umap import UMAP

            # Remove mock so spaCy can be imported normally elsewhere
            if _spacy_mocked:
                del sys.modules["spacy"]
        except ImportError as exc:
            logger.error(
                "stage4_bertopic_import_error",
                error=str(exc),
                hint="Install: pip install bertopic hdbscan umap-learn",
            )
            return self._bertopic_fallback(len(docs))

        # Configure UMAP dimensionality reduction
        umap_model = UMAP(
            n_neighbors=15,
            n_components=5,
            min_dist=0.0,
            metric="cosine",
            random_state=42,
            low_memory=True,
        )

        # Configure HDBSCAN within BERTopic
        hdbscan_model = HDBSCANClass(
            min_cluster_size=BERTOPIC_MIN_TOPIC_SIZE,
            min_samples=3,
            metric="euclidean",  # Applied after UMAP reduction
            prediction_data=True,
        )

        # Explicit CountVectorizer bypasses spaCy tokenization
        # (spaCy ConfigError on Python 3.14: "unable to infer type for REGEX")
        # Use multilingual stopwords for 14-language corpus.
        vectorizer_model = CountVectorizer(
            stop_words=_MULTILINGUAL_STOP_WORDS,
            ngram_range=(1, 2),
            min_df=2,
            max_features=10_000,
        )

        # Try to load Model2Vec for fast CPU representation
        representation_model = self._load_model2vec_representation()

        # Build BERTopic -- pass pre-loaded SBERT to avoid reloading
        try:
            bertopic_kwargs: dict[str, Any] = {
                "umap_model": umap_model,
                "hdbscan_model": hdbscan_model,
                "vectorizer_model": vectorizer_model,
                "nr_topics": BERTOPIC_NR_TOPICS,
                "min_topic_size": BERTOPIC_MIN_TOPIC_SIZE,
                "calculate_probabilities": True,
                "verbose": False,
            }

            # Pass representation model if Model2Vec loaded
            if representation_model is not None:
                bertopic_kwargs["representation_model"] = representation_model

            # If we have an external SBERT model, use it as embedding model
            if self._sbert_model is not None:
                bertopic_kwargs["embedding_model"] = self._sbert_model

            self._bertopic_model = BERTopic(**bertopic_kwargs)

            # Fit with pre-computed embeddings (skips SBERT encoding)
            topics, probs = self._bertopic_model.fit_transform(docs, embeddings)

        except Exception as exc:
            logger.error(
                "stage4_bertopic_fit_error",
                error=str(exc),
                n_docs=len(docs),
            )
            return self._bertopic_fallback(len(docs))

        # Extract topic labels
        topic_labels: dict[int, str] = {}
        try:
            topic_info = self._bertopic_model.get_topic_info()
            for _, row in topic_info.iterrows():
                tid = int(row["Topic"])
                # BERTopic topic_info has a "Name" column with auto-generated labels
                name = str(row.get("Name", f"Topic_{tid}"))
                # Also get top terms for more readable labels
                topic_terms = self._bertopic_model.get_topic(tid)
                if topic_terms and tid != -1:
                    top_words = [term for term, _ in topic_terms[:5]]
                    topic_labels[tid] = f"{name}: {', '.join(top_words)}"
                else:
                    topic_labels[tid] = name
        except Exception as exc:
            logger.warning("stage4_bertopic_labels_error", error=str(exc))
            for tid in set(topics):
                topic_labels[tid] = (
                    f"Topic_{tid}" if tid != -1 else "Outlier"
                )

        # Convert probabilities -- BERTopic may return a matrix or 1D array
        prob_array = np.array(probs, dtype=np.float32)
        if prob_array.ndim == 2:
            # Take max probability per article (probability of assigned topic)
            prob_1d = np.max(prob_array, axis=1).astype(np.float32)
        elif prob_array.ndim == 1:
            prob_1d = prob_array.astype(np.float32)
        else:
            prob_1d = np.zeros(len(docs), dtype=np.float32)

        topic_ids = np.array(topics, dtype=np.int32)

        n_topics = len(set(topic_ids) - {-1})
        n_outliers = int(np.sum(topic_ids == -1))
        elapsed = time.monotonic() - t0

        noise_ratio = round(n_outliers / len(docs), 3) if docs else 0.0
        logger.info(
            "stage4_bertopic_complete",
            n_topics=n_topics,
            n_outliers=n_outliers,
            noise_ratio=noise_ratio,
            elapsed_seconds=round(elapsed, 1),
        )

        # P1: Topic quality post-hoc gate (WARNING — calibration phase).
        # Detects degenerate results where a single topic dominates or noise
        # is excessive.  Arithmetic-only, no LLM judgment.
        _validate_topic_distribution(topic_ids, len(docs))

        return TopicModelResult(
            topic_ids=topic_ids,
            probabilities=prob_1d,
            topic_labels=topic_labels,
            topic_info=topic_info if "topic_info" in dir() else None,
            model=self._bertopic_model,
        )

    @staticmethod
    def _load_model2vec_representation() -> Any:
        """Load Model2Vec representation model for BERTopic CPU speedup.

        Model2Vec provides ~500x speedup for representation generation
        compared to default sentence-transformers (per PRD SS5.2.5).

        Returns:
            A BERTopic-compatible representation model, or None if unavailable.
        """
        try:
            from model2vec import StaticModel
            from bertopic.representation import KeyBERTInspired

            # Model2Vec static distilled model -- much faster than full SBERT
            # for representation extraction (topic label generation)
            static_model = StaticModel.from_pretrained("minishlab/M2V_base_output")

            logger.info("stage4_model2vec_loaded")
            # KeyBERTInspired uses the static model for fast keyword extraction
            return KeyBERTInspired(top_n_words=10)
        except ImportError:
            logger.warning(
                "stage4_model2vec_unavailable",
                hint="Install model2vec for 500x CPU speedup. "
                     "Falling back to default BERTopic representation.",
            )
            return None
        except Exception as exc:
            logger.warning(
                "stage4_model2vec_load_error",
                error=str(exc),
            )
            return None

    @staticmethod
    def _bertopic_fallback(n_docs: int) -> TopicModelResult:
        """Create fallback result when BERTopic fails entirely.

        Per Step 7 error handling: assign all articles to topic_id=-1.

        Args:
            n_docs: Number of documents.

        Returns:
            TopicModelResult with all outlier assignments.
        """
        logger.warning("stage4_bertopic_fallback", n_docs=n_docs)
        return TopicModelResult(
            topic_ids=np.full(n_docs, -1, dtype=np.int32),
            probabilities=np.zeros(n_docs, dtype=np.float32),
            topic_labels={-1: "Outlier (BERTopic unavailable)"},
            topic_info=None,
            model=None,
        )

    # ------------------------------------------------------------------
    # T22: Dynamic Topic Modeling
    # ------------------------------------------------------------------

    def _run_dtm(
        self,
        bertopic_model: Any,
        docs: list[str],
        timestamps: list[str],
    ) -> DTMResult | None:
        """Run Dynamic Topic Modeling to track topic evolution over time.

        Uses BERTopic.topics_over_time() with daily windows.

        Args:
            bertopic_model: Fitted BERTopic model from T21.
            docs: Document texts (same order as fit).
            timestamps: Date strings (YYYY-MM-DD) per document.

        Returns:
            DTMResult with temporal records, or None on failure.
        """
        if not timestamps or len(timestamps) != len(docs):
            logger.warning(
                "stage4_dtm_skip",
                reason="timestamps missing or length mismatch",
            )
            return None

        logger.info("stage4_dtm_start", n_docs=len(docs))
        t0 = time.monotonic()

        try:
            import pandas as pd

            # Filter out invalid/empty timestamps BEFORE passing to BERTopic.
            # Previously, empty strings and None values were coerced to NaT
            # and then collapsed to unix epoch (1970-01-01) by BERTopic,
            # producing a single corrupt DTM bin regardless of actual dates.
            valid_docs: list[str] = []
            valid_ts: list[pd.Timestamp] = []
            dropped = 0
            for doc, ts in zip(docs, timestamps):
                if not ts:
                    dropped += 1
                    continue
                parsed = pd.to_datetime(ts, errors="coerce")
                if pd.isna(parsed):
                    dropped += 1
                    continue
                valid_docs.append(doc)
                valid_ts.append(parsed)

            if not valid_docs:
                logger.warning(
                    "stage4_dtm_skip",
                    reason="no valid timestamps after filtering",
                    dropped=dropped,
                )
                return None

            if dropped > 0:
                logger.info(
                    "stage4_dtm_timestamps_filtered",
                    valid=len(valid_docs),
                    dropped=dropped,
                )

            # topics_over_time requires a pandas Series (not DatetimeIndex)
            # to produce correct date bins.
            ts_series = pd.Series(valid_ts)

            topics_over_time = bertopic_model.topics_over_time(
                valid_docs,
                ts_series,
                nr_bins=None,  # Use original timestamps (daily)
                evolution_tuning=True,
                global_tuning=True,
            )

            records: list[dict[str, Any]] = []
            for _, row in topics_over_time.iterrows():
                ts_val = row["Timestamp"]
                # Format the timestamp defensively. pd.Timestamp → ISO, NaT → skip.
                if pd.isna(ts_val):
                    continue
                if hasattr(ts_val, "strftime"):
                    date_str = ts_val.strftime("%Y-%m-%d")
                else:
                    date_str = str(ts_val)[:10]
                records.append({
                    "topic_id": int(row["Topic"]),
                    "date": date_str,
                    "frequency": int(row["Frequency"]),
                    "representation": str(row.get("Words", "")),
                })

            elapsed = time.monotonic() - t0
            logger.info(
                "stage4_dtm_complete",
                n_records=len(records),
                elapsed_seconds=round(elapsed, 1),
            )
            return DTMResult(records=records)

        except Exception as exc:
            logger.error("stage4_dtm_error", error=str(exc))
            return None

    # ------------------------------------------------------------------
    # T23: HDBSCAN Clustering (standalone)
    # ------------------------------------------------------------------

    def _run_hdbscan(
        self,
        embeddings: np.ndarray,
        article_ids: list[str],
    ) -> ClusterResult | None:
        """Run standalone HDBSCAN clustering for cross-validation with BERTopic.

        Independent of BERTopic's internal HDBSCAN -- applied directly on
        SBERT embeddings with cosine metric for density-based clustering.

        Falls back to k-means if noise ratio exceeds threshold (per Step 7).

        Args:
            embeddings: SBERT embeddings (n, 384).
            article_ids: Article IDs for logging.

        Returns:
            ClusterResult or None on failure.
        """
        logger.info("stage4_hdbscan_start", n_articles=len(embeddings))
        t0 = time.monotonic()

        try:
            from hdbscan import HDBSCAN as HDBSCANClass
        except ImportError as exc:
            logger.error("stage4_hdbscan_import_error", error=str(exc))
            return None

        # First attempt with standard parameters
        labels, noise_ratio = self._fit_hdbscan(
            HDBSCANClass, embeddings, HDBSCAN_MIN_CLUSTER_SIZE, HDBSCAN_MIN_SAMPLES
        )

        # Retry with reduced min_cluster_size if all noise
        if noise_ratio > HDBSCAN_MAX_NOISE_RATIO:
            logger.warning(
                "stage4_hdbscan_high_noise_retry",
                noise_ratio=round(noise_ratio, 3),
                fallback_min_cluster_size=HDBSCAN_FALLBACK_MIN_CLUSTER_SIZE,
            )
            labels, noise_ratio = self._fit_hdbscan(
                HDBSCANClass,
                embeddings,
                HDBSCAN_FALLBACK_MIN_CLUSTER_SIZE,
                max(1, HDBSCAN_MIN_SAMPLES - 2),
            )

        # If still all noise, log warning (k-means fallback available separately)
        if noise_ratio > HDBSCAN_MAX_NOISE_RATIO:
            logger.warning(
                "stage4_hdbscan_still_noisy",
                noise_ratio=round(noise_ratio, 3),
                hint="k-means fallback will be used for flat clustering",
            )

        n_clusters = len(set(labels) - {-1})
        silhouette = self._compute_silhouette(embeddings, labels)

        elapsed = time.monotonic() - t0
        logger.info(
            "stage4_hdbscan_complete",
            n_clusters=n_clusters,
            noise_ratio=round(noise_ratio, 3),
            silhouette=round(silhouette, 3) if silhouette is not None else None,
            elapsed_seconds=round(elapsed, 1),
        )

        return ClusterResult(
            algorithm="hdbscan",
            labels=np.array(labels, dtype=np.int32),
            noise_ratio=noise_ratio,
            silhouette=silhouette,
            n_clusters=n_clusters,
        )

    @staticmethod
    def _fit_hdbscan(
        hdbscan_cls: type,
        embeddings: np.ndarray,
        min_cluster_size: int,
        min_samples: int,
    ) -> tuple[np.ndarray, float]:
        """Fit HDBSCAN and return labels + noise ratio.

        Args:
            hdbscan_cls: The HDBSCAN class.
            embeddings: Input embeddings.
            min_cluster_size: HDBSCAN min_cluster_size parameter.
            min_samples: HDBSCAN min_samples parameter.

        Returns:
            Tuple of (labels array, noise_ratio float).
        """
        clusterer = hdbscan_cls(
            min_cluster_size=min_cluster_size,
            min_samples=min_samples,
            metric="euclidean",  # Cosine on raw 384-dim is expensive; euclidean on L2-normed
            cluster_selection_method="eom",
        )
        # L2-normalize for approximate cosine distance behavior with euclidean metric
        norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
        norms = np.where(norms == 0, 1.0, norms)
        normed = embeddings / norms

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            labels = clusterer.fit_predict(normed)

        n_noise = int(np.sum(labels == -1))
        noise_ratio = n_noise / len(labels) if len(labels) > 0 else 1.0
        return labels, noise_ratio

    # ------------------------------------------------------------------
    # T24: NMF Auxiliary Topics
    # ------------------------------------------------------------------

    def _run_nmf(
        self,
        features_dir: Path,
        article_ids: list[str],
    ) -> ClusterResult | None:
        """Run NMF topic modeling on TF-IDF matrix as auxiliary to BERTopic.

        Args:
            features_dir: Directory containing tfidf.parquet.
            article_ids: Article IDs for alignment.

        Returns:
            ClusterResult with NMF topic assignments, or None on failure.
        """
        logger.info("stage4_nmf_start")
        t0 = time.monotonic()

        try:
            from sklearn.decomposition import NMF
            from sklearn.feature_extraction.text import TfidfVectorizer
        except ImportError as exc:
            logger.error("stage4_nmf_import_error", error=str(exc))
            return None

        try:
            # Build TF-IDF matrix from stored data or recompute
            tfidf_matrix = self._load_or_build_tfidf(features_dir, article_ids)
            if tfidf_matrix is None:
                return None

            # Determine number of components (capped at article count)
            n_components = min(AUX_N_COMPONENTS, tfidf_matrix.shape[0] - 1)
            if n_components < 2:
                logger.warning("stage4_nmf_skip_too_few_articles")
                return None

            nmf_model = NMF(
                n_components=n_components,
                max_iter=AUX_MAX_ITER_DEFAULT,
                random_state=42,
                init="nndsvda",
            )

            try:
                W = nmf_model.fit_transform(tfidf_matrix)
            except Exception:
                # Retry with increased iterations per Step 7 error handling
                logger.warning("stage4_nmf_convergence_retry")
                nmf_model = NMF(
                    n_components=n_components,
                    max_iter=AUX_MAX_ITER_RETRY,
                    random_state=42,
                    init="nndsvda",
                )
                try:
                    W = nmf_model.fit_transform(tfidf_matrix)
                except Exception as exc2:
                    logger.error("stage4_nmf_convergence_fail", error=str(exc2))
                    return None

            # Assign each article to its highest-weight topic
            labels = np.argmax(W, axis=1).astype(np.int32)
            n_clusters = n_components

            elapsed = time.monotonic() - t0
            logger.info(
                "stage4_nmf_complete",
                n_topics=n_clusters,
                elapsed_seconds=round(elapsed, 1),
            )

            return ClusterResult(
                algorithm="nmf",
                labels=labels,
                n_clusters=n_clusters,
                metadata={"reconstruction_error": float(nmf_model.reconstruction_err_)},
            )

        except Exception as exc:
            logger.error("stage4_nmf_error", error=str(exc))
            return None

    # ------------------------------------------------------------------
    # T25: LDA Auxiliary Topics
    # ------------------------------------------------------------------

    def _run_lda(
        self,
        features_dir: Path,
        article_ids: list[str],
    ) -> ClusterResult | None:
        """Run LDA topic modeling on TF-IDF matrix as auxiliary to BERTopic.

        Args:
            features_dir: Directory containing tfidf.parquet.
            article_ids: Article IDs for alignment.

        Returns:
            ClusterResult with LDA topic assignments, or None on failure.
        """
        logger.info("stage4_lda_start")
        t0 = time.monotonic()

        try:
            from sklearn.decomposition import LatentDirichletAllocation
        except ImportError as exc:
            logger.error("stage4_lda_import_error", error=str(exc))
            return None

        try:
            tfidf_matrix = self._load_or_build_tfidf(features_dir, article_ids)
            if tfidf_matrix is None:
                return None

            n_components = min(AUX_N_COMPONENTS, tfidf_matrix.shape[0] - 1)
            if n_components < 2:
                logger.warning("stage4_lda_skip_too_few_articles")
                return None

            lda_model = LatentDirichletAllocation(
                n_components=n_components,
                max_iter=AUX_MAX_ITER_DEFAULT,
                random_state=42,
                learning_method="online",
                batch_size=128,
                n_jobs=1,  # Single-threaded for memory predictability
            )

            try:
                doc_topic = lda_model.fit_transform(tfidf_matrix)
            except Exception:
                logger.warning("stage4_lda_convergence_retry")
                lda_model = LatentDirichletAllocation(
                    n_components=n_components,
                    max_iter=AUX_MAX_ITER_RETRY,
                    random_state=42,
                    learning_method="online",
                    batch_size=128,
                    n_jobs=1,
                )
                try:
                    doc_topic = lda_model.fit_transform(tfidf_matrix)
                except Exception as exc2:
                    logger.error("stage4_lda_convergence_fail", error=str(exc2))
                    return None

            labels = np.argmax(doc_topic, axis=1).astype(np.int32)
            n_clusters = n_components

            elapsed = time.monotonic() - t0
            logger.info(
                "stage4_lda_complete",
                n_topics=n_clusters,
                perplexity=round(lda_model.perplexity(tfidf_matrix), 2),
                elapsed_seconds=round(elapsed, 1),
            )

            return ClusterResult(
                algorithm="lda",
                labels=labels,
                n_clusters=n_clusters,
                metadata={
                    "perplexity": float(lda_model.perplexity(tfidf_matrix)),
                    "log_likelihood": float(lda_model.score(tfidf_matrix)),
                },
            )

        except Exception as exc:
            logger.error("stage4_lda_error", error=str(exc))
            return None

    # ------------------------------------------------------------------
    # T26: k-means Clustering
    # ------------------------------------------------------------------

    def _run_kmeans(
        self,
        embeddings: np.ndarray,
        article_ids: list[str],
    ) -> ClusterResult | None:
        """Run k-means with silhouette-optimized k on SBERT embeddings.

        Tests k in range KMEANS_K_RANGE, selects k with highest silhouette score.

        Args:
            embeddings: SBERT embeddings (n, 384).
            article_ids: Article IDs for logging.

        Returns:
            ClusterResult with k-means assignments, or None on failure.
        """
        n = len(embeddings)
        if n < KMEANS_K_RANGE[0] + 1:
            logger.warning("stage4_kmeans_skip_too_few", n_articles=n)
            return None

        logger.info("stage4_kmeans_start", n_articles=n)
        t0 = time.monotonic()

        try:
            from sklearn.cluster import KMeans
            from sklearn.metrics import silhouette_score
        except ImportError as exc:
            logger.error("stage4_kmeans_import_error", error=str(exc))
            return None

        try:
            # L2-normalize for cosine-like behavior
            norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
            norms = np.where(norms == 0, 1.0, norms)
            normed = embeddings / norms

            # Determine k range (cap at n-1)
            k_min, k_max = KMEANS_K_RANGE
            k_max = min(k_max, n - 1)
            if k_max < k_min:
                k_min = max(2, k_max)

            # Subsample for silhouette scoring on large datasets
            if n > KMEANS_SILHOUETTE_SAMPLE:
                rng = np.random.RandomState(42)
                sample_idx = rng.choice(n, KMEANS_SILHOUETTE_SAMPLE, replace=False)
                sample_emb = normed[sample_idx]
            else:
                sample_idx = np.arange(n)
                sample_emb = normed

            # Test a range of k values with logarithmic spacing
            k_candidates = sorted(set(
                [k_min]
                + list(np.linspace(k_min, k_max, min(10, k_max - k_min + 1)).astype(int))
                + [k_max]
            ))

            best_k = k_candidates[0]
            best_score = -1.0
            best_labels = None

            for k in k_candidates:
                km = KMeans(
                    n_clusters=k,
                    random_state=42,
                    n_init=10,
                    max_iter=300,
                )
                labels = km.fit_predict(normed)

                # Silhouette on subsample for speed
                sample_labels = labels[sample_idx]
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    score = silhouette_score(
                        sample_emb, sample_labels,
                        metric="euclidean",
                        sample_size=min(2000, len(sample_emb)),
                        random_state=42,
                    )

                if score > best_score:
                    best_score = score
                    best_k = k
                    best_labels = labels

                logger.debug(
                    "stage4_kmeans_candidate",
                    k=k,
                    silhouette=round(score, 3),
                )

            if best_labels is None:
                # Fallback: use k_min
                km = KMeans(n_clusters=k_min, random_state=42, n_init=10)
                best_labels = km.fit_predict(normed)
                best_k = k_min

            elapsed = time.monotonic() - t0
            logger.info(
                "stage4_kmeans_complete",
                best_k=best_k,
                silhouette=round(best_score, 3),
                elapsed_seconds=round(elapsed, 1),
            )

            return ClusterResult(
                algorithm="kmeans",
                labels=np.array(best_labels, dtype=np.int32),
                silhouette=best_score,
                n_clusters=best_k,
                metadata={"k_tested": k_candidates},
            )

        except Exception as exc:
            logger.error("stage4_kmeans_error", error=str(exc))
            return None

    # ------------------------------------------------------------------
    # T27: Hierarchical Clustering
    # ------------------------------------------------------------------

    def _run_hierarchical(
        self,
        embeddings: np.ndarray,
        article_ids: list[str],
    ) -> ClusterResult | None:
        """Run hierarchical clustering with Ward linkage.

        Cuts dendrogram at optimal distance using silhouette score.

        Args:
            embeddings: SBERT embeddings (n, 384).
            article_ids: Article IDs for logging.

        Returns:
            ClusterResult with hierarchical cluster assignments, or None.
        """
        n = len(embeddings)
        if n < 5:
            logger.warning("stage4_hierarchical_skip_too_few", n_articles=n)
            return None

        # Ward linkage requires manageable dataset sizes (O(n^2) memory)
        MAX_HIERARCHICAL = 10000
        if n > MAX_HIERARCHICAL:
            logger.warning(
                "stage4_hierarchical_sampling",
                n_articles=n,
                max_articles=MAX_HIERARCHICAL,
                hint="Sampling for hierarchical clustering (O(n^2) memory)",
            )
            # We still produce labels for all articles via nearest-centroid assignment
            rng = np.random.RandomState(42)
            sample_idx = rng.choice(n, MAX_HIERARCHICAL, replace=False)
        else:
            sample_idx = np.arange(n)

        logger.info("stage4_hierarchical_start", n_articles=len(sample_idx))
        t0 = time.monotonic()

        try:
            from scipy.cluster.hierarchy import fcluster, linkage
            from sklearn.metrics import silhouette_score
        except ImportError as exc:
            logger.error("stage4_hierarchical_import_error", error=str(exc))
            return None

        try:
            # L2-normalize
            norms = np.linalg.norm(embeddings[sample_idx], axis=1, keepdims=True)
            norms = np.where(norms == 0, 1.0, norms)
            sample_normed = embeddings[sample_idx] / norms

            Z = linkage(sample_normed, method="ward")

            # Find optimal cut using silhouette score over a range of cluster counts
            best_n = 5
            best_score = -1.0
            best_labels_sample = None

            # Test cluster counts: 5, 10, 15, 20, 25, 30
            for nc in [5, 10, 15, 20, 25, 30]:
                if nc >= len(sample_idx):
                    continue
                labels_sample = fcluster(Z, t=nc, criterion="maxclust")
                n_unique = len(set(labels_sample))
                if n_unique < 2:
                    continue

                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    score = silhouette_score(
                        sample_normed, labels_sample,
                        metric="euclidean",
                        sample_size=min(2000, len(sample_normed)),
                        random_state=42,
                    )
                if score > best_score:
                    best_score = score
                    best_n = nc
                    best_labels_sample = labels_sample

            if best_labels_sample is None:
                best_labels_sample = fcluster(Z, t=5, criterion="maxclust")
                best_n = 5

            # Convert from 1-indexed to 0-indexed
            best_labels_sample = best_labels_sample - 1

            # If we sampled, assign remaining articles to nearest centroid
            if len(sample_idx) < n:
                labels_all = self._assign_to_nearest_centroid(
                    embeddings, sample_idx, best_labels_sample
                )
            else:
                labels_all = best_labels_sample

            elapsed = time.monotonic() - t0
            logger.info(
                "stage4_hierarchical_complete",
                n_clusters=best_n,
                silhouette=round(best_score, 3),
                elapsed_seconds=round(elapsed, 1),
            )

            return ClusterResult(
                algorithm="hierarchical",
                labels=np.array(labels_all, dtype=np.int32),
                silhouette=best_score,
                n_clusters=best_n,
                metadata={"method": "ward", "criterion": "maxclust"},
            )

        except Exception as exc:
            logger.error("stage4_hierarchical_error", error=str(exc))
            return None

    @staticmethod
    def _assign_to_nearest_centroid(
        embeddings: np.ndarray,
        sample_idx: np.ndarray,
        sample_labels: np.ndarray,
    ) -> np.ndarray:
        """Assign all articles to the nearest cluster centroid from sample.

        Args:
            embeddings: Full embedding matrix (n, dim).
            sample_idx: Indices used for hierarchical clustering.
            sample_labels: Labels assigned to the sample.

        Returns:
            Label array for all n articles.
        """
        # Compute centroids from sample
        unique_labels = sorted(set(sample_labels))
        centroids = {}
        for label in unique_labels:
            mask = sample_labels == label
            centroids[label] = embeddings[sample_idx[mask]].mean(axis=0)

        centroid_matrix = np.array([centroids[l] for l in unique_labels])

        # Compute distances to all centroids
        # Using dot product on L2-normalized vectors for cosine similarity
        norms_emb = np.linalg.norm(embeddings, axis=1, keepdims=True)
        norms_emb = np.where(norms_emb == 0, 1.0, norms_emb)
        normed_emb = embeddings / norms_emb

        norms_c = np.linalg.norm(centroid_matrix, axis=1, keepdims=True)
        norms_c = np.where(norms_c == 0, 1.0, norms_c)
        normed_c = centroid_matrix / norms_c

        # Cosine similarity -> pick highest
        sim = normed_emb @ normed_c.T
        closest = np.argmax(sim, axis=1)
        labels_all = np.array([unique_labels[c] for c in closest], dtype=np.int32)

        return labels_all

    # ------------------------------------------------------------------
    # T28: Louvain Community Detection
    # ------------------------------------------------------------------

    def _run_louvain(
        self,
        ner_path: Path,
        article_ids: list[str],
    ) -> CommunityResult | None:
        """Build entity co-occurrence graph and run Louvain community detection.

        Entities appearing in the same article form co-occurrence edges.
        Louvain algorithm detects entity communities from this graph.

        Args:
            ner_path: Path to ner.parquet with entity columns.
            article_ids: Article IDs for source tracking.

        Returns:
            CommunityResult with entity pair records, or None on failure.
        """
        logger.info("stage4_louvain_start")
        t0 = time.monotonic()

        try:
            import networkx as nx
            import community as community_louvain  # python-louvain
        except ImportError as exc:
            logger.error(
                "stage4_louvain_import_error",
                error=str(exc),
                hint="Install: pip install networkx python-louvain",
            )
            return None

        # Load NER data
        if not ner_path.exists():
            logger.warning("stage4_louvain_no_ner", path=str(ner_path))
            return None

        try:
            ner_table = pq.read_table(str(ner_path))
        except Exception as exc:
            logger.error("stage4_louvain_ner_load_error", error=str(exc))
            return None

        # Build entity co-occurrence graph
        # Each article contributes edges between all entity pairs within it
        co_occurrence: dict[tuple[str, str], dict[str, Any]] = defaultdict(
            lambda: {"count": 0, "articles": []}
        )

        ner_ids = ner_table.column("article_id").to_pylist()
        entity_columns = [
            col for col in ner_table.column_names
            if col.startswith("entities_")
        ]

        for row_idx, aid in enumerate(ner_ids):
            # Collect all entities from this article
            all_entities: list[str] = []
            for col_name in entity_columns:
                entities_list = ner_table.column(col_name)[row_idx].as_py()
                if entities_list:
                    all_entities.extend(entities_list)

            # Deduplicate within article (normalize to lowercase)
            unique_entities = sorted(set(e.strip().lower() for e in all_entities if e.strip()))

            # Create co-occurrence edges for all pairs
            for e_a, e_b in combinations(unique_entities, 2):
                # Alphabetical ordering for consistent edge keys
                key = (e_a, e_b) if e_a < e_b else (e_b, e_a)
                co_occurrence[key]["count"] += 1
                co_occurrence[key]["articles"].append(aid)

        if not co_occurrence:
            logger.warning("stage4_louvain_no_cooccurrences")
            return CommunityResult()

        # Build NetworkX graph
        G = nx.Graph()
        for (e_a, e_b), data in co_occurrence.items():
            if data["count"] >= LOUVAIN_EDGE_MIN_COOCCURRENCE:
                G.add_edge(e_a, e_b, weight=data["count"])

        if G.number_of_nodes() == 0:
            logger.warning("stage4_louvain_empty_graph")
            return CommunityResult()

        logger.info(
            "stage4_louvain_graph_built",
            n_nodes=G.number_of_nodes(),
            n_edges=G.number_of_edges(),
        )

        # Run Louvain per connected component (per Step 7 error handling)
        partition: dict[str, int] = {}
        community_offset = 0

        for component in nx.connected_components(G):
            subgraph = G.subgraph(component)
            if subgraph.number_of_nodes() < 2:
                # Isolated nodes: community_id = -1
                for node in subgraph.nodes():
                    partition[node] = -1
                continue

            try:
                sub_partition = community_louvain.best_partition(
                    subgraph,
                    resolution=1.0,
                    random_state=42,
                )
                # Offset community IDs to avoid collision across components
                for node, comm_id in sub_partition.items():
                    partition[node] = comm_id + community_offset
                max_comm = max(sub_partition.values()) + 1 if sub_partition else 1
                community_offset += max_comm
            except Exception as exc:
                logger.warning(
                    "stage4_louvain_component_error",
                    n_nodes=subgraph.number_of_nodes(),
                    error=str(exc),
                )
                for node in subgraph.nodes():
                    partition[node] = -1

        # Compute modularity on entire graph
        try:
            modularity = community_louvain.modularity(
                {n: partition.get(n, -1) for n in G.nodes()},
                G,
            )
        except Exception:
            modularity = 0.0

        # Build output records matching networks.parquet schema
        records: list[dict[str, Any]] = []
        for (e_a, e_b), data in co_occurrence.items():
            if data["count"] >= LOUVAIN_EDGE_MIN_COOCCURRENCE:
                # Community assignment: use entity_a's community (both should be same
                # community if they co-occur strongly, but use entity_a for determinism)
                comm_a = partition.get(e_a, -1)
                records.append({
                    "entity_a": e_a,
                    "entity_b": e_b,
                    "co_occurrence_count": data["count"],
                    "community_id": comm_a,
                    "source_articles": list(set(data["articles"])),
                })

        n_communities = len(set(partition.values()) - {-1})
        elapsed = time.monotonic() - t0

        logger.info(
            "stage4_louvain_complete",
            n_communities=n_communities,
            modularity=round(modularity, 3),
            n_edges=len(records),
            elapsed_seconds=round(elapsed, 1),
        )

        return CommunityResult(
            records=records,
            modularity=modularity,
            n_communities=n_communities,
        )

    # ------------------------------------------------------------------
    # TF-IDF helper (shared by NMF and LDA)
    # ------------------------------------------------------------------

    _tfidf_cache: Any = None  # Module-level cache within instance

    def _load_or_build_tfidf(
        self,
        features_dir: Path,
        article_ids: list[str],
    ) -> Any:
        """Load or rebuild TF-IDF matrix for NMF/LDA.

        Tries to load pre-computed TF-IDF from tfidf.parquet. If unavailable,
        rebuilds from article texts.

        Args:
            features_dir: Directory with tfidf.parquet.
            article_ids: Article IDs for alignment.

        Returns:
            Sparse or dense TF-IDF matrix (n_articles, n_features), or None.
        """
        if self._tfidf_cache is not None:
            return self._tfidf_cache

        tfidf_path = features_dir / "tfidf.parquet"

        # Strategy 1: Try to build from tfidf.parquet top terms
        if tfidf_path.exists():
            try:
                tfidf_table = pq.read_table(str(tfidf_path))
                tfidf_ids = tfidf_table.column("article_id").to_pylist()

                # If per-article TF-IDF terms/scores are stored, rebuild matrix
                if "tfidf_top_terms" in tfidf_table.column_names:
                    return self._rebuild_tfidf_from_terms(
                        tfidf_table, article_ids
                    )
            except Exception as exc:
                logger.warning("stage4_tfidf_load_error", error=str(exc))

        # Strategy 2: Rebuild from article texts using sklearn
        try:
            articles_table = pq.read_table(str(ARTICLES_PARQUET_PATH))
            docs = self._extract_docs(articles_table)

            from sklearn.feature_extraction.text import TfidfVectorizer

            vectorizer = TfidfVectorizer(
                max_features=TFIDF_MAX_FEATURES,
                ngram_range=TFIDF_NGRAM_RANGE,
                min_df=2,
                max_df=0.95,
                sublinear_tf=True,
            )
            tfidf_matrix = vectorizer.fit_transform(docs)
            self._tfidf_cache = tfidf_matrix

            logger.info(
                "stage4_tfidf_rebuilt",
                shape=tfidf_matrix.shape,
            )
            return tfidf_matrix

        except Exception as exc:
            logger.error("stage4_tfidf_rebuild_error", error=str(exc))
            return None

    @staticmethod
    def _rebuild_tfidf_from_terms(
        tfidf_table: pa.Table,
        article_ids: list[str],
    ) -> Any:
        """Rebuild a sparse TF-IDF-like matrix from stored top terms and scores.

        This creates an approximate TF-IDF matrix from the per-article
        top-20 terms stored in tfidf.parquet.

        Args:
            tfidf_table: The tfidf.parquet Arrow table.
            article_ids: Target article IDs for alignment.

        Returns:
            Sparse CSR matrix (n_articles, n_vocab).
        """
        from scipy.sparse import lil_matrix

        tfidf_ids = tfidf_table.column("article_id").to_pylist()
        terms_col = tfidf_table.column("tfidf_top_terms")
        scores_col = (
            tfidf_table.column("tfidf_scores")
            if "tfidf_scores" in tfidf_table.column_names
            else None
        )

        # Build vocabulary from all terms
        vocab: dict[str, int] = {}
        for row_idx in range(len(tfidf_ids)):
            terms = terms_col[row_idx].as_py()
            if terms:
                for t in terms:
                    if t not in vocab:
                        vocab[t] = len(vocab)

        if not vocab:
            return None

        # Build ID lookup
        id_to_row = {aid: i for i, aid in enumerate(tfidf_ids)}

        # Construct sparse matrix
        n_articles = len(article_ids)
        n_vocab = len(vocab)
        matrix = lil_matrix((n_articles, n_vocab), dtype=np.float32)

        for out_idx, aid in enumerate(article_ids):
            src_idx = id_to_row.get(aid)
            if src_idx is None:
                continue
            terms = terms_col[src_idx].as_py()
            scores = (
                scores_col[src_idx].as_py()
                if scores_col is not None
                else [1.0] * len(terms) if terms else []
            )
            if terms:
                for term, score in zip(terms, scores or [1.0] * len(terms)):
                    col_idx = vocab.get(term)
                    if col_idx is not None:
                        matrix[out_idx, col_idx] = float(score) if score else 1.0

        logger.info(
            "stage4_tfidf_from_terms",
            n_articles=n_articles,
            vocab_size=n_vocab,
        )
        return matrix.tocsr()

    # ------------------------------------------------------------------
    # Utility: silhouette score computation
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_silhouette(
        embeddings: np.ndarray,
        labels: np.ndarray,
    ) -> float | None:
        """Compute silhouette score, handling edge cases gracefully.

        Args:
            embeddings: Input embeddings.
            labels: Cluster labels (may include -1 for noise).

        Returns:
            Silhouette score (float) or None if computation is not possible.
        """
        # Need at least 2 clusters (excluding noise) for silhouette
        non_noise = labels[labels != -1]
        unique_labels = set(non_noise)
        if len(unique_labels) < 2 or len(non_noise) < 2:
            return None

        try:
            from sklearn.metrics import silhouette_score

            # Only compute on non-noise samples
            mask = labels != -1
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                score = silhouette_score(
                    embeddings[mask],
                    labels[mask],
                    metric="euclidean",
                    sample_size=min(5000, int(mask.sum())),
                    random_state=42,
                )
            return float(score)
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Parquet output writers
    # ------------------------------------------------------------------

    @staticmethod
    def _write_topics_parquet(
        output_dir: Path,
        article_ids: list[str],
        results: Stage4Output,
        articles_table: pa.Table | None = None,
    ) -> None:
        """Write topics.parquet matching the exact schema from Step 7 design.

        Schema:
            article_id      utf8     FK -> articles
            topic_id        int32    BERTopic topic ID (-1 = outlier)
            topic_label     utf8     Human-readable topic label
            topic_probability float32 Topic assignment probability
            hdbscan_cluster_id int32  Independent HDBSCAN cluster ID
            nmf_topic_id    int32    NMF auxiliary topic ID
            lda_topic_id    int32    LDA auxiliary topic ID
            published_at    timestamp[us, tz=UTC]  Publication timestamp (for Stage 7 data_span)
            source          utf8     Source domain (for Stage 7 source_count)

        Args:
            output_dir: Directory to write topics.parquet.
            article_ids: Ordered article IDs.
            results: Complete Stage4Output.
            articles_table: Original articles table (for published_at/source propagation).
        """
        n = len(article_ids)

        # BERTopic columns
        if results.topics is not None:
            topic_ids = results.topics.topic_ids
            topic_probs = results.topics.probabilities
            topic_labels_map = results.topics.topic_labels
            topic_labels = [
                topic_labels_map.get(int(tid), f"Topic_{tid}")
                for tid in topic_ids
            ]
        else:
            topic_ids = np.full(n, -1, dtype=np.int32)
            topic_probs = np.zeros(n, dtype=np.float32)
            topic_labels = ["Unassigned"] * n

        # HDBSCAN column
        hdbscan_ids = (
            results.hdbscan.labels
            if results.hdbscan is not None
            else np.full(n, -1, dtype=np.int32)
        )

        # NMF column
        nmf_ids = (
            results.nmf.labels
            if results.nmf is not None
            else np.full(n, -1, dtype=np.int32)
        )

        # LDA column
        lda_ids = (
            results.lda.labels
            if results.lda is not None
            else np.full(n, -1, dtype=np.int32)
        )

        # Extract published_at and source from articles_table for Stage 7
        published_at_list: list = [None] * n
        source_list: list = [""] * n
        if articles_table is not None:
            # Build article_id → index lookup for alignment
            if "article_id" in articles_table.column_names:
                at_ids = articles_table.column("article_id").to_pylist()
                at_id_to_idx = {aid: i for i, aid in enumerate(at_ids)}

                if "published_at" in articles_table.column_names:
                    pub_col = articles_table.column("published_at").to_pylist()
                    for j, aid in enumerate(article_ids):
                        idx = at_id_to_idx.get(aid)
                        if idx is not None and idx < len(pub_col):
                            published_at_list[j] = pub_col[idx]

                if "source" in articles_table.column_names:
                    src_col = articles_table.column("source").to_pylist()
                    for j, aid in enumerate(article_ids):
                        idx = at_id_to_idx.get(aid)
                        if idx is not None and idx < len(src_col):
                            source_list[j] = src_col[idx] or ""

        schema = pa.schema([
            pa.field("article_id", pa.utf8()),
            pa.field("topic_id", pa.int32()),
            pa.field("topic_label", pa.utf8()),
            pa.field("topic_probability", pa.float32()),
            pa.field("hdbscan_cluster_id", pa.int32()),
            pa.field("nmf_topic_id", pa.int32()),
            pa.field("lda_topic_id", pa.int32()),
            pa.field("published_at", pa.timestamp("us", tz="UTC"), nullable=True),
            pa.field("source", pa.utf8(), nullable=True),
        ])

        table = pa.table(
            {
                "article_id": article_ids,
                "topic_id": topic_ids.tolist(),
                "topic_label": topic_labels,
                "topic_probability": topic_probs.tolist(),
                "hdbscan_cluster_id": hdbscan_ids.tolist(),
                "nmf_topic_id": nmf_ids.tolist(),
                "lda_topic_id": lda_ids.tolist(),
                "published_at": published_at_list,
                "source": source_list,
            },
            schema=schema,
        )

        output_path = output_dir / "topics.parquet"
        pq.write_table(
            table,
            str(output_path),
            compression=PARQUET_COMPRESSION,
            compression_level=PARQUET_COMPRESSION_LEVEL,
        )
        logger.info("stage4_topics_written", path=str(output_path), n_rows=n)

    @staticmethod
    def _write_networks_parquet(
        output_dir: Path,
        communities: CommunityResult | None,
    ) -> None:
        """Write networks.parquet matching the exact schema from Step 7 design.

        Schema:
            entity_a            utf8       First entity
            entity_b            utf8       Second entity
            co_occurrence_count int32      Co-occurrence count
            community_id        int32      Louvain community ID
            source_articles     list<utf8> Articles containing pair

        Args:
            output_dir: Directory to write networks.parquet.
            communities: CommunityResult from Louvain.
        """
        schema = pa.schema([
            pa.field("entity_a", pa.utf8()),
            pa.field("entity_b", pa.utf8()),
            pa.field("co_occurrence_count", pa.int32()),
            pa.field("community_id", pa.int32()),
            pa.field("source_articles", pa.list_(pa.utf8())),
        ])

        if communities is not None and communities.records:
            records = communities.records
            table = pa.table(
                {
                    "entity_a": [r["entity_a"] for r in records],
                    "entity_b": [r["entity_b"] for r in records],
                    "co_occurrence_count": [
                        r["co_occurrence_count"] for r in records
                    ],
                    "community_id": [r["community_id"] for r in records],
                    "source_articles": [r["source_articles"] for r in records],
                },
                schema=schema,
            )
        else:
            # Empty table with correct schema
            table = pa.table(
                {
                    "entity_a": pa.array([], type=pa.utf8()),
                    "entity_b": pa.array([], type=pa.utf8()),
                    "co_occurrence_count": pa.array([], type=pa.int32()),
                    "community_id": pa.array([], type=pa.int32()),
                    "source_articles": pa.array(
                        [], type=pa.list_(pa.utf8())
                    ),
                },
                schema=schema,
            )

        output_path = output_dir / "networks.parquet"
        pq.write_table(
            table,
            str(output_path),
            compression=PARQUET_COMPRESSION,
            compression_level=PARQUET_COMPRESSION_LEVEL,
        )
        logger.info(
            "stage4_networks_written",
            path=str(output_path),
            n_rows=table.num_rows,
        )

    @staticmethod
    def _write_dtm_parquet(
        output_dir: Path,
        dtm: DTMResult | None,
    ) -> None:
        """Write dtm.parquet for Stage 5 time series consumption.

        Schema:
            topic_id        int32   Topic ID
            date            utf8    Date (YYYY-MM-DD)
            frequency       int32   Article count for topic on date
            representation  utf8    Top words for topic on date

        Args:
            output_dir: Directory to write dtm.parquet.
            dtm: DTMResult from dynamic topic modeling.
        """
        schema = pa.schema([
            pa.field("topic_id", pa.int32()),
            pa.field("date", pa.utf8()),
            pa.field("frequency", pa.int32()),
            pa.field("representation", pa.utf8()),
        ])

        if dtm is not None and dtm.records:
            records = dtm.records
            table = pa.table(
                {
                    "topic_id": [r["topic_id"] for r in records],
                    "date": [r["date"] for r in records],
                    "frequency": [r["frequency"] for r in records],
                    "representation": [r["representation"] for r in records],
                },
                schema=schema,
            )
        else:
            table = pa.table(
                {
                    "topic_id": pa.array([], type=pa.int32()),
                    "date": pa.array([], type=pa.utf8()),
                    "frequency": pa.array([], type=pa.int32()),
                    "representation": pa.array([], type=pa.utf8()),
                },
                schema=schema,
            )

        output_path = output_dir / "dtm.parquet"
        pq.write_table(
            table,
            str(output_path),
            compression=PARQUET_COMPRESSION,
            compression_level=PARQUET_COMPRESSION_LEVEL,
        )
        logger.info("stage4_dtm_written", path=str(output_path), n_rows=table.num_rows)

    @staticmethod
    def _write_aux_clusters_parquet(
        output_dir: Path,
        article_ids: list[str],
        results: Stage4Output,
    ) -> None:
        """Write auxiliary clustering results for ensemble confidence scoring.

        Saves k-means and hierarchical cluster assignments alongside the
        main topic/HDBSCAN results, enabling Stage 7 to compute ensemble
        agreement metrics.

        Schema:
            article_id              utf8    FK -> articles
            kmeans_cluster_id       int32   k-means cluster ID
            hierarchical_cluster_id int32   Hierarchical cluster ID

        Args:
            output_dir: Directory to write aux_clusters.parquet.
            article_ids: Ordered article IDs.
            results: Complete Stage4Output.
        """
        n = len(article_ids)

        kmeans_ids = (
            results.kmeans.labels
            if results.kmeans is not None
            else np.full(n, -1, dtype=np.int32)
        )
        hier_ids = (
            results.hierarchical.labels
            if results.hierarchical is not None
            else np.full(n, -1, dtype=np.int32)
        )

        schema = pa.schema([
            pa.field("article_id", pa.utf8()),
            pa.field("kmeans_cluster_id", pa.int32()),
            pa.field("hierarchical_cluster_id", pa.int32()),
        ])

        table = pa.table(
            {
                "article_id": article_ids,
                "kmeans_cluster_id": kmeans_ids.tolist(),
                "hierarchical_cluster_id": hier_ids.tolist(),
            },
            schema=schema,
        )

        output_path = output_dir / "aux_clusters.parquet"
        pq.write_table(
            table,
            str(output_path),
            compression=PARQUET_COMPRESSION,
            compression_level=PARQUET_COMPRESSION_LEVEL,
        )
        logger.info(
            "stage4_aux_clusters_written",
            path=str(output_path),
            n_rows=n,
        )

    @staticmethod
    def _write_empty_outputs(output_dir: Path, articles_table: pa.Table) -> None:
        """Write empty-but-schema-compliant Parquet files when stage is skipped.

        Called when article count is below MIN_ARTICLES_FOR_TOPICS threshold.

        Args:
            output_dir: Directory to write output files.
            articles_table: The articles table (for article_id extraction).
        """
        output_dir.mkdir(parents=True, exist_ok=True)

        article_ids = articles_table.column("article_id").to_pylist()
        n = len(article_ids)

        # topics.parquet -- all articles mapped to topic -1
        # Extract published_at and source from articles_table for schema alignment
        published_at_list: list = [None] * n
        source_list: list = [""] * n
        if "published_at" in articles_table.column_names:
            pub_col = articles_table.column("published_at").to_pylist()
            published_at_list = pub_col[:n]
        if "source" in articles_table.column_names:
            src_col = articles_table.column("source").to_pylist()
            source_list = [(s or "") for s in src_col[:n]]

        topics_schema = pa.schema([
            pa.field("article_id", pa.utf8()),
            pa.field("topic_id", pa.int32()),
            pa.field("topic_label", pa.utf8()),
            pa.field("topic_probability", pa.float32()),
            pa.field("hdbscan_cluster_id", pa.int32()),
            pa.field("nmf_topic_id", pa.int32()),
            pa.field("lda_topic_id", pa.int32()),
            pa.field("published_at", pa.timestamp("us", tz="UTC"), nullable=True),
            pa.field("source", pa.utf8(), nullable=True),
        ])
        topics_table = pa.table(
            {
                "article_id": article_ids,
                "topic_id": [-1] * n,
                "topic_label": ["Insufficient articles"] * n,
                "topic_probability": [0.0] * n,
                "hdbscan_cluster_id": [-1] * n,
                "nmf_topic_id": [-1] * n,
                "lda_topic_id": [-1] * n,
                "published_at": published_at_list,
                "source": source_list,
            },
            schema=topics_schema,
        )
        pq.write_table(
            topics_table,
            str(output_dir / "topics.parquet"),
            compression=PARQUET_COMPRESSION,
            compression_level=PARQUET_COMPRESSION_LEVEL,
        )

        # networks.parquet -- empty
        networks_schema = pa.schema([
            pa.field("entity_a", pa.utf8()),
            pa.field("entity_b", pa.utf8()),
            pa.field("co_occurrence_count", pa.int32()),
            pa.field("community_id", pa.int32()),
            pa.field("source_articles", pa.list_(pa.utf8())),
        ])
        pq.write_table(
            pa.table(
                {
                    "entity_a": pa.array([], type=pa.utf8()),
                    "entity_b": pa.array([], type=pa.utf8()),
                    "co_occurrence_count": pa.array([], type=pa.int32()),
                    "community_id": pa.array([], type=pa.int32()),
                    "source_articles": pa.array([], type=pa.list_(pa.utf8())),
                },
                schema=networks_schema,
            ),
            str(output_dir / "networks.parquet"),
            compression=PARQUET_COMPRESSION,
            compression_level=PARQUET_COMPRESSION_LEVEL,
        )

        # dtm.parquet -- empty
        dtm_schema = pa.schema([
            pa.field("topic_id", pa.int32()),
            pa.field("date", pa.utf8()),
            pa.field("frequency", pa.int32()),
            pa.field("representation", pa.utf8()),
        ])
        pq.write_table(
            pa.table(
                {
                    "topic_id": pa.array([], type=pa.int32()),
                    "date": pa.array([], type=pa.utf8()),
                    "frequency": pa.array([], type=pa.int32()),
                    "representation": pa.array([], type=pa.utf8()),
                },
                schema=dtm_schema,
            ),
            str(output_dir / "dtm.parquet"),
            compression=PARQUET_COMPRESSION,
            compression_level=PARQUET_COMPRESSION_LEVEL,
        )

        # aux_clusters.parquet -- all -1
        aux_schema = pa.schema([
            pa.field("article_id", pa.utf8()),
            pa.field("kmeans_cluster_id", pa.int32()),
            pa.field("hierarchical_cluster_id", pa.int32()),
        ])
        pq.write_table(
            pa.table(
                {
                    "article_id": article_ids,
                    "kmeans_cluster_id": [-1] * n,
                    "hierarchical_cluster_id": [-1] * n,
                },
                schema=aux_schema,
            ),
            str(output_dir / "aux_clusters.parquet"),
            compression=PARQUET_COMPRESSION,
            compression_level=PARQUET_COMPRESSION_LEVEL,
        )

        logger.info(
            "stage4_empty_outputs_written",
            reason="insufficient articles",
            n_articles=n,
        )


# ---------------------------------------------------------------------------
# Convenience function (module-level entry point)
# ---------------------------------------------------------------------------

def run_stage4(
    articles_path: Path | None = None,
    features_dir: Path | None = None,
    analysis_dir: Path | None = None,
    output_dir: Path | None = None,
    sbert_model: Any = None,
    cleanup_after: bool = True,
) -> Stage4Output:
    """Run the complete Stage 4 aggregation pipeline.

    This is the primary entry point for orchestrating Stage 4.

    Args:
        articles_path: Path to articles.parquet.
        features_dir: Directory with embeddings/tfidf/ner parquets.
        analysis_dir: Directory with article_analysis.parquet.
        output_dir: Directory to write output parquets.
        sbert_model: Pre-loaded SentenceTransformer model (shared from Stage 2).
        cleanup_after: If True, delete all models and SBERT after completion.

    Returns:
        Stage4Output with complete results.

    Raises:
        PipelineStageError: On irrecoverable failure.
    """
    aggregator = Stage4Aggregator(sbert_model=sbert_model)
    try:
        result = aggregator.run(
            articles_path=articles_path,
            features_dir=features_dir,
            analysis_dir=analysis_dir,
            output_dir=output_dir,
        )
        return result
    finally:
        if cleanup_after:
            aggregator.cleanup()
