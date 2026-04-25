"""Stage 3: Per-Article Analysis -- sentiment, emotion, STEEPS, importance.

Implements 6 analysis techniques per article:
    T13: Sentiment Analysis (Korean) -- KoBERT
    T14: Sentiment Analysis (English) -- cardiffnlp/twitter-roberta-base-sentiment-latest
    T15: 8-Dimension Emotion (Plutchik) -- Zero-shot BART-MNLI + KcELECTRA fallback
    T16: Zero-Shot STEEPS Classification -- facebook/bart-large-mnli
    T18: Social Mood Index -- Aggregation formula
    T19: Emotion Trajectory -- Rolling 7-day delta
    --:  Importance Scoring -- Composite formula

Input:
    data/processed/articles.parquet (ARTICLES_SCHEMA)
    data/features/embeddings.parquet
    data/features/ner.parquet

Output:
    data/analysis/article_analysis.parquet (13 columns)
    data/analysis/mood_trajectory.parquet (mood index + emotion trajectory)

Memory Budget:
    Peak ~1.8 GB (KoBERT ~500 MB + BART-MNLI ~500 MB + processing overhead)
    Models loaded sequentially, shared BART-MNLI for emotion/STEEPS.

Performance Target:
    1,000 articles in ~8.0 min

Reference: Step 7 Pipeline Design, Section 3.3.
"""

from __future__ import annotations

import gc
import logging
import math
import os
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import numpy as np

from src.config.constants import (
    ARTICLES_PARQUET_PATH,
    ARTICLE_ANALYSIS_PARQUET_PATH,
    BART_MNLI_MODEL_NAME,
    DATA_ANALYSIS_DIR,
    EMBEDDINGS_PARQUET_PATH,
    KOBERT_MODEL_NAME,
    MULTILINGUAL_SENTIMENT_MODEL_NAME,
    NER_PARQUET_PATH,
    PARQUET_COMPRESSION,
    PARQUET_COMPRESSION_LEVEL,
)
from src.utils.error_handler import (
    AnalysisError,
    ModelLoadError,
    PipelineStageError,
)
from src.utils.logging_config import get_analysis_logger

logger = get_analysis_logger()


# =============================================================================
# Constants
# =============================================================================

# Sentiment model for English articles
EN_SENTIMENT_MODEL_NAME = "cardiffnlp/twitter-roberta-base-sentiment-latest"

# Plutchik 8 basic emotions
PLUTCHIK_EMOTIONS = [
    "joy", "trust", "fear", "surprise",
    "sadness", "disgust", "anger", "anticipation",
]

# STEEPS candidate labels for zero-shot classification
STEEPS_LABELS = [
    "Social issue",
    "Technology development",
    "Economic trend",
    "Environmental concern",
    "Political event",
    "Security threat",
]

# STEEPS label -> code mapping
STEEPS_CODE_MAP = {
    "Social issue": "S",
    "Technology development": "T",
    "Economic trend": "E",
    "Environmental concern": "En",
    "Political event": "P",
    "Security threat": "Se",
}

# Source authority tiers (configurable via sources.yaml; these are fallback
# defaults based on site reconnaissance anti_block_tier as proxy for size/authority)
# Higher anti_block_tier often correlates with larger, more authoritative sources.
# Scale: 0-100; used as one component of importance_score.
DEFAULT_SOURCE_AUTHORITY = {
    # Korean Major Dailies (Group A) -- high authority
    "chosun.com": 90, "joongang.co.kr": 88, "donga.com": 85,
    "hani.co.kr": 82, "yna.co.kr": 95,  # Wire service = highest
    "mk.co.kr": 78, "hankyung.com": 80, "fnnews.com": 72,
    "mt.co.kr": 70, "nocutnews.co.kr": 68, "kmib.co.kr": 75,
    # Korean Tech/Niche (Group B/C/D) -- moderate authority
    "zdnet.co.kr": 65, "bloter.net": 55, "etnews.com": 68,
    "ohmynews.com": 60, "sciencetimes.co.kr": 62, "irobotnews.com": 50,
    "techneedle.com": 48, "north38.com": 55,
    # English-Language Western (Group E) -- high authority
    "reuters.com": 95, "apnews.com": 93, "wsj.com": 92,
    "nytimes.com": 95, "washingtonpost.com": 90, "ft.com": 92,
    "economist.com": 90, "bbc.com": 93, "theguardian.com": 88,
    "cnn.com": 85, "aljazeera.com": 82, "bloomberg.com": 92,
    # Asia-Pacific (Group F) -- moderate-high authority
    "nhk.or.jp": 88, "asahi.com": 85, "nikkei.com": 88,
    "xinhuanet.com": 80, "scmp.com": 82, "caixin.com": 75,
    # Europe/Middle East (Group G) -- moderate-high authority
    "aljazeera.net": 78, "alarabiya.net": 72, "lemonde.fr": 85,
    "spiegel.de": 82, "elpais.com": 78, "tass.com": 65,
    "afp.com": 90,
}

# Importance score weights (Step 7 formula)
IMPORTANCE_WEIGHTS = {
    "authority": 0.25,
    "entity_density": 0.20,
    "coverage": 0.25,
    "recency": 0.15,
    "extremity": 0.15,
}

# Paywall penalty for importance score
PAYWALL_PENALTY = -20

# Batch size for transformer inference (reduced from 16 to 4 for 16GB memory)
TRANSFORMER_BATCH_SIZE = 4

# Maximum text length for transformer input (in characters)
MAX_TEXT_LENGTH = 512

# Mood trajectory output path
MOOD_TRAJECTORY_PARQUET_PATH = DATA_ANALYSIS_DIR / "mood_trajectory.parquet"


# =============================================================================
# Lazy Import Helpers
# =============================================================================

def _lazy_import_pyarrow():
    """Lazy import pyarrow to avoid startup overhead."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    return pa, pq


def _lazy_import_torch():
    """Lazy import torch."""
    import torch
    return torch


def _lazy_import_transformers():
    """Lazy import transformers pipeline."""
    from transformers import pipeline as hf_pipeline
    return hf_pipeline


def _lazy_import_pandas():
    """Lazy import pandas."""
    import pandas as pd
    return pd


def _detect_device() -> int | str:
    """Detect best available device for HuggingFace pipeline.

    Returns:
        -1 for CPU, 0 for CUDA, or "mps" for Apple Silicon GPU.
    """
    try:
        torch = _lazy_import_torch()
        if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            # Verify MPS works with a small tensor operation
            try:
                t = torch.tensor([1.0], device="mps")
                _ = t + t
                logger.info("device_detected", device="mps")
                return "mps"  # type: ignore[return-value]
            except Exception:
                pass
        if torch.cuda.is_available():
            logger.info("device_detected", device="cuda:0")
            return 0
    except ImportError:
        pass
    logger.info("device_detected", device="cpu")
    return -1


# =============================================================================
# VADER Fallback (rule-based sentiment for English)
# =============================================================================

class _VaderFallback:
    """Lightweight VADER-based sentiment fallback for English articles.

    Used when the transformer sentiment model fails to load.
    Wraps nltk.sentiment.vader.SentimentIntensityAnalyzer.
    """

    def __init__(self) -> None:
        self._analyzer = None

    def _ensure_loaded(self) -> bool:
        """Attempt to load VADER. Returns True if available."""
        if self._analyzer is not None:
            return True
        try:
            import nltk
            try:
                nltk.data.find("sentiment/vader_lexicon.zip")
            except LookupError:
                nltk.download("vader_lexicon", quiet=True)
            from nltk.sentiment.vader import SentimentIntensityAnalyzer
            self._analyzer = SentimentIntensityAnalyzer()
            return True
        except Exception as e:
            logger.warning("vader_fallback_unavailable", error=str(e))
            return False

    def analyze(self, text: str) -> tuple[str, float]:
        """Return (label, score) using VADER compound score.

        Returns:
            Tuple of (sentiment_label, sentiment_score).
            sentiment_score is in [-1.0, 1.0].
        """
        if not self._ensure_loaded():
            return ("neutral", 0.0)
        scores = self._analyzer.polarity_scores(text)
        compound = scores["compound"]
        if compound >= 0.05:
            label = "positive"
        elif compound <= -0.05:
            label = "negative"
        else:
            label = "neutral"
        return (label, compound)


class _KoreanLexiconFallback:
    """Simple Korean sentiment lexicon fallback.

    A minimal rule-based approach using positive/negative word counts.
    This is deliberately simple; production Korean sentiment should use KoBERT.
    """

    # Common Korean sentiment indicator words (subset)
    _POSITIVE_WORDS = frozenset([
        "좋", "긍정", "성장", "증가", "호조", "상승", "개선", "발전",
        "확대", "활발", "호황", "강화", "기대", "성과", "혁신", "돌파",
        "회복", "안정", "협력", "합의", "지원", "투자", "성공",
    ])
    _NEGATIVE_WORDS = frozenset([
        "나쁘", "부정", "하락", "감소", "위기", "악화", "축소", "침체",
        "불안", "갈등", "위험", "논란", "비판", "실패", "손실", "폭락",
        "불황", "긴장", "제재", "처벌", "사망", "피해", "충돌",
    ])

    def analyze(self, text: str) -> tuple[str, float]:
        """Return (label, score) using keyword counting.

        Returns:
            Tuple of (sentiment_label, sentiment_score in [-1.0, 1.0]).
        """
        pos_count = sum(1 for w in self._POSITIVE_WORDS if w in text)
        neg_count = sum(1 for w in self._NEGATIVE_WORDS if w in text)
        total = pos_count + neg_count
        if total == 0:
            return ("neutral", 0.0)
        score = (pos_count - neg_count) / total
        if score > 0.1:
            label = "positive"
        elif score < -0.1:
            label = "negative"
        else:
            label = "neutral"
        return (label, max(-1.0, min(1.0, score)))


# =============================================================================
# Stage 3 Analyzer
# =============================================================================

class Stage3ArticleAnalyzer:
    """Per-article analysis: sentiment, emotion, STEEPS, importance.

    Loads transformer models on demand, processes articles in batches,
    and writes results to Parquet. Supports graceful degradation to
    rule-based fallbacks when transformer models are unavailable.

    Attributes:
        articles_path: Path to the input articles Parquet file.
        features_dir: Path to the features directory.
        output_path: Path for the output article_analysis Parquet file.
        source_authority: Domain -> authority score mapping (0-100).
    """

    def __init__(
        self,
        articles_path: Path | None = None,
        features_dir: Path | None = None,
        output_path: Path | None = None,
        source_authority: dict[str, int] | None = None,
    ) -> None:
        self.articles_path = articles_path or ARTICLES_PARQUET_PATH
        self.features_dir = features_dir or EMBEDDINGS_PARQUET_PATH.parent
        self.output_path = output_path or ARTICLE_ANALYSIS_PARQUET_PATH
        self.source_authority = source_authority or dict(DEFAULT_SOURCE_AUTHORITY)

        # Model references (lazy loaded)
        self._en_sentiment_pipeline: Any = None
        self._ko_sentiment_pipeline: Any = None
        self._multilingual_sentiment_pipeline: Any = None
        self._zeroshot_pipeline: Any = None

        # Fallback analyzers
        self._vader_fallback = _VaderFallback()
        self._korean_fallback = _KoreanLexiconFallback()

        # Flags to track model availability
        self._en_sentiment_available = True
        self._ko_sentiment_available = True
        self._multilingual_sentiment_available = True
        self._zeroshot_available = True

        # Track memory for reporting
        self._memory_log: list[dict[str, Any]] = []

        # Device detection (CPU / MPS / CUDA) — cached for all model loads
        self._device = _detect_device()

    # -----------------------------------------------------------------
    # Model Loading
    # -----------------------------------------------------------------

    def _log_memory(self, label: str) -> None:
        """Log current memory usage."""
        try:
            import psutil
            proc = psutil.Process(os.getpid())
            rss_gb = proc.memory_info().rss / (1024 ** 3)
            self._memory_log.append({"label": label, "rss_gb": round(rss_gb, 3)})
            logger.info("memory_snapshot", label=label, rss_gb=round(rss_gb, 3))
        except ImportError:
            pass

    def _load_en_sentiment(self) -> None:
        """Load English sentiment model (cardiffnlp/twitter-roberta-base-sentiment-latest).

        Falls back to VADER if the transformer model cannot be loaded.
        """
        if self._en_sentiment_pipeline is not None:
            return
        try:
            hf_pipeline = _lazy_import_transformers()
            logger.info("loading_model", model=EN_SENTIMENT_MODEL_NAME)
            self._en_sentiment_pipeline = hf_pipeline(
                "sentiment-analysis",
                model=EN_SENTIMENT_MODEL_NAME,
                tokenizer=EN_SENTIMENT_MODEL_NAME,
                max_length=MAX_TEXT_LENGTH,
                truncation=True,
                device=self._device,
            )
            self._log_memory("en_sentiment_loaded")
        except Exception as e:
            logger.warning(
                "en_sentiment_load_failed",
                model=EN_SENTIMENT_MODEL_NAME,
                error=str(e),
                fallback="VADER",
            )
            self._en_sentiment_available = False

    def _load_ko_sentiment(self) -> None:
        """Load Korean sentiment model (KoBERT-based).

        Falls back to lexicon-based sentiment if the model cannot be loaded.
        The monologg/kobert model requires special tokenizer handling.
        """
        if self._ko_sentiment_pipeline is not None:
            return
        try:
            hf_pipeline = _lazy_import_transformers()
            # Try loading KoBERT for sentiment. The monologg/kobert model
            # is a BERT-base model fine-tuned for Korean; we use it as a
            # text-classification pipeline. If this specific model is not
            # available, try a multilingual fallback.
            logger.info("loading_model", model=KOBERT_MODEL_NAME)
            try:
                self._ko_sentiment_pipeline = hf_pipeline(
                    "sentiment-analysis",
                    model=KOBERT_MODEL_NAME,
                    tokenizer=KOBERT_MODEL_NAME,
                    trust_remote_code=True,
                    max_length=MAX_TEXT_LENGTH,
                    truncation=True,
                    device=self._device,
                )
            except Exception:
                # Fallback: try nlptown multilingual sentiment model
                fallback_model = "nlptown/bert-base-multilingual-uncased-sentiment"
                logger.info(
                    "ko_sentiment_fallback",
                    primary=KOBERT_MODEL_NAME,
                    fallback=fallback_model,
                )
                self._ko_sentiment_pipeline = hf_pipeline(
                    "sentiment-analysis",
                    model=fallback_model,
                    tokenizer=fallback_model,
                    max_length=MAX_TEXT_LENGTH,
                    truncation=True,
                    device=self._device,
                )
            self._log_memory("ko_sentiment_loaded")
        except Exception as e:
            logger.warning(
                "ko_sentiment_load_failed",
                model=KOBERT_MODEL_NAME,
                error=str(e),
                fallback="korean_lexicon",
            )
            self._ko_sentiment_available = False

    def _load_multilingual_sentiment(self) -> None:
        """Load multilingual sentiment model for non-ko/non-en languages.

        Uses cardiffnlp/twitter-xlm-roberta-base-sentiment-multilingual
        which covers 8 languages natively (ar, en, fr, de, hi, it, pt, es)
        and provides cross-lingual transfer for others (ko, ja, ru, zh).
        Falls back to VADER (English-biased) if unavailable.
        """
        if self._multilingual_sentiment_pipeline is not None:
            return
        try:
            hf_pipeline = _lazy_import_transformers()
            logger.info("loading_model", model=MULTILINGUAL_SENTIMENT_MODEL_NAME)
            self._multilingual_sentiment_pipeline = hf_pipeline(
                "sentiment-analysis",
                model=MULTILINGUAL_SENTIMENT_MODEL_NAME,
                tokenizer=MULTILINGUAL_SENTIMENT_MODEL_NAME,
                max_length=MAX_TEXT_LENGTH,
                truncation=True,
                device=self._device,
            )
            self._log_memory("multilingual_sentiment_loaded")
        except Exception as e:
            logger.warning(
                "multilingual_sentiment_load_failed",
                model=MULTILINGUAL_SENTIMENT_MODEL_NAME,
                error=str(e),
                fallback="VADER",
            )
            self._multilingual_sentiment_available = False

    def _load_zeroshot(self) -> None:
        """Load facebook/bart-large-mnli for zero-shot classification.

        Shared across emotion and STEEPS extraction.
        Falls back to uniform defaults if unavailable.
        """
        if self._zeroshot_pipeline is not None:
            return
        try:
            hf_pipeline = _lazy_import_transformers()
            logger.info("loading_model", model=BART_MNLI_MODEL_NAME)
            self._zeroshot_pipeline = hf_pipeline(
                "zero-shot-classification",
                model=BART_MNLI_MODEL_NAME,
                device=self._device,
            )
            self._log_memory("zeroshot_loaded")
        except Exception as e:
            logger.warning(
                "zeroshot_load_failed",
                model=BART_MNLI_MODEL_NAME,
                error=str(e),
                fallback="defaults",
            )
            self._zeroshot_available = False

    def _unload_models(self) -> None:
        """Release all loaded models and reclaim memory."""
        del self._en_sentiment_pipeline
        del self._ko_sentiment_pipeline
        del self._multilingual_sentiment_pipeline
        del self._zeroshot_pipeline
        self._en_sentiment_pipeline = None
        self._ko_sentiment_pipeline = None
        self._multilingual_sentiment_pipeline = None
        self._zeroshot_pipeline = None
        gc.collect()
        try:
            torch = _lazy_import_torch()
            if hasattr(torch, "mps") and torch.backends.mps.is_available():
                torch.mps.empty_cache()
            elif torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass
        self._log_memory("models_unloaded")

    # -----------------------------------------------------------------
    # Sentiment Analysis (T13 Korean, T14 English)
    # -----------------------------------------------------------------

    def _analyze_sentiment_en(self, text: str) -> tuple[str, float]:
        """Analyze English text sentiment.

        The cardiffnlp model outputs labels: positive, negative, neutral.
        We map the raw score to [-1, 1] range.

        Args:
            text: English text to analyze.

        Returns:
            Tuple of (label, score) where score is in [-1.0, 1.0].
        """
        if not text or not text.strip():
            return ("neutral", 0.0)

        if self._en_sentiment_available and self._en_sentiment_pipeline is not None:
            try:
                truncated = text[:MAX_TEXT_LENGTH * 2]  # chars, tokenizer truncates
                result = self._en_sentiment_pipeline(truncated)[0]
                raw_label = result["label"].lower()
                raw_score = result["score"]

                # Map cardiffnlp output to our standard:
                # The model outputs LABEL_0 (negative), LABEL_1 (neutral),
                # LABEL_2 (positive) -- or plain labels depending on version.
                if "positive" in raw_label or raw_label == "label_2":
                    return ("positive", raw_score)
                elif "negative" in raw_label or raw_label == "label_0":
                    return ("negative", -raw_score)
                else:
                    return ("neutral", 0.0)
            except Exception as e:
                logger.warning("en_sentiment_inference_error", error=str(e))

        # VADER fallback
        return self._vader_fallback.analyze(text)

    def _analyze_sentiment_multilingual(self, text: str) -> tuple[str, float]:
        """Analyze non-Korean non-English text sentiment.

        Uses the XLM-RoBERTa multilingual sentiment model which supports
        8 languages natively and provides cross-lingual transfer for others.
        Falls back to VADER if model unavailable.

        Args:
            text: Non-English, non-Korean text to analyze.

        Returns:
            Tuple of (sentiment_label, sentiment_score).
        """
        if not text or not text.strip():
            return ("neutral", 0.0)

        if (
            self._multilingual_sentiment_available
            and self._multilingual_sentiment_pipeline is not None
        ):
            try:
                truncated = text[:MAX_TEXT_LENGTH * 2]
                result = self._multilingual_sentiment_pipeline(truncated)[0]
                raw_label = result["label"].lower()
                raw_score = result["score"]

                # Map XLM-R output labels to standard set.
                # Model outputs: positive, negative, neutral
                if "positive" in raw_label:
                    return ("positive", raw_score)
                elif "negative" in raw_label:
                    return ("negative", -raw_score)
                else:
                    return ("neutral", 0.0)
            except Exception as e:
                logger.warning("multilingual_sentiment_error", error=str(e))

        # mDeBERTa zero-shot fallback (news-domain capable, 100+ languages)
        # Preferred over VADER because: (1) truly multilingual, (2) not
        # trained on tweets/reviews, (3) NLI-based = understands context.
        if self._zeroshot_available and self._zeroshot_pipeline is not None:
            try:
                truncated = text[:MAX_TEXT_LENGTH]
                result = self._zeroshot_pipeline(
                    truncated,
                    candidate_labels=["positive", "negative", "neutral"],
                    hypothesis_template="The sentiment of this text is {}.",
                )
                top_label = result["labels"][0]
                top_score = result["scores"][0]
                if top_label == "positive":
                    return ("positive", top_score)
                elif top_label == "negative":
                    return ("negative", -top_score)
                else:
                    return ("neutral", 0.0)
            except Exception as e:
                logger.warning("zeroshot_sentiment_fallback_error", error=str(e))

        # VADER fallback (English-biased, last resort)
        return self._vader_fallback.analyze(text)

    def _analyze_sentiment_ko(self, text: str) -> tuple[str, float]:
        """Analyze Korean text sentiment.

        Args:
            text: Korean text to analyze.

        Returns:
            Tuple of (label, score) where score is in [-1.0, 1.0].
        """
        if not text or not text.strip():
            return ("neutral", 0.0)

        if self._ko_sentiment_available and self._ko_sentiment_pipeline is not None:
            try:
                truncated = text[:MAX_TEXT_LENGTH * 2]
                result = self._ko_sentiment_pipeline(truncated)[0]
                raw_label = result["label"].lower()
                raw_score = result["score"]

                # Handle various model output formats:
                # monologg/kobert: "positive"/"negative"
                # nlptown: "1 star".."5 stars"
                if "positive" in raw_label or "5 star" in raw_label or "4 star" in raw_label:
                    return ("positive", raw_score)
                elif "negative" in raw_label or "1 star" in raw_label or "2 star" in raw_label:
                    return ("negative", -raw_score)
                elif "3 star" in raw_label:
                    return ("neutral", 0.0)
                else:
                    # Assume binary model with thresholding
                    if raw_score >= 0.6:
                        return ("positive", raw_score)
                    elif raw_score <= 0.4:
                        return ("negative", -(1.0 - raw_score))
                    else:
                        return ("neutral", 0.0)
            except Exception as e:
                logger.warning("ko_sentiment_inference_error", error=str(e))

        # mDeBERTa zero-shot for Korean (better than lexicon for news)
        if self._zeroshot_available and self._zeroshot_pipeline is not None:
            try:
                truncated = text[:MAX_TEXT_LENGTH * 2]
                result = self._zeroshot_pipeline(
                    truncated,
                    candidate_labels=["긍정적", "부정적", "중립적"],
                    hypothesis_template="이 텍스트의 감성은 {}이다.",
                )
                label_map = {"긍정적": "positive", "부정적": "negative", "중립적": "neutral"}
                top_label = label_map.get(result["labels"][0], "neutral")
                top_score = result["scores"][0]
                if top_label == "positive":
                    return ("positive", top_score)
                elif top_label == "negative":
                    return ("negative", -top_score)
                return ("neutral", 0.0)
            except Exception:
                pass

        # Korean lexicon fallback (last resort)
        return self._korean_fallback.analyze(text)

    def _analyze_sentiment(
        self, title: str, body: str, language: str
    ) -> tuple[str, float]:
        """Dual-pass sentiment: title and body, returning final sentiment.

        Strategy (Step 7 Section 2.1):
            - Compute both title_sentiment and body_sentiment
            - Final = body_sentiment if body available, else title_sentiment

        Args:
            title: Article title.
            body: Article body text (empty for paywall-truncated).
            language: ISO 639-1 language code.

        Returns:
            Tuple of (sentiment_label, sentiment_score).
        """
        if language == "ko":
            analyze_fn = self._analyze_sentiment_ko
        elif language == "en":
            analyze_fn = self._analyze_sentiment_en
        else:
            # Non-ko/en: use multilingual model → VADER fallback
            analyze_fn = self._analyze_sentiment_multilingual

        # Always compute title sentiment as baseline
        title_label, title_score = analyze_fn(title)

        # If body is available, compute body sentiment (authoritative)
        if body and body.strip():
            body_label, body_score = analyze_fn(body)
            return (body_label, body_score)

        return (title_label, title_score)

    # -----------------------------------------------------------------
    # Emotion Classification (T15 -- Plutchik 8 emotions)
    # -----------------------------------------------------------------

    def _classify_emotions(self, text: str) -> dict[str, float]:
        """Classify text into Plutchik's 8 basic emotions via zero-shot.

        Args:
            text: Text to classify.

        Returns:
            Dict mapping emotion name to confidence score (0-1).
        """
        default_uniform = {e: 0.125 for e in PLUTCHIK_EMOTIONS}

        if not text or not text.strip():
            return default_uniform

        if self._zeroshot_available and self._zeroshot_pipeline is not None:
            try:
                truncated = text[:MAX_TEXT_LENGTH * 2]
                result = self._zeroshot_pipeline(
                    truncated,
                    candidate_labels=PLUTCHIK_EMOTIONS,
                    multi_label=True,
                )
                emotions = {}
                for label, score in zip(result["labels"], result["scores"]):
                    emotions[label] = float(score)

                # Sanity check: if all scores are near zero, use uniform
                if all(v < 0.05 for v in emotions.values()):
                    logger.debug("emotion_all_near_zero", text_preview=text[:80])
                    return default_uniform

                return emotions
            except Exception as e:
                logger.warning("emotion_classification_error", error=str(e))

        return default_uniform

    # -----------------------------------------------------------------
    # STEEPS Classification (T16)
    # -----------------------------------------------------------------

    def _classify_steeps(self, text: str, source: str = "") -> str:
        """Classify text into STEEPS category via zero-shot.

        Args:
            text: Text to classify.
            source: Source domain (used for fallback default).

        Returns:
            STEEPS code: "S", "T", "E", "En", "P", or "Se".
        """
        default_category = "E"  # Economic is most common for news

        if not text or not text.strip():
            return default_category

        if self._zeroshot_available and self._zeroshot_pipeline is not None:
            try:
                truncated = text[:MAX_TEXT_LENGTH * 2]
                result = self._zeroshot_pipeline(
                    truncated,
                    candidate_labels=STEEPS_LABELS,
                )
                top_label = result["labels"][0]
                return STEEPS_CODE_MAP.get(top_label, default_category)
            except Exception as e:
                logger.warning("steeps_classification_error", error=str(e))

        # Source-based fallback heuristic
        tech_sources = {
            "zdnet.co.kr", "bloter.net", "etnews.com", "techneedle.com",
            "irobotnews.com", "sciencetimes.co.kr",
        }
        finance_sources = {
            "mk.co.kr", "hankyung.com", "fnnews.com", "mt.co.kr",
            "wsj.com", "ft.com", "bloomberg.com", "nikkei.com", "caixin.com",
        }
        if source in tech_sources:
            return "T"
        if source in finance_sources:
            return "E"
        return default_category

    # -----------------------------------------------------------------
    # Importance Scoring
    # -----------------------------------------------------------------

    def _compute_importance_score(
        self,
        source: str,
        entity_count: int,
        word_count: int,
        coverage_count: int,
        total_articles: int,
        published_at: Optional[datetime],
        sentiment_score: float,
        is_paywall: bool,
    ) -> float:
        """Compute composite importance score (0-100).

        Formula (Step 7 Section 3.3):
            importance = 0.25*authority + 0.20*entity_density
                       + 0.25*coverage + 0.15*recency
                       + 0.15*extremity + paywall_adjustment

        Args:
            source: Source domain (for authority lookup).
            entity_count: Number of named entities in article.
            word_count: Article word count.
            coverage_count: Number of sources covering the same topic.
            total_articles: Total articles in the batch (for normalization).
            published_at: Publication timestamp (for recency).
            sentiment_score: Sentiment score [-1, 1] (for extremity).
            is_paywall: Whether article is paywall-truncated.

        Returns:
            Importance score clamped to [0, 100].
        """
        # 1. Authority (0-100): source tier from authority mapping
        authority = self.source_authority.get(source, 50)

        # 2. Entity density (0-100): entity_count / word_count, scaled
        if word_count > 0:
            raw_density = entity_count / word_count
            # Typical news: 0.01-0.10 entities per word. Scale to 0-100.
            entity_density = min(raw_density * 1000, 100.0)
        else:
            entity_density = 0.0

        # 3. Coverage (0-100): how many sources covered similar topic
        # Normalize by total articles; higher cross-source coverage = more important
        if total_articles > 0:
            coverage = min((coverage_count / max(total_articles, 1)) * 500, 100.0)
        else:
            coverage = 0.0

        # 4. Recency (0-100): exponential decay from published_at
        now = datetime.now(timezone.utc)
        if published_at is not None:
            try:
                if published_at.tzinfo is None:
                    published_at = published_at.replace(tzinfo=timezone.utc)
                hours_old = max((now - published_at).total_seconds() / 3600, 0)
                # Half-life of 24 hours: score = 100 * exp(-0.693 * hours / 24)
                recency = 100.0 * math.exp(-0.0289 * hours_old)
            except (TypeError, OverflowError):
                recency = 50.0
        else:
            recency = 50.0

        # 5. Extremity (0-100): |sentiment_score| scaled
        extremity = abs(sentiment_score) * 100.0

        # Composite score
        importance = (
            IMPORTANCE_WEIGHTS["authority"] * authority
            + IMPORTANCE_WEIGHTS["entity_density"] * entity_density
            + IMPORTANCE_WEIGHTS["coverage"] * coverage
            + IMPORTANCE_WEIGHTS["recency"] * recency
            + IMPORTANCE_WEIGHTS["extremity"] * extremity
        )

        # Paywall adjustment
        if is_paywall:
            importance += PAYWALL_PENALTY

        # Clamp to [0, 100]
        importance = max(0.0, min(100.0, importance))

        # NaN guard (Step 7 error handling)
        if math.isnan(importance):
            logger.warning("importance_nan_clamped", source=source)
            importance = 0.0

        return round(importance, 2)

    # -----------------------------------------------------------------
    # Social Mood Index (T18)
    # -----------------------------------------------------------------

    @staticmethod
    def _compute_mood_index(
        sentiment_scores: list[float],
        emotion_dicts: list[dict[str, float]],
    ) -> float:
        """Compute social mood index for a group of articles.

        Formula (Step 7 Section 3.3):
            mood_index = weighted_avg(sentiment_scores) * (1 - entropy(emotions))

        Entropy is computed over the average emotion distribution.

        Args:
            sentiment_scores: List of sentiment scores [-1, 1].
            emotion_dicts: List of per-article emotion dictionaries.

        Returns:
            Mood index value (higher = more positive, more focused emotion).
        """
        if not sentiment_scores:
            return 0.0

        # Weighted average of sentiment scores
        avg_sentiment = float(np.mean(sentiment_scores))

        # Average emotion distribution
        if emotion_dicts:
            avg_emotions = {}
            for e in PLUTCHIK_EMOTIONS:
                avg_emotions[e] = float(np.mean(
                    [d.get(e, 0.0) for d in emotion_dicts]
                ))
            # Normalize to probability distribution
            total = sum(avg_emotions.values())
            if total > 0:
                probs = [v / total for v in avg_emotions.values()]
            else:
                probs = [1.0 / len(PLUTCHIK_EMOTIONS)] * len(PLUTCHIK_EMOTIONS)

            # Shannon entropy
            entropy = -sum(
                p * math.log2(p) for p in probs if p > 0
            )
            # Normalize entropy to [0, 1] range (max entropy for 8 labels = log2(8) = 3)
            max_entropy = math.log2(len(PLUTCHIK_EMOTIONS))
            normalized_entropy = entropy / max_entropy if max_entropy > 0 else 0.0
        else:
            normalized_entropy = 1.0  # Maximum uncertainty

        # Mood index: sentiment magnitude * emotional focus
        mood_index = avg_sentiment * (1.0 - normalized_entropy)
        return round(float(mood_index), 4)

    # -----------------------------------------------------------------
    # Emotion Trajectory (T19)
    # -----------------------------------------------------------------

    @staticmethod
    def _compute_emotion_trajectory(
        current_emotions: dict[str, float],
        past_emotions: dict[str, float],
    ) -> dict[str, float]:
        """Compute emotion delta between current and 7-day-ago snapshot.

        Args:
            current_emotions: Current aggregated emotion scores.
            past_emotions: Emotion scores from 7 days ago.

        Returns:
            Dict mapping emotion to delta value.
        """
        delta = {}
        for e in PLUTCHIK_EMOTIONS:
            delta[e] = round(
                current_emotions.get(e, 0.0) - past_emotions.get(e, 0.0), 4
            )
        return delta

    # -----------------------------------------------------------------
    # Coverage Estimation (for importance score)
    # -----------------------------------------------------------------

    @staticmethod
    def _estimate_coverage(
        embeddings: Optional[np.ndarray],
        article_idx: int,
        similarity_threshold: float = 0.75,
    ) -> int:
        """Estimate cross-source coverage by counting similar articles.

        Uses cosine similarity on SBERT embeddings to find articles
        covering the same topic from different sources.

        Args:
            embeddings: NxD embedding matrix.
            article_idx: Index of the target article.
            similarity_threshold: Cosine similarity threshold.

        Returns:
            Count of similar articles (coverage proxy).
        """
        if embeddings is None or article_idx >= len(embeddings):
            return 1

        target = embeddings[article_idx]
        norm_target = np.linalg.norm(target)
        if norm_target == 0:
            return 1

        # Compute cosine similarity against all articles
        norms = np.linalg.norm(embeddings, axis=1)
        # Avoid division by zero
        norms = np.where(norms == 0, 1.0, norms)
        similarities = np.dot(embeddings, target) / (norms * norm_target)

        # Count articles above threshold (excluding self)
        similar_count = int(np.sum(similarities > similarity_threshold)) - 1
        return max(similar_count, 1)

    # -----------------------------------------------------------------
    # Batch Processing
    # -----------------------------------------------------------------

    def _process_article_batch(
        self,
        batch_articles: list[dict[str, Any]],
        embeddings: Optional[np.ndarray],
        ner_entity_counts: dict[str, int],
        batch_start_idx: int,
        total_articles: int,
    ) -> list[dict[str, Any]]:
        """Process a batch of articles through all Stage 3 analyses.

        Args:
            batch_articles: List of article dicts from the Parquet table.
            embeddings: Embedding matrix for coverage estimation.
            ner_entity_counts: article_id -> entity count mapping.
            batch_start_idx: Starting index in the global article array.
            total_articles: Total number of articles.

        Returns:
            List of analysis result dicts (one per article).
        """
        results = []

        for i, article in enumerate(batch_articles):
            article_id = article.get("article_id", "")
            title = article.get("title", "") or ""
            body = article.get("body", "") or ""
            source = article.get("source", "") or ""
            language = article.get("language", "en") or "en"
            word_count = article.get("word_count", 0) or 0
            published_at = article.get("published_at")

            # Determine text to use for analysis
            analysis_text = body if body.strip() else title

            # Detect paywall status (empty body = paywall-truncated)
            is_paywall = not bool(body and body.strip())

            # 1. Sentiment Analysis (T13/T14)
            sentiment_label, sentiment_score = self._analyze_sentiment(
                title, body, language
            )

            # 2. Emotion Classification (T15)
            emotions = self._classify_emotions(analysis_text)

            # 3. STEEPS Classification (T16)
            steeps_category = self._classify_steeps(analysis_text, source)

            # 4. Importance Score
            entity_count = ner_entity_counts.get(article_id, 0)
            global_idx = batch_start_idx + i
            coverage = self._estimate_coverage(embeddings, global_idx)

            importance_score = self._compute_importance_score(
                source=source,
                entity_count=entity_count,
                word_count=word_count,
                coverage_count=coverage,
                total_articles=total_articles,
                published_at=published_at,
                sentiment_score=sentiment_score,
                is_paywall=is_paywall,
            )

            results.append({
                "article_id": article_id,
                "sentiment_label": sentiment_label,
                "sentiment_score": float(sentiment_score),
                "emotion_joy": float(emotions.get("joy", 0.0)),
                "emotion_trust": float(emotions.get("trust", 0.0)),
                "emotion_fear": float(emotions.get("fear", 0.0)),
                "emotion_surprise": float(emotions.get("surprise", 0.0)),
                "emotion_sadness": float(emotions.get("sadness", 0.0)),
                "emotion_anger": float(emotions.get("anger", 0.0)),
                "emotion_disgust": float(emotions.get("disgust", 0.0)),
                "emotion_anticipation": float(emotions.get("anticipation", 0.0)),
                "steeps_category": steeps_category,
                "importance_score": float(importance_score),
                # Additional columns stored internally for downstream stages
                # stance/narrative removed: not persisted to output, wasted 10/24 NLI passes
                "_emotions_dict": emotions,
                "_source": source,
                "_published_at": published_at,
            })

        return results

    # -----------------------------------------------------------------
    # Main Entry Point
    # -----------------------------------------------------------------

    def run(self) -> Path:
        """Execute the full Stage 3 analysis pipeline.

        Loads input data, processes all articles through sentiment, emotion,
        STEEPS, and importance analyses, then writes
        output Parquet files.

        Returns:
            Path to the output article_analysis.parquet file.

        Raises:
            PipelineStageError: If the stage fails to complete.
        """
        pa, pq = _lazy_import_pyarrow()
        pd = _lazy_import_pandas()

        start_time = time.time()
        logger.info("stage3_start")
        self._log_memory("stage3_start")

        # Ensure output directory exists
        self.output_path.parent.mkdir(parents=True, exist_ok=True)

        # ----- Load Input Data -----

        try:
            logger.info("loading_articles", path=str(self.articles_path))
            articles_table = pq.read_table(self.articles_path)
            articles_df = articles_table.to_pandas()
            num_articles = len(articles_df)
            logger.info("articles_loaded", count=num_articles)
        except Exception as e:
            raise PipelineStageError(
                f"Failed to load articles: {e}",
                stage_name="stage_3_article",
                stage_number=3,
            )

        if num_articles == 0:
            logger.warning("stage3_empty_input")
            self._write_empty_output(pa, pq)
            return self.output_path

        # Load embeddings for coverage estimation (optional)
        embeddings = None
        try:
            emb_path = self.features_dir / "embeddings.parquet"
            if emb_path.exists():
                logger.info("loading_embeddings", path=str(emb_path))
                emb_table = pq.read_table(emb_path)
                emb_df = emb_table.to_pandas()
                if "embedding" in emb_df.columns:
                    embeddings = np.array(emb_df["embedding"].tolist(), dtype=np.float32)
                    logger.info("embeddings_loaded", shape=str(embeddings.shape))
        except Exception as e:
            logger.warning("embeddings_load_failed", error=str(e))

        # Load NER entity counts for importance scoring (optional)
        ner_entity_counts: dict[str, int] = {}
        try:
            ner_path = self.features_dir / "ner.parquet"
            if ner_path.exists():
                logger.info("loading_ner", path=str(ner_path))
                ner_table = pq.read_table(ner_path)
                ner_df = ner_table.to_pandas()
                for _, row in ner_df.iterrows():
                    aid = row.get("article_id", "")
                    # Count total entities across all entity columns
                    count = 0
                    for col in ["entities_person", "entities_org", "entities_location"]:
                        entities = row.get(col)
                        if isinstance(entities, (list, np.ndarray)):
                            count += len(entities)
                    ner_entity_counts[aid] = count
                logger.info("ner_loaded", articles_with_entities=len(ner_entity_counts))
        except Exception as e:
            logger.warning("ner_load_failed", error=str(e))

        # ----- Load Models -----

        self._load_en_sentiment()
        self._load_ko_sentiment()
        self._load_zeroshot()
        self._log_memory("all_models_loaded")

        # ----- Process Articles in Batches -----

        all_results: list[dict[str, Any]] = []
        articles_list = articles_df.to_dict("records")

        for batch_start in range(0, num_articles, TRANSFORMER_BATCH_SIZE):
            batch_end = min(batch_start + TRANSFORMER_BATCH_SIZE, num_articles)
            batch = articles_list[batch_start:batch_end]

            batch_results = self._process_article_batch(
                batch_articles=batch,
                embeddings=embeddings,
                ner_entity_counts=ner_entity_counts,
                batch_start_idx=batch_start,
                total_articles=num_articles,
            )
            all_results.extend(batch_results)

            # Periodic garbage collection to manage memory on 16GB systems
            if batch_end % 100 == 0:
                gc.collect()

            if (batch_end % 100 == 0) or batch_end == num_articles:
                elapsed = time.time() - start_time
                rate = batch_end / elapsed if elapsed > 0 else 0
                logger.info(
                    "stage3_progress",
                    processed=batch_end,
                    total=num_articles,
                    elapsed_s=round(elapsed, 1),
                    rate_per_s=round(rate, 1),
                )

        # ----- Compute Mood Index and Emotion Trajectory (T18, T19) -----

        mood_trajectory_records = self._compute_mood_and_trajectory(all_results)

        # ----- Write Main Output: article_analysis.parquet -----

        self._write_analysis_output(all_results, pa, pq)

        # ----- Write Mood Trajectory Output -----

        self._write_mood_trajectory(mood_trajectory_records, pa, pq)

        # ----- Unload Models -----

        self._unload_models()

        elapsed = time.time() - start_time
        logger.info(
            "stage3_complete",
            articles_processed=num_articles,
            elapsed_s=round(elapsed, 1),
            output_path=str(self.output_path),
        )

        return self.output_path

    # -----------------------------------------------------------------
    # Output Writers
    # -----------------------------------------------------------------

    def _write_analysis_output(
        self, results: list[dict[str, Any]], pa: Any, pq: Any
    ) -> None:
        """Write article_analysis.parquet with the exact 13-column schema.

        Schema (Step 7 Section 3.3 output):
            article_id: utf8
            sentiment_label: utf8
            sentiment_score: float32
            emotion_joy: float32
            emotion_trust: float32
            emotion_fear: float32
            emotion_surprise: float32
            emotion_sadness: float32
            emotion_anger: float32
            emotion_disgust: float32
            emotion_anticipation: float32
            steeps_category: utf8
            importance_score: float32
        """
        schema = pa.schema([
            pa.field("article_id", pa.utf8()),
            pa.field("sentiment_label", pa.utf8()),
            pa.field("sentiment_score", pa.float32()),
            pa.field("emotion_joy", pa.float32()),
            pa.field("emotion_trust", pa.float32()),
            pa.field("emotion_fear", pa.float32()),
            pa.field("emotion_surprise", pa.float32()),
            pa.field("emotion_sadness", pa.float32()),
            pa.field("emotion_anger", pa.float32()),
            pa.field("emotion_disgust", pa.float32()),
            pa.field("emotion_anticipation", pa.float32()),
            pa.field("steeps_category", pa.utf8()),
            pa.field("importance_score", pa.float32()),
        ])

        # Extract only the schema columns from results
        columns = {
            "article_id": [r["article_id"] for r in results],
            "sentiment_label": [r["sentiment_label"] for r in results],
            "sentiment_score": [float(r["sentiment_score"]) for r in results],
            "emotion_joy": [float(r["emotion_joy"]) for r in results],
            "emotion_trust": [float(r["emotion_trust"]) for r in results],
            "emotion_fear": [float(r["emotion_fear"]) for r in results],
            "emotion_surprise": [float(r["emotion_surprise"]) for r in results],
            "emotion_sadness": [float(r["emotion_sadness"]) for r in results],
            "emotion_anger": [float(r["emotion_anger"]) for r in results],
            "emotion_disgust": [float(r["emotion_disgust"]) for r in results],
            "emotion_anticipation": [float(r["emotion_anticipation"]) for r in results],
            "steeps_category": [r["steeps_category"] for r in results],
            "importance_score": [float(r["importance_score"]) for r in results],
        }

        table = pa.table(columns, schema=schema)

        pq.write_table(
            table,
            self.output_path,
            compression=PARQUET_COMPRESSION,
            compression_level=PARQUET_COMPRESSION_LEVEL,
        )

        logger.info(
            "analysis_output_written",
            path=str(self.output_path),
            rows=len(results),
            columns=len(schema),
        )

    def _write_mood_trajectory(
        self, records: list[dict[str, Any]], pa: Any, pq: Any
    ) -> None:
        """Write mood_trajectory.parquet for Stage 5 time series consumption.

        Schema:
            source: utf8
            date: utf8 (ISO date string)
            mood_index: float32
            article_count: int32
            avg_sentiment: float32
            emotion_joy_avg: float32
            emotion_trust_avg: float32
            emotion_fear_avg: float32
            emotion_surprise_avg: float32
            emotion_sadness_avg: float32
            emotion_anger_avg: float32
            emotion_disgust_avg: float32
            emotion_anticipation_avg: float32
        """
        if not records:
            logger.info("mood_trajectory_empty")
            return

        schema = pa.schema([
            pa.field("source", pa.utf8()),
            pa.field("date", pa.utf8()),
            pa.field("mood_index", pa.float32()),
            pa.field("article_count", pa.int32()),
            pa.field("avg_sentiment", pa.float32()),
            pa.field("emotion_joy_avg", pa.float32()),
            pa.field("emotion_trust_avg", pa.float32()),
            pa.field("emotion_fear_avg", pa.float32()),
            pa.field("emotion_surprise_avg", pa.float32()),
            pa.field("emotion_sadness_avg", pa.float32()),
            pa.field("emotion_anger_avg", pa.float32()),
            pa.field("emotion_disgust_avg", pa.float32()),
            pa.field("emotion_anticipation_avg", pa.float32()),
        ])

        columns: dict[str, list] = {field.name: [] for field in schema}
        for rec in records:
            for field in schema:
                columns[field.name].append(rec.get(field.name))

        table = pa.table(columns, schema=schema)
        output_path = MOOD_TRAJECTORY_PARQUET_PATH
        output_path.parent.mkdir(parents=True, exist_ok=True)

        pq.write_table(
            table,
            output_path,
            compression=PARQUET_COMPRESSION,
            compression_level=PARQUET_COMPRESSION_LEVEL,
        )

        logger.info(
            "mood_trajectory_written",
            path=str(output_path),
            rows=len(records),
        )

    def _write_empty_output(self, pa: Any, pq: Any) -> None:
        """Write an empty but schema-valid article_analysis.parquet."""
        schema = pa.schema([
            pa.field("article_id", pa.utf8()),
            pa.field("sentiment_label", pa.utf8()),
            pa.field("sentiment_score", pa.float32()),
            pa.field("emotion_joy", pa.float32()),
            pa.field("emotion_trust", pa.float32()),
            pa.field("emotion_fear", pa.float32()),
            pa.field("emotion_surprise", pa.float32()),
            pa.field("emotion_sadness", pa.float32()),
            pa.field("emotion_anger", pa.float32()),
            pa.field("emotion_disgust", pa.float32()),
            pa.field("emotion_anticipation", pa.float32()),
            pa.field("steeps_category", pa.utf8()),
            pa.field("importance_score", pa.float32()),
        ])
        table = pa.table({field.name: [] for field in schema}, schema=schema)
        pq.write_table(table, self.output_path)
        logger.info("empty_output_written", path=str(self.output_path))

    # -----------------------------------------------------------------
    # Mood & Trajectory Aggregation
    # -----------------------------------------------------------------

    def _compute_mood_and_trajectory(
        self, all_results: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Aggregate per-source-per-day mood index and emotion averages.

        Groups articles by (source, date), computes mood index and
        average emotions for each group.

        Args:
            all_results: Full list of per-article analysis results
                         (including _emotions_dict, _source, _published_at).

        Returns:
            List of mood_trajectory records (one per source-date group).
        """
        # Group by (source, date)
        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for r in all_results:
            source = r.get("_source", "unknown")
            pub_at = r.get("_published_at")
            if pub_at is not None:
                try:
                    if hasattr(pub_at, "date"):
                        date_str = pub_at.date().isoformat()
                    elif hasattr(pub_at, "isoformat"):
                        date_str = pub_at.isoformat()[:10]
                    else:
                        date_str = str(pub_at)[:10]
                except Exception:
                    date_str = "unknown"
            else:
                date_str = "unknown"

            key = (source, date_str)
            if key not in groups:
                groups[key] = []
            groups[key].append(r)

        records = []
        for (source, date_str), group in groups.items():
            sentiment_scores = [r["sentiment_score"] for r in group]
            emotion_dicts = [r.get("_emotions_dict", {}) for r in group]

            mood_index = self._compute_mood_index(sentiment_scores, emotion_dicts)

            # Average emotions for this group
            avg_emotions = {}
            for e in PLUTCHIK_EMOTIONS:
                vals = [d.get(e, 0.0) for d in emotion_dicts]
                avg_emotions[e] = float(np.mean(vals)) if vals else 0.0

            records.append({
                "source": source,
                "date": date_str,
                "mood_index": round(float(mood_index), 4),
                "article_count": len(group),
                "avg_sentiment": round(float(np.mean(sentiment_scores)), 4),
                "emotion_joy_avg": round(avg_emotions.get("joy", 0.0), 4),
                "emotion_trust_avg": round(avg_emotions.get("trust", 0.0), 4),
                "emotion_fear_avg": round(avg_emotions.get("fear", 0.0), 4),
                "emotion_surprise_avg": round(avg_emotions.get("surprise", 0.0), 4),
                "emotion_sadness_avg": round(avg_emotions.get("sadness", 0.0), 4),
                "emotion_anger_avg": round(avg_emotions.get("anger", 0.0), 4),
                "emotion_disgust_avg": round(avg_emotions.get("disgust", 0.0), 4),
                "emotion_anticipation_avg": round(avg_emotions.get("anticipation", 0.0), 4),
            })

        return records

    # -----------------------------------------------------------------
    # Schema Validation
    # -----------------------------------------------------------------

    def validate_output(self) -> dict[str, Any]:
        """Validate the output Parquet against the expected schema.

        Checks:
            1. File exists and is readable.
            2. Correct number of columns (13).
            3. All column names match schema.
            4. Data types match schema.
            5. No missing article_ids.
            6. Sentiment scores in valid range.
            7. Emotion scores in valid range.
            8. STEEPS categories are valid codes.
            9. Importance scores in valid range.

        Returns:
            Dict with 'valid' (bool) and 'issues' (list of strings).
        """
        pa, pq = _lazy_import_pyarrow()
        issues: list[str] = []

        # 1. File exists
        if not self.output_path.exists():
            return {"valid": False, "issues": ["Output file does not exist"]}

        try:
            table = pq.read_table(self.output_path)
        except Exception as e:
            return {"valid": False, "issues": [f"Cannot read Parquet: {e}"]}

        # 2. Column count
        expected_columns = 13
        if len(table.schema) != expected_columns:
            issues.append(
                f"Column count mismatch: expected {expected_columns}, "
                f"got {len(table.schema)}"
            )

        # 3. Column names
        expected_names = [
            "article_id", "sentiment_label", "sentiment_score",
            "emotion_joy", "emotion_trust", "emotion_fear",
            "emotion_surprise", "emotion_sadness", "emotion_anger",
            "emotion_disgust", "emotion_anticipation",
            "steeps_category", "importance_score",
        ]
        actual_names = table.schema.names
        for name in expected_names:
            if name not in actual_names:
                issues.append(f"Missing column: {name}")

        # 4. Data types
        type_checks = {
            "article_id": pa.utf8(),
            "sentiment_label": pa.utf8(),
            "sentiment_score": pa.float32(),
            "steeps_category": pa.utf8(),
            "importance_score": pa.float32(),
        }
        for col_name, expected_type in type_checks.items():
            if col_name in actual_names:
                actual_type = table.schema.field(col_name).type
                if actual_type != expected_type:
                    issues.append(
                        f"Type mismatch for {col_name}: "
                        f"expected {expected_type}, got {actual_type}"
                    )

        # Numeric range checks on actual data
        df = table.to_pandas()

        # 5. No missing article_ids
        null_ids = df["article_id"].isna().sum() if "article_id" in df.columns else 0
        if null_ids > 0:
            issues.append(f"{null_ids} articles have null article_id")

        # 6. Sentiment score range
        if "sentiment_score" in df.columns:
            s = df["sentiment_score"]
            out_of_range = ((s < -1.0) | (s > 1.0)).sum()
            if out_of_range > 0:
                issues.append(f"{out_of_range} sentiment_scores out of [-1, 1] range")

        # 7. Emotion score ranges
        for e in PLUTCHIK_EMOTIONS:
            col = f"emotion_{e}"
            if col in df.columns:
                vals = df[col]
                out_of_range = ((vals < 0.0) | (vals > 1.0)).sum()
                if out_of_range > 0:
                    issues.append(f"{out_of_range} {col} values out of [0, 1] range")

        # 8. STEEPS categories
        valid_steeps = set(STEEPS_CODE_MAP.values())
        if "steeps_category" in df.columns:
            invalid = df[~df["steeps_category"].isin(valid_steeps)]
            if len(invalid) > 0:
                bad_values = invalid["steeps_category"].unique().tolist()
                issues.append(f"Invalid STEEPS categories: {bad_values}")

        # 9. Importance score range
        if "importance_score" in df.columns:
            s = df["importance_score"]
            out_of_range = ((s < 0.0) | (s > 100.0)).sum()
            if out_of_range > 0:
                issues.append(f"{out_of_range} importance_scores out of [0, 100] range")

        # 10. Sentiment distribution sanity
        if "sentiment_label" in df.columns and len(df) > 10:
            dist = df["sentiment_label"].value_counts(normalize=True)
            neutral_ratio = dist.get("neutral", 0.0)
            if neutral_ratio > 0.95:
                issues.append(
                    f"Sentiment distribution suspect: {neutral_ratio:.1%} neutral "
                    "(may indicate model failure)"
                )

        # 11. STEEPS coverage
        if "steeps_category" in df.columns and len(df) > 50:
            present_cats = set(df["steeps_category"].unique())
            missing_cats = valid_steeps - present_cats
            if missing_cats:
                issues.append(
                    f"Missing STEEPS categories: {missing_cats} "
                    "(possible classification bias)"
                )

        return {
            "valid": len(issues) == 0,
            "issues": issues,
            "rows": len(df),
            "columns": len(table.schema),
        }

    # -----------------------------------------------------------------
    # Distribution Statistics (for execution report)
    # -----------------------------------------------------------------

    def get_distribution_stats(self) -> dict[str, Any]:
        """Compute distribution statistics from the output Parquet.

        Returns:
            Dict with sentiment_distribution, steeps_distribution,
            emotion_stats, importance_stats, and memory_log.
        """
        pa, pq = _lazy_import_pyarrow()

        if not self.output_path.exists():
            return {"error": "Output file does not exist"}

        df = pq.read_table(self.output_path).to_pandas()

        stats: dict[str, Any] = {
            "total_articles": len(df),
            "memory_log": self._memory_log,
        }

        # Sentiment distribution
        if "sentiment_label" in df.columns:
            stats["sentiment_distribution"] = (
                df["sentiment_label"].value_counts().to_dict()
            )

        # STEEPS distribution
        if "steeps_category" in df.columns:
            stats["steeps_distribution"] = (
                df["steeps_category"].value_counts().to_dict()
            )

        # Emotion statistics
        emotion_stats = {}
        for e in PLUTCHIK_EMOTIONS:
            col = f"emotion_{e}"
            if col in df.columns:
                emotion_stats[e] = {
                    "mean": round(float(df[col].mean()), 4),
                    "std": round(float(df[col].std()), 4),
                    "max": round(float(df[col].max()), 4),
                }
        stats["emotion_stats"] = emotion_stats

        # Importance score statistics
        if "importance_score" in df.columns:
            stats["importance_stats"] = {
                "mean": round(float(df["importance_score"].mean()), 2),
                "std": round(float(df["importance_score"].std()), 2),
                "min": round(float(df["importance_score"].min()), 2),
                "max": round(float(df["importance_score"].max()), 2),
                "median": round(float(df["importance_score"].median()), 2),
            }

        return stats


# =============================================================================
# Convenience Function
# =============================================================================

def run_stage3(
    articles_path: Path | str | None = None,
    features_dir: Path | str | None = None,
    output_path: Path | str | None = None,
    source_authority: dict[str, int] | None = None,
    validate: bool = True,
) -> dict[str, Any]:
    """Convenience function to run Stage 3 article analysis pipeline.

    Instantiates Stage3ArticleAnalyzer, runs the analysis, optionally
    validates output, and returns a summary dict.

    Args:
        articles_path: Path to input articles Parquet. Default from constants.
        features_dir: Path to features directory. Default from constants.
        output_path: Path for output Parquet. Default from constants.
        source_authority: Custom source authority mapping. Default built-in.
        validate: Whether to validate output schema after writing.

    Returns:
        Dict with 'output_path', 'validation', 'stats', 'elapsed_s'.
    """
    start = time.time()

    analyzer = Stage3ArticleAnalyzer(
        articles_path=Path(articles_path) if articles_path else None,
        features_dir=Path(features_dir) if features_dir else None,
        output_path=Path(output_path) if output_path else None,
        source_authority=source_authority,
    )

    result_path = analyzer.run()
    elapsed = time.time() - start

    summary: dict[str, Any] = {
        "output_path": str(result_path),
        "elapsed_s": round(elapsed, 1),
    }

    if validate:
        validation = analyzer.validate_output()
        summary["validation"] = validation
        if not validation["valid"]:
            logger.warning("stage3_validation_issues", issues=validation["issues"])

    summary["stats"] = analyzer.get_distribution_stats()

    return summary


# =============================================================================
# CLI Entry Point
# =============================================================================

if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(
        description="Stage 3: Per-Article Analysis (sentiment, emotion, STEEPS, importance)"
    )
    parser.add_argument(
        "--articles",
        type=str,
        default=None,
        help="Path to input articles.parquet",
    )
    parser.add_argument(
        "--features-dir",
        type=str,
        default=None,
        help="Path to features directory",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Path for output article_analysis.parquet",
    )
    parser.add_argument(
        "--no-validate",
        action="store_true",
        help="Skip output validation",
    )
    parser.add_argument(
        "--stats-only",
        action="store_true",
        help="Only print distribution stats from existing output (no processing)",
    )

    args = parser.parse_args()

    from src.utils.logging_config import setup_logging
    setup_logging()

    if args.stats_only:
        analyzer = Stage3ArticleAnalyzer(
            output_path=Path(args.output) if args.output else None,
        )
        stats = analyzer.get_distribution_stats()
        print(json.dumps(stats, indent=2, default=str))
    else:
        result = run_stage3(
            articles_path=args.articles,
            features_dir=args.features_dir,
            output_path=args.output,
            validate=not args.no_validate,
        )
        print(json.dumps(result, indent=2, default=str))
