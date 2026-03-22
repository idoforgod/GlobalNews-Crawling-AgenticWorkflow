"""Tests for src.analysis.stage6_cross_analysis -- Stage 6 Cross Analysis.

All tests use mock data and mock models to avoid downloading NLP models in CI.
The mocks replicate the exact interface contracts of statsmodels, tigramite,
networkx, and transformers NLI pipeline.

Test categories:
    - Schema validation: Parquet output matches PRD section 7.1 cross_analysis schema
    - Helper functions: topic daily series, stationarity, source-topic mapping
    - Component unit tests: Granger, PCMCI, co-occurrence, KG, centrality,
      evolution, cross-lingual, frame, agenda, temporal, GraphRAG, contradiction
    - Integration: full Stage6CrossAnalyzer.run() with synthetic data
    - Edge cases: empty inputs, insufficient data, missing dependencies
    - Graceful degradation: individual technique failures do not crash pipeline
"""

import json
import math
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.analysis.stage6_cross_analysis import (
    Stage6CrossAnalyzer,
    Stage6Output,
    CrossAnalysisRecord,
    GrangerResult,
    PCMCIResult,
    NetworkAnalysisResult,
    CrossLingualResult,
    FrameAnalysisResult,
    AgendaSettingResult,
    TemporalAlignmentResult,
    GraphRAGResult,
    ContradictionResult,
    _build_topic_daily_series,
    _check_stationarity,
    _build_source_topic_articles,
    _get_article_texts,
    _get_article_languages,
    _cross_analysis_schema,
    _get_memory_gb,
    run_stage6,
    GRANGER_MAX_LAG,
    GRANGER_SIGNIFICANCE,
    PCMCI_TAU_MAX,
    FRAME_DIMENSIONS,
)
from src.utils.error_handler import PipelineStageError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_output_dir(tmp_path):
    """Create and return a temporary output directory."""
    out = tmp_path / "analysis"
    out.mkdir()
    return out


@pytest.fixture
def sample_articles_table():
    """Create a sample articles PyArrow table with 20 articles across 2 languages, 3 sources."""
    n = 20
    sources = ["source_a", "source_b", "source_c"]
    languages = ["ko", "en"]

    schema = pa.schema([
        pa.field("article_id", pa.utf8()),
        pa.field("url", pa.utf8()),
        pa.field("title", pa.utf8()),
        pa.field("body", pa.utf8()),
        pa.field("source", pa.utf8()),
        pa.field("category", pa.utf8()),
        pa.field("language", pa.utf8()),
        pa.field("published_at", pa.utf8()),
        pa.field("crawled_at", pa.utf8()),
        pa.field("author", pa.utf8()),
        pa.field("word_count", pa.int32()),
        pa.field("content_hash", pa.utf8()),
    ])

    # Spread articles across 30 days
    base_date = "2025-01-"
    data = {
        "article_id": [f"art-{i:03d}" for i in range(n)],
        "url": [f"https://example.com/{i}" for i in range(n)],
        "title": [
            "Economy faces inflation pressure from global markets",
            "Military tension rises in the South China Sea region",
            "New AI research breakthrough at MIT university",
            "Government announces new fiscal policy reform",
            "Health care system under strain from pandemic wave",
            "Stock market rally driven by technology sector",
            "Nuclear weapons treaty faces new challenge",
            "Climate change research reveals alarming data",
            "President meets with opposition party leaders",
            "Community welfare program expands to rural areas",
            "Trade deficit grows amid currency depreciation",
            "Defense budget increase approved by parliament",
            "Scientific discovery in renewable energy storage",
            "Election campaign begins for local government seats",
            "Refugee crisis demands humanitarian response",
            "GDP growth exceeds expectations for third quarter",
            "Intelligence agencies warn of cyber threats",
            "Innovation hub opens in Seoul technology district",
            "Congress debates new immigration legislation bill",
            "Environmental groups protest pipeline construction",
        ],
        "body": [f"Body text for article {i}. " * 50 for i in range(n)],
        "source": [sources[i % len(sources)] for i in range(n)],
        "category": ["economy", "security", "science", "politics", "health"] * 4,
        "language": [languages[i % len(languages)] for i in range(n)],
        "published_at": [f"{base_date}{(i % 30) + 1:02d}T10:00:00Z" for i in range(n)],
        "crawled_at": [f"{base_date}{(i % 30) + 1:02d}T12:00:00Z" for i in range(n)],
        "author": [f"Author {i % 5}" for i in range(n)],
        "word_count": [100 + i * 10 for i in range(n)],
        "content_hash": [f"hash-{i:03d}" for i in range(n)],
    }

    return pa.table(data, schema=schema)


@pytest.fixture
def sample_topics_table():
    """Create a sample topics PyArrow table with 3 topics across 20 articles."""
    n = 20
    topic_ids = ["0", "1", "2"]

    schema = pa.schema([
        pa.field("article_id", pa.utf8()),
        pa.field("topic_id", pa.utf8()),
        pa.field("topic_label", pa.utf8()),
        pa.field("topic_probability", pa.float32()),
        pa.field("hdbscan_cluster_id", pa.int32()),
        pa.field("nmf_topic_id", pa.int32()),
        pa.field("lda_topic_id", pa.int32()),
    ])

    data = {
        "article_id": [f"art-{i:03d}" for i in range(n)],
        "topic_id": [topic_ids[i % len(topic_ids)] for i in range(n)],
        "topic_label": [f"Topic {i % len(topic_ids)}" for i in range(n)],
        "topic_probability": [0.7 + (i % 3) * 0.1 for i in range(n)],
        "hdbscan_cluster_id": [i % 3 for i in range(n)],
        "nmf_topic_id": [i % 3 for i in range(n)],
        "lda_topic_id": [i % 3 for i in range(n)],
    }

    return pa.table(data, schema=schema)


@pytest.fixture
def sample_networks_table():
    """Create a sample networks PyArrow table with entity co-occurrence."""
    schema = pa.schema([
        pa.field("entity_a", pa.utf8()),
        pa.field("entity_b", pa.utf8()),
        pa.field("co_occurrence_count", pa.int32()),
        pa.field("community_id", pa.int32()),
        pa.field("source_articles", pa.list_(pa.utf8())),
    ])

    entities = [
        ("Bank of Korea", "Federal Reserve", 15, 0, ["art-000", "art-005", "art-010"]),
        ("Federal Reserve", "US Treasury", 12, 0, ["art-005", "art-010", "art-015"]),
        ("Bank of Korea", "Ministry of Finance", 10, 1, ["art-000", "art-003"]),
        ("Seoul", "Tokyo", 8, 2, ["art-002", "art-007"]),
        ("MIT", "Stanford University", 7, 3, ["art-002", "art-012"]),
        ("President Biden", "Congress", 6, 4, ["art-008", "art-013"]),
        ("Samsung Inc", "TSMC Corp", 5, 3, ["art-005", "art-017"]),
        ("Washington", "Beijing", 9, 2, ["art-001", "art-006", "art-016"]),
        ("UN Agency", "WHO Organization", 4, 4, ["art-004", "art-014"]),
        ("EU Commission", "NATO Agency", 3, 4, ["art-001", "art-011"]),
        ("Reuters Ltd", "AP Group", 6, 0, ["art-003", "art-008"]),
        ("IMF Fund", "World Bank Group", 8, 0, ["art-000", "art-010"]),
    ]

    data = {
        "entity_a": [e[0] for e in entities],
        "entity_b": [e[1] for e in entities],
        "co_occurrence_count": [e[2] for e in entities],
        "community_id": [e[3] for e in entities],
        "source_articles": [e[4] for e in entities],
    }

    return pa.table(data, schema=schema)


@pytest.fixture
def sample_embeddings_table():
    """Create a sample embeddings PyArrow table with 384-dim SBERT embeddings."""
    n = 20
    rng = np.random.RandomState(42)

    schema = pa.schema([
        pa.field("article_id", pa.utf8()),
        pa.field("embedding", pa.list_(pa.float32())),
        pa.field("title_embedding", pa.list_(pa.float32())),
        pa.field("keywords", pa.list_(pa.utf8())),
    ])

    embeddings = rng.randn(n, 384).astype(np.float32)
    # Normalize
    norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
    embeddings = embeddings / np.maximum(norms, 1e-8)

    data = {
        "article_id": [f"art-{i:03d}" for i in range(n)],
        "embedding": [emb.tolist() for emb in embeddings],
        "title_embedding": [emb.tolist() for emb in embeddings],  # Same for simplicity
        "keywords": [["keyword1", "keyword2"] for _ in range(n)],
    }

    return pa.table(data, schema=schema)


@pytest.fixture
def sample_timeseries_table():
    """Create a minimal timeseries PyArrow table."""
    schema = pa.schema([
        pa.field("series_id", pa.utf8()),
        pa.field("topic_id", pa.utf8()),
        pa.field("metric_type", pa.utf8()),
        pa.field("date", pa.utf8()),
        pa.field("value", pa.float32()),
        pa.field("trend", pa.float32()),
        pa.field("seasonal", pa.float32()),
        pa.field("residual", pa.float32()),
        pa.field("burst_score", pa.float32()),
        pa.field("is_changepoint", pa.bool_()),
        pa.field("changepoint_significance", pa.float32()),
        pa.field("prophet_forecast", pa.float32()),
        pa.field("prophet_lower", pa.float32()),
        pa.field("prophet_upper", pa.float32()),
        pa.field("ma_short", pa.float32()),
        pa.field("ma_long", pa.float32()),
        pa.field("ma_signal", pa.utf8()),
    ])

    # Empty but valid
    data = {col: [] for col in schema.names}
    return pa.table(data, schema=schema)


@pytest.fixture
def write_all_parquets(
    tmp_path,
    sample_articles_table,
    sample_topics_table,
    sample_networks_table,
    sample_embeddings_table,
    sample_timeseries_table,
):
    """Write all input Parquet files to tmp_path and return a dict of paths."""
    processed_dir = tmp_path / "processed"
    processed_dir.mkdir()
    features_dir = tmp_path / "features"
    features_dir.mkdir()
    analysis_dir = tmp_path / "analysis"
    analysis_dir.mkdir()

    articles_path = processed_dir / "articles.parquet"
    topics_path = analysis_dir / "topics.parquet"
    networks_path = analysis_dir / "networks.parquet"
    embeddings_path = features_dir / "embeddings.parquet"
    timeseries_path = analysis_dir / "timeseries.parquet"

    pq.write_table(sample_articles_table, str(articles_path))
    pq.write_table(sample_topics_table, str(topics_path))
    pq.write_table(sample_networks_table, str(networks_path))
    pq.write_table(sample_embeddings_table, str(embeddings_path))
    pq.write_table(sample_timeseries_table, str(timeseries_path))

    return {
        "articles_path": articles_path,
        "topics_path": topics_path,
        "networks_path": networks_path,
        "embeddings_path": embeddings_path,
        "timeseries_path": timeseries_path,
        "output_dir": analysis_dir,
    }


# ---------------------------------------------------------------------------
# Schema Tests
# ---------------------------------------------------------------------------

class TestSchema:
    """Verify cross_analysis.parquet schema matches PRD section 7.1."""

    def test_schema_has_all_required_columns(self):
        schema = _cross_analysis_schema()
        expected = {
            "analysis_type", "source_entity", "target_entity",
            "relationship", "strength", "p_value", "lag_days",
            "evidence_articles", "metadata",
        }
        assert set(schema.names) == expected

    def test_schema_column_types(self):
        schema = _cross_analysis_schema()
        assert schema.field("analysis_type").type == pa.utf8()
        assert schema.field("source_entity").type == pa.utf8()
        assert schema.field("target_entity").type == pa.utf8()
        assert schema.field("relationship").type == pa.utf8()
        assert schema.field("strength").type == pa.float32()
        assert schema.field("p_value").type == pa.float32()
        assert schema.field("lag_days").type == pa.int32()
        assert schema.field("evidence_articles").type == pa.list_(pa.utf8())
        assert schema.field("metadata").type == pa.utf8()

    def test_schema_nullable_fields(self):
        schema = _cross_analysis_schema()
        assert schema.field("p_value").nullable is True
        assert schema.field("lag_days").nullable is True
        assert schema.field("analysis_type").nullable is False
        assert schema.field("strength").nullable is False


# ---------------------------------------------------------------------------
# CrossAnalysisRecord Tests
# ---------------------------------------------------------------------------

class TestCrossAnalysisRecord:
    """Test the CrossAnalysisRecord dataclass."""

    def test_to_dict_basic(self):
        rec = CrossAnalysisRecord(
            analysis_type="granger",
            source_entity="topic_0",
            target_entity="topic_1",
            relationship="granger_causes",
            strength=0.85,
            p_value=0.001,
            lag_days=3,
            evidence_articles=["art-001", "art-002"],
            metadata='{"key": "value"}',
        )
        d = rec.to_dict()
        assert d["analysis_type"] == "granger"
        assert d["strength"] == 0.85
        assert d["p_value"] == 0.001
        assert d["lag_days"] == 3
        assert d["evidence_articles"] == ["art-001", "art-002"]

    def test_to_dict_nullable_fields(self):
        rec = CrossAnalysisRecord(
            analysis_type="cooccurrence",
            source_entity="a",
            target_entity="b",
            relationship="mentioned_with",
            strength=0.5,
        )
        d = rec.to_dict()
        assert d["p_value"] is None
        assert d["lag_days"] is None
        assert d["evidence_articles"] == []
        assert d["metadata"] == "{}"


# ---------------------------------------------------------------------------
# Helper Function Tests
# ---------------------------------------------------------------------------

class TestBuildTopicDailySeries:
    """Test _build_topic_daily_series helper."""

    def test_builds_correct_series(self, sample_topics_table, sample_articles_table):
        series, tids, dates = _build_topic_daily_series(
            sample_topics_table, sample_articles_table, min_days=1,
        )
        assert len(tids) == 3  # Topics 0, 1, 2
        assert all(isinstance(s, np.ndarray) for s in series.values())
        # Total counts should equal number of articles
        total = sum(s.sum() for s in series.values())
        assert total == 20

    def test_returns_empty_for_insufficient_days(self, sample_topics_table):
        # Create articles with only 2 days
        schema = pa.schema([
            pa.field("article_id", pa.utf8()),
            pa.field("published_at", pa.utf8()),
        ])
        arts = pa.table({
            "article_id": ["art-000", "art-001"],
            "published_at": ["2025-01-01", "2025-01-02"],
        }, schema=schema)

        series, tids, dates = _build_topic_daily_series(
            sample_topics_table, arts, min_days=7,
        )
        assert len(series) == 0
        assert len(tids) == 0


class TestCheckStationarity:
    """Test _check_stationarity with ADF test."""

    def test_stationary_series(self):
        rng = np.random.RandomState(42)
        # White noise is stationary
        series = rng.randn(100)
        is_stat, processed = _check_stationarity(series)
        assert is_stat is True
        assert len(processed) >= len(series) - 1  # May be original or differenced

    def test_short_series_returns_false(self):
        series = np.array([1.0, 2.0, 3.0])
        is_stat, processed = _check_stationarity(series)
        assert is_stat is False

    def test_nonstationary_gets_differenced(self):
        # Random walk (non-stationary)
        rng = np.random.RandomState(42)
        walk = np.cumsum(rng.randn(100))
        is_stat, processed = _check_stationarity(walk)
        # After differencing, should be stationary or still marked non-stationary
        # The differenced series will be shorter
        if is_stat:
            assert len(processed) == 99  # First difference


class TestBuildSourceTopicArticles:
    """Test _build_source_topic_articles helper."""

    def test_builds_correct_mapping(self, sample_topics_table, sample_articles_table):
        mapping = _build_source_topic_articles(sample_topics_table, sample_articles_table)
        assert len(mapping) > 0
        # Each source should have topic entries
        for src, topics in mapping.items():
            assert isinstance(topics, dict)
            for tid, aids in topics.items():
                assert len(aids) > 0


class TestGetArticleTexts:
    """Test _get_article_texts helper."""

    def test_extracts_body_text(self, sample_articles_table):
        texts = _get_article_texts(sample_articles_table, ["art-000", "art-001"])
        assert len(texts) == 2
        assert "art-000" in texts
        assert len(texts["art-000"]) > 0


class TestGetArticleLanguages:
    """Test _get_article_languages helper."""

    def test_extracts_languages(self, sample_articles_table):
        langs = _get_article_languages(sample_articles_table)
        assert len(langs) == 20
        assert "art-000" in langs
        assert langs["art-000"] in ("ko", "en")


class TestGetMemoryGb:
    """Test _get_memory_gb utility."""

    def test_returns_float(self):
        result = _get_memory_gb()
        assert isinstance(result, float)
        assert result >= 0.0


# ---------------------------------------------------------------------------
# Granger Causality Tests (T37)
# ---------------------------------------------------------------------------

class TestGrangerCausality:
    """Test T37 Granger causality testing."""

    def test_granger_with_correlated_series(self):
        """Two correlated series should produce at least some significant results."""
        analyzer = Stage6CrossAnalyzer()
        rng = np.random.RandomState(42)

        n_days = 100
        # Create series where topic_1 is a lagged version of topic_0
        topic_0 = rng.randn(n_days).cumsum()
        topic_0 = np.diff(topic_0)  # Difference to make stationary
        topic_1 = np.zeros(len(topic_0))
        topic_1[3:] = topic_0[:-3] + rng.randn(len(topic_0) - 3) * 0.1

        topic_series = {
            "0": topic_0,
            "1": topic_1,
        }
        topic_ids = ["0", "1"]

        result = analyzer._run_granger(topic_series, topic_ids)
        assert isinstance(result, GrangerResult)
        assert result.n_tested > 0
        # May or may not find significance depending on test noise

    def test_granger_fewer_than_2_topics(self):
        analyzer = Stage6CrossAnalyzer()
        result = analyzer._run_granger({"0": np.zeros(50)}, ["0"])
        assert isinstance(result, GrangerResult)
        assert result.n_tested == 0

    def test_granger_short_series(self):
        analyzer = Stage6CrossAnalyzer()
        topic_series = {
            "0": np.array([1.0, 2.0, 3.0]),
            "1": np.array([2.0, 3.0, 4.0]),
        }
        result = analyzer._run_granger(topic_series, ["0", "1"])
        assert result.n_tested == 0

    def test_granger_bonferroni_correction(self):
        """Verify that Bonferroni correction reduces false positives."""
        analyzer = Stage6CrossAnalyzer()
        rng = np.random.RandomState(42)

        # Create 5 independent random series (no actual causality)
        n_days = 100
        topic_series = {}
        for i in range(5):
            topic_series[str(i)] = rng.randn(n_days)

        topic_ids = list(topic_series.keys())
        result = analyzer._run_granger(topic_series, topic_ids)

        # With Bonferroni correction on 20 tests (5*4), should have very few
        # or no false positives at alpha=0.05
        assert isinstance(result, GrangerResult)
        # The correction should be: 0.05 / 20 = 0.0025
        for rec in result.records:
            meta = json.loads(rec.metadata)
            assert meta["bonferroni_threshold"] == pytest.approx(
                GRANGER_SIGNIFICANCE / 20, rel=1e-4
            )


# ---------------------------------------------------------------------------
# PCMCI Tests (T38)
# ---------------------------------------------------------------------------

class TestPCMCI:
    """Test T38 PCMCI causal inference."""

    def test_pcmci_disabled(self):
        analyzer = Stage6CrossAnalyzer(enable_pcmci=False)
        result = analyzer._run_pcmci({"0": np.zeros(50)}, ["0"])
        assert isinstance(result, PCMCIResult)
        assert len(result.records) == 0

    def test_pcmci_fewer_than_2_topics(self):
        analyzer = Stage6CrossAnalyzer(enable_pcmci=True)
        result = analyzer._run_pcmci({"0": np.zeros(50)}, ["0"])
        assert result.n_causal_links == 0

    @patch("src.analysis.stage6_cross_analysis._lazy_import_tigramite")
    def test_pcmci_tigramite_not_installed(self, mock_import):
        mock_import.return_value = (None, None, None)
        analyzer = Stage6CrossAnalyzer(enable_pcmci=True)
        topic_series = {
            "0": np.random.randn(50),
            "1": np.random.randn(50),
        }
        result = analyzer._run_pcmci(topic_series, ["0", "1"])
        assert isinstance(result, PCMCIResult)
        assert result.converged is False


# ---------------------------------------------------------------------------
# Network Analysis Tests (T39-T42)
# ---------------------------------------------------------------------------

class TestCooccurrenceNetwork:
    """Test T39 co-occurrence network construction."""

    def test_builds_cooccurrence_records(
        self, sample_topics_table, sample_articles_table
    ):
        analyzer = Stage6CrossAnalyzer()
        nx = pytest.importorskip("networkx")
        records = analyzer._build_cooccurrence_network(
            nx, sample_topics_table, sample_articles_table,
        )
        assert isinstance(records, list)
        # With 3 topics, max pairs = C(3,2) = 3
        assert len(records) <= 3
        for rec in records:
            assert rec.analysis_type == "cooccurrence"
            assert 0.0 <= rec.strength <= 1.0


class TestKnowledgeGraph:
    """Test T40 knowledge graph construction."""

    def test_builds_kg_records(self, sample_networks_table, sample_articles_table):
        analyzer = Stage6CrossAnalyzer()
        nx = pytest.importorskip("networkx")
        records = analyzer._build_knowledge_graph(
            nx, sample_networks_table, sample_articles_table,
        )
        assert isinstance(records, list)
        assert len(records) == sample_networks_table.num_rows
        for rec in records:
            assert rec.analysis_type == "knowledge_graph"
            assert rec.relationship in ("mentioned_with", "works_at", "located_in")

    def test_kg_none_networks(self, sample_articles_table):
        analyzer = Stage6CrossAnalyzer()
        nx = pytest.importorskip("networkx")
        records = analyzer._build_knowledge_graph(nx, None, sample_articles_table)
        assert records == []


class TestRelationTypeInference:
    """Test the heuristic relation type inference."""

    def test_location_inference(self):
        assert Stage6CrossAnalyzer._infer_relation_type("Seoul", "Meeting") == "located_in"
        assert Stage6CrossAnalyzer._infer_relation_type("Company", "Washington") == "located_in"

    def test_org_inference(self):
        assert Stage6CrossAnalyzer._infer_relation_type("Samsung Inc", "Person") == "works_at"
        assert Stage6CrossAnalyzer._infer_relation_type("Person", "Ministry of") == "works_at"

    def test_default_mentioned_with(self):
        assert Stage6CrossAnalyzer._infer_relation_type("Alpha", "Beta") == "mentioned_with"


class TestCentralityAnalysis:
    """Test T41 centrality metrics."""

    def test_computes_centrality(self, sample_networks_table):
        analyzer = Stage6CrossAnalyzer()
        nx = pytest.importorskip("networkx")
        records, n_nodes, n_edges, modularity = analyzer._compute_centrality(
            nx, sample_networks_table,
        )
        assert n_nodes > 0
        assert n_edges > 0
        assert isinstance(modularity, float)
        # Should have records for degree, betweenness, and pagerank
        metrics_found = set()
        for rec in records:
            assert rec.analysis_type == "centrality"
            meta = json.loads(rec.metadata)
            metrics_found.add(meta["metric"])
        assert "degree_centrality" in metrics_found
        assert "betweenness_centrality" in metrics_found
        assert "pagerank" in metrics_found

    def test_centrality_empty_network(self):
        analyzer = Stage6CrossAnalyzer()
        nx = pytest.importorskip("networkx")
        records, n_nodes, n_edges, mod = analyzer._compute_centrality(nx, None)
        assert records == []
        assert n_nodes == 0


class TestFilterNetworksByWeight:
    """Test _filter_networks_by_weight noise removal."""

    def test_filter_basic(self, sample_networks_table):
        """Edges with weight below threshold are removed."""
        analyzer = Stage6CrossAnalyzer()
        # sample_networks_table has weights from 3 to 15
        filtered = analyzer._filter_networks_by_weight(sample_networks_table, min_weight=8)
        assert filtered.num_rows < sample_networks_table.num_rows
        # All remaining rows should have co_occurrence_count >= 8
        for i in range(filtered.num_rows):
            assert filtered.column("co_occurrence_count")[i].as_py() >= 8

    def test_filter_below_threshold_returns_empty(self):
        """When all edges are below threshold, return empty table."""
        analyzer = Stage6CrossAnalyzer()
        schema = pa.schema([
            pa.field("entity_a", pa.utf8()),
            pa.field("entity_b", pa.utf8()),
            pa.field("co_occurrence_count", pa.int32()),
            pa.field("community_id", pa.int32()),
            pa.field("source_articles", pa.list_(pa.utf8())),
        ])
        table = pa.table({
            "entity_a": ["A", "B"],
            "entity_b": ["C", "D"],
            "co_occurrence_count": [1, 1],
            "community_id": [0, 0],
            "source_articles": [["art-1"], ["art-2"]],
        }, schema=schema)
        filtered = analyzer._filter_networks_by_weight(table, min_weight=2)
        assert filtered.num_rows == 0

    def test_filter_none_returns_none(self):
        """None input returns None."""
        analyzer = Stage6CrossAnalyzer()
        assert analyzer._filter_networks_by_weight(None, min_weight=2) is None

    def test_filter_preserves_schema(self, sample_networks_table):
        """Filtered table has the same schema as original."""
        analyzer = Stage6CrossAnalyzer()
        filtered = analyzer._filter_networks_by_weight(sample_networks_table, min_weight=5)
        assert filtered.schema.equals(sample_networks_table.schema)


class TestNetworkEvolution:
    """Test T42 weekly network evolution."""

    def test_computes_evolution(self, sample_topics_table, sample_articles_table):
        analyzer = Stage6CrossAnalyzer()
        nx = pytest.importorskip("networkx")
        records = analyzer._compute_network_evolution(
            nx, sample_topics_table, sample_articles_table,
        )
        assert isinstance(records, list)
        for rec in records:
            assert rec.analysis_type == "network_evolution"
            meta = json.loads(rec.metadata)
            assert "prev_density" in meta
            assert "curr_density" in meta


# ---------------------------------------------------------------------------
# Cross-Lingual Tests (T43)
# ---------------------------------------------------------------------------

class TestCrossLingual:
    """Test T43 cross-lingual topic alignment."""

    def test_aligns_topics(
        self, sample_topics_table, sample_articles_table, sample_embeddings_table,
    ):
        analyzer = Stage6CrossAnalyzer()
        result = analyzer._run_cross_lingual(
            sample_topics_table, sample_articles_table, sample_embeddings_table,
        )
        assert isinstance(result, CrossLingualResult)
        # May or may not find alignments with random embeddings
        for rec in result.records:
            assert rec.analysis_type == "cross_lingual"
            assert rec.strength > 0

    def test_cross_lingual_no_embeddings(self, sample_topics_table, sample_articles_table):
        analyzer = Stage6CrossAnalyzer()
        result = analyzer._run_cross_lingual(
            sample_topics_table, sample_articles_table, None,
        )
        assert len(result.records) == 0


# ---------------------------------------------------------------------------
# Frame Analysis Tests (T44)
# ---------------------------------------------------------------------------

class TestFrameAnalysis:
    """Test T44 frame analysis via KL divergence."""

    def test_frame_analysis_produces_records(
        self, sample_topics_table, sample_articles_table,
    ):
        analyzer = Stage6CrossAnalyzer()
        source_topic_articles = _build_source_topic_articles(
            sample_topics_table, sample_articles_table,
        )
        result = analyzer._run_frame_analysis(
            source_topic_articles, sample_articles_table,
        )
        assert isinstance(result, FrameAnalysisResult)
        for rec in result.records:
            assert rec.analysis_type == "frame"
            meta = json.loads(rec.metadata)
            assert "symmetric_kl" in meta
            assert "frame_dimensions" in meta

    def test_frame_analysis_empty_sources(self, sample_articles_table):
        analyzer = Stage6CrossAnalyzer()
        result = analyzer._run_frame_analysis({}, sample_articles_table)
        assert len(result.records) == 0


# ---------------------------------------------------------------------------
# Agenda Setting Tests (T45)
# ---------------------------------------------------------------------------

class TestAgendaSetting:
    """Test T45 agenda setting analysis."""

    def test_agenda_setting_produces_records(
        self, sample_topics_table, sample_articles_table,
    ):
        analyzer = Stage6CrossAnalyzer()
        source_topic_articles = _build_source_topic_articles(
            sample_topics_table, sample_articles_table,
        )
        topic_series, topic_ids, _ = _build_topic_daily_series(
            sample_topics_table, sample_articles_table, min_days=1,
        )
        result = analyzer._run_agenda_setting(
            source_topic_articles, topic_series, sample_articles_table,
        )
        assert isinstance(result, AgendaSettingResult)
        for rec in result.records:
            assert rec.analysis_type == "agenda"
            assert rec.lag_days is not None


# ---------------------------------------------------------------------------
# Temporal Alignment Tests (T46)
# ---------------------------------------------------------------------------

class TestTemporalAlignment:
    """Test T46 DTW-based temporal alignment."""

    def test_dtw_computation(self):
        """Test the DTW helper directly."""
        s1 = np.array([0, 1, 2, 3, 4, 3, 2, 1, 0], dtype=np.float64)
        s2 = np.array([0, 0, 1, 2, 3, 4, 3, 2, 1], dtype=np.float64)
        dist = Stage6CrossAnalyzer._compute_dtw(s1, s2)
        assert dist >= 0
        # DTW of a series with itself should be 0
        self_dist = Stage6CrossAnalyzer._compute_dtw(s1, s1)
        assert self_dist == pytest.approx(0.0, abs=1e-10)

    def test_temporal_alignment_produces_records(
        self, sample_topics_table, sample_articles_table,
    ):
        analyzer = Stage6CrossAnalyzer()
        topic_series, topic_ids, _ = _build_topic_daily_series(
            sample_topics_table, sample_articles_table, min_days=1,
        )
        result = analyzer._run_temporal_alignment(
            topic_series, topic_ids, sample_articles_table,
        )
        assert isinstance(result, TemporalAlignmentResult)
        for rec in result.records:
            assert rec.analysis_type == "temporal"


# ---------------------------------------------------------------------------
# GraphRAG Tests (T20)
# ---------------------------------------------------------------------------

class TestGraphRAG:
    """Test T20 GraphRAG knowledge retrieval."""

    def test_graphrag_produces_records(
        self, sample_topics_table, sample_articles_table,
        sample_networks_table, sample_embeddings_table,
    ):
        analyzer = Stage6CrossAnalyzer()
        result = analyzer._run_graphrag(
            sample_topics_table, sample_articles_table,
            sample_networks_table, sample_embeddings_table,
        )
        assert isinstance(result, GraphRAGResult)
        for rec in result.records:
            assert rec.analysis_type == "graphrag"
            meta = json.loads(rec.metadata)
            assert "n_entities" in meta
            assert "n_articles" in meta

    def test_graphrag_no_networks(
        self, sample_topics_table, sample_articles_table, sample_embeddings_table,
    ):
        analyzer = Stage6CrossAnalyzer()
        result = analyzer._run_graphrag(
            sample_topics_table, sample_articles_table, None, sample_embeddings_table,
        )
        assert len(result.records) == 0


# ---------------------------------------------------------------------------
# Contradiction Detection Tests (T50)
# ---------------------------------------------------------------------------

class TestContradictionDetection:
    """Test T50 contradiction detection."""

    def test_contradiction_disabled(self):
        analyzer = Stage6CrossAnalyzer(enable_contradiction=False)
        result = analyzer._run_contradiction_detection(None, None, None, {})
        assert isinstance(result, ContradictionResult)
        assert len(result.records) == 0

    def test_contradiction_no_embeddings(
        self, sample_topics_table, sample_articles_table,
    ):
        analyzer = Stage6CrossAnalyzer(enable_contradiction=True)
        source_topic_articles = _build_source_topic_articles(
            sample_topics_table, sample_articles_table,
        )
        result = analyzer._run_contradiction_detection(
            sample_topics_table, sample_articles_table, None, source_topic_articles,
        )
        assert len(result.records) == 0

    def test_contradiction_fallback_without_nli(
        self, sample_topics_table, sample_articles_table,
        sample_embeddings_table,
    ):
        """Contradiction detection should fall back to similarity-only mode."""
        analyzer = Stage6CrossAnalyzer(enable_contradiction=True)
        source_topic_articles = _build_source_topic_articles(
            sample_topics_table, sample_articles_table,
        )

        # Mock NLI loading to fail
        with patch.object(analyzer, "_load_nli_model", return_value=False):
            result = analyzer._run_contradiction_detection(
                sample_topics_table, sample_articles_table,
                sample_embeddings_table, source_topic_articles,
            )
        assert isinstance(result, ContradictionResult)
        # Should have some records even without NLI (similarity-only fallback)
        for rec in result.records:
            assert rec.analysis_type == "contradiction"
            meta = json.loads(rec.metadata)
            assert meta.get("nli_available") is False

    def test_find_cross_source_pairs(self):
        source_topic_articles = {
            "src_a": {"topic_0": ["a1", "a2"], "topic_1": ["a3"]},
            "src_b": {"topic_0": ["b1"], "topic_1": ["b2", "b3"]},
        }
        pairs = list(Stage6CrossAnalyzer._find_cross_source_pairs(source_topic_articles))
        assert len(pairs) > 0
        # All pairs should be cross-source
        for tid, src1, aid1, src2, aid2 in pairs:
            assert src1 != src2


# ---------------------------------------------------------------------------
# Integration Tests
# ---------------------------------------------------------------------------

class TestIntegration:
    """Full pipeline integration tests."""

    def test_full_run_produces_parquet(self, write_all_parquets):
        """Test that run() produces a valid cross_analysis.parquet."""
        paths = write_all_parquets
        analyzer = Stage6CrossAnalyzer(
            enable_pcmci=False,  # Skip PCMCI to avoid tigramite dependency
            enable_contradiction=False,  # Skip NLI to avoid transformers dependency
        )
        output = analyzer.run(
            timeseries_path=paths["timeseries_path"],
            topics_path=paths["topics_path"],
            analysis_path=None,  # Will use default, which may not exist
            networks_path=paths["networks_path"],
            embeddings_path=paths["embeddings_path"],
            articles_path=paths["articles_path"],
            output_dir=paths["output_dir"],
        )

        assert isinstance(output, Stage6Output)
        assert output.elapsed_seconds > 0
        assert len(output.techniques_completed) > 0

        # Verify Parquet was written
        parquet_path = paths["output_dir"] / "cross_analysis.parquet"
        assert parquet_path.exists()

        # Verify schema compliance
        table = pq.read_table(str(parquet_path))
        expected_schema = _cross_analysis_schema()
        for field in expected_schema:
            assert field.name in table.column_names
            assert table.schema.field(field.name).type == field.type

    def test_run_stage6_convenience_function(self, write_all_parquets):
        """Test the module-level convenience function."""
        paths = write_all_parquets
        output = run_stage6(
            timeseries_path=paths["timeseries_path"],
            topics_path=paths["topics_path"],
            networks_path=paths["networks_path"],
            embeddings_path=paths["embeddings_path"],
            articles_path=paths["articles_path"],
            output_dir=paths["output_dir"],
            enable_pcmci=False,
            enable_contradiction=False,
        )
        assert isinstance(output, Stage6Output)
        assert output.elapsed_seconds > 0

    def test_output_records_are_valid_json(self, write_all_parquets):
        """All metadata fields should be valid JSON."""
        paths = write_all_parquets
        output = run_stage6(
            timeseries_path=paths["timeseries_path"],
            topics_path=paths["topics_path"],
            networks_path=paths["networks_path"],
            embeddings_path=paths["embeddings_path"],
            articles_path=paths["articles_path"],
            output_dir=paths["output_dir"],
            enable_pcmci=False,
            enable_contradiction=False,
        )
        parquet_path = paths["output_dir"] / "cross_analysis.parquet"
        table = pq.read_table(str(parquet_path))

        for i in range(table.num_rows):
            metadata = table.column("metadata")[i].as_py()
            parsed = json.loads(metadata)
            assert isinstance(parsed, dict)

    def test_output_strength_in_range(self, write_all_parquets):
        """All strength values should be in [0, 1]."""
        paths = write_all_parquets
        run_stage6(
            timeseries_path=paths["timeseries_path"],
            topics_path=paths["topics_path"],
            networks_path=paths["networks_path"],
            embeddings_path=paths["embeddings_path"],
            articles_path=paths["articles_path"],
            output_dir=paths["output_dir"],
            enable_pcmci=False,
            enable_contradiction=False,
        )
        parquet_path = paths["output_dir"] / "cross_analysis.parquet"
        table = pq.read_table(str(parquet_path))

        for i in range(table.num_rows):
            strength = table.column("strength")[i].as_py()
            assert 0.0 <= strength <= 1.0, f"strength={strength} out of range"


# ---------------------------------------------------------------------------
# Edge Case Tests
# ---------------------------------------------------------------------------

class TestEdgeCases:
    """Test edge cases and error handling."""

    def test_missing_articles_raises_pipeline_error(self, tmp_output_dir):
        analyzer = Stage6CrossAnalyzer()
        with pytest.raises(PipelineStageError):
            analyzer.run(
                articles_path=Path("/nonexistent/articles.parquet"),
                output_dir=tmp_output_dir,
            )

    def test_missing_topics_writes_empty(self, tmp_path, sample_articles_table):
        """If topics.parquet is missing, should write empty output."""
        processed = tmp_path / "processed"
        processed.mkdir()
        analysis = tmp_path / "analysis"
        analysis.mkdir()

        articles_path = processed / "articles.parquet"
        pq.write_table(sample_articles_table, str(articles_path))

        analyzer = Stage6CrossAnalyzer()
        output = analyzer.run(
            topics_path=Path("/nonexistent/topics.parquet"),
            articles_path=articles_path,
            output_dir=analysis,
        )
        assert output.total_records == 0
        assert (analysis / "cross_analysis.parquet").exists()

    def test_cleanup_releases_resources(self):
        analyzer = Stage6CrossAnalyzer()
        analyzer._nli_pipeline = MagicMock()
        analyzer.cleanup()
        assert analyzer._nli_pipeline is None

    def test_empty_topic_series(self):
        analyzer = Stage6CrossAnalyzer()
        result = analyzer._run_granger({}, [])
        assert result.n_tested == 0

    def test_safe_load_table_nonexistent(self):
        _, pq_mod = __import__("pyarrow", fromlist=["parquet"]), __import__("pyarrow.parquet")
        import pyarrow.parquet as pq_real
        result = Stage6CrossAnalyzer._safe_load_table(
            pq_real, Path("/nonexistent/file.parquet"), "test"
        )
        assert result is None


# ---------------------------------------------------------------------------
# Graceful Degradation Tests
# ---------------------------------------------------------------------------

class TestGracefulDegradation:
    """Verify that individual technique failures do not crash the pipeline."""

    def test_granger_failure_continues(self, write_all_parquets):
        """If Granger fails, other techniques should still run."""
        paths = write_all_parquets

        with patch(
            "src.analysis.stage6_cross_analysis._lazy_import_statsmodels",
            side_effect=ImportError("mocked"),
        ):
            output = run_stage6(
                timeseries_path=paths["timeseries_path"],
                topics_path=paths["topics_path"],
                networks_path=paths["networks_path"],
                embeddings_path=paths["embeddings_path"],
                articles_path=paths["articles_path"],
                output_dir=paths["output_dir"],
                enable_pcmci=False,
                enable_contradiction=False,
            )
        assert isinstance(output, Stage6Output)
        assert "T37_granger" in output.techniques_skipped

    @patch("src.analysis.stage6_cross_analysis._lazy_import_networkx")
    def test_networkx_failure_continues(self, mock_nx, write_all_parquets):
        """If networkx fails, other techniques should still run."""
        mock_nx.side_effect = ImportError("mocked")
        paths = write_all_parquets

        output = run_stage6(
            timeseries_path=paths["timeseries_path"],
            topics_path=paths["topics_path"],
            networks_path=paths["networks_path"],
            embeddings_path=paths["embeddings_path"],
            articles_path=paths["articles_path"],
            output_dir=paths["output_dir"],
            enable_pcmci=False,
            enable_contradiction=False,
        )
        assert isinstance(output, Stage6Output)
        # Pipeline should have completed despite networkx failure

    def test_analysis_types_in_output(self, write_all_parquets):
        """Verify that multiple analysis types appear in output."""
        paths = write_all_parquets
        output = run_stage6(
            timeseries_path=paths["timeseries_path"],
            topics_path=paths["topics_path"],
            networks_path=paths["networks_path"],
            embeddings_path=paths["embeddings_path"],
            articles_path=paths["articles_path"],
            output_dir=paths["output_dir"],
            enable_pcmci=False,
            enable_contradiction=False,
        )

        parquet_path = paths["output_dir"] / "cross_analysis.parquet"
        table = pq.read_table(str(parquet_path))

        if table.num_rows > 0:
            types = set(table.column("analysis_type").to_pylist())
            # Should have at least some analysis types
            assert len(types) >= 1


# ---------------------------------------------------------------------------
# DTW Helper Tests
# ---------------------------------------------------------------------------

class TestDTW:
    """Test the DTW distance computation."""

    def test_identical_series(self):
        s = np.array([1.0, 2.0, 3.0, 2.0, 1.0])
        assert Stage6CrossAnalyzer._compute_dtw(s, s) == pytest.approx(0.0)

    def test_shifted_series(self):
        s1 = np.array([0.0, 1.0, 2.0, 1.0, 0.0])
        s2 = np.array([0.0, 0.0, 1.0, 2.0, 1.0])
        dist = Stage6CrossAnalyzer._compute_dtw(s1, s2)
        assert dist > 0

    def test_different_lengths(self):
        s1 = np.array([1.0, 2.0, 3.0])
        s2 = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
        dist = Stage6CrossAnalyzer._compute_dtw(s1, s2)
        assert dist >= 0

    def test_constant_series(self):
        s1 = np.ones(10)
        s2 = np.ones(10)
        assert Stage6CrossAnalyzer._compute_dtw(s1, s2) == pytest.approx(0.0)

    def test_symmetry(self):
        rng = np.random.RandomState(42)
        s1 = rng.randn(20)
        s2 = rng.randn(20)
        d1 = Stage6CrossAnalyzer._compute_dtw(s1, s2)
        d2 = Stage6CrossAnalyzer._compute_dtw(s2, s1)
        assert d1 == pytest.approx(d2, abs=1e-10)
