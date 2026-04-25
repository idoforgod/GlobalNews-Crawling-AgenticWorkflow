"""M6: Economic Intelligence — EPU, Sector Sentiment, Momentum, Narratives, Hype Cycle.

Five Type A (arithmetic) / Type B (rule-based) metrics:

    EI-1  Multilingual EPU Index
          Economic Policy Uncertainty per language per date.
          count(articles matching EPU keywords) / total_articles_in_lang.
          Window aggregate: mean over available dates.

    EI-2  Sector Sentiment Index
          Daily mean sentiment per industry sector (energy, technology,
          healthcare, financial, manufacturing).  Keyword-based sector
          classification with case-insensitive matching.

    EI-3  Sector Sentiment Momentum
          1st derivative (velocity) and 2nd derivative (acceleration)
          of EI-2 daily series via np.gradient.  Negative 2nd + positive
          1st = decelerating growth (early warning).

    EI-4  Narrative Economics Tracker
          Prevalence and sentiment of economic narrative keywords
          (recession, inflation, bubble, growth, crisis) per language
          per date.  7-day rolling average of count and sentiment.

    EI-5  Technology Hype Cycle Phase
          Classify tech entities by hype phase using volume trend
          (linear regression slope) + sentiment rules.

No LLM.  No ML inference.  All deterministic arithmetic.

Outputs (Parquet, ZSTD):
    epu_index.parquet          — EI-1 per language × date
    sector_sentiment.parquet   — EI-2 + EI-3 per sector × date
    narrative_economics.parquet — EI-4 per narrative × language × date
    hype_cycle.parquet         — EI-5 per tech entity

Reference: research/bigdata-insight-workflow-design.md §M6
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from src.config.constants import (
    INSIGHT_MIN_STEEPS_E_ARTICLES,
    PARQUET_COMPRESSION,
    PARQUET_COMPRESSION_LEVEL,
)
from src.insights.constants import (
    EPU_KEYWORDS,
    HYPE_SENTIMENT_NEGATIVE,
    HYPE_SENTIMENT_NEUTRAL,
    HYPE_SENTIMENT_POSITIVE,
    HYPE_VOLUME_FALLING,
    HYPE_VOLUME_RISING,
    HYPE_VOLUME_STABLE,
    NARRATIVE_KEYWORDS,
    SECTOR_KEYWORDS,
)
from src.insights.window_assembler import WindowCorpus

logger = logging.getLogger(__name__)

# Minimum articles for a tech entity to be classified in EI-5
_HYPE_MIN_ARTICLES = 10

# Sector keyword matching: languages to try in order
_SECTOR_LANGS = ("en", "ko")

# Rolling window for narrative economics (EI-4) in days
_NARRATIVE_ROLLING_WINDOW = 7

# Sectors for deterministic iteration order
_SECTORS = ("energy", "technology", "healthcare", "financial", "manufacturing")


# =============================================================================
# Parquet I/O helpers
# =============================================================================


def _write_parquet(df: pd.DataFrame, path: Path) -> None:
    """Write a DataFrame to Parquet with project-standard ZSTD compression."""
    table = pa.Table.from_pandas(df, preserve_index=False)
    pq.write_table(
        table,
        str(path),
        compression=PARQUET_COMPRESSION,
        compression_level=PARQUET_COMPRESSION_LEVEL,
    )
    logger.info(
        "Parquet written: %s (%d rows, %d cols)",
        path.name, len(df), len(df.columns),
    )


def _write_empty_parquet(
    path: Path,
    schema: dict[str, str],
) -> None:
    """Write an empty Parquet file with the correct column schema.

    Ensures L0 Anti-Skip Guard (file exists + min size) is satisfied
    even when data is insufficient.
    """
    dtype_map = {"str": "object", "int": "int64", "float": "float64"}
    df = pd.DataFrame({
        col: pd.Series(dtype=dtype_map.get(dtype, dtype))
        for col, dtype in schema.items()
    })
    table = pa.Table.from_pandas(df, preserve_index=False)
    pq.write_table(
        table,
        str(path),
        compression=PARQUET_COMPRESSION,
        compression_level=PARQUET_COMPRESSION_LEVEL,
    )
    logger.info("Empty Parquet written: %s (schema-only)", path.name)


# =============================================================================
# Text matching helpers
# =============================================================================


# Languages that use Latin script (word boundaries work correctly with \b)
_LATIN_SCRIPT_LANGS = frozenset({"en", "de", "fr", "es", "it", "pt", "sv", "no", "cs", "pl"})


def _build_keyword_regex(keywords: set[str], language: str = "en") -> re.Pattern[str]:
    """Compile a single regex for case-insensitive matching of a keyword set.

    P1 Hallucination Prevention: Uses word boundaries (\\b) for Latin-script
    languages to prevent false positives (e.g., "ai" matching "fair",
    "gas" matching "Madagascar"). CJK/Korean keywords use substring match
    because they are independent morphological units.

    Args:
        keywords: Set of keyword strings.
        language: ISO language code. Latin-script langs get \\b boundaries.
    """
    escaped = sorted(re.escape(kw) for kw in keywords)
    if language in _LATIN_SCRIPT_LANGS:
        pattern = "|".join(f"\\b{kw}\\b" for kw in escaped)
    else:
        # CJK/Korean: substring match is correct (morphological units)
        pattern = "|".join(escaped)
    return re.compile(pattern, re.IGNORECASE)


def _text_matches(text: Any, compiled_re: re.Pattern[str]) -> bool:
    """Return True if text contains at least one keyword match."""
    if not isinstance(text, str) or not text:
        return False
    return compiled_re.search(text) is not None


def _count_keyword_matches(text: Any, compiled_re: re.Pattern[str]) -> int:
    """Return the number of keyword matches in text."""
    if not isinstance(text, str) or not text:
        return 0
    return len(compiled_re.findall(text))


def _combine_text(title: Any, body: Any) -> str:
    """Combine title and body into a single searchable string."""
    parts = []
    if isinstance(title, str) and title:
        parts.append(title)
    if isinstance(body, str) and body:
        parts.append(body)
    return " ".join(parts)


# =============================================================================
# EI-1: Multilingual EPU Index
# =============================================================================


def _compute_epu(
    articles_df: pd.DataFrame,
) -> tuple[pd.DataFrame, dict[str, float]]:
    """Compute Economic Policy Uncertainty index per language per date.

    For each language with EPU_KEYWORDS defined, count articles where
    title or body contains at least one keyword from the set.
    EPU = count / total_articles_in_lang.

    Returns:
        (epu_df, epu_values) where epu_values is lang -> mean EPU over window.
    """
    rows: list[dict[str, Any]] = []
    epu_values: dict[str, float] = {}

    if articles_df.empty:
        return pd.DataFrame(rows), epu_values

    # Pre-compile regex per language
    lang_patterns: dict[str, re.Pattern[str]] = {}
    for lang, keywords in EPU_KEYWORDS.items():
        if keywords:
            lang_patterns[lang] = _build_keyword_regex(keywords, language=lang)

    # Ensure date column exists
    if "_crawl_date" not in articles_df.columns:
        logger.warning("EI-1: _crawl_date column missing, cannot compute daily EPU")
        return pd.DataFrame(rows), epu_values

    # Combine title + body once
    articles_df = articles_df.copy()
    articles_df["_text"] = articles_df.apply(
        lambda r: _combine_text(r.get("title"), r.get("body")), axis=1
    )

    for lang, pattern in lang_patterns.items():
        lang_articles = articles_df[articles_df["language"] == lang]
        if lang_articles.empty:
            continue

        for date_val, group in lang_articles.groupby("_crawl_date"):
            total = len(group)
            if total == 0:
                continue
            match_count = group["_text"].apply(
                lambda t: _text_matches(t, pattern)
            ).sum()
            epu_score = float(match_count) / total

            rows.append({
                "language": lang,
                "date": str(date_val),
                "epu_count": int(match_count),
                "total_articles": total,
                "epu_score": epu_score,
            })

    epu_df = pd.DataFrame(rows)

    # Aggregate: mean EPU over the window per language
    if not epu_df.empty:
        for lang in epu_df["language"].unique():
            lang_data = epu_df[epu_df["language"] == lang]
            epu_values[lang] = float(lang_data["epu_score"].mean())

    logger.info(
        "EI-1 EPU: %d language(s), %d daily records",
        len(epu_values), len(epu_df),
    )
    return epu_df, epu_values


# =============================================================================
# EI-2: Sector Sentiment Index
# =============================================================================


def _classify_sector(
    text: str,
    sector_patterns: dict[str, list[tuple[str, re.Pattern[str]]]],
) -> str | None:
    """Classify article text into a sector by keyword match count.

    For each sector, try all language keyword sets. The sector with
    the most total matches wins. Ties: first sector in iteration order.

    Args:
        text: Combined title + body text.
        sector_patterns: sector -> [(lang, compiled_regex), ...].

    Returns:
        Sector name or None if no keywords match.
    """
    best_sector: str | None = None
    best_count = 0

    for sector in _SECTORS:
        if sector not in sector_patterns:
            continue
        total_matches = 0
        for _lang, pattern in sector_patterns[sector]:
            total_matches += _count_keyword_matches(text, pattern)
        if total_matches > best_count:
            best_count = total_matches
            best_sector = sector

    return best_sector


def _build_sector_patterns() -> dict[str, list[tuple[str, re.Pattern[str]]]]:
    """Pre-compile sector keyword regexes for each sector × language.

    Uses _SECTOR_LANGS (en, ko) for broadest coverage. For articles
    in other languages, 'en' keywords often still match loan words.

    Returns:
        sector -> [(lang, compiled_pattern), ...]
    """
    result: dict[str, list[tuple[str, re.Pattern[str]]]] = {}
    for sector, lang_keywords in SECTOR_KEYWORDS.items():
        patterns: list[tuple[str, re.Pattern[str]]] = []
        for lang in _SECTOR_LANGS:
            kws = lang_keywords.get(lang)
            if kws:
                patterns.append((lang, _build_keyword_regex(kws, language=lang)))
        if patterns:
            result[sector] = patterns
    return result


def _compute_sector_sentiment(
    articles_df: pd.DataFrame,
    analysis_df: pd.DataFrame,
) -> tuple[pd.DataFrame, dict[str, float], pd.DataFrame]:
    """Compute daily mean sentiment per industry sector.

    Filters to STEEPS='E' (Economic) articles. Classifies each article
    into a sector by keyword matching. Computes daily mean sentiment_score.

    Returns:
        (sector_daily_df, sector_sentiment_values, tech_articles_df)
        where sector_sentiment_values is sector -> window mean sentiment,
        and tech_articles_df contains articles classified as 'technology'
        for downstream EI-5 use.
    """
    rows: list[dict[str, Any]] = []
    sector_values: dict[str, float] = {}
    tech_articles = pd.DataFrame()

    # Merge articles with analysis to get sentiment + STEEPS category
    merged = articles_df.merge(
        analysis_df[["article_id", "sentiment_score", "steeps_category"]],
        on="article_id",
        how="inner",
    )

    # Filter to Economic articles
    econ_mask = merged["steeps_category"].str.upper() == "E"
    econ_df = merged[econ_mask].copy()

    if len(econ_df) < INSIGHT_MIN_STEEPS_E_ARTICLES:
        logger.warning(
            "EI-2: Only %d Economic articles (min=%d), returning empty",
            len(econ_df), INSIGHT_MIN_STEEPS_E_ARTICLES,
        )
        return pd.DataFrame(rows), sector_values, tech_articles

    logger.info("EI-2: %d Economic articles for sector classification", len(econ_df))

    # Combine text for keyword matching
    econ_df["_text"] = econ_df.apply(
        lambda r: _combine_text(r.get("title"), r.get("body")), axis=1
    )

    # Pre-compile sector patterns
    sector_patterns = _build_sector_patterns()

    # Classify each article
    econ_df["_sector"] = econ_df["_text"].apply(
        lambda t: _classify_sector(t, sector_patterns)
    )

    # Filter out unclassified articles
    classified = econ_df[econ_df["_sector"].notna()].copy()
    if classified.empty:
        logger.warning("EI-2: No articles matched any sector keywords")
        return pd.DataFrame(rows), sector_values, tech_articles

    # Extract tech articles for EI-5 before aggregation
    tech_mask = classified["_sector"] == "technology"
    tech_articles = classified[tech_mask].copy()

    logger.info(
        "EI-2: Classified %d/%d articles into sectors (tech=%d)",
        len(classified), len(econ_df), len(tech_articles),
    )

    # Ensure date column
    if "_crawl_date" not in classified.columns:
        logger.warning("EI-2: _crawl_date missing, cannot compute daily series")
        return pd.DataFrame(rows), sector_values, tech_articles

    # Daily mean sentiment per sector
    for sector in _SECTORS:
        sector_data = classified[classified["_sector"] == sector]
        if sector_data.empty:
            continue

        for date_val, group in sector_data.groupby("_crawl_date"):
            mean_sent = float(group["sentiment_score"].mean())
            rows.append({
                "sector": sector,
                "date": str(date_val),
                "mean_sentiment": mean_sent,
                "article_count": len(group),
            })

        # Window aggregate
        sector_values[sector] = float(sector_data["sentiment_score"].mean())

    sector_df = pd.DataFrame(rows)
    logger.info(
        "EI-2 Sector Sentiment: %d sector(s) with data, %d daily records",
        len(sector_values), len(sector_df),
    )
    return sector_df, sector_values, tech_articles


# =============================================================================
# EI-3: Sector Sentiment Momentum
# =============================================================================


def _compute_momentum(sector_df: pd.DataFrame) -> pd.DataFrame:
    """Compute 1st and 2nd derivatives of daily sector sentiment.

    For each sector's daily sentiment series, compute:
        velocity (1st derivative) — rate of sentiment change
        acceleration (2nd derivative) — rate of velocity change

    Uses np.gradient which handles uneven spacing gracefully.

    A negative 2nd derivative with positive 1st derivative indicates
    'decelerating growth' — an early warning signal.

    Returns:
        Updated sector_df with velocity, acceleration, early_warning columns.
    """
    if sector_df.empty:
        sector_df["velocity"] = pd.Series(dtype="float64")
        sector_df["acceleration"] = pd.Series(dtype="float64")
        sector_df["early_warning"] = pd.Series(dtype="bool")
        return sector_df

    result_parts: list[pd.DataFrame] = []

    for sector in sector_df["sector"].unique():
        s_data = sector_df[sector_df["sector"] == sector].copy()
        s_data = s_data.sort_values("date").reset_index(drop=True)

        sentiment_vals = s_data["mean_sentiment"].values.astype(float)

        if len(sentiment_vals) < 2:
            # Single data point: derivatives are zero
            s_data["velocity"] = 0.0
            s_data["acceleration"] = 0.0
            s_data["early_warning"] = False
        else:
            velocity = np.gradient(sentiment_vals)
            acceleration = np.gradient(velocity)
            s_data["velocity"] = velocity
            s_data["acceleration"] = acceleration
            # Early warning: decelerating growth (positive velocity, negative acceleration)
            s_data["early_warning"] = (velocity > 0) & (acceleration < 0)

        result_parts.append(s_data)

    result = pd.concat(result_parts, ignore_index=True) if result_parts else sector_df
    logger.info("EI-3 Momentum: computed derivatives for %d sector(s)",
                len(sector_df["sector"].unique()))
    return result


# =============================================================================
# EI-4: Narrative Economics Tracker
# =============================================================================


def _compute_narrative_economics(
    articles_df: pd.DataFrame,
    analysis_df: pd.DataFrame,
) -> pd.DataFrame:
    """Track prevalence and sentiment of economic narrative keywords.

    For each narrative type × language with keywords defined:
        - Count articles matching any keyword per date
        - Mean sentiment of matching articles per date
        - 7-day rolling average of count and sentiment

    Returns:
        DataFrame with narrative × language × date rows.
    """
    rows: list[dict[str, Any]] = []

    if articles_df.empty:
        return pd.DataFrame(rows)

    # Merge with analysis for sentiment
    merged = articles_df.merge(
        analysis_df[["article_id", "sentiment_score"]],
        on="article_id",
        how="inner",
    )

    if merged.empty or "_crawl_date" not in merged.columns:
        return pd.DataFrame(rows)

    # Combine text once
    merged = merged.copy()
    merged["_text"] = merged.apply(
        lambda r: _combine_text(r.get("title"), r.get("body")), axis=1
    )

    for narrative, lang_keywords in NARRATIVE_KEYWORDS.items():
        for lang, keywords in lang_keywords.items():
            if not keywords:
                continue

            pattern = _build_keyword_regex(keywords, language=lang)
            lang_articles = merged[merged["language"] == lang]
            if lang_articles.empty:
                continue

            for date_val, group in lang_articles.groupby("_crawl_date"):
                matches_mask = group["_text"].apply(
                    lambda t: _text_matches(t, pattern)
                )
                match_count = int(matches_mask.sum())
                if match_count > 0:
                    mean_sentiment = float(
                        group.loc[matches_mask, "sentiment_score"].mean()
                    )
                else:
                    mean_sentiment = 0.0

                rows.append({
                    "narrative": narrative,
                    "language": lang,
                    "date": str(date_val),
                    "match_count": match_count,
                    "total_articles": len(group),
                    "prevalence": match_count / len(group) if len(group) > 0 else 0.0,
                    "mean_sentiment": mean_sentiment,
                })

    narrative_df = pd.DataFrame(rows)

    # Compute 7-day rolling averages per narrative × language
    if not narrative_df.empty:
        narrative_df = narrative_df.sort_values(
            ["narrative", "language", "date"]
        ).reset_index(drop=True)

        rolling_cols: list[pd.Series] = []
        rolling_sent_cols: list[pd.Series] = []

        for (_narr, _lang), group in narrative_df.groupby(
            ["narrative", "language"], sort=False
        ):
            group_sorted = group.sort_values("date")
            rc = group_sorted["match_count"].rolling(
                window=_NARRATIVE_ROLLING_WINDOW, min_periods=1
            ).mean()
            rs = group_sorted["mean_sentiment"].rolling(
                window=_NARRATIVE_ROLLING_WINDOW, min_periods=1
            ).mean()
            rolling_cols.append(rc)
            rolling_sent_cols.append(rs)

        narrative_df["rolling_count_7d"] = pd.concat(rolling_cols).values
        narrative_df["rolling_sentiment_7d"] = pd.concat(rolling_sent_cols).values

    logger.info(
        "EI-4 Narrative Economics: %d narratives, %d total records",
        len(NARRATIVE_KEYWORDS), len(narrative_df),
    )
    return narrative_df


# =============================================================================
# EI-5: Technology Hype Cycle Phase
# =============================================================================


def _parse_entities(entities_value: Any) -> list[str]:
    """Parse entities_org column which may be a list, string, numpy array, or NaN."""
    if entities_value is None:
        return []
    if isinstance(entities_value, (list, tuple)):
        return [str(e).strip() for e in entities_value if e]
    # pyarrow hands list columns back as numpy.ndarray of object — treat
    # them as iterable sequences, not scalars.
    if isinstance(entities_value, np.ndarray):
        return [str(e).strip() for e in entities_value.tolist() if e]
    if isinstance(entities_value, str):
        if not entities_value.strip():
            return []
        if entities_value.startswith("["):
            try:
                import ast
                parsed = ast.literal_eval(entities_value)
                if isinstance(parsed, list):
                    return [str(e).strip() for e in parsed if e]
            except (ValueError, SyntaxError):
                pass
        # Fallback: treat as comma-separated or single entity
        return [e.strip() for e in entities_value.split(",") if e.strip()]
    return []


def _linear_slope(values: np.ndarray) -> float:
    """Compute the slope of a simple linear regression on the values.

    Uses np.polyfit(degree=1) which is closed-form (arithmetic).

    Args:
        values: 1-D array of numeric values.

    Returns:
        Slope (float). Returns 0.0 if < 2 values.
    """
    if len(values) < 2:
        return 0.0
    x = np.arange(len(values), dtype=float)
    try:
        slope, _ = np.polyfit(x, values, deg=1)
        return float(slope)
    except (np.linalg.LinAlgError, ValueError):
        return 0.0


def _classify_hype_phase(
    volume_trend: float,
    sentiment_mean: float,
    sentiment_trend: float,
) -> str:
    """Classify a tech entity into a hype cycle phase.

    Rules:
        1. Rising volume + positive sentiment => peak_of_inflated_expectations
        2. Falling volume + negative sentiment => trough_of_disillusionment
        3. Rising volume + neutral sentiment => slope_of_enlightenment
        4. Stable volume => plateau_of_productivity
        5. Default => trigger (technology trigger)

    Args:
        volume_trend: Linear regression slope of daily article count.
        sentiment_mean: Mean sentiment across all articles.
        sentiment_trend: Linear regression slope of daily sentiment (unused
            in classification but available for downstream analysis).

    Returns:
        Hype cycle phase string.
    """
    if (volume_trend > HYPE_VOLUME_RISING
            and sentiment_mean > HYPE_SENTIMENT_POSITIVE):
        return "peak_of_inflated_expectations"
    elif (volume_trend < HYPE_VOLUME_FALLING
          and sentiment_mean < HYPE_SENTIMENT_NEGATIVE):
        return "trough_of_disillusionment"
    elif volume_trend > 0 and abs(sentiment_mean) < HYPE_SENTIMENT_NEUTRAL:
        return "slope_of_enlightenment"
    elif abs(volume_trend) < HYPE_VOLUME_STABLE:
        return "plateau_of_productivity"
    else:
        return "trigger"


def _compute_hype_cycle(
    tech_articles: pd.DataFrame,
    ner_df: pd.DataFrame,
) -> tuple[pd.DataFrame, dict[str, str]]:
    """Classify technology entities by hype cycle phase.

    For tech-sector articles (from EI-2), group by top entity
    (from entities_org). For each entity with >= _HYPE_MIN_ARTICLES
    articles, compute volume_trend, sentiment_mean, sentiment_trend
    and classify into a hype phase.

    Args:
        tech_articles: Articles classified as 'technology' sector (from EI-2).
        ner_df: NER DataFrame with article_id and entities_org columns.

    Returns:
        (hype_df, hype_phases) where hype_phases is entity -> phase string.
    """
    rows: list[dict[str, Any]] = []
    hype_phases: dict[str, str] = {}

    if tech_articles.empty:
        return pd.DataFrame(rows), hype_phases

    # Merge tech articles with NER to get entities
    merged = tech_articles.merge(
        ner_df[["article_id", "entities_org"]],
        on="article_id",
        how="inner",
    )

    if merged.empty:
        logger.info("EI-5: No tech articles with NER entities")
        return pd.DataFrame(rows), hype_phases

    # Explode entities: each article may mention multiple orgs
    entity_rows: list[dict[str, Any]] = []
    for _, row in merged.iterrows():
        entities = _parse_entities(row.get("entities_org"))
        for entity in entities:
            entity_rows.append({
                "article_id": row["article_id"],
                "entity": entity,
                "sentiment_score": row.get("sentiment_score", 0.0),
                "date": str(row.get("_crawl_date", "")),
            })

    if not entity_rows:
        return pd.DataFrame(rows), hype_phases

    entity_df = pd.DataFrame(entity_rows)

    # Filter entities with >= _HYPE_MIN_ARTICLES articles
    entity_counts = entity_df["entity"].value_counts()
    qualifying = entity_counts[entity_counts >= _HYPE_MIN_ARTICLES].index.tolist()

    if not qualifying:
        logger.info(
            "EI-5: No entities with >= %d articles (max was %d)",
            _HYPE_MIN_ARTICLES,
            int(entity_counts.max()) if not entity_counts.empty else 0,
        )
        return pd.DataFrame(rows), hype_phases

    for entity in qualifying:
        e_data = entity_df[entity_df["entity"] == entity].copy()
        e_data = e_data.sort_values("date")

        # Daily counts for volume trend
        daily_counts = e_data.groupby("date").size()
        daily_counts = daily_counts.sort_index()
        volume_trend = _linear_slope(daily_counts.values.astype(float))

        # Sentiment: mean and trend
        sentiment_mean = float(e_data["sentiment_score"].mean())
        daily_sentiment = e_data.groupby("date")["sentiment_score"].mean()
        daily_sentiment = daily_sentiment.sort_index()
        sentiment_trend = _linear_slope(daily_sentiment.values.astype(float))

        # Classify phase
        phase = _classify_hype_phase(volume_trend, sentiment_mean, sentiment_trend)
        hype_phases[entity] = phase

        rows.append({
            "entity": entity,
            "article_count": len(e_data),
            "volume_trend": volume_trend,
            "sentiment_mean": sentiment_mean,
            "sentiment_trend": sentiment_trend,
            "hype_phase": phase,
        })

    hype_df = pd.DataFrame(rows)
    logger.info(
        "EI-5 Hype Cycle: %d entities classified — %s",
        len(hype_phases),
        {phase: sum(1 for p in hype_phases.values() if p == phase)
         for phase in sorted(set(hype_phases.values()))},
    )
    return hype_df, hype_phases


# =============================================================================
# Public entry point
# =============================================================================


def run_economic_analysis(
    corpus: WindowCorpus,
    output_dir: Path,
    prior_metrics: dict[str, Any],
) -> dict[str, Any]:
    """Execute M6 Economic Intelligence: EPU, Sector Sentiment, Momentum, Narratives, Hype Cycle.

    All metrics are Type A (pure arithmetic) or Type B (rule-based).
    No LLM, no ML inference.

    Args:
        corpus: WindowCorpus providing lazy-loaded multi-date Parquet access.
        output_dir: Directory to write output Parquet files.
        prior_metrics: Metrics from previously completed modules (M1-M5).

    Returns:
        Dict with keys: epu_values, sector_sentiment, hype_phases.
        Used by P1 validators and downstream M7 synthesis.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    logger.info("=== M6 Economic Intelligence: start ===")

    # --- Load data (column-selective for memory efficiency) ---
    articles_df = corpus.load_parquet(
        "articles",
        columns=["article_id", "language", "published_at", "title", "body"],
    )
    analysis_df = corpus.load_parquet(
        "article_analysis",
        columns=["article_id", "sentiment_score", "steeps_category"],
    )
    ner_df = corpus.load_parquet(
        "ner",
        columns=["article_id", "entities_org"],
    )

    logger.info(
        "M6: Data loaded — articles=%d, analysis=%d, ner=%d",
        len(articles_df), len(analysis_df), len(ner_df),
    )

    # Edge case: completely empty data
    if articles_df.empty:
        logger.warning("M6: No articles loaded, returning empty metrics")
        _write_all_empty(output_dir)
        return _empty_metrics()

    # --- EI-1: Multilingual EPU Index ---
    logger.info("--- EI-1: Multilingual EPU Index ---")
    epu_df, epu_values = _compute_epu(articles_df)

    epu_path = output_dir / "epu_index.parquet"
    if not epu_df.empty:
        _write_parquet(epu_df, epu_path)
    else:
        _write_empty_parquet(epu_path, _EPU_SCHEMA_COLS)
        logger.warning("EI-1: No EPU data — wrote empty %s", epu_path.name)

    # --- EI-2: Sector Sentiment Index ---
    logger.info("--- EI-2: Sector Sentiment Index ---")
    sector_df, sector_values, tech_articles = _compute_sector_sentiment(
        articles_df, analysis_df,
    )

    # --- EI-3: Sector Sentiment Momentum ---
    logger.info("--- EI-3: Sector Sentiment Momentum ---")
    sector_df = _compute_momentum(sector_df)

    sector_path = output_dir / "sector_sentiment.parquet"
    if not sector_df.empty:
        _write_parquet(sector_df, sector_path)
    else:
        _write_empty_parquet(sector_path, _SECTOR_SCHEMA_COLS)
        logger.warning("EI-2/3: No sector data — wrote empty %s", sector_path.name)

    # --- EI-4: Narrative Economics Tracker ---
    logger.info("--- EI-4: Narrative Economics Tracker ---")
    narrative_df = _compute_narrative_economics(articles_df, analysis_df)

    narrative_path = output_dir / "narrative_economics.parquet"
    if not narrative_df.empty:
        _write_parquet(narrative_df, narrative_path)
    else:
        _write_empty_parquet(narrative_path, _NARRATIVE_SCHEMA_COLS)
        logger.warning("EI-4: No narrative data — wrote empty %s", narrative_path.name)

    # --- EI-5: Technology Hype Cycle Phase ---
    logger.info("--- EI-5: Technology Hype Cycle Phase ---")
    hype_df, hype_phases = _compute_hype_cycle(tech_articles, ner_df)

    hype_path = output_dir / "hype_cycle.parquet"
    if not hype_df.empty:
        _write_parquet(hype_df, hype_path)
    else:
        _write_empty_parquet(hype_path, _HYPE_SCHEMA_COLS)
        logger.warning("EI-5: No hype cycle data — wrote empty %s", hype_path.name)

    # --- Return metrics dict for P1 validation and M7 synthesis ---
    metrics: dict[str, Any] = {
        "epu_values": epu_values,
        "sector_sentiment": sector_values,
        "hype_phases": hype_phases,
    }

    logger.info(
        "=== M6 Economic Intelligence: complete — "
        "EPU langs=%d, sectors=%d, narratives=%d rows, "
        "hype entities=%d ===",
        len(epu_values),
        len(sector_values),
        len(narrative_df),
        len(hype_phases),
    )

    return metrics


# =============================================================================
# Empty schema definitions (for L0 validation when data is insufficient)
# =============================================================================

_EPU_SCHEMA_COLS = {
    "language": "str",
    "date": "str",
    "epu_count": "int",
    "total_articles": "int",
    "epu_score": "float",
}

_SECTOR_SCHEMA_COLS = {
    "sector": "str",
    "date": "str",
    "mean_sentiment": "float",
    "article_count": "int",
    "velocity": "float",
    "acceleration": "float",
    "early_warning": "str",  # bool stored as object for empty schema compat
}

_NARRATIVE_SCHEMA_COLS = {
    "narrative": "str",
    "language": "str",
    "date": "str",
    "match_count": "int",
    "total_articles": "int",
    "prevalence": "float",
    "mean_sentiment": "float",
    "rolling_count_7d": "float",
    "rolling_sentiment_7d": "float",
}

_HYPE_SCHEMA_COLS = {
    "entity": "str",
    "article_count": "int",
    "volume_trend": "float",
    "sentiment_mean": "float",
    "sentiment_trend": "float",
    "hype_phase": "str",
}


# =============================================================================
# Edge-case helpers
# =============================================================================


def _empty_metrics() -> dict[str, Any]:
    """Return the metrics dict shape with empty values."""
    return {
        "epu_values": {},
        "sector_sentiment": {},
        "hype_phases": {},
    }


def _write_all_empty(output_dir: Path) -> None:
    """Write all 4 empty Parquet outputs when no data is available."""
    _write_empty_parquet(output_dir / "epu_index.parquet", _EPU_SCHEMA_COLS)
    _write_empty_parquet(output_dir / "sector_sentiment.parquet", _SECTOR_SCHEMA_COLS)
    _write_empty_parquet(output_dir / "narrative_economics.parquet", _NARRATIVE_SCHEMA_COLS)
    _write_empty_parquet(output_dir / "hype_cycle.parquet", _HYPE_SCHEMA_COLS)
    logger.info("M6: wrote empty placeholder Parquet files")
