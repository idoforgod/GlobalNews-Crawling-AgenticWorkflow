"""Shared constants for the GlobalNews Crawling & Analysis System.

All constants are centralized here to avoid magic numbers scattered
across modules. Changes to any constant propagate automatically.

Reference: Step 5 Architecture Blueprint, Section 3 + 5c + 5d.
"""

from pathlib import Path

# =============================================================================
# Project Paths
# =============================================================================

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# Data directories
DATA_DIR = PROJECT_ROOT / "data"
DATA_RAW_DIR = DATA_DIR / "raw"
DATA_PROCESSED_DIR = DATA_DIR / "processed"
DATA_FEATURES_DIR = DATA_DIR / "features"
DATA_ANALYSIS_DIR = DATA_DIR / "analysis"
DATA_OUTPUT_DIR = DATA_DIR / "output"
DATA_MODELS_DIR = DATA_DIR / "models"
DATA_LOGS_DIR = DATA_DIR / "logs"
DATA_CONFIG_DIR = DATA_DIR / "config"

# Config files
SOURCES_YAML_PATH = DATA_CONFIG_DIR / "sources.yaml"
PIPELINE_YAML_PATH = DATA_CONFIG_DIR / "pipeline.yaml"

# Log files
CRAWL_LOG_PATH = DATA_LOGS_DIR / "crawl.log"
ANALYSIS_LOG_PATH = DATA_LOGS_DIR / "analysis.log"
ERROR_LOG_PATH = DATA_LOGS_DIR / "errors.log"

# Output files
ANALYSIS_PARQUET_PATH = DATA_OUTPUT_DIR / "analysis.parquet"
SIGNALS_PARQUET_PATH = DATA_OUTPUT_DIR / "signals.parquet"
SQLITE_INDEX_PATH = DATA_OUTPUT_DIR / "index.sqlite"
DEDUP_SQLITE_PATH = DATA_DIR / "dedup.sqlite"
RUN_METADATA_PATH = DATA_OUTPUT_DIR / "run_metadata.json"

# Intermediate Parquet files
ARTICLES_PARQUET_PATH = DATA_PROCESSED_DIR / "articles.parquet"
EMBEDDINGS_PARQUET_PATH = DATA_FEATURES_DIR / "embeddings.parquet"
TFIDF_PARQUET_PATH = DATA_FEATURES_DIR / "tfidf.parquet"
NER_PARQUET_PATH = DATA_FEATURES_DIR / "ner.parquet"
ARTICLE_ANALYSIS_PARQUET_PATH = DATA_ANALYSIS_DIR / "article_analysis.parquet"
TOPICS_PARQUET_PATH = DATA_ANALYSIS_DIR / "topics.parquet"
NETWORKS_PARQUET_PATH = DATA_ANALYSIS_DIR / "networks.parquet"
TIMESERIES_PARQUET_PATH = DATA_ANALYSIS_DIR / "timeseries.parquet"
CROSS_ANALYSIS_PARQUET_PATH = DATA_ANALYSIS_DIR / "cross_analysis.parquet"

# =============================================================================
# Retry & Backoff Constants
# =============================================================================

MAX_RETRIES = 5
BACKOFF_FACTOR = 2.0
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_MAX_SECONDS = 60.0
RETRY_STATUS_CODES = {429, 500, 502, 503, 504}

# =============================================================================
# Circuit Breaker Constants
# =============================================================================

CIRCUIT_BREAKER_FAILURE_THRESHOLD = 5
CIRCUIT_BREAKER_RECOVERY_TIMEOUT_SECONDS = 300
CIRCUIT_BREAKER_HALF_OPEN_MAX_CALLS = 3

# =============================================================================
# Rate Limiting
# =============================================================================

DEFAULT_RATE_LIMIT_SECONDS = 5
MIN_RATE_LIMIT_SECONDS = 1
MAX_RATE_LIMIT_SECONDS = 120

# =============================================================================
# Crawling Defaults
# =============================================================================

DEFAULT_REQUEST_TIMEOUT_SECONDS = 30
PLAYWRIGHT_TIMEOUT_MS = 30000
PLAYWRIGHT_NAVIGATION_TIMEOUT_MS = 60000
MAX_ARTICLES_PER_SITE_PER_DAY = 5000
MAX_CONCURRENT_CRAWL_GROUPS = 6

# Absolute rule: only collect articles published within the last 24 hours
# from the crawl start time. This system is designed for daily execution.
CRAWL_LOOKBACK_HOURS = 24

# ── Crawling Absolute Principle (크롤링 절대 원칙) ──
# NEVER abandon a crawl target. When a site blocks, firewalls, or defends
# against our crawling, the system MUST find a way through — escalating
# anti-block strategies in real-time, bypassing or overcoming defenses.
# This is the supreme directive above all other crawling considerations.
# When circuit breaker opens, force immediate re-probe with maximum
# escalation instead of skipping the site entirely.
CRAWL_NEVER_ABANDON = True

# Maximum bypass discovery attempts per feed URL when normal discovery is blocked.
DISCOVERY_BYPASS_MAX_ATTEMPTS = 5

# Maximum extra full-site passes after L4 restarts exhaust (outer multi-pass loop).
# Independent from per-site NEVER_ABANDON_MAX_CYCLES in retry_manager.py.
MULTI_PASS_MAX_EXTRA = 10

# Bypass state persistence — cross-crawl learning SOT
BYPASS_STATE_PATH = DATA_CONFIG_DIR / "bypass_state.json"

# User-Agent pool sizes by tier
UA_TIER_SIZES = {
    1: 1,
    2: 10,
    3: 50,
    4: 0,  # Dynamic (Patchright fingerprints)
}

# =============================================================================
# Analysis Pipeline Constants
# =============================================================================

DEFAULT_BATCH_SIZE = 500
SBERT_BATCH_SIZE = 64  # Step 2 R4: optimized for M2 Pro 16GB
SBERT_EMBEDDING_DIM = 384
TFIDF_MAX_FEATURES = 10000
TFIDF_NGRAM_RANGE = (1, 2)
NER_BATCH_SIZE = 32
KEYBERT_TOP_N = 10

# Memory management
MAX_MEMORY_GB = 100.0  # Hard limit — 128GB host, generous budget for full NLP pipeline
GC_BETWEEN_STAGES = True

# Pipeline stage timeout defaults (seconds)
STAGE_TIMEOUTS = {
    "stage_1_preprocessing": 1800,
    "stage_2_features": 3600,
    "stage_3_article": 3600,
    "stage_4_aggregation": 3600,
    "stage_5_timeseries": 1800,
    "stage_6_cross": 3600,
    "stage_7_signals": 1800,
    "stage_8_output": 1800,
}

# Minimum articles required for certain analyses
MIN_ARTICLES_FOR_TOPICS = 50
MIN_ARTICLES_FOR_GRANGER = 100
MIN_DAYS_FOR_ANALYSIS = 7
FORECAST_HORIZON_DAYS = 30

# =============================================================================
# Signal Classification Thresholds
# =============================================================================

SIGNAL_CONFIDENCE_THRESHOLD = 0.5
SINGULARITY_THRESHOLD = 0.65

SINGULARITY_WEIGHTS = {
    "w1_ood": 0.20,
    "w2_changepoint": 0.15,
    "w3_cross_domain": 0.20,
    "w4_bertrend": 0.15,
    "w5_entropy": 0.10,
    "w6_novelty": 0.10,
    "w7_network": 0.10,
}

# 5-Layer thresholds
L1_VOLUME_ZSCORE_THRESHOLD = 3.0
L1_BURST_SCORE_THRESHOLD = 2.0
L2_SUSTAINED_DAYS_THRESHOLD = 7
L3_CHANGEPOINT_SIGNIFICANCE_THRESHOLD = 0.8
L3_MODULARITY_DELTA_THRESHOLD = 0.1
L4_EMBEDDING_DRIFT_THRESHOLD = 0.3
L4_WAVELET_PERIOD_THRESHOLD = 90
L5_NOVELTY_THRESHOLD = 0.7
L5_CROSS_DOMAIN_THRESHOLD = 0.3

# =============================================================================
# Parquet & Storage Constants
# =============================================================================

PARQUET_COMPRESSION = "zstd"
PARQUET_COMPRESSION_LEVEL = 3
SQLITE_FTS_TOKENIZER = "unicode61"

# Schema column counts (for validation)
ARTICLES_SCHEMA_COLUMNS = 12
ANALYSIS_SCHEMA_COLUMNS = 21
SIGNALS_SCHEMA_COLUMNS = 12

# =============================================================================
# NLP Model Names
# =============================================================================

SBERT_MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2"
KIWI_VERSION = "0.22.2"
SPACY_MODEL_NAME = "en_core_web_sm"
KOBERT_MODEL_NAME = "monologg/kobert"
KCELECTRA_MODEL_NAME = "monologg/koelectra-base-finetuned-naver-ner"
BART_MNLI_MODEL_NAME = "facebook/bart-large-mnli"
NER_MULTILINGUAL_MODEL_NAME = "Davlan/xlm-roberta-base-ner-hrl"

# =============================================================================
# Crawling Group Definitions
# =============================================================================

CRAWL_GROUPS = {
    "A": "Korean Major Dailies",
    "B": "Korean Economy",
    "C": "Korean Niche",
    "D": "Korean IT/Science",
    "E": "English-Language Western",
    "F": "Asia-Pacific",
    "G": "Europe/Middle East",
    "H": "Africa",
    "I": "Latin America",
    "J": "Russia/Central Asia",
}

# Valid values for configuration validation
# Region codes are validated case-insensitively (config_loader normalizes to lower)
VALID_REGIONS = {
    "kr", "us", "uk", "gb", "cn", "jp", "de", "fr", "me", "in", "tw",
    "il", "ru", "mx", "sg",
    # Groups H-J regions (Africa, Latin America, Russia/Central Asia, etc.)
    "af", "ar", "br", "cl", "co", "cz", "es", "eu", "fi", "id", "is",
    "it", "jo", "mn", "no", "pe", "ph", "pl", "se", "vn",
}
VALID_LANGUAGES = {
    "ko", "en", "zh", "ja", "de", "fr", "es", "ar", "he",
    # Groups H-J languages
    "it", "pt", "pl", "cs", "sv", "no", "mn", "ru", "hi",
}
VALID_GROUPS = {"A", "B", "C", "D", "E", "F", "G", "H", "I", "J"}
VALID_CRAWL_METHODS = {"rss", "rss_content", "sitemap", "api", "playwright", "dom"}
VALID_PAYWALL_TYPES = {"none", "soft-metered", "hard"}
VALID_DIFFICULTY_TIERS = {"Easy", "Medium", "Hard", "Extreme"}
VALID_BOT_BLOCK_LEVELS = {"LOW", "MEDIUM", "HIGH"}
VALID_PARQUET_COMPRESSIONS = {"zstd", "snappy", "lz4", "none"}

# =============================================================================
# Source Configuration Defaults
# =============================================================================

# D-7 (13): meta.enabled default — opt-out pattern (sites enabled by default).
# Cross-ref: config_loader.py _SOURCE_DEFAULTS["meta"]["enabled"],
#            config_loader.py get_enabled_sites(),
#            pipeline.py _resolve_target_sites() + _run_single_pass() (3 locations),
#            crawler.py crawl_site(),
#            main.py status reporting,
#            preflight_check.py enabled_count (hardcoded — standalone, can't import src)
# Change this value → 5 consumers auto-sync via import, 1 AST-validated by
# scripts/validate_enabled_default_sync.py (ED1-ED7 + ED-CROSS).
ENABLED_DEFAULT = True
