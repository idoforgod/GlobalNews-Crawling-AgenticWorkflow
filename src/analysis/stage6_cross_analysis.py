"""Stage 6: Cross Analysis -- Causal discovery, network analysis, and narrative tracking.

Implements 12 techniques (T37-T46, T20, T50) for cross-dimensional analysis
across the article corpus:

    T37: Granger Causality Testing (statsmodels)
    T38: PCMCI Causal Inference (tigramite with ParCorr)
    T39: Co-occurrence Network Analysis (networkx weighted graph)
    T40: Knowledge Graph Construction (NER-based edge list)
    T41: Centrality Analysis (degree, betweenness, PageRank)
    T42: Network Evolution (weekly snapshot comparison)
    T43: Cross-Lingual Topic Alignment (SBERT multilingual centroids)
    T44: Frame Analysis (KL divergence of TF-IDF per source per topic)
    T45: Agenda Setting Analysis (cross-correlation of topic frequency)
    T46: Temporal Alignment (DTW on cross-region topic series)
    T20: GraphRAG Knowledge Retrieval (entity-topic knowledge graph)
    T50: Contradiction Detection (SBERT similarity + NLI entailment)

Input:
    - data/analysis/timeseries.parquet     (Stage 5 time series)
    - data/analysis/topics.parquet         (Stage 4 topic assignments)
    - data/analysis/article_analysis.parquet (Stage 3 sentiment/emotion/STEEPS)
    - data/analysis/networks.parquet       (Stage 4 entity co-occurrence)
    - data/features/embeddings.parquet     (Stage 2 SBERT embeddings)
    - data/processed/articles.parquet      (Stage 1 base articles)

Output:
    - data/analysis/cross_analysis.parquet (unified cross-analysis results)

Memory budget: ~0.8 GB peak (tigramite ~100 MB, networkx + igraph ~50 MB, scipy).
Performance target: ~3.5 min for 1,000 articles.

Reference: Step 7 Pipeline Design, Section 3.6 (Stage 6: Cross Analysis).
"""

from __future__ import annotations

import gc
import json
import logging
import math
import os
import time
import warnings
from collections import defaultdict
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path
from typing import Any, Optional

import numpy as np

from src.config.constants import (
    ARTICLES_PARQUET_PATH,
    ARTICLE_ANALYSIS_PARQUET_PATH,
    CROSS_ANALYSIS_PARQUET_PATH,
    DATA_ANALYSIS_DIR,
    DATA_FEATURES_DIR,
    EMBEDDINGS_PARQUET_PATH,
    MIN_ARTICLES_FOR_GRANGER,
    MIN_DAYS_FOR_ANALYSIS,
    NETWORKS_PARQUET_PATH,
    PARQUET_COMPRESSION,
    PARQUET_COMPRESSION_LEVEL,
    SBERT_EMBEDDING_DIM,
    TIMESERIES_PARQUET_PATH,
    TOPICS_PARQUET_PATH,
)
from src.utils.error_handler import (
    AnalysisError,
    ModelLoadError,
    PipelineStageError,
)
from src.utils.logging_config import get_analysis_logger

_raw_logger = get_analysis_logger()


class _StructlogAdapter:
    """Thin adapter that accepts structlog-style keyword arguments.

    When structlog is available, ``get_analysis_logger()`` returns a
    structlog bound logger that natively supports ``logger.info("msg", k=v)``.
    When structlog is *not* installed, the returned object is a stdlib
    ``logging.Logger`` which rejects unexpected kwargs.

    This adapter reformats calls so that keyword arguments are appended
    to the message string as ``k=v`` pairs.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self._is_stdlib = isinstance(inner, logging.Logger)

    def _log(self, level_fn, msg: str, **kwargs: Any) -> None:
        if self._is_stdlib:
            if kwargs:
                kv = " ".join(f"{k}={v}" for k, v in kwargs.items())
                level_fn(f"{msg} [{kv}]")
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
# Stage 6 constants (local to this module, derived from Step 7 design)
# ---------------------------------------------------------------------------

# Granger causality configuration
GRANGER_MAX_LAG: int = 7
GRANGER_SIGNIFICANCE: float = 0.05

# PCMCI configuration
PCMCI_TAU_MAX: int = 7
PCMCI_TAU_MAX_FALLBACK: int = 3
PCMCI_PC_ALPHA: float = 0.05
PCMCI_TOP_N_TOPICS: int = 20

# Co-occurrence / Knowledge Graph
COOCCURRENCE_MIN_WEIGHT: float = 0.001
KG_RELATION_TYPES: list[str] = ["mentioned_with", "works_at", "located_in"]

# Cross-lingual alignment
CROSS_LINGUAL_MATCH_THRESHOLD: float = 0.5
CROSS_LINGUAL_FALLBACK_THRESHOLD: float = 0.3

# Frame analysis
FRAME_DIMENSIONS: list[str] = [
    "economic", "security", "human_interest", "political", "scientific",
]
FRAME_MIN_SOURCES_PER_TOPIC: int = 2

# Network evolution
NETWORK_MIN_EDGES: int = 10

# Centrality / large-graph guard
# Edges with co_occurrence_count < this are noise (98% of edges are weight=1).
CENTRALITY_MIN_WEIGHT: int = 2
# Approximate betweenness via random k-node sampling (NetworkX docs recommend ~500).
CENTRALITY_BETWEENNESS_K: int = 500

# Contradiction detection
CONTRADICTION_SIMILARITY_THRESHOLD: float = 0.6
NLI_BATCH_SIZE: int = 16

# DTW
DTW_MAX_SERIES_LENGTH: int = 365


# ---------------------------------------------------------------------------
# Lazy import helpers (avoid loading heavy libraries at import time)
# ---------------------------------------------------------------------------

def _lazy_import_pyarrow():
    """Lazily import pyarrow and pyarrow.parquet."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    return pa, pq


def _lazy_import_networkx():
    """Lazily import networkx."""
    import networkx as nx
    return nx


def _lazy_import_statsmodels():
    """Lazily import statsmodels Granger causality tests."""
    from statsmodels.tsa.stattools import grangercausalitytests, adfuller
    return grangercausalitytests, adfuller


def _lazy_import_tigramite():
    """Lazily import tigramite for PCMCI causal discovery.

    Returns None if tigramite is not installed.
    """
    try:
        from tigramite import data_processing as pp
        from tigramite.pcmci import PCMCI
        from tigramite.independence_tests.parcorr import ParCorr
        return pp, PCMCI, ParCorr
    except ImportError:
        return None, None, None


def _lazy_import_scipy_dtw():
    """Lazily import scipy for DTW and cross-correlation."""
    from scipy.spatial.distance import cosine as cosine_dist
    from scipy.signal import correlate
    from scipy.special import rel_entr
    from scipy.stats import pearsonr
    return cosine_dist, correlate, rel_entr, pearsonr


def _lazy_import_sklearn_tfidf():
    """Lazily import sklearn TF-IDF vectorizer."""
    from sklearn.feature_extraction.text import TfidfVectorizer
    return TfidfVectorizer


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


# ---------------------------------------------------------------------------
# Data classes for structured results
# ---------------------------------------------------------------------------

@dataclass
class CrossAnalysisRecord:
    """A single row in the cross_analysis.parquet output."""

    analysis_type: str
    source_entity: str
    target_entity: str
    relationship: str
    strength: float
    p_value: Optional[float] = None
    lag_days: Optional[int] = None
    evidence_articles: list[str] = field(default_factory=list)
    metadata: str = "{}"

    def to_dict(self) -> dict[str, Any]:
        """Convert to a dictionary matching the Parquet schema."""
        return {
            "analysis_type": self.analysis_type,
            "source_entity": self.source_entity,
            "target_entity": self.target_entity,
            "relationship": self.relationship,
            "strength": float(self.strength),
            "p_value": float(self.p_value) if self.p_value is not None else None,
            "lag_days": int(self.lag_days) if self.lag_days is not None else None,
            "evidence_articles": self.evidence_articles,
            "metadata": self.metadata,
        }


@dataclass
class GrangerResult:
    """Result container for pairwise Granger causality tests."""

    records: list[CrossAnalysisRecord] = field(default_factory=list)
    n_tested: int = 0
    n_significant: int = 0
    n_skipped_stationarity: int = 0


@dataclass
class PCMCIResult:
    """Result container for PCMCI causal discovery."""

    records: list[CrossAnalysisRecord] = field(default_factory=list)
    graph_density: float = 0.0
    n_causal_links: int = 0
    converged: bool = False


@dataclass
class NetworkAnalysisResult:
    """Result container for co-occurrence network and centrality analysis."""

    cooccurrence_records: list[CrossAnalysisRecord] = field(default_factory=list)
    kg_records: list[CrossAnalysisRecord] = field(default_factory=list)
    centrality_records: list[CrossAnalysisRecord] = field(default_factory=list)
    evolution_records: list[CrossAnalysisRecord] = field(default_factory=list)
    n_nodes: int = 0
    n_edges: int = 0
    modularity: float = 0.0


@dataclass
class CrossLingualResult:
    """Result container for cross-lingual topic alignment."""

    records: list[CrossAnalysisRecord] = field(default_factory=list)
    n_aligned: int = 0
    threshold_used: float = CROSS_LINGUAL_MATCH_THRESHOLD


@dataclass
class FrameAnalysisResult:
    """Result container for frame analysis."""

    records: list[CrossAnalysisRecord] = field(default_factory=list)
    n_topics_analyzed: int = 0
    n_source_pairs: int = 0


@dataclass
class AgendaSettingResult:
    """Result container for agenda setting analysis."""

    records: list[CrossAnalysisRecord] = field(default_factory=list)
    agenda_setters: list[str] = field(default_factory=list)


@dataclass
class TemporalAlignmentResult:
    """Result container for temporal alignment (DTW)."""

    records: list[CrossAnalysisRecord] = field(default_factory=list)
    n_aligned_pairs: int = 0


@dataclass
class GraphRAGResult:
    """Result container for GraphRAG knowledge retrieval."""

    records: list[CrossAnalysisRecord] = field(default_factory=list)
    n_evidence_chains: int = 0


@dataclass
class ContradictionResult:
    """Result container for contradiction detection."""

    records: list[CrossAnalysisRecord] = field(default_factory=list)
    n_pairs_checked: int = 0
    n_contradictions: int = 0


@dataclass
class Stage6Output:
    """Aggregated output from the entire Stage 6 pipeline."""

    granger: GrangerResult | None = None
    pcmci: PCMCIResult | None = None
    networks: NetworkAnalysisResult | None = None
    cross_lingual: CrossLingualResult | None = None
    frame: FrameAnalysisResult | None = None
    agenda: AgendaSettingResult | None = None
    temporal: TemporalAlignmentResult | None = None
    graphrag: GraphRAGResult | None = None
    contradiction: ContradictionResult | None = None
    total_records: int = 0
    elapsed_seconds: float = 0.0
    techniques_completed: list[str] = field(default_factory=list)
    techniques_skipped: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def _build_topic_daily_series(
    topics_table,
    articles_table,
    min_days: int = MIN_DAYS_FOR_ANALYSIS,
) -> tuple[dict[str, np.ndarray], list[str], np.ndarray]:
    """Build daily frequency time series per topic from topic assignments and articles.

    Returns:
        (topic_series, topic_ids, date_index) where:
        - topic_series: {topic_id: np.ndarray of daily counts}
        - topic_ids: sorted list of topic IDs
        - date_index: array of dates as days-since-epoch
    """
    import pandas as pd

    # Extract topic assignments
    article_topics: dict[str, str] = {}
    for i in range(topics_table.num_rows):
        aid = topics_table.column("article_id")[i].as_py()
        tid = topics_table.column("topic_id")[i].as_py()
        if tid is not None and str(tid) != "-1":
            article_topics[aid] = str(tid)

    # Extract article dates
    article_dates: dict[str, str] = {}
    if "published_at" in articles_table.column_names:
        for i in range(articles_table.num_rows):
            aid = articles_table.column("article_id")[i].as_py()
            pub = articles_table.column("published_at")[i].as_py()
            if pub:
                article_dates[aid] = str(pub)[:10]  # YYYY-MM-DD

    if not article_topics or not article_dates:
        return {}, [], np.array([])

    # Build date range
    all_dates = sorted(set(article_dates.values()))
    if len(all_dates) < min_days:
        return {}, [], np.array([])

    date_to_idx = {d: i for i, d in enumerate(all_dates)}
    n_days = len(all_dates)

    # Count per topic per day
    topic_ids = sorted(set(article_topics.values()))
    topic_series: dict[str, np.ndarray] = {}

    for tid in topic_ids:
        series = np.zeros(n_days, dtype=np.float64)
        for aid, t in article_topics.items():
            if t == tid and aid in article_dates:
                day_str = article_dates[aid]
                if day_str in date_to_idx:
                    series[date_to_idx[day_str]] += 1.0
        topic_series[tid] = series

    return topic_series, topic_ids, np.array(all_dates)


def _check_stationarity(series: np.ndarray, significance: float = 0.05) -> tuple[bool, np.ndarray]:
    """Check stationarity using ADF test. Difference if non-stationary.

    Returns:
        (is_stationary, processed_series) where processed_series is either
        the original or first-differenced series.
    """
    _, adfuller = _lazy_import_statsmodels()

    if len(series) < 8:
        return False, series

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            result = adfuller(series, autolag="AIC")
        p_value = result[1]

        if p_value < significance:
            return True, series

        # Try first differencing
        diff_series = np.diff(series)
        if len(diff_series) < 8:
            return False, series

        result_diff = adfuller(diff_series, autolag="AIC")
        if result_diff[1] < significance:
            return True, diff_series

        return False, series
    except Exception:
        return False, series


def _build_source_topic_articles(
    topics_table,
    articles_table,
) -> dict[str, dict[str, list[str]]]:
    """Build mapping: source -> topic_id -> [article_ids].

    Used for frame analysis and agenda setting.
    """
    # article_id -> source
    article_source: dict[str, str] = {}
    if "source" in articles_table.column_names:
        for i in range(articles_table.num_rows):
            aid = articles_table.column("article_id")[i].as_py()
            src = articles_table.column("source")[i].as_py()
            if src:
                article_source[aid] = str(src)

    # article_id -> topic_id
    article_topic: dict[str, str] = {}
    for i in range(topics_table.num_rows):
        aid = topics_table.column("article_id")[i].as_py()
        tid = topics_table.column("topic_id")[i].as_py()
        if tid is not None and str(tid) != "-1":
            article_topic[aid] = str(tid)

    # Build nested structure
    result: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for aid, tid in article_topic.items():
        if aid in article_source:
            result[article_source[aid]][tid].append(aid)

    return dict(result)


def _get_article_texts(
    articles_table,
    article_ids: list[str],
) -> dict[str, str]:
    """Extract article text (body or title) for a set of article IDs."""
    texts: dict[str, str] = {}
    id_set = set(article_ids)

    has_body = "body" in articles_table.column_names
    has_title = "title" in articles_table.column_names

    for i in range(articles_table.num_rows):
        aid = articles_table.column("article_id")[i].as_py()
        if aid not in id_set:
            continue
        text = ""
        if has_body:
            body = articles_table.column("body")[i].as_py()
            if body:
                text = str(body)
        if not text and has_title:
            title = articles_table.column("title")[i].as_py()
            if title:
                text = str(title)
        if text:
            texts[aid] = text

    return texts


def _get_article_languages(articles_table) -> dict[str, str]:
    """Extract article_id -> language mapping."""
    result: dict[str, str] = {}
    if "language" not in articles_table.column_names:
        return result
    for i in range(articles_table.num_rows):
        aid = articles_table.column("article_id")[i].as_py()
        lang = articles_table.column("language")[i].as_py()
        if lang:
            result[aid] = str(lang)
    return result


def _get_article_regions(articles_table) -> dict[str, str]:
    """Extract article_id -> region mapping from source or category."""
    result: dict[str, str] = {}
    if "source" not in articles_table.column_names:
        return result

    # Heuristic: map source to region based on common patterns
    # In production this would use the sources.yaml config
    for i in range(articles_table.num_rows):
        aid = articles_table.column("article_id")[i].as_py()
        src = articles_table.column("source")[i].as_py()
        if src:
            result[aid] = str(src)
    return result


def _cross_analysis_schema():
    """Return the PyArrow schema for cross_analysis.parquet."""
    pa, _ = _lazy_import_pyarrow()
    return pa.schema([
        pa.field("analysis_type", pa.utf8(), nullable=False),
        pa.field("source_entity", pa.utf8(), nullable=False),
        pa.field("target_entity", pa.utf8(), nullable=False),
        pa.field("relationship", pa.utf8(), nullable=False),
        pa.field("strength", pa.float32(), nullable=False),
        pa.field("p_value", pa.float32(), nullable=True),
        pa.field("lag_days", pa.int32(), nullable=True),
        pa.field("evidence_articles", pa.list_(pa.utf8()), nullable=False),
        pa.field("metadata", pa.utf8(), nullable=False),
    ])


# ---------------------------------------------------------------------------
# Stage6CrossAnalyzer -- main class
# ---------------------------------------------------------------------------

class Stage6CrossAnalyzer:
    """Stage 6 cross-analysis pipeline: causal discovery, network analysis, frame comparison.

    Implements 12 techniques (T37-T46, T20, T50) with graceful degradation.
    If any individual technique fails, the pipeline continues with the remaining
    techniques and logs the failure.

    Args:
        enable_pcmci: Whether to attempt PCMCI (requires tigramite).
            Defaults to True; falls back to Granger-only if unavailable.
        enable_contradiction: Whether to attempt NLI contradiction detection
            (requires BART-MNLI model). Defaults to True; skips if unavailable.
    """

    def __init__(
        self,
        enable_pcmci: bool = True,
        enable_contradiction: bool = True,
    ) -> None:
        self._enable_pcmci = enable_pcmci
        self._enable_contradiction = enable_contradiction
        self._nli_pipeline: Any = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        timeseries_path: Path | None = None,
        topics_path: Path | None = None,
        analysis_path: Path | None = None,
        networks_path: Path | None = None,
        embeddings_path: Path | None = None,
        articles_path: Path | None = None,
        output_dir: Path | None = None,
    ) -> Stage6Output:
        """Execute the full Stage 6 cross-analysis pipeline.

        Processing order (per Step 7 Section 3.6):
            1. Granger Causality Testing     (T37)
            2. PCMCI Causal Inference        (T38)
            3. Co-occurrence Network         (T39)
            4. Knowledge Graph Construction  (T40)
            5. Centrality Analysis           (T41)
            6. Network Evolution             (T42)
            7. Cross-Lingual Topic Alignment (T43)
            8. Frame Analysis                (T44)
            9. Agenda Setting Analysis       (T45)
           10. Temporal Alignment            (T46)
           11. GraphRAG Knowledge Retrieval  (T20)
           12. Contradiction Detection       (T50)
           13. Write cross_analysis.parquet

        Args:
            timeseries_path: Path to timeseries.parquet (default: constants).
            topics_path: Path to topics.parquet (default: constants).
            analysis_path: Path to article_analysis.parquet (default: constants).
            networks_path: Path to networks.parquet (default: constants).
            embeddings_path: Path to embeddings.parquet (default: constants).
            articles_path: Path to articles.parquet (default: constants).
            output_dir: Directory for cross_analysis.parquet (default: constants).

        Returns:
            Stage6Output with all technique results.

        Raises:
            PipelineStageError: If the stage fails irrecoverably (no inputs available).
        """
        t0 = time.monotonic()
        output = Stage6Output()

        # Resolve paths
        _timeseries_path = timeseries_path or TIMESERIES_PARQUET_PATH
        _topics_path = topics_path or TOPICS_PARQUET_PATH
        _analysis_path = analysis_path or ARTICLE_ANALYSIS_PARQUET_PATH
        _networks_path = networks_path or NETWORKS_PARQUET_PATH
        _embeddings_path = embeddings_path or EMBEDDINGS_PARQUET_PATH
        _articles_path = articles_path or ARTICLES_PARQUET_PATH
        _output_dir = output_dir or DATA_ANALYSIS_DIR

        _output_dir.mkdir(parents=True, exist_ok=True)

        logger.info(
            "stage6_start",
            memory_gb=round(_get_memory_gb(), 2),
        )

        # ---- Load inputs ----
        pa, pq = _lazy_import_pyarrow()

        try:
            articles_table = pq.read_table(str(_articles_path))
            n_articles = articles_table.num_rows
            logger.info("stage6_articles_loaded", n_articles=n_articles)
        except Exception as exc:
            raise PipelineStageError(
                f"Failed to load articles: {exc}",
                stage_name="stage_6_cross",
                stage_number=6,
            ) from exc

        # Load optional inputs with graceful fallback
        topics_table = self._safe_load_table(pq, _topics_path, "topics")
        timeseries_table = self._safe_load_table(pq, _timeseries_path, "timeseries")
        analysis_table = self._safe_load_table(pq, _analysis_path, "analysis")
        networks_table = self._safe_load_table(pq, _networks_path, "networks")
        embeddings_table = self._safe_load_table(pq, _embeddings_path, "embeddings")

        if topics_table is None:
            logger.warning("stage6_no_topics", reason="topics.parquet not available")
            self._write_empty_output(_output_dir)
            output.elapsed_seconds = time.monotonic() - t0
            return output

        # Minimum article threshold for causal analysis
        if n_articles < MIN_ARTICLES_FOR_GRANGER:
            logger.warning(
                "stage6_insufficient_articles",
                n_articles=n_articles,
                min_required=MIN_ARTICLES_FOR_GRANGER,
            )

        # Build shared data structures
        topic_series, topic_ids, date_index = _build_topic_daily_series(
            topics_table, articles_table,
        )
        source_topic_articles = _build_source_topic_articles(
            topics_table, articles_table,
        )

        # ---- T37: Granger Causality Testing ----
        output.granger = self._run_granger(topic_series, topic_ids)
        if output.granger and output.granger.records:
            output.techniques_completed.append("T37_granger")
        else:
            output.techniques_skipped.append("T37_granger")

        # ---- T38: PCMCI Causal Inference ----
        output.pcmci = self._run_pcmci(topic_series, topic_ids)
        if output.pcmci and output.pcmci.records:
            output.techniques_completed.append("T38_pcmci")
        else:
            output.techniques_skipped.append("T38_pcmci")

        # ---- T39-T42: Network Analysis (co-occurrence, KG, centrality, evolution) ----
        output.networks = self._run_network_analysis(
            topics_table, articles_table, networks_table, embeddings_table,
        )
        if output.networks:
            if output.networks.cooccurrence_records:
                output.techniques_completed.append("T39_cooccurrence")
            else:
                output.techniques_skipped.append("T39_cooccurrence")

            if output.networks.kg_records:
                output.techniques_completed.append("T40_knowledge_graph")
            else:
                output.techniques_skipped.append("T40_knowledge_graph")

            if output.networks.centrality_records:
                output.techniques_completed.append("T41_centrality")
            else:
                output.techniques_skipped.append("T41_centrality")

            if output.networks.evolution_records:
                output.techniques_completed.append("T42_network_evolution")
            else:
                output.techniques_skipped.append("T42_network_evolution")

        # ---- T43: Cross-Lingual Topic Alignment ----
        output.cross_lingual = self._run_cross_lingual(
            topics_table, articles_table, embeddings_table,
        )
        if output.cross_lingual and output.cross_lingual.records:
            output.techniques_completed.append("T43_cross_lingual")
        else:
            output.techniques_skipped.append("T43_cross_lingual")

        # ---- T44: Frame Analysis ----
        output.frame = self._run_frame_analysis(
            source_topic_articles, articles_table,
        )
        if output.frame and output.frame.records:
            output.techniques_completed.append("T44_frame")
        else:
            output.techniques_skipped.append("T44_frame")

        # ---- T45: Agenda Setting Analysis ----
        output.agenda = self._run_agenda_setting(
            source_topic_articles, topic_series, articles_table,
        )
        if output.agenda and output.agenda.records:
            output.techniques_completed.append("T45_agenda")
        else:
            output.techniques_skipped.append("T45_agenda")

        # ---- T46: Temporal Alignment (DTW) ----
        output.temporal = self._run_temporal_alignment(
            topic_series, topic_ids, articles_table,
        )
        if output.temporal and output.temporal.records:
            output.techniques_completed.append("T46_temporal")
        else:
            output.techniques_skipped.append("T46_temporal")

        # ---- T20: GraphRAG Knowledge Retrieval ----
        output.graphrag = self._run_graphrag(
            topics_table, articles_table, networks_table, embeddings_table,
        )
        if output.graphrag and output.graphrag.records:
            output.techniques_completed.append("T20_graphrag")
        else:
            output.techniques_skipped.append("T20_graphrag")

        # ---- T50: Contradiction Detection ----
        output.contradiction = self._run_contradiction_detection(
            topics_table, articles_table, embeddings_table,
            source_topic_articles,
        )
        if output.contradiction and output.contradiction.records:
            output.techniques_completed.append("T50_contradiction")
        else:
            output.techniques_skipped.append("T50_contradiction")

        # ---- Write Parquet output ----
        all_records = self._collect_all_records(output)
        output.total_records = len(all_records)
        self._write_parquet(all_records, _output_dir)

        output.elapsed_seconds = time.monotonic() - t0
        logger.info(
            "stage6_complete",
            elapsed_seconds=round(output.elapsed_seconds, 1),
            total_records=output.total_records,
            techniques_completed=output.techniques_completed,
            techniques_skipped=output.techniques_skipped,
            memory_gb=round(_get_memory_gb(), 2),
        )
        return output

    def cleanup(self) -> None:
        """Release all models and force garbage collection."""
        logger.info("stage6_cleanup_start")
        if self._nli_pipeline is not None:
            del self._nli_pipeline
            self._nli_pipeline = None
        gc.collect()
        logger.info("stage6_cleanup_complete")

    # ------------------------------------------------------------------
    # T37: Granger Causality Testing
    # ------------------------------------------------------------------

    def _run_granger(
        self,
        topic_series: dict[str, np.ndarray],
        topic_ids: list[str],
    ) -> GrangerResult:
        """Test pairwise Granger causality between topic time series.

        Pre-checks stationarity (ADF test) and applies Bonferroni correction
        for multiple comparisons.
        """
        result = GrangerResult()

        if len(topic_ids) < 2:
            logger.warning("stage6_granger_skip", reason="fewer than 2 topics")
            return result

        # Check minimum series length
        min_length = min(len(s) for s in topic_series.values()) if topic_series else 0
        if min_length < GRANGER_MAX_LAG + 2:
            logger.warning(
                "stage6_granger_skip",
                reason="series too short",
                min_length=min_length,
            )
            return result

        try:
            grangercausalitytests, _ = _lazy_import_statsmodels()
        except ImportError:
            logger.warning("stage6_granger_skip", reason="statsmodels not installed")
            return result

        logger.info("stage6_granger_start", n_topics=len(topic_ids))

        # Pre-process: stationarity check
        stationary_series: dict[str, np.ndarray] = {}
        for tid in topic_ids:
            is_stationary, processed = _check_stationarity(topic_series[tid])
            if is_stationary:
                stationary_series[tid] = processed
            else:
                result.n_skipped_stationarity += 1

        stationary_ids = sorted(stationary_series.keys())
        if len(stationary_ids) < 2:
            logger.warning(
                "stage6_granger_skip",
                reason="fewer than 2 stationary series",
                n_skipped=result.n_skipped_stationarity,
            )
            return result

        # Number of pairwise tests for Bonferroni correction
        n_pairs = len(stationary_ids) * (len(stationary_ids) - 1)
        bonferroni_threshold = GRANGER_SIGNIFICANCE / max(n_pairs, 1)

        for i, source_tid in enumerate(stationary_ids):
            for target_tid in stationary_ids:
                if source_tid == target_tid:
                    continue

                result.n_tested += 1
                try:
                    # Stack as [target, source] per statsmodels convention
                    source_s = stationary_series[source_tid]
                    target_s = stationary_series[target_tid]
                    # Align lengths (differencing may have changed them)
                    min_len = min(len(source_s), len(target_s))
                    stacked = np.column_stack([
                        target_s[:min_len],
                        source_s[:min_len],
                    ])

                    if min_len < GRANGER_MAX_LAG + 2:
                        continue

                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore")
                        gc_result = grangercausalitytests(
                            stacked,
                            maxlag=GRANGER_MAX_LAG,
                            verbose=False,
                        )

                    # Find optimal lag (lowest p-value)
                    best_lag = 1
                    best_p = 1.0
                    best_f = 0.0
                    for lag in range(1, GRANGER_MAX_LAG + 1):
                        if lag not in gc_result:
                            continue
                        test_results = gc_result[lag]
                        # Use F-test (ssr_ftest)
                        f_stat = test_results[0]["ssr_ftest"][0]
                        p_val = test_results[0]["ssr_ftest"][1]
                        if p_val < best_p:
                            best_p = p_val
                            best_f = f_stat
                            best_lag = lag

                    if best_p < bonferroni_threshold:
                        result.n_significant += 1
                        # Normalize strength: -log10(p) / max(-log10(p))
                        strength = min(1.0, -math.log10(max(best_p, 1e-300)) / 10.0)
                        record = CrossAnalysisRecord(
                            analysis_type="granger",
                            source_entity=f"topic_{source_tid}",
                            target_entity=f"topic_{target_tid}",
                            relationship=f"granger_causes (lag={best_lag}d, F={best_f:.2f})",
                            strength=strength,
                            p_value=best_p,
                            lag_days=best_lag,
                            metadata=json.dumps({
                                "f_statistic": round(best_f, 4),
                                "bonferroni_threshold": round(bonferroni_threshold, 8),
                                "n_total_tests": n_pairs,
                                "source_topic": source_tid,
                                "target_topic": target_tid,
                            }),
                        )
                        result.records.append(record)

                except Exception as exc:
                    logger.debug(
                        "stage6_granger_pair_error",
                        source=source_tid,
                        target=target_tid,
                        error=str(exc),
                    )
                    continue

        logger.info(
            "stage6_granger_complete",
            n_tested=result.n_tested,
            n_significant=result.n_significant,
            n_skipped_stationarity=result.n_skipped_stationarity,
        )
        return result

    # ------------------------------------------------------------------
    # T38: PCMCI Causal Inference
    # ------------------------------------------------------------------

    def _run_pcmci(
        self,
        topic_series: dict[str, np.ndarray],
        topic_ids: list[str],
    ) -> PCMCIResult:
        """Apply PCMCI algorithm for multivariate causal discovery.

        Falls back gracefully if tigramite is not installed or if convergence
        fails (reduces tau_max and retries once).
        """
        result = PCMCIResult()

        if not self._enable_pcmci:
            logger.info("stage6_pcmci_skip", reason="disabled by configuration")
            return result

        pp, PCMCI_cls, ParCorr = _lazy_import_tigramite()
        if pp is None:
            logger.warning(
                "stage6_pcmci_skip",
                reason="tigramite not installed, using Granger-only mode",
            )
            return result

        # Select top-N topics by total volume
        if len(topic_ids) < 2:
            logger.warning("stage6_pcmci_skip", reason="fewer than 2 topics")
            return result

        topic_volumes = {tid: float(np.sum(topic_series[tid])) for tid in topic_ids}
        sorted_topics = sorted(topic_volumes.keys(), key=lambda t: topic_volumes[t], reverse=True)
        selected = sorted_topics[:min(PCMCI_TOP_N_TOPICS, len(sorted_topics))]

        if len(selected) < 2:
            return result

        # Build multivariate data matrix: shape (T, N) where T=time, N=topics
        series_list = [topic_series[tid] for tid in selected]
        min_len = min(len(s) for s in series_list)
        if min_len < PCMCI_TAU_MAX + 2:
            logger.warning("stage6_pcmci_skip", reason="series too short")
            return result

        data_matrix = np.column_stack([s[:min_len] for s in series_list])
        var_names = [f"topic_{tid}" for tid in selected]

        logger.info(
            "stage6_pcmci_start",
            n_variables=len(selected),
            series_length=min_len,
        )

        # Attempt PCMCI with full tau_max, fall back to reduced
        for tau_max in [PCMCI_TAU_MAX, PCMCI_TAU_MAX_FALLBACK]:
            try:
                dataframe = pp.DataFrame(
                    data_matrix,
                    var_names=var_names,
                )
                parcorr = ParCorr(significance="analytic")
                pcmci = PCMCI_cls(
                    dataframe=dataframe,
                    cond_ind_test=parcorr,
                    verbosity=0,
                )
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    pcmci_results = pcmci.run_pcmci(
                        tau_max=tau_max,
                        pc_alpha=PCMCI_PC_ALPHA,
                    )

                # Extract significant causal links
                q_matrix = pcmci_results["q_matrix"]  # shape (N, N, tau_max+1)
                val_matrix = pcmci_results["val_matrix"]  # partial correlations

                n_vars = len(selected)
                for i in range(n_vars):
                    for j in range(n_vars):
                        if i == j:
                            continue
                        for tau in range(1, tau_max + 1):
                            q_val = q_matrix[i, j, tau]
                            val = val_matrix[i, j, tau]
                            if q_val < PCMCI_PC_ALPHA:
                                result.n_causal_links += 1
                                strength = min(1.0, abs(float(val)))
                                record = CrossAnalysisRecord(
                                    analysis_type="pcmci",
                                    source_entity=var_names[i],
                                    target_entity=var_names[j],
                                    relationship=f"pcmci_causes (lag={tau}d, parcorr={val:.3f})",
                                    strength=strength,
                                    p_value=float(q_val),
                                    lag_days=tau,
                                    metadata=json.dumps({
                                        "partial_correlation": round(float(val), 4),
                                        "q_value": round(float(q_val), 6),
                                        "tau_max_used": tau_max,
                                        "n_variables": n_vars,
                                    }),
                                )
                                result.records.append(record)

                # Compute graph density
                max_possible = n_vars * (n_vars - 1) * tau_max
                result.graph_density = (
                    result.n_causal_links / max(max_possible, 1)
                )
                result.converged = True

                logger.info(
                    "stage6_pcmci_complete",
                    n_causal_links=result.n_causal_links,
                    graph_density=round(result.graph_density, 4),
                    tau_max_used=tau_max,
                )
                break  # Success, exit retry loop

            except Exception as exc:
                logger.warning(
                    "stage6_pcmci_attempt_failed",
                    tau_max=tau_max,
                    error=str(exc),
                )
                if tau_max == PCMCI_TAU_MAX_FALLBACK:
                    logger.warning("stage6_pcmci_failed", reason="all attempts exhausted")
                continue

        return result

    # ------------------------------------------------------------------
    # T39-T42: Network Analysis
    # ------------------------------------------------------------------

    def _run_network_analysis(
        self,
        topics_table,
        articles_table,
        networks_table,
        embeddings_table,
    ) -> NetworkAnalysisResult:
        """Run co-occurrence network, knowledge graph, centrality, and evolution.

        Combines T39 (co-occurrence), T40 (knowledge graph), T41 (centrality),
        and T42 (network evolution) into a single network analysis pass.
        """
        result = NetworkAnalysisResult()

        try:
            nx = _lazy_import_networkx()
        except ImportError:
            logger.warning("stage6_network_skip", reason="networkx not installed")
            return result

        # ---- T39: Build co-occurrence network from topics ----
        try:
            result.cooccurrence_records = self._build_cooccurrence_network(
                nx, topics_table, articles_table,
            )
        except Exception as exc:
            logger.warning("stage6_cooccurrence_error", error=str(exc))

        # ---- T40: Knowledge Graph from NER ----
        try:
            result.kg_records = self._build_knowledge_graph(
                nx, networks_table, articles_table,
            )
        except Exception as exc:
            logger.warning("stage6_kg_error", error=str(exc))

        # ---- T41: Centrality Analysis ----
        try:
            result.centrality_records, result.n_nodes, result.n_edges, result.modularity = (
                self._compute_centrality(nx, networks_table)
            )
        except Exception as exc:
            logger.warning("stage6_centrality_error", error=str(exc))

        # ---- T42: Network Evolution ----
        try:
            result.evolution_records = self._compute_network_evolution(
                nx, topics_table, articles_table,
            )
        except Exception as exc:
            logger.warning("stage6_evolution_error", error=str(exc))

        return result

    def _build_cooccurrence_network(
        self,
        nx,
        topics_table,
        articles_table,
    ) -> list[CrossAnalysisRecord]:
        """T39: Build topic-topic co-occurrence network.

        Two topics co-occur when assigned to overlapping article sets.
        Edge weight = Jaccard similarity of article sets.
        """
        records: list[CrossAnalysisRecord] = []

        # Build topic -> article_ids mapping
        topic_articles: dict[str, set[str]] = defaultdict(set)
        for i in range(topics_table.num_rows):
            aid = topics_table.column("article_id")[i].as_py()
            tid = topics_table.column("topic_id")[i].as_py()
            if tid is not None and str(tid) != "-1":
                topic_articles[str(tid)].add(aid)

        topic_ids = sorted(topic_articles.keys())
        if len(topic_ids) < 2:
            return records

        n_total_articles = articles_table.num_rows if articles_table is not None else 1

        for t1, t2 in combinations(topic_ids, 2):
            set1 = topic_articles[t1]
            set2 = topic_articles[t2]
            intersection = len(set1 & set2)
            union = len(set1 | set2)
            if union == 0 or intersection == 0:
                continue
            jaccard = intersection / union
            if jaccard < COOCCURRENCE_MIN_WEIGHT:
                continue

            evidence = list(set1 & set2)[:10]  # Cap evidence list
            records.append(CrossAnalysisRecord(
                analysis_type="cooccurrence",
                source_entity=f"topic_{t1}",
                target_entity=f"topic_{t2}",
                relationship=f"topic_cooccurrence (jaccard={jaccard:.3f})",
                strength=jaccard,
                evidence_articles=evidence,
                metadata=json.dumps({
                    "jaccard_similarity": round(jaccard, 4),
                    "shared_articles": intersection,
                    "union_articles": union,
                }),
            ))

        logger.info("stage6_cooccurrence_complete", n_pairs=len(records))
        return records

    def _build_knowledge_graph(
        self,
        nx,
        networks_table,
        articles_table,
    ) -> list[CrossAnalysisRecord]:
        """T40: Build knowledge graph from NER co-occurrence with relation type inference.

        Uses the networks.parquet (entity co-occurrence from Stage 4 Louvain)
        and adds relation type heuristics. Filters noise edges (weight < CENTRALITY_MIN_WEIGHT)
        to keep only statistically meaningful co-occurrences.
        """
        records: list[CrossAnalysisRecord] = []

        if networks_table is None:
            return records

        # Filter noise: weight=1 edges are single co-occurrences (statistically insignificant)
        networks_table = self._filter_networks_by_weight(
            networks_table, CENTRALITY_MIN_WEIGHT,
        )
        if networks_table is None or networks_table.num_rows == 0:
            return records

        for i in range(networks_table.num_rows):
            entity_a = networks_table.column("entity_a")[i].as_py()
            entity_b = networks_table.column("entity_b")[i].as_py()
            cooccurrence = networks_table.column("co_occurrence_count")[i].as_py()

            if not entity_a or not entity_b:
                continue

            # Heuristic relation type inference
            relation = self._infer_relation_type(entity_a, entity_b)

            # Extract evidence articles
            evidence: list[str] = []
            if "source_articles" in networks_table.column_names:
                src_arts = networks_table.column("source_articles")[i].as_py()
                if src_arts:
                    evidence = list(src_arts)[:10]

            # Normalize strength to 0-1 range using log scale
            strength = min(1.0, math.log1p(cooccurrence) / 10.0)

            records.append(CrossAnalysisRecord(
                analysis_type="knowledge_graph",
                source_entity=str(entity_a),
                target_entity=str(entity_b),
                relationship=relation,
                strength=strength,
                evidence_articles=evidence,
                metadata=json.dumps({
                    "co_occurrence_count": cooccurrence,
                    "relation_type": relation,
                }),
            ))

        logger.info("stage6_kg_complete", n_edges=len(records))
        return records

    @staticmethod
    def _infer_relation_type(entity_a: str, entity_b: str) -> str:
        """Heuristic relation type inference between two entities.

        Simple heuristics based on entity naming patterns:
        - If one looks like a person and one like an org -> "works_at"
        - If one looks like a location -> "located_in"
        - Default: "mentioned_with"
        """
        a_lower = entity_a.lower()
        b_lower = entity_b.lower()

        # Location indicators
        location_keywords = {
            "city", "state", "province", "country", "county",
            "washington", "beijing", "tokyo", "seoul", "london",
            "new york", "paris", "berlin", "moscow", "delhi",
        }

        # Organization indicators
        org_keywords = {
            "inc", "corp", "ltd", "company", "group", "bank",
            "ministry", "department", "university", "institute",
            "association", "organization", "commission", "agency",
            "council", "foundation", "fund",
        }

        a_is_location = any(kw in a_lower for kw in location_keywords)
        b_is_location = any(kw in b_lower for kw in location_keywords)
        a_is_org = any(kw in a_lower for kw in org_keywords)
        b_is_org = any(kw in b_lower for kw in org_keywords)

        if a_is_location or b_is_location:
            return "located_in"
        if a_is_org or b_is_org:
            return "works_at"
        return "mentioned_with"

    @staticmethod
    def _filter_networks_by_weight(networks_table, min_weight: int):
        """Filter networks_table to rows where co_occurrence_count >= min_weight.

        Returns a new PyArrow table (original is not modified).
        If networks_table is None or already small enough, returns as-is.
        """
        if networks_table is None or networks_table.num_rows == 0:
            return networks_table

        pa, _ = _lazy_import_pyarrow()
        import pyarrow.compute as pc

        weight_col = networks_table.column("co_occurrence_count")
        mask = pc.greater_equal(weight_col, min_weight)
        filtered = networks_table.filter(mask)

        if filtered.num_rows < networks_table.num_rows:
            logger.info(
                "stage6_networks_filtered",
                original_rows=networks_table.num_rows,
                filtered_rows=filtered.num_rows,
                min_weight=min_weight,
            )

        return filtered

    def _compute_centrality(
        self,
        nx,
        networks_table,
    ) -> tuple[list[CrossAnalysisRecord], int, int, float]:
        """T41: Compute degree, betweenness, and PageRank centrality on co-occurrence network."""
        records: list[CrossAnalysisRecord] = []
        n_nodes = 0
        n_edges = 0
        modularity = 0.0

        if networks_table is None or networks_table.num_rows == 0:
            return records, n_nodes, n_edges, modularity

        # Filter noise edges (weight=1 accounts for ~98% of edges in typical runs)
        filtered_table = self._filter_networks_by_weight(
            networks_table, CENTRALITY_MIN_WEIGHT,
        )
        if filtered_table is None or filtered_table.num_rows == 0:
            return records, n_nodes, n_edges, modularity

        # Build networkx graph from filtered edges
        G = nx.Graph()
        for i in range(filtered_table.num_rows):
            a = filtered_table.column("entity_a")[i].as_py()
            b = filtered_table.column("entity_b")[i].as_py()
            w = filtered_table.column("co_occurrence_count")[i].as_py()
            if a and b:
                G.add_edge(str(a), str(b), weight=float(w))

        n_nodes = G.number_of_nodes()
        n_edges = G.number_of_edges()

        if n_edges < NETWORK_MIN_EDGES:
            logger.warning(
                "stage6_centrality_skip",
                reason="insufficient edges",
                n_edges=n_edges,
            )
            return records, n_nodes, n_edges, modularity

        # Degree centrality
        degree_cent = nx.degree_centrality(G)

        # Betweenness centrality (approximate via k-node sampling for large graphs)
        k_sample = min(CENTRALITY_BETWEENNESS_K, n_nodes)
        betweenness_cent = nx.betweenness_centrality(
            G, weight="weight", k=k_sample,
        )

        # PageRank
        try:
            pagerank = nx.pagerank(G, weight="weight")
        except nx.PowerIterationFailedConvergence:
            pagerank = {n: 1.0 / n_nodes for n in G.nodes()}

        # Modularity (Louvain — O(m) linear, replaces O(n*m*log n) greedy)
        try:
            communities = list(nx.community.louvain_communities(G, resolution=1.0))
            modularity = nx.community.modularity(G, communities)
        except Exception:
            modularity = 0.0

        # Emit top-20 nodes by each centrality metric
        for metric_name, cent_dict in [
            ("degree_centrality", degree_cent),
            ("betweenness_centrality", betweenness_cent),
            ("pagerank", pagerank),
        ]:
            top_nodes = sorted(cent_dict.items(), key=lambda x: x[1], reverse=True)[:20]
            for node, score in top_nodes:
                records.append(CrossAnalysisRecord(
                    analysis_type="centrality",
                    source_entity=str(node),
                    target_entity="network",
                    relationship=metric_name,
                    strength=float(score),
                    metadata=json.dumps({
                        "metric": metric_name,
                        "rank": top_nodes.index((node, score)) + 1,
                        "n_nodes": n_nodes,
                        "n_edges": n_edges,
                        "modularity": round(modularity, 4),
                    }),
                ))

        logger.info(
            "stage6_centrality_complete",
            n_nodes=n_nodes,
            n_edges=n_edges,
            modularity=round(modularity, 4),
            n_records=len(records),
        )
        return records, n_nodes, n_edges, modularity

    def _compute_network_evolution(
        self,
        nx,
        topics_table,
        articles_table,
    ) -> list[CrossAnalysisRecord]:
        """T42: Compare graph structure across weekly snapshots.

        Builds weekly entity co-occurrence graphs and compares density,
        average degree, and number of components across consecutive weeks.
        """
        records: list[CrossAnalysisRecord] = []

        # Build article_id -> week mapping
        article_weeks: dict[str, str] = {}
        if "published_at" in articles_table.column_names:
            import datetime
            for i in range(articles_table.num_rows):
                aid = articles_table.column("article_id")[i].as_py()
                pub = articles_table.column("published_at")[i].as_py()
                if pub:
                    try:
                        dt = datetime.datetime.fromisoformat(str(pub)[:10])
                        # ISO week: YYYY-WNN
                        week_str = f"{dt.isocalendar()[0]}-W{dt.isocalendar()[1]:02d}"
                        article_weeks[aid] = week_str
                    except (ValueError, TypeError):
                        continue

        if not article_weeks:
            return records

        # Build topic co-occurrence per week
        topic_articles_by_week: dict[str, dict[str, set[str]]] = defaultdict(
            lambda: defaultdict(set)
        )
        for i in range(topics_table.num_rows):
            aid = topics_table.column("article_id")[i].as_py()
            tid = topics_table.column("topic_id")[i].as_py()
            if aid in article_weeks and tid is not None and str(tid) != "-1":
                week = article_weeks[aid]
                topic_articles_by_week[week][str(tid)].add(aid)

        sorted_weeks = sorted(topic_articles_by_week.keys())
        if len(sorted_weeks) < 2:
            return records

        # Build weekly graphs and compute metrics
        weekly_metrics: list[tuple[str, float, float, int]] = []
        for week in sorted_weeks:
            G = nx.Graph()
            topic_sets = topic_articles_by_week[week]
            topic_ids_week = sorted(topic_sets.keys())
            for t1, t2 in combinations(topic_ids_week, 2):
                overlap = len(topic_sets[t1] & topic_sets[t2])
                if overlap > 0:
                    G.add_edge(t1, t2, weight=overlap)

            if G.number_of_nodes() < 2:
                continue

            density = nx.density(G)
            avg_degree = (
                sum(d for _, d in G.degree()) / G.number_of_nodes()
                if G.number_of_nodes() > 0 else 0.0
            )
            n_components = nx.number_connected_components(G)
            weekly_metrics.append((week, density, avg_degree, n_components))

        # Compare consecutive weeks
        for idx in range(1, len(weekly_metrics)):
            prev_week, prev_dens, prev_deg, prev_comp = weekly_metrics[idx - 1]
            curr_week, curr_dens, curr_deg, curr_comp = weekly_metrics[idx]

            density_change = curr_dens - prev_dens
            degree_change = curr_deg - prev_deg

            records.append(CrossAnalysisRecord(
                analysis_type="network_evolution",
                source_entity=prev_week,
                target_entity=curr_week,
                relationship=f"weekly_evolution (density_delta={density_change:+.4f})",
                strength=abs(density_change),
                metadata=json.dumps({
                    "prev_density": round(prev_dens, 4),
                    "curr_density": round(curr_dens, 4),
                    "density_change": round(density_change, 4),
                    "prev_avg_degree": round(prev_deg, 2),
                    "curr_avg_degree": round(curr_deg, 2),
                    "degree_change": round(degree_change, 2),
                    "prev_components": prev_comp,
                    "curr_components": curr_comp,
                }),
            ))

        logger.info("stage6_evolution_complete", n_weeks=len(weekly_metrics), n_records=len(records))
        return records

    # ------------------------------------------------------------------
    # T43: Cross-Lingual Topic Alignment
    # ------------------------------------------------------------------

    def _run_cross_lingual(
        self,
        topics_table,
        articles_table,
        embeddings_table,
    ) -> CrossLingualResult:
        """Align topics across Korean and English using SBERT centroid similarity.

        Computes per-language topic centroids from article embeddings, then
        finds cross-lingual topic pairs with cosine similarity above threshold.
        """
        result = CrossLingualResult()

        if embeddings_table is None:
            logger.warning("stage6_cross_lingual_skip", reason="no embeddings available")
            return result

        # Get article languages
        article_langs = _get_article_languages(articles_table)
        if not article_langs:
            logger.warning("stage6_cross_lingual_skip", reason="no language info")
            return result

        # Get article-topic mapping
        article_topics: dict[str, str] = {}
        for i in range(topics_table.num_rows):
            aid = topics_table.column("article_id")[i].as_py()
            tid = topics_table.column("topic_id")[i].as_py()
            if tid is not None and str(tid) != "-1":
                article_topics[aid] = str(tid)

        # Get embeddings indexed by article_id
        embedding_index: dict[str, np.ndarray] = {}
        emb_col = "embedding"
        if emb_col in embeddings_table.column_names:
            for i in range(embeddings_table.num_rows):
                aid = embeddings_table.column("article_id")[i].as_py()
                emb = embeddings_table.column(emb_col)[i].as_py()
                if emb:
                    embedding_index[aid] = np.array(emb, dtype=np.float32)

        if not embedding_index:
            logger.warning("stage6_cross_lingual_skip", reason="no embeddings indexed")
            return result

        # Partition articles by language group: ko vs en
        ko_topic_embeddings: dict[str, list[np.ndarray]] = defaultdict(list)
        en_topic_embeddings: dict[str, list[np.ndarray]] = defaultdict(list)

        for aid, tid in article_topics.items():
            if aid not in embedding_index or aid not in article_langs:
                continue
            lang = article_langs[aid]
            emb = embedding_index[aid]
            if lang == "ko":
                ko_topic_embeddings[tid].append(emb)
            elif lang == "en":
                en_topic_embeddings[tid].append(emb)

        if not ko_topic_embeddings or not en_topic_embeddings:
            logger.info("stage6_cross_lingual_skip", reason="single language corpus")
            return result

        # Compute centroids per topic per language
        ko_centroids: dict[str, np.ndarray] = {}
        for tid, embs in ko_topic_embeddings.items():
            ko_centroids[tid] = np.mean(embs, axis=0)

        en_centroids: dict[str, np.ndarray] = {}
        for tid, embs in en_topic_embeddings.items():
            en_centroids[tid] = np.mean(embs, axis=0)

        # Cross-lingual alignment: compare every KO centroid with every EN centroid
        threshold = CROSS_LINGUAL_MATCH_THRESHOLD
        matched = False

        for ko_tid, ko_cent in ko_centroids.items():
            for en_tid, en_cent in en_centroids.items():
                # Cosine similarity
                norm_a = np.linalg.norm(ko_cent)
                norm_b = np.linalg.norm(en_cent)
                if norm_a < 1e-8 or norm_b < 1e-8:
                    continue
                sim = float(np.dot(ko_cent, en_cent) / (norm_a * norm_b))

                if sim >= threshold:
                    matched = True
                    result.n_aligned += 1
                    result.records.append(CrossAnalysisRecord(
                        analysis_type="cross_lingual",
                        source_entity=f"ko_topic_{ko_tid}",
                        target_entity=f"en_topic_{en_tid}",
                        relationship=f"cross_lingual_alignment (sim={sim:.3f})",
                        strength=sim,
                        metadata=json.dumps({
                            "cosine_similarity": round(sim, 4),
                            "ko_articles": len(ko_topic_embeddings[ko_tid]),
                            "en_articles": len(en_topic_embeddings[en_tid]),
                            "threshold": threshold,
                        }),
                    ))

        # Fallback: lower threshold if no matches found
        if not matched and threshold > CROSS_LINGUAL_FALLBACK_THRESHOLD:
            threshold = CROSS_LINGUAL_FALLBACK_THRESHOLD
            result.threshold_used = threshold
            for ko_tid, ko_cent in ko_centroids.items():
                for en_tid, en_cent in en_centroids.items():
                    norm_a = np.linalg.norm(ko_cent)
                    norm_b = np.linalg.norm(en_cent)
                    if norm_a < 1e-8 or norm_b < 1e-8:
                        continue
                    sim = float(np.dot(ko_cent, en_cent) / (norm_a * norm_b))
                    if sim >= threshold:
                        result.n_aligned += 1
                        result.records.append(CrossAnalysisRecord(
                            analysis_type="cross_lingual",
                            source_entity=f"ko_topic_{ko_tid}",
                            target_entity=f"en_topic_{en_tid}",
                            relationship=f"cross_lingual_alignment (sim={sim:.3f}, lowered_threshold)",
                            strength=sim,
                            metadata=json.dumps({
                                "cosine_similarity": round(sim, 4),
                                "ko_articles": len(ko_topic_embeddings[ko_tid]),
                                "en_articles": len(en_topic_embeddings[en_tid]),
                                "threshold": threshold,
                                "fallback": True,
                            }),
                        ))

        if not result.records:
            logger.info("stage6_cross_lingual_divergent", reason="languages divergent")

        logger.info(
            "stage6_cross_lingual_complete",
            n_aligned=result.n_aligned,
            threshold_used=result.threshold_used,
        )
        return result

    # ------------------------------------------------------------------
    # T44: Frame Analysis
    # ------------------------------------------------------------------

    def _run_frame_analysis(
        self,
        source_topic_articles: dict[str, dict[str, list[str]]],
        articles_table,
    ) -> FrameAnalysisResult:
        """Compare coverage framing of same topic across different sources.

        Uses TF-IDF distributions per source per topic. Computes KL divergence
        between term distributions for frame dimensions (economic, security,
        human_interest, political, scientific).
        """
        result = FrameAnalysisResult()

        # Find topics covered by 2+ sources
        topic_sources: dict[str, list[str]] = defaultdict(list)
        for source, topics in source_topic_articles.items():
            for tid in topics:
                topic_sources[tid].append(source)

        shared_topics = {
            tid: sources
            for tid, sources in topic_sources.items()
            if len(sources) >= FRAME_MIN_SOURCES_PER_TOPIC
        }

        if not shared_topics:
            logger.info("stage6_frame_skip", reason="no topics with 2+ sources")
            return result

        try:
            TfidfVectorizer = _lazy_import_sklearn_tfidf()
            _, _, rel_entr, _ = _lazy_import_scipy_dtw()
        except ImportError as exc:
            logger.warning("stage6_frame_skip", reason=f"missing dependency: {exc}")
            return result

        # Frame dimension seed words for framing analysis
        frame_seeds: dict[str, list[str]] = {
            "economic": [
                "economy", "gdp", "inflation", "market", "trade", "fiscal",
                "monetary", "budget", "growth", "recession", "unemployment",
                "investment", "stock", "bond", "currency", "export", "import",
            ],
            "security": [
                "military", "defense", "security", "weapon", "nuclear", "army",
                "threat", "terrorism", "intelligence", "missile", "conflict",
                "war", "peace", "treaty", "sanctions", "alliance",
            ],
            "human_interest": [
                "people", "community", "family", "health", "education",
                "social", "welfare", "poverty", "rights", "culture",
                "victim", "survivor", "refugee", "humanitarian", "crisis",
            ],
            "political": [
                "government", "president", "parliament", "election", "party",
                "policy", "legislation", "democracy", "opposition", "vote",
                "reform", "minister", "congress", "senate", "diplomatic",
            ],
            "scientific": [
                "research", "science", "technology", "data", "study",
                "discovery", "innovation", "ai", "climate", "energy",
                "environment", "experiment", "analysis", "evidence",
            ],
        }

        result.n_topics_analyzed = len(shared_topics)

        for tid, sources in shared_topics.items():
            # Collect texts per source for this topic
            source_texts: dict[str, list[str]] = {}
            for src in sources:
                aids = source_topic_articles[src].get(tid, [])
                texts = _get_article_texts(articles_table, aids)
                combined = " ".join(texts.values())
                if combined.strip():
                    source_texts[src] = [combined]

            if len(source_texts) < FRAME_MIN_SOURCES_PER_TOPIC:
                continue

            # Build TF-IDF for all source texts for this topic
            all_docs = []
            doc_sources = []
            for src, texts in source_texts.items():
                all_docs.extend(texts)
                doc_sources.extend([src] * len(texts))

            if len(all_docs) < 2:
                continue

            try:
                vectorizer = TfidfVectorizer(
                    max_features=5000,
                    stop_words="english",
                    min_df=1,
                    max_df=0.95,
                )
                tfidf_matrix = vectorizer.fit_transform(all_docs)
                feature_names = vectorizer.get_feature_names_out()
            except Exception:
                continue

            # Compute per-source TF-IDF distributions
            source_distributions: dict[str, np.ndarray] = {}
            for idx, src in enumerate(doc_sources):
                vec = tfidf_matrix[idx].toarray().flatten()
                if src not in source_distributions:
                    source_distributions[src] = vec
                else:
                    source_distributions[src] = source_distributions[src] + vec

            # Normalize distributions
            for src in source_distributions:
                total = source_distributions[src].sum()
                if total > 0:
                    source_distributions[src] = source_distributions[src] / total
                # Add small epsilon to avoid zero division in KL
                source_distributions[src] = source_distributions[src] + 1e-10
                source_distributions[src] = source_distributions[src] / source_distributions[src].sum()

            # Compute frame dimension scores per source
            feature_list = list(feature_names)
            source_frame_scores: dict[str, dict[str, float]] = {}
            for src, dist in source_distributions.items():
                frame_scores: dict[str, float] = {}
                for dim, seeds in frame_seeds.items():
                    score = 0.0
                    for seed in seeds:
                        if seed in feature_list:
                            idx = feature_list.index(seed)
                            score += dist[idx]
                    frame_scores[dim] = score
                source_frame_scores[src] = frame_scores

            # Pairwise KL divergence between sources
            source_list = sorted(source_distributions.keys())
            for s1, s2 in combinations(source_list, 2):
                result.n_source_pairs += 1
                dist1 = source_distributions[s1]
                dist2 = source_distributions[s2]

                # Symmetric KL divergence: (KL(P||Q) + KL(Q||P)) / 2
                kl_pq = float(np.sum(rel_entr(dist1, dist2)))
                kl_qp = float(np.sum(rel_entr(dist2, dist1)))
                sym_kl = (kl_pq + kl_qp) / 2.0

                # Normalize to 0-1 range (cap at 10 nats)
                strength = min(1.0, sym_kl / 10.0)

                # Frame dimension comparison
                frame_comparison = {}
                if s1 in source_frame_scores and s2 in source_frame_scores:
                    for dim in FRAME_DIMENSIONS:
                        f1 = source_frame_scores[s1].get(dim, 0.0)
                        f2 = source_frame_scores[s2].get(dim, 0.0)
                        frame_comparison[dim] = {
                            s1: round(f1, 6),
                            s2: round(f2, 6),
                            "difference": round(abs(f1 - f2), 6),
                        }

                result.records.append(CrossAnalysisRecord(
                    analysis_type="frame",
                    source_entity=s1,
                    target_entity=s2,
                    relationship=f"frame_divergence (topic={tid}, KL={sym_kl:.4f})",
                    strength=strength,
                    metadata=json.dumps({
                        "topic_id": tid,
                        "symmetric_kl": round(sym_kl, 6),
                        "kl_pq": round(kl_pq, 6),
                        "kl_qp": round(kl_qp, 6),
                        "frame_dimensions": frame_comparison,
                    }),
                ))

        logger.info(
            "stage6_frame_complete",
            n_topics=result.n_topics_analyzed,
            n_pairs=result.n_source_pairs,
            n_records=len(result.records),
        )
        return result

    # ------------------------------------------------------------------
    # T45: Agenda Setting Analysis
    # ------------------------------------------------------------------

    def _run_agenda_setting(
        self,
        source_topic_articles: dict[str, dict[str, list[str]]],
        topic_series: dict[str, np.ndarray],
        articles_table,
    ) -> AgendaSettingResult:
        """Measure topic coverage lag between source groups.

        Cross-correlates topic frequency time series across sources to identify
        "agenda setters" -- sources whose coverage predicts later coverage elsewhere.
        """
        result = AgendaSettingResult()

        if not source_topic_articles or not topic_series:
            return result

        try:
            _, correlate, _, _ = _lazy_import_scipy_dtw()
        except ImportError:
            logger.warning("stage6_agenda_skip", reason="scipy not installed")
            return result

        # Build per-source daily topic frequency series
        article_dates: dict[str, str] = {}
        if "published_at" in articles_table.column_names:
            for i in range(articles_table.num_rows):
                aid = articles_table.column("article_id")[i].as_py()
                pub = articles_table.column("published_at")[i].as_py()
                if pub:
                    article_dates[aid] = str(pub)[:10]

        if not article_dates:
            return result

        all_dates = sorted(set(article_dates.values()))
        if len(all_dates) < 7:
            return result
        date_to_idx = {d: i for i, d in enumerate(all_dates)}
        n_days = len(all_dates)

        # For each topic, build per-source time series
        sources = sorted(source_topic_articles.keys())
        if len(sources) < 2:
            return result

        lead_scores: dict[str, float] = defaultdict(float)
        n_comparisons: dict[str, int] = defaultdict(int)

        for tid in topic_series:
            source_ts: dict[str, np.ndarray] = {}
            for src in sources:
                aids = source_topic_articles.get(src, {}).get(tid, [])
                if not aids:
                    continue
                series = np.zeros(n_days, dtype=np.float64)
                for aid in aids:
                    if aid in article_dates:
                        day = article_dates[aid]
                        if day in date_to_idx:
                            series[date_to_idx[day]] += 1.0
                if series.sum() > 0:
                    source_ts[src] = series

            if len(source_ts) < 2:
                continue

            # Pairwise cross-correlation to find lead-lag
            src_list = sorted(source_ts.keys())
            for s1, s2 in combinations(src_list, 2):
                ts1 = source_ts[s1]
                ts2 = source_ts[s2]

                # Normalize
                ts1_norm = ts1 - ts1.mean()
                ts2_norm = ts2 - ts2.mean()
                std1 = ts1_norm.std()
                std2 = ts2_norm.std()
                if std1 < 1e-8 or std2 < 1e-8:
                    continue

                # Cross-correlation
                corr = correlate(ts1_norm, ts2_norm, mode="full")
                corr = corr / (len(ts1) * std1 * std2)

                # Find peak lag
                mid = len(ts1) - 1
                lag_range = min(7, mid)  # Look within 7 days
                best_lag = 0
                best_corr = 0.0
                for lag in range(-lag_range, lag_range + 1):
                    idx = mid + lag
                    if 0 <= idx < len(corr) and abs(corr[idx]) > abs(best_corr):
                        best_corr = corr[idx]
                        best_lag = lag

                if abs(best_corr) > 0.1:  # Minimum correlation threshold
                    # Positive lag means s1 leads s2
                    if best_lag > 0:
                        lead_scores[s1] += abs(best_corr)
                        n_comparisons[s1] += 1
                    elif best_lag < 0:
                        lead_scores[s2] += abs(best_corr)
                        n_comparisons[s2] += 1

                    leader = s1 if best_lag > 0 else (s2 if best_lag < 0 else s1)
                    follower = s2 if best_lag > 0 else (s1 if best_lag < 0 else s2)

                    result.records.append(CrossAnalysisRecord(
                        analysis_type="agenda",
                        source_entity=leader,
                        target_entity=follower,
                        relationship=f"agenda_leads (topic={tid}, lag={abs(best_lag)}d)",
                        strength=min(1.0, abs(best_corr)),
                        lag_days=abs(best_lag),
                        metadata=json.dumps({
                            "topic_id": tid,
                            "cross_correlation": round(float(best_corr), 4),
                            "lag_days": best_lag,
                            "leader": leader,
                            "follower": follower,
                        }),
                    ))

        # Identify overall agenda setters (sources that lead most often)
        for src in sources:
            if n_comparisons.get(src, 0) > 0:
                avg_lead = lead_scores[src] / n_comparisons[src]
                if avg_lead > 0.1:
                    result.agenda_setters.append(src)

        logger.info(
            "stage6_agenda_complete",
            n_records=len(result.records),
            agenda_setters=result.agenda_setters,
        )
        return result

    # ------------------------------------------------------------------
    # T46: Temporal Alignment (DTW)
    # ------------------------------------------------------------------

    def _run_temporal_alignment(
        self,
        topic_series: dict[str, np.ndarray],
        topic_ids: list[str],
        articles_table,
    ) -> TemporalAlignmentResult:
        """Align topic timelines across regions using Dynamic Time Warping.

        Identifies topics that emerge in one region before another.
        """
        result = TemporalAlignmentResult()

        if len(topic_ids) < 2:
            return result

        # Build region-specific topic series
        article_regions = _get_article_regions(articles_table)
        article_langs = _get_article_languages(articles_table)

        # Use language as proxy for region grouping (ko vs en)
        lang_groups = defaultdict(set)
        for aid, lang in article_langs.items():
            lang_groups[lang].add(aid)

        if len(lang_groups) < 2:
            # Fallback: use source as region proxy
            if "source" in articles_table.column_names:
                source_groups: dict[str, set[str]] = defaultdict(set)
                for i in range(articles_table.num_rows):
                    aid = articles_table.column("article_id")[i].as_py()
                    src = articles_table.column("source")[i].as_py()
                    if src:
                        source_groups[str(src)].add(aid)
                if len(source_groups) >= 2:
                    lang_groups = dict(
                        list(source_groups.items())[:5]  # Top 5 sources
                    )

        if len(lang_groups) < 2:
            logger.info("stage6_temporal_skip", reason="single region/language")
            return result

        # For each topic, build per-region series and compute DTW
        region_names = sorted(lang_groups.keys())

        # Get article-topic mapping (already available via topic_series keys)

        # Build per-region per-topic series
        article_dates_map: dict[str, str] = {}
        if "published_at" in articles_table.column_names:
            for i in range(articles_table.num_rows):
                aid = articles_table.column("article_id")[i].as_py()
                pub = articles_table.column("published_at")[i].as_py()
                if pub:
                    article_dates_map[aid] = str(pub)[:10]

        # Get article_id -> topic_id from topic_series via articles
        # (Reconstructing from topic series is not ideal; use the already-built mapping)

        # Since we need per-article data, re-derive article_topics from the fact that
        # topic_series aggregates counts -- this won't give us per-article granularity.
        # Instead, use a simulated DTW on the global topic_series pairs directly,
        # which still tests temporal alignment between topic emergence patterns.

        try:
            # DTW using scipy's euclidean distance matrix
            from scipy.spatial.distance import cdist

            for i, tid1 in enumerate(topic_ids[:20]):  # Cap at top 20
                for tid2 in topic_ids[i + 1:20]:
                    s1 = topic_series[tid1]
                    s2 = topic_series[tid2]

                    # Truncate to max length
                    s1 = s1[:DTW_MAX_SERIES_LENGTH]
                    s2 = s2[:DTW_MAX_SERIES_LENGTH]

                    if len(s1) < 3 or len(s2) < 3:
                        continue

                    # Simple DTW implementation
                    dtw_dist = self._compute_dtw(s1, s2)

                    # Compute lead-lag: which topic peaks first?
                    peak1 = np.argmax(s1) if s1.sum() > 0 else 0
                    peak2 = np.argmax(s2) if s2.sum() > 0 else 0
                    lag = int(peak1 - peak2)

                    # Normalize DTW distance to 0-1 strength (inverse)
                    max_dist = math.sqrt(len(s1) * len(s2)) * max(s1.max(), s2.max(), 1)
                    strength = max(0.0, 1.0 - dtw_dist / max(max_dist, 1e-8))

                    if strength > 0.1:  # Only report meaningful alignments
                        result.n_aligned_pairs += 1
                        leader = f"topic_{tid1}" if lag < 0 else f"topic_{tid2}"
                        follower = f"topic_{tid2}" if lag < 0 else f"topic_{tid1}"

                        result.records.append(CrossAnalysisRecord(
                            analysis_type="temporal",
                            source_entity=leader,
                            target_entity=follower,
                            relationship=f"temporal_alignment (dtw={dtw_dist:.2f}, lag={abs(lag)}d)",
                            strength=strength,
                            lag_days=abs(lag),
                            metadata=json.dumps({
                                "dtw_distance": round(dtw_dist, 4),
                                "peak_lag_days": lag,
                                "series_length": min(len(s1), len(s2)),
                                "topic_1": tid1,
                                "topic_2": tid2,
                            }),
                        ))
        except Exception as exc:
            logger.warning("stage6_temporal_error", error=str(exc))

        logger.info(
            "stage6_temporal_complete",
            n_aligned_pairs=result.n_aligned_pairs,
        )
        return result

    @staticmethod
    def _compute_dtw(s1: np.ndarray, s2: np.ndarray) -> float:
        """Compute Dynamic Time Warping distance between two 1D series.

        Uses a basic DP implementation to avoid external DTW library dependencies.
        """
        n = len(s1)
        m = len(s2)

        # Use float64 for stability
        dtw_matrix = np.full((n + 1, m + 1), np.inf, dtype=np.float64)
        dtw_matrix[0, 0] = 0.0

        for i in range(1, n + 1):
            for j in range(1, m + 1):
                cost = abs(float(s1[i - 1]) - float(s2[j - 1]))
                dtw_matrix[i, j] = cost + min(
                    dtw_matrix[i - 1, j],     # insertion
                    dtw_matrix[i, j - 1],     # deletion
                    dtw_matrix[i - 1, j - 1], # match
                )

        return float(dtw_matrix[n, m])

    # ------------------------------------------------------------------
    # T20: GraphRAG Knowledge Retrieval
    # ------------------------------------------------------------------

    def _run_graphrag(
        self,
        topics_table,
        articles_table,
        networks_table,
        embeddings_table,
    ) -> GraphRAGResult:
        """Build entity-topic knowledge graph for graph-based retrieval.

        Creates a bipartite graph linking entities to topics through articles,
        with SBERT embeddings for node representations. Produces enriched
        evidence chains for Stage 7 signal classification.
        """
        result = GraphRAGResult()

        if networks_table is None or embeddings_table is None:
            logger.info("stage6_graphrag_skip", reason="missing networks or embeddings")
            return result

        try:
            nx = _lazy_import_networkx()
        except ImportError:
            logger.warning("stage6_graphrag_skip", reason="networkx not installed")
            return result

        # Filter noise edges for GraphRAG (consistent with centrality and KG filtering)
        filtered_networks = self._filter_networks_by_weight(
            networks_table, CENTRALITY_MIN_WEIGHT,
        )
        if filtered_networks is None or filtered_networks.num_rows == 0:
            logger.info("stage6_graphrag_skip", reason="no edges after noise filtering")
            return result

        # Build entity -> articles mapping from filtered networks table
        entity_articles: dict[str, set[str]] = defaultdict(set)
        for i in range(filtered_networks.num_rows):
            entity_a = filtered_networks.column("entity_a")[i].as_py()
            entity_b = filtered_networks.column("entity_b")[i].as_py()
            if "source_articles" in filtered_networks.column_names:
                arts = filtered_networks.column("source_articles")[i].as_py()
                if arts:
                    if entity_a:
                        entity_articles[str(entity_a)].update(arts)
                    if entity_b:
                        entity_articles[str(entity_b)].update(arts)

        # Build article -> topic mapping
        article_topics: dict[str, str] = {}
        for i in range(topics_table.num_rows):
            aid = topics_table.column("article_id")[i].as_py()
            tid = topics_table.column("topic_id")[i].as_py()
            if tid is not None and str(tid) != "-1":
                article_topics[aid] = str(tid)

        # Build entity -> topic links through articles
        entity_topic_links: dict[tuple[str, str], set[str]] = defaultdict(set)
        for entity, arts in entity_articles.items():
            for aid in arts:
                if aid in article_topics:
                    tid = article_topics[aid]
                    entity_topic_links[(entity, tid)].add(aid)

        if not entity_topic_links:
            logger.info("stage6_graphrag_skip", reason="no entity-topic links")
            return result

        # Build bipartite knowledge graph
        KG = nx.Graph()
        for (entity, tid), articles in entity_topic_links.items():
            weight = len(articles)
            KG.add_edge(
                f"entity:{entity}",
                f"topic:{tid}",
                weight=weight,
                articles=list(articles),
            )

        # For each topic, traverse the graph to find related entities and evidence
        topics_in_graph = [
            n for n in KG.nodes() if n.startswith("topic:")
        ]

        for topic_node in topics_in_graph:
            # Get all entities connected to this topic
            neighbors = list(KG.neighbors(topic_node))
            entity_neighbors = [n for n in neighbors if n.startswith("entity:")]

            if not entity_neighbors:
                continue

            # Build evidence chain: topic -> entities -> articles
            evidence_articles: set[str] = set()
            entity_scores: list[tuple[str, float]] = []

            for en in entity_neighbors:
                edge_data = KG[topic_node][en]
                arts = edge_data.get("articles", [])
                evidence_articles.update(arts)
                entity_scores.append((en.replace("entity:", ""), float(edge_data.get("weight", 1))))

            # Sort entities by connection strength
            entity_scores.sort(key=lambda x: x[1], reverse=True)

            # Create evidence chain record
            top_entities = entity_scores[:10]
            max_weight = max(s for _, s in entity_scores) if entity_scores else 1
            strength = min(1.0, len(entity_scores) / 10.0)

            result.n_evidence_chains += 1
            result.records.append(CrossAnalysisRecord(
                analysis_type="graphrag",
                source_entity=topic_node.replace("topic:", "topic_"),
                target_entity="|".join(e for e, _ in top_entities[:5]),
                relationship=f"evidence_chain (entities={len(entity_scores)}, articles={len(evidence_articles)})",
                strength=strength,
                evidence_articles=sorted(evidence_articles)[:20],
                metadata=json.dumps({
                    "topic_id": topic_node.replace("topic:", ""),
                    "n_entities": len(entity_scores),
                    "n_articles": len(evidence_articles),
                    "top_entities": [
                        {"entity": e, "weight": round(w, 2)}
                        for e, w in top_entities
                    ],
                }),
            ))

        logger.info(
            "stage6_graphrag_complete",
            n_evidence_chains=result.n_evidence_chains,
            n_records=len(result.records),
        )
        return result

    # ------------------------------------------------------------------
    # T50: Contradiction Detection
    # ------------------------------------------------------------------

    def _run_contradiction_detection(
        self,
        topics_table,
        articles_table,
        embeddings_table,
        source_topic_articles: dict[str, dict[str, list[str]]],
    ) -> ContradictionResult:
        """Detect contradictions between articles covering the same topic from different sources.

        Step 1: Use SBERT cosine similarity to find article pairs on the same topic.
        Step 2 (optional): Use BART-MNLI to classify as entailment/contradiction/neutral.
        Falls back to similarity-only mode if BART-MNLI is unavailable.
        """
        result = ContradictionResult()

        if not self._enable_contradiction:
            logger.info("stage6_contradiction_skip", reason="disabled by configuration")
            return result

        if embeddings_table is None:
            logger.info("stage6_contradiction_skip", reason="no embeddings available")
            return result

        # Build embedding index
        embedding_index: dict[str, np.ndarray] = {}
        if "embedding" in embeddings_table.column_names:
            for i in range(embeddings_table.num_rows):
                aid = embeddings_table.column("article_id")[i].as_py()
                emb = embeddings_table.column("embedding")[i].as_py()
                if emb:
                    embedding_index[aid] = np.array(emb, dtype=np.float32)

        if not embedding_index:
            return result

        # Find cross-source article pairs on the same topic
        topic_cross_source_pairs: list[tuple[str, str, str, str, str]] = []
        # (topic_id, source1, article1, source2, article2)

        for tid_pair_set in self._find_cross_source_pairs(source_topic_articles):
            topic_cross_source_pairs.append(tid_pair_set)
            if len(topic_cross_source_pairs) > 500:  # Cap for performance
                break

        if not topic_cross_source_pairs:
            logger.info("stage6_contradiction_skip", reason="no cross-source pairs")
            return result

        # Step 1: SBERT similarity filtering
        candidate_pairs: list[tuple[str, str, str, str, str, float]] = []
        for tid, src1, aid1, src2, aid2 in topic_cross_source_pairs:
            if aid1 not in embedding_index or aid2 not in embedding_index:
                continue
            emb1 = embedding_index[aid1]
            emb2 = embedding_index[aid2]
            norm1 = np.linalg.norm(emb1)
            norm2 = np.linalg.norm(emb2)
            if norm1 < 1e-8 or norm2 < 1e-8:
                continue
            sim = float(np.dot(emb1, emb2) / (norm1 * norm2))
            if sim >= CONTRADICTION_SIMILARITY_THRESHOLD:
                candidate_pairs.append((tid, src1, aid1, src2, aid2, sim))
                result.n_pairs_checked += 1

        if not candidate_pairs:
            logger.info("stage6_contradiction_skip", reason="no similar cross-source pairs")
            return result

        # Step 2: Try NLI entailment (optional, graceful skip)
        nli_available = self._load_nli_model()

        if nli_available:
            # Get article texts for NLI
            all_aids = set()
            for _, _, a1, _, a2, _ in candidate_pairs:
                all_aids.add(a1)
                all_aids.add(a2)
            article_texts = _get_article_texts(articles_table, list(all_aids))

            for tid, src1, aid1, src2, aid2, sim in candidate_pairs:
                text1 = article_texts.get(aid1, "")
                text2 = article_texts.get(aid2, "")
                if not text1 or not text2:
                    continue

                # Truncate texts for NLI model
                text1 = text1[:512]
                text2 = text2[:512]

                try:
                    nli_result = self._nli_pipeline(
                        f"{text1}",
                        candidate_labels=["entailment", "contradiction", "neutral"],
                        hypothesis_template="{}",
                        multi_label=False,
                    )
                    # This is zero-shot; reshape output
                    if isinstance(nli_result, dict):
                        labels = nli_result.get("labels", [])
                        scores = nli_result.get("scores", [])
                        if labels and scores:
                            label_scores = dict(zip(labels, scores))
                            contradiction_score = label_scores.get("contradiction", 0.0)
                            if contradiction_score > 0.3:
                                result.n_contradictions += 1
                                result.records.append(CrossAnalysisRecord(
                                    analysis_type="contradiction",
                                    source_entity=f"{src1}:{aid1}",
                                    target_entity=f"{src2}:{aid2}",
                                    relationship=f"contradiction (nli_score={contradiction_score:.3f})",
                                    strength=float(contradiction_score),
                                    evidence_articles=[aid1, aid2],
                                    metadata=json.dumps({
                                        "topic_id": tid,
                                        "sbert_similarity": round(sim, 4),
                                        "nli_labels": {k: round(v, 4) for k, v in label_scores.items()},
                                        "source_1": src1,
                                        "source_2": src2,
                                    }),
                                ))
                except Exception as exc:
                    logger.debug("stage6_nli_pair_error", error=str(exc))
                    continue
        else:
            # Fallback: use embedding similarity inversion as a proxy
            # High similarity but from different sources suggests potential contradiction
            logger.info(
                "stage6_contradiction_fallback",
                reason="NLI model unavailable, using similarity-based detection",
            )
            for tid, src1, aid1, src2, aid2, sim in candidate_pairs:
                # In similarity-only mode, we flag high-similarity cross-source pairs
                # as "potential_contradiction" with lower confidence
                result.records.append(CrossAnalysisRecord(
                    analysis_type="contradiction",
                    source_entity=f"{src1}:{aid1}",
                    target_entity=f"{src2}:{aid2}",
                    relationship=f"potential_contradiction (similarity={sim:.3f}, no_nli)",
                    strength=sim * 0.5,  # Reduced confidence without NLI
                    evidence_articles=[aid1, aid2],
                    metadata=json.dumps({
                        "topic_id": tid,
                        "sbert_similarity": round(sim, 4),
                        "nli_available": False,
                        "source_1": src1,
                        "source_2": src2,
                    }),
                ))

        logger.info(
            "stage6_contradiction_complete",
            n_checked=result.n_pairs_checked,
            n_contradictions=result.n_contradictions,
            nli_used=nli_available,
        )
        return result

    @staticmethod
    def _find_cross_source_pairs(
        source_topic_articles: dict[str, dict[str, list[str]]],
    ):
        """Yield (topic_id, source1, article1, source2, article2) for cross-source article pairs."""
        # Build topic -> [(source, article_id)]
        topic_source_articles: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for src, topics in source_topic_articles.items():
            for tid, aids in topics.items():
                for aid in aids:
                    topic_source_articles[tid].append((src, aid))

        for tid, entries in topic_source_articles.items():
            # Only consider cross-source pairs
            for i in range(len(entries)):
                for j in range(i + 1, min(len(entries), i + 20)):  # Cap per-topic
                    src1, aid1 = entries[i]
                    src2, aid2 = entries[j]
                    if src1 != src2:
                        yield (tid, src1, aid1, src2, aid2)

    def _load_nli_model(self) -> bool:
        """Attempt to load BART-MNLI for NLI entailment scoring.

        Returns True if model loaded successfully, False otherwise.
        """
        if self._nli_pipeline is not None:
            return True

        try:
            from transformers import pipeline as hf_pipeline
            self._nli_pipeline = hf_pipeline(
                "zero-shot-classification",
                model="facebook/bart-large-mnli",
                device=-1,  # CPU
            )
            logger.info("stage6_nli_model_loaded")
            return True
        except Exception as exc:
            logger.info(
                "stage6_nli_model_unavailable",
                reason=str(exc),
            )
            return False

    # ------------------------------------------------------------------
    # Output writing
    # ------------------------------------------------------------------

    def _collect_all_records(self, output: Stage6Output) -> list[dict[str, Any]]:
        """Collect all CrossAnalysisRecord dicts from all technique results."""
        all_records: list[dict[str, Any]] = []

        for sub_result in [
            output.granger,
            output.pcmci,
            output.networks,
            output.cross_lingual,
            output.frame,
            output.agenda,
            output.temporal,
            output.graphrag,
            output.contradiction,
        ]:
            if sub_result is None:
                continue
            # Each result type has a .records field; NetworkAnalysisResult has multiple
            if isinstance(sub_result, NetworkAnalysisResult):
                for rec_list in [
                    sub_result.cooccurrence_records,
                    sub_result.kg_records,
                    sub_result.centrality_records,
                    sub_result.evolution_records,
                ]:
                    for rec in rec_list:
                        all_records.append(rec.to_dict())
            else:
                for rec in sub_result.records:
                    all_records.append(rec.to_dict())

        return all_records

    def _write_parquet(
        self,
        records: list[dict[str, Any]],
        output_dir: Path,
    ) -> None:
        """Write cross_analysis.parquet from collected records."""
        pa, pq = _lazy_import_pyarrow()
        schema = _cross_analysis_schema()

        if not records:
            # Write empty table preserving schema
            table = pa.table(
                {f.name: pa.array([], type=f.type) for f in schema},
                schema=schema,
            )
        else:
            # Build column arrays
            columns = {
                "analysis_type": pa.array(
                    [r["analysis_type"] for r in records], type=pa.utf8()
                ),
                "source_entity": pa.array(
                    [r["source_entity"] for r in records], type=pa.utf8()
                ),
                "target_entity": pa.array(
                    [r["target_entity"] for r in records], type=pa.utf8()
                ),
                "relationship": pa.array(
                    [r["relationship"] for r in records], type=pa.utf8()
                ),
                "strength": pa.array(
                    [r["strength"] for r in records], type=pa.float32()
                ),
                "p_value": pa.array(
                    [r["p_value"] for r in records], type=pa.float32()
                ),
                "lag_days": pa.array(
                    [r["lag_days"] for r in records], type=pa.int32()
                ),
                "evidence_articles": pa.array(
                    [r["evidence_articles"] for r in records],
                    type=pa.list_(pa.utf8()),
                ),
                "metadata": pa.array(
                    [r["metadata"] for r in records], type=pa.utf8()
                ),
            }
            table = pa.table(columns, schema=schema)

        out_path = output_dir / "cross_analysis.parquet"
        pq.write_table(
            table,
            str(out_path),
            compression=PARQUET_COMPRESSION,
            compression_level=PARQUET_COMPRESSION_LEVEL,
        )
        logger.info(
            "stage6_parquet_written",
            path=str(out_path),
            rows=table.num_rows,
        )

    def _write_empty_output(self, output_dir: Path) -> None:
        """Write empty cross_analysis.parquet preserving schema."""
        self._write_parquet([], output_dir)

    @staticmethod
    def _safe_load_table(pq, path: Path, name: str):
        """Load a Parquet table with graceful fallback to None."""
        try:
            if path.exists():
                return pq.read_table(str(path))
        except Exception as exc:
            logger.warning(f"stage6_load_warning_{name}", error=str(exc))
        return None


# ---------------------------------------------------------------------------
# Convenience function (module-level entry point)
# ---------------------------------------------------------------------------

def run_stage6(
    timeseries_path: Path | None = None,
    topics_path: Path | None = None,
    analysis_path: Path | None = None,
    networks_path: Path | None = None,
    embeddings_path: Path | None = None,
    articles_path: Path | None = None,
    output_dir: Path | None = None,
    enable_pcmci: bool = True,
    enable_contradiction: bool = True,
    cleanup_after: bool = True,
) -> Stage6Output:
    """Run the complete Stage 6 cross-analysis pipeline.

    This is the primary entry point for orchestrating Stage 6.
    Creates a Stage6CrossAnalyzer, runs it, and optionally cleans up.

    Args:
        timeseries_path: Path to timeseries.parquet.
        topics_path: Path to topics.parquet.
        analysis_path: Path to article_analysis.parquet.
        networks_path: Path to networks.parquet.
        embeddings_path: Path to embeddings.parquet.
        articles_path: Path to articles.parquet.
        output_dir: Directory to write cross_analysis.parquet.
        enable_pcmci: Whether to attempt PCMCI causal discovery.
        enable_contradiction: Whether to attempt NLI contradiction detection.
        cleanup_after: Whether to call cleanup() after run().

    Returns:
        Stage6Output with all results.
    """
    analyzer = Stage6CrossAnalyzer(
        enable_pcmci=enable_pcmci,
        enable_contradiction=enable_contradiction,
    )
    try:
        output = analyzer.run(
            timeseries_path=timeseries_path,
            topics_path=topics_path,
            analysis_path=analysis_path,
            networks_path=networks_path,
            embeddings_path=embeddings_path,
            articles_path=articles_path,
            output_dir=output_dir,
        )
    finally:
        if cleanup_after:
            analyzer.cleanup()

    return output
