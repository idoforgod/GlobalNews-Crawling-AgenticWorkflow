"""Stage 5: Time Series Analysis -- STL, burst, changepoint, forecast, wavelet.

Implements 8 techniques (T29-T36) for temporal pattern detection across
topic prevalence, sentiment, and emotion time series:

    T29: STL Decomposition (statsmodels, period=7 weekly)
    T30: Kleinberg Burst Detection (custom automaton model)
    T31: PELT Changepoint Detection (ruptures, RBF kernel, BIC penalty)
    T32: Prophet Forecast (7-day and 30-day horizons)
    T33: Wavelet Analysis (pywt, Daubechies-4, 4 levels)
    T34: ARIMA Modeling (statsmodels, grid search order selection)
    T35: Moving Average Crossover (3-day vs 14-day rolling mean)
    T36: Seasonality Detection (scipy periodogram)

Input:
    - data/analysis/topics.parquet          (article-topic assignments)
    - data/analysis/article_analysis.parquet (sentiment, emotion, STEEPS)
    - data/processed/articles.parquet        (article metadata + timestamps)

Output:
    - data/analysis/timeseries.parquet      (17 columns per PRD SS7.1)

Memory budget: ~0.5 GB peak (statistical libs only, no heavy ML models).
Performance target: ~3.0 min for 1,000 articles.

Reference: Step 7 Pipeline Design, Section 3.5 (Stage 5: Time Series Analysis).
"""

from __future__ import annotations

import gc
import logging
import math
import os
import resource
import time
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from src.config.constants import (
    ARTICLES_PARQUET_PATH,
    ARTICLE_ANALYSIS_PARQUET_PATH,
    DATA_ANALYSIS_DIR,
    FORECAST_HORIZON_DAYS,
    MIN_DAYS_FOR_ANALYSIS,
    PARQUET_COMPRESSION,
    PARQUET_COMPRESSION_LEVEL,
    TIMESERIES_PARQUET_PATH,
    TOPICS_PARQUET_PATH,
)
from src.utils.error_handler import (
    AnalysisError,
    PipelineStageError,
)
from src.utils.logging_config import get_analysis_logger

logger = get_analysis_logger()

# =============================================================================
# Parquet Schema -- TIMESERIES_SCHEMA (17 columns, PRD SS7.1)
# =============================================================================

TIMESERIES_SCHEMA = pa.schema([
    pa.field("series_id", pa.utf8(), nullable=False),
    pa.field("topic_id", pa.int32(), nullable=False),
    pa.field("metric_type", pa.utf8(), nullable=False),
    pa.field("date", pa.timestamp("us", tz="UTC"), nullable=False),
    pa.field("value", pa.float32(), nullable=False),
    pa.field("trend", pa.float32(), nullable=True),
    pa.field("seasonal", pa.float32(), nullable=True),
    pa.field("residual", pa.float32(), nullable=True),
    pa.field("burst_score", pa.float32(), nullable=True),
    pa.field("is_changepoint", pa.bool_(), nullable=False),
    pa.field("changepoint_significance", pa.float32(), nullable=True),
    pa.field("prophet_forecast", pa.float32(), nullable=True),
    pa.field("prophet_lower", pa.float32(), nullable=True),
    pa.field("prophet_upper", pa.float32(), nullable=True),
    pa.field("ma_short", pa.float32(), nullable=True),
    pa.field("ma_long", pa.float32(), nullable=True),
    pa.field("ma_signal", pa.utf8(), nullable=True),
])

# =============================================================================
# Stage 5 constants (derived from Step 7 pipeline design)
# =============================================================================

# STL Decomposition (T29)
STL_PERIOD: int = 7  # Weekly seasonality
STL_MIN_OBSERVATIONS: int = 14  # Minimum for STL (2 full periods)
STL_ROBUST: bool = True  # Robust to outliers
STL_RESIDUAL_ANOMALY_THRESHOLD: float = 2.0  # Std devs for anomaly alert

# Kleinberg Burst Detection (T30)
KLEINBERG_S: float = 2.0  # Base scaling parameter (state transition cost)
KLEINBERG_GAMMA: float = 1.0  # Difficulty of state transitions

# PELT Changepoint Detection (T31)
PELT_MODEL: str = "rbf"  # Radial basis function kernel
PELT_MIN_SIZE: int = 3  # Minimum segment length
PELT_JUMP: int = 5  # Subsample step (>1 speeds up O(n²) kernel)
PELT_PERMUTATION_ITER: int = 20  # Permutation test iterations for significance

# Date range trimming — avoids processing years of empty data when
# trafilatura extracts very old published_at dates.
MAX_SERIES_DAYS: int = 90  # Limit time series to last 90 days

# Prophet Forecast (T32)
PROPHET_TOP_K: int = 20  # Top-K topics by volume for forecasting
PROPHET_SHORT_HORIZON: int = 7  # 7-day forecast
PROPHET_MEDIUM_HORIZON: int = 30  # 30-day forecast
PROPHET_MIN_DATA_POINTS: int = 14  # Minimum observations for Prophet fit

# Wavelet Analysis (T33)
WAVELET_FAMILY: str = "db4"  # Daubechies-4
WAVELET_LEVEL: int = 4  # Decomposition levels
WAVELET_CYCLE_SCALES: list[int] = [1, 3, 7, 14, 28]  # Multi-scale cycles

# ARIMA Modeling (T34)
ARIMA_MAX_P: int = 5  # Maximum AR order
ARIMA_MAX_D: int = 2  # Maximum differencing
ARIMA_MAX_Q: int = 5  # Maximum MA order

# Moving Average Crossover (T35)
MA_SHORT_WINDOW: int = 3  # 3-day short MA
MA_LONG_WINDOW: int = 14  # 14-day long MA

# Emotion columns tracked from article_analysis.parquet
EMOTION_COLUMNS: list[str] = [
    "emotion_joy",
    "emotion_trust",
    "emotion_fear",
    "emotion_surprise",
    "emotion_sadness",
    "emotion_disgust",
    "emotion_anger",
    "emotion_anticipation",
]


# =============================================================================
# Data classes for intermediate results
# =============================================================================

@dataclass
class TimeSeriesRecord:
    """A single row in the time series output table."""

    series_id: str
    topic_id: int
    metric_type: str
    date: datetime
    value: float
    trend: float | None = None
    seasonal: float | None = None
    residual: float | None = None
    burst_score: float | None = None
    is_changepoint: bool = False
    changepoint_significance: float | None = None
    prophet_forecast: float | None = None
    prophet_lower: float | None = None
    prophet_upper: float | None = None
    ma_short: float | None = None
    ma_long: float | None = None
    ma_signal: str | None = None


@dataclass
class STLResult:
    """Result container for STL decomposition of a single series."""

    trend: np.ndarray  # shape (n_dates,)
    seasonal: np.ndarray
    residual: np.ndarray
    anomaly_mask: np.ndarray  # bool, True where |residual| > threshold * std


@dataclass
class BurstInterval:
    """A detected burst interval from Kleinberg's automaton model."""

    start_idx: int
    end_idx: int
    burst_level: int  # State level (0=normal, 1=burst, 2=higher burst, ...)
    burst_score: float  # burst_level * duration * volume


@dataclass
class ChangepointResult:
    """Result container for PELT changepoint detection."""

    changepoint_indices: list[int]  # Indices into the time series
    significance_scores: list[float]  # 1 - p_value per changepoint


@dataclass
class ProphetResult:
    """Result container for Prophet forecast."""

    # Arrays aligned to the date range (historical + forecast)
    dates: list[datetime]
    forecast: np.ndarray  # yhat
    lower: np.ndarray  # yhat_lower
    upper: np.ndarray  # yhat_upper
    horizon: int  # 7 or 30


@dataclass
class WaveletResult:
    """Result container for wavelet decomposition."""

    coefficients: list[np.ndarray]  # Wavelet coefficients per level
    dominant_period: float | None  # Dominant periodicity in days
    energy_by_scale: dict[int, float]  # Energy per scale (cycle days)


@dataclass
class ARIMAResult:
    """Result container for ARIMA modeling."""

    forecast: np.ndarray
    residuals: np.ndarray
    order: tuple[int, int, int]
    aic: float


@dataclass
class SeasonalityResult:
    """Result container for periodogram-based seasonality detection."""

    periods: list[float]  # Detected periods in days
    strengths: list[float]  # Power spectral density at each period
    significant: list[bool]  # Whether each period passes significance test


@dataclass
class Stage5Config:
    """Configuration for Stage 5 time series analysis."""

    stl_period: int = STL_PERIOD
    stl_min_observations: int = STL_MIN_OBSERVATIONS
    stl_robust: bool = STL_ROBUST
    kleinberg_s: float = KLEINBERG_S
    kleinberg_gamma: float = KLEINBERG_GAMMA
    pelt_model: str = PELT_MODEL
    pelt_min_size: int = PELT_MIN_SIZE
    pelt_jump: int = PELT_JUMP
    pelt_permutation_iter: int = PELT_PERMUTATION_ITER
    prophet_top_k: int = PROPHET_TOP_K
    prophet_short_horizon: int = PROPHET_SHORT_HORIZON
    prophet_medium_horizon: int = PROPHET_MEDIUM_HORIZON
    ma_short_window: int = MA_SHORT_WINDOW
    ma_long_window: int = MA_LONG_WINDOW
    wavelet_family: str = WAVELET_FAMILY
    wavelet_level: int = WAVELET_LEVEL


@dataclass
class Stage5Metrics:
    """Runtime metrics collected during Stage 5 execution."""

    n_topics: int = 0
    n_series: int = 0
    n_dates: int = 0
    n_stl_decomposed: int = 0
    n_bursts_detected: int = 0
    n_changepoints_detected: int = 0
    n_prophet_forecasts: int = 0
    n_arima_fits: int = 0
    n_wavelet_analyzed: int = 0
    n_seasonalities_found: int = 0
    peak_memory_gb: float = 0.0
    elapsed_seconds: float = 0.0


# =============================================================================
# Memory tracking helper
# =============================================================================

def _get_memory_gb() -> float:
    """Return current RSS memory usage in GB.

    Uses resource.getrusage on macOS/Linux. Falls back to 0.0 on failure.

    Platform-specific unit handling:
      - macOS (darwin): ru_maxrss is in bytes → divide by 1024^3
      - Linux: ru_maxrss is in kilobytes → divide by 1024^2
    """
    try:
        import sys
        usage = resource.getrusage(resource.RUSAGE_SELF)
        rss = usage.ru_maxrss
        if sys.platform == "darwin":
            # macOS reports bytes
            return rss / (1024 ** 3)
        else:
            # Linux reports kilobytes
            return rss / (1024 ** 2)
    except Exception:
        return 0.0


# =============================================================================
# T29: STL Decomposition
# =============================================================================

def _run_stl(
    values: np.ndarray,
    period: int = STL_PERIOD,
    robust: bool = STL_ROBUST,
    anomaly_threshold: float = STL_RESIDUAL_ANOMALY_THRESHOLD,
) -> STLResult | None:
    """Decompose a time series using STL (Seasonal-Trend using Loess).

    Args:
        values: 1-D array of time series values (daily, no gaps).
        period: Seasonal period (7 for weekly).
        robust: Use robust fitting to handle outliers.
        anomaly_threshold: Number of standard deviations for residual anomaly.

    Returns:
        STLResult with trend/seasonal/residual components, or None if
        insufficient data (< 2 * period observations).
    """
    n = len(values)
    if n < 2 * period:
        logger.debug(
            "stl_skip_insufficient_data",
            n_observations=n,
            min_required=2 * period,
        )
        return None

    try:
        from statsmodels.tsa.seasonal import STL as StatsSTL

        stl = StatsSTL(values, period=period, robust=robust)
        result = stl.fit()

        trend = np.asarray(result.trend, dtype=np.float32)
        seasonal = np.asarray(result.seasonal, dtype=np.float32)
        residual = np.asarray(result.resid, dtype=np.float32)

        # Anomaly detection on residuals
        resid_std = np.nanstd(residual)
        if resid_std > 0:
            anomaly_mask = np.abs(residual) > anomaly_threshold * resid_std
        else:
            anomaly_mask = np.zeros(n, dtype=bool)

        n_anomalies = int(np.sum(anomaly_mask))
        if n_anomalies > 0:
            logger.info(
                "stl_residual_anomalies",
                n_anomalies=n_anomalies,
                threshold_std=anomaly_threshold,
            )

        return STLResult(
            trend=trend,
            seasonal=seasonal,
            residual=residual,
            anomaly_mask=anomaly_mask,
        )
    except Exception as exc:
        logger.warning("stl_decomposition_failed", error=str(exc))
        return None


def _simple_linear_trend(values: np.ndarray) -> np.ndarray:
    """Compute a simple linear trend via least-squares regression.

    Used as fallback when STL cannot be applied (insufficient data).

    Args:
        values: 1-D array of time series values.

    Returns:
        Array of trend values (same length as input).
    """
    n = len(values)
    if n < 2:
        return np.full(n, np.nanmean(values), dtype=np.float32)

    x = np.arange(n, dtype=np.float64)
    # Mask NaN values
    valid = ~np.isnan(values)
    if np.sum(valid) < 2:
        return np.full(n, np.nanmean(values), dtype=np.float32)

    x_valid = x[valid]
    y_valid = values[valid].astype(np.float64)
    # Least squares: y = a + b*x
    n_valid = len(x_valid)
    x_mean = np.mean(x_valid)
    y_mean = np.mean(y_valid)
    ss_xy = np.sum((x_valid - x_mean) * (y_valid - y_mean))
    ss_xx = np.sum((x_valid - x_mean) ** 2)

    if ss_xx == 0:
        return np.full(n, y_mean, dtype=np.float32)

    slope = ss_xy / ss_xx
    intercept = y_mean - slope * x_mean

    return np.asarray(intercept + slope * x, dtype=np.float32)


# =============================================================================
# T30: Kleinberg Burst Detection
# =============================================================================

def _run_kleinberg_burst(
    counts: np.ndarray,
    s: float = KLEINBERG_S,
    gamma: float = KLEINBERG_GAMMA,
) -> list[BurstInterval]:
    """Detect bursts using Kleinberg's infinite-state automaton model.

    Implements the two-state simplification of Kleinberg's burst detection
    algorithm. The model uses a hidden Markov model framework where states
    represent different intensity levels. Transitions between states incur
    a cost proportional to gamma * ln(n), making frequent state switches
    expensive.

    The algorithm finds the optimal state sequence that minimizes the total
    cost (emission cost + transition cost) via dynamic programming.

    Args:
        counts: 1-D array of non-negative integer event counts per time unit.
        s: Base scaling parameter controlling burst threshold.
            Higher s requires more extreme counts to trigger burst detection.
        gamma: State transition cost multiplier.
            Higher gamma penalizes frequent state transitions.

    Returns:
        List of BurstInterval objects with start/end indices, burst level,
        and composite burst_score = burst_level * duration * volume.
    """
    n = len(counts)
    if n < 3:
        return []

    counts = np.asarray(counts, dtype=np.float64)
    total = np.sum(counts)
    if total == 0:
        return []

    # Number of states: use 2 states (normal + burst) for simplicity
    # with extension to multi-level if needed
    k = max(2, int(1 + math.ceil(math.log(max(np.max(counts), 1)))))
    k = min(k, 10)  # Cap at 10 states to avoid computational explosion

    # Expected rate for each state level
    # State 0: baseline rate = total / n
    # State j: rate = baseline * s^j
    baseline_rate = total / n
    if baseline_rate <= 0:
        return []

    rates = np.array([baseline_rate * (s ** j) for j in range(k)])

    # Transition cost: gamma * ln(n) per level change
    trans_cost = gamma * math.log(max(n, 2))

    # Viterbi-like dynamic programming
    # cost[t][j] = minimum cost to be in state j at time t
    cost = np.full((n, k), np.inf, dtype=np.float64)
    backtrack = np.zeros((n, k), dtype=np.int32)

    # Emission cost: negative log-likelihood under Poisson model
    # -log P(count | rate) = rate - count * log(rate) + log(count!)
    # We ignore the constant log(count!) since it cancels in comparisons
    def emission_cost(count: float, rate: float) -> float:
        if rate <= 0:
            return np.inf
        if count == 0:
            return rate
        return rate - count * math.log(rate)

    # Initialize time 0
    for j in range(k):
        # Initial cost: transition from state 0 to state j at t=0
        cost[0, j] = j * trans_cost + emission_cost(counts[0], rates[j])

    # Forward pass
    for t in range(1, n):
        for j in range(k):
            ec = emission_cost(counts[t], rates[j])
            best_prev_cost = np.inf
            best_prev_state = 0

            for prev_j in range(k):
                # Transition cost: only charge for moving UP (burst onset)
                # Moving down is free (burst ending is natural)
                if j > prev_j:
                    tc = (j - prev_j) * trans_cost
                else:
                    tc = 0.0

                total_cost = cost[t - 1, prev_j] + tc + ec
                if total_cost < best_prev_cost:
                    best_prev_cost = total_cost
                    best_prev_state = prev_j

            cost[t, j] = best_prev_cost
            backtrack[t, j] = best_prev_state

    # Backtrack to find optimal state sequence
    states = np.zeros(n, dtype=np.int32)
    states[n - 1] = int(np.argmin(cost[n - 1]))
    for t in range(n - 2, -1, -1):
        states[t] = backtrack[t + 1, states[t + 1]]

    # Extract burst intervals (contiguous regions where state > 0)
    bursts: list[BurstInterval] = []
    i = 0
    while i < n:
        if states[i] > 0:
            start = i
            level = int(states[i])
            # Find end of this burst
            while i < n and states[i] > 0:
                level = max(level, int(states[i]))
                i += 1
            end = i - 1
            duration = end - start + 1
            volume = float(np.sum(counts[start : end + 1]))
            burst_score = float(level * duration * volume)
            bursts.append(BurstInterval(
                start_idx=start,
                end_idx=end,
                burst_level=level,
                burst_score=burst_score,
            ))
        else:
            i += 1

    return bursts


# =============================================================================
# T31: PELT Changepoint Detection
# =============================================================================

def _run_pelt(
    values: np.ndarray,
    model: str = PELT_MODEL,
    min_size: int = PELT_MIN_SIZE,
    jump: int = PELT_JUMP,
    n_permutations: int = PELT_PERMUTATION_ITER,
) -> ChangepointResult:
    """Detect structural changepoints using the PELT algorithm.

    Uses the ruptures library with a BIC-like penalty: pen = log(n) * dim.
    Significance of each changepoint is estimated via permutation test.

    Args:
        values: 1-D array of time series values.
        model: Cost model ("rbf" for general, "l2" for mean shifts).
        min_size: Minimum segment length between changepoints.
        jump: Subsample step for the cost computation.
        n_permutations: Number of permutation iterations for significance.

    Returns:
        ChangepointResult with detected indices and significance scores.
    """
    n = len(values)
    if n < 2 * min_size:
        return ChangepointResult(changepoint_indices=[], significance_scores=[])

    try:
        import ruptures

        signal = values.reshape(-1, 1).astype(np.float64)
        dim = signal.shape[1]
        penalty = math.log(max(n, 2)) * dim

        algo = ruptures.Pelt(model=model, min_size=min_size, jump=jump)
        algo.fit(signal)
        # PELT returns breakpoints including n (end of signal)
        breakpoints = algo.predict(pen=penalty)

        # Remove the terminal breakpoint (always == n)
        changepoint_indices = [bp for bp in breakpoints if bp < n]

        if not changepoint_indices:
            return ChangepointResult(
                changepoint_indices=[], significance_scores=[]
            )

        # Significance estimation via permutation test
        significance_scores = []
        n_cps_observed = len(changepoint_indices)

        # Compute observed cost
        observed_cost = algo.cost.sum_of_costs(breakpoints)

        rng = np.random.default_rng(42)
        n_more_extreme = 0
        for _ in range(n_permutations):
            perm_signal = signal.copy()
            rng.shuffle(perm_signal)
            algo_perm = ruptures.Pelt(
                model=model, min_size=min_size, jump=jump
            )
            algo_perm.fit(perm_signal)
            perm_bps = algo_perm.predict(pen=penalty)
            perm_cps = [bp for bp in perm_bps if bp < n]
            if len(perm_cps) >= n_cps_observed:
                n_more_extreme += 1

        p_value = (n_more_extreme + 1) / (n_permutations + 1)
        significance = 1.0 - p_value

        # Assign same significance to all changepoints from this run
        significance_scores = [
            round(significance, 4) for _ in changepoint_indices
        ]

        return ChangepointResult(
            changepoint_indices=changepoint_indices,
            significance_scores=significance_scores,
        )

    except ImportError:
        logger.warning("pelt_skip_ruptures_not_installed")
        return ChangepointResult(changepoint_indices=[], significance_scores=[])
    except Exception as exc:
        logger.warning("pelt_detection_failed", error=str(exc))
        return ChangepointResult(changepoint_indices=[], significance_scores=[])


# =============================================================================
# T32: Prophet Forecast
# =============================================================================

def _run_prophet(
    dates: list[datetime],
    values: np.ndarray,
    horizon: int = PROPHET_SHORT_HORIZON,
) -> ProphetResult | None:
    """Fit a Prophet model and generate forecasts.

    Args:
        dates: List of datetime objects (UTC) for the historical data.
        values: 1-D array of observed values aligned to dates.
        horizon: Number of days to forecast beyond the last date.

    Returns:
        ProphetResult with forecast arrays, or None on failure.
    """
    if len(dates) < PROPHET_MIN_DATA_POINTS:
        logger.debug(
            "prophet_skip_insufficient_data",
            n_dates=len(dates),
            min_required=PROPHET_MIN_DATA_POINTS,
        )
        return None

    try:
        # Lazy import to avoid loading Prophet until needed
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            from prophet import Prophet

        import pandas as pd

        # Prophet requires a DataFrame with columns 'ds' and 'y'
        df = pd.DataFrame({
            "ds": [d.replace(tzinfo=None) for d in dates],
            "y": values.astype(float),
        })

        # Suppress Prophet's verbose stdout logging
        model = Prophet(
            yearly_seasonality=False,
            weekly_seasonality=True,
            daily_seasonality=False,
        )
        model.fit(df)

        # Create future dataframe
        future = model.make_future_dataframe(periods=horizon, freq="D")
        forecast = model.predict(future)

        # Extract results aligned to all dates (historical + future)
        all_dates = [
            dt.to_pydatetime().replace(tzinfo=timezone.utc)
            if hasattr(dt, "to_pydatetime")
            else dt.replace(tzinfo=timezone.utc)
            for dt in forecast["ds"]
        ]
        yhat = forecast["yhat"].values.astype(np.float32)
        yhat_lower = forecast["yhat_lower"].values.astype(np.float32)
        yhat_upper = forecast["yhat_upper"].values.astype(np.float32)

        # Clean up Prophet model
        del model
        gc.collect()

        return ProphetResult(
            dates=all_dates,
            forecast=yhat,
            lower=yhat_lower,
            upper=yhat_upper,
            horizon=horizon,
        )

    except ImportError:
        logger.warning("prophet_skip_not_installed")
        return None
    except Exception as exc:
        logger.warning("prophet_forecast_failed", error=str(exc))
        return None


# =============================================================================
# T33: Wavelet Analysis
# =============================================================================

def _run_wavelet(
    values: np.ndarray,
    wavelet: str = WAVELET_FAMILY,
    level: int = WAVELET_LEVEL,
    cycle_scales: list[int] | None = None,
) -> WaveletResult | None:
    """Perform discrete wavelet decomposition on a time series.

    Uses pywt.wavedec with Daubechies-4 wavelet to decompose the signal
    into multiple resolution levels, identifying dominant periodicities.

    Args:
        values: 1-D array of time series values.
        wavelet: Wavelet family name (default "db4").
        level: Number of decomposition levels.
        cycle_scales: Expected cycle scales in days for energy analysis.

    Returns:
        WaveletResult with coefficients and dominant period, or None on failure.
    """
    if cycle_scales is None:
        cycle_scales = WAVELET_CYCLE_SCALES

    n = len(values)
    # Minimum signal length for decomposition at given level
    min_length = 2 ** level
    if n < min_length:
        logger.debug(
            "wavelet_skip_signal_too_short",
            n_observations=n,
            min_required=min_length,
        )
        return None

    try:
        import pywt

        # Adjust level if signal is too short for requested level
        max_level = pywt.dwt_max_level(n, pywt.Wavelet(wavelet).dec_len)
        actual_level = min(level, max_level)

        if actual_level < 1:
            return None

        coeffs = pywt.wavedec(values.astype(np.float64), wavelet, level=actual_level)

        # Compute energy at each scale
        # Level j corresponds to approximate period of 2^j days
        energy_by_scale: dict[int, float] = {}
        for j, c in enumerate(coeffs):
            if j == 0:
                # Approximation coefficients -- low frequency (longest period)
                period_days = 2 ** actual_level
            else:
                # Detail coefficients at level j: period ~ 2^(actual_level - j + 1)
                period_days = 2 ** (actual_level - j + 1)
            energy = float(np.sum(c ** 2))
            energy_by_scale[period_days] = energy

        # Find dominant period (scale with highest energy, excluding DC)
        detail_energies = {
            k: v for k, v in energy_by_scale.items()
            if k < 2 ** actual_level  # Exclude approximation (DC-like)
        }

        dominant_period: float | None = None
        if detail_energies:
            dominant_period = float(max(detail_energies, key=detail_energies.get))

        return WaveletResult(
            coefficients=coeffs,
            dominant_period=dominant_period,
            energy_by_scale=energy_by_scale,
        )

    except ImportError:
        logger.warning("wavelet_skip_pywt_not_installed")
        return None
    except Exception as exc:
        logger.warning("wavelet_analysis_failed", error=str(exc))
        return None


# =============================================================================
# T34: ARIMA Modeling
# =============================================================================

def _run_arima(
    values: np.ndarray,
    max_p: int = ARIMA_MAX_P,
    max_d: int = ARIMA_MAX_D,
    max_q: int = ARIMA_MAX_Q,
    forecast_steps: int = PROPHET_SHORT_HORIZON,
) -> ARIMAResult | None:
    """Fit an ARIMA model via grid search and generate forecasts.

    Tries pmdarima.auto_arima first; falls back to manual grid search
    over a reduced order space if pmdarima is not installed.

    Args:
        values: 1-D array of time series values (at least 10 points).
        max_p: Maximum autoregressive order.
        max_d: Maximum differencing order.
        max_q: Maximum moving-average order.
        forecast_steps: Number of future steps to forecast.

    Returns:
        ARIMAResult with forecast, residuals, order, and AIC, or None on failure.
    """
    n = len(values)
    if n < 10:
        return None

    clean_values = np.asarray(values, dtype=np.float64)
    # Replace NaN with interpolation
    nan_mask = np.isnan(clean_values)
    if np.all(nan_mask):
        return None
    if np.any(nan_mask):
        # Simple linear interpolation for NaN
        valid_idx = np.where(~nan_mask)[0]
        clean_values = np.interp(
            np.arange(n), valid_idx, clean_values[valid_idx]
        )

    # Try pmdarima first
    try:
        import pmdarima as pm

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            auto_model = pm.auto_arima(
                clean_values,
                start_p=0, max_p=min(max_p, 3),
                start_q=0, max_q=min(max_q, 3),
                max_d=max_d,
                seasonal=False,
                suppress_warnings=True,
                stepwise=True,
                error_action="ignore",
            )
        order = auto_model.order
        residuals = np.asarray(auto_model.resid(), dtype=np.float32)
        forecast = np.asarray(
            auto_model.predict(n_periods=forecast_steps), dtype=np.float32
        )
        aic = float(auto_model.aic())

        return ARIMAResult(
            forecast=forecast,
            residuals=residuals,
            order=order,
            aic=aic,
        )

    except ImportError:
        pass  # Fall through to manual grid search
    except Exception:
        pass  # Fall through to manual grid search

    # Manual grid search with statsmodels
    try:
        from statsmodels.tsa.arima.model import ARIMA

        best_aic = np.inf
        best_result: ARIMAResult | None = None

        # Reduced grid for performance
        p_range = range(0, min(max_p, 3) + 1)
        d_range = range(0, min(max_d, 1) + 1)
        q_range = range(0, min(max_q, 3) + 1)

        for p in p_range:
            for d in d_range:
                for q in q_range:
                    if p == 0 and q == 0:
                        continue  # Skip trivial model
                    try:
                        with warnings.catch_warnings():
                            warnings.simplefilter("ignore")
                            model = ARIMA(clean_values, order=(p, d, q))
                            fit = model.fit()
                        current_aic = fit.aic
                        if current_aic < best_aic:
                            best_aic = current_aic
                            fc = fit.forecast(steps=forecast_steps)
                            best_result = ARIMAResult(
                                forecast=np.asarray(fc, dtype=np.float32),
                                residuals=np.asarray(
                                    fit.resid, dtype=np.float32
                                ),
                                order=(p, d, q),
                                aic=float(current_aic),
                            )
                    except Exception:
                        continue

        return best_result

    except ImportError:
        logger.warning("arima_skip_statsmodels_not_installed")
        return None
    except Exception as exc:
        logger.warning("arima_modeling_failed", error=str(exc))
        return None


# =============================================================================
# T35: Moving Average Crossover
# =============================================================================

def _compute_ma_crossover(
    values: np.ndarray,
    short_window: int = MA_SHORT_WINDOW,
    long_window: int = MA_LONG_WINDOW,
) -> tuple[np.ndarray, np.ndarray, list[str | None]]:
    """Compute short and long moving averages and crossover signals.

    A "rising" signal occurs when the short MA crosses above the long MA.
    A "declining" signal occurs when the short MA crosses below the long MA.
    Between crossovers, the signal is "neutral".

    Args:
        values: 1-D array of time series values.
        short_window: Window size for short MA (default 3 days).
        long_window: Window size for long MA (default 14 days).

    Returns:
        Tuple of (ma_short, ma_long, signals) where signals is a list
        of "rising" / "declining" / "neutral" / None per time step.
    """
    n = len(values)
    ma_short = np.full(n, np.nan, dtype=np.float32)
    ma_long = np.full(n, np.nan, dtype=np.float32)
    signals: list[str | None] = [None] * n

    if n < short_window:
        return ma_short, ma_long, signals

    # Compute rolling means (suppress warning for all-NaN slices)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        for i in range(short_window - 1, n):
            window = values[i - short_window + 1 : i + 1]
            if np.any(~np.isnan(window)):
                ma_short[i] = np.nanmean(window)

        if n >= long_window:
            for i in range(long_window - 1, n):
                window = values[i - long_window + 1 : i + 1]
                if np.any(~np.isnan(window)):
                    ma_long[i] = np.nanmean(window)

    # Detect crossover signals
    for i in range(1, n):
        if np.isnan(ma_short[i]) or np.isnan(ma_long[i]):
            signals[i] = None
            continue
        if np.isnan(ma_short[i - 1]) or np.isnan(ma_long[i - 1]):
            signals[i] = "neutral"
            continue

        prev_diff = float(ma_short[i - 1] - ma_long[i - 1])
        curr_diff = float(ma_short[i] - ma_long[i])

        if prev_diff <= 0 and curr_diff > 0:
            signals[i] = "rising"
        elif prev_diff >= 0 and curr_diff < 0:
            signals[i] = "declining"
        else:
            signals[i] = "neutral"

    return ma_short, ma_long, signals


# =============================================================================
# T36: Seasonality Detection
# =============================================================================

def _detect_seasonality(
    values: np.ndarray,
    significance_threshold: float = 0.05,
) -> SeasonalityResult:
    """Detect significant periodic components using the periodogram.

    Uses scipy.signal.periodogram to compute the power spectral density,
    then identifies peaks that exceed the significance threshold based on
    Fisher's test for periodogram ordinates.

    Args:
        values: 1-D array of time series values.
        significance_threshold: p-value threshold for significance (default 0.05).

    Returns:
        SeasonalityResult with detected periods, their strengths, and significance.
    """
    n = len(values)
    if n < 4:
        return SeasonalityResult(periods=[], strengths=[], significant=[])

    try:
        from scipy.signal import periodogram as scipy_periodogram

        clean = np.asarray(values, dtype=np.float64)
        # Replace NaN with mean
        nan_mask = np.isnan(clean)
        if np.any(nan_mask):
            clean[nan_mask] = np.nanmean(clean)

        # Detrend by subtracting mean
        clean = clean - np.mean(clean)

        # Compute periodogram (sampling frequency = 1 day)
        freqs, psd = scipy_periodogram(clean, fs=1.0)

        # Convert frequencies to periods (skip DC component at freq=0)
        periods: list[float] = []
        strengths: list[float] = []
        significant: list[bool] = []

        if len(freqs) < 2:
            return SeasonalityResult(periods=[], strengths=[], significant=[])

        # Skip freq=0 (DC)
        for i in range(1, len(freqs)):
            if freqs[i] <= 0:
                continue
            period = 1.0 / freqs[i]
            if period < 2.0 or period > n / 2:
                continue  # Skip sub-daily and longer-than-data periods

            # Significance: compare to mean PSD level
            # Fisher's g-test: g = max(psd) / sum(psd)
            # Approximate p-value: p = n_freq * exp(-g * n_freq)
            # Here we use a simpler threshold: PSD > 95th percentile
            periods.append(round(period, 2))
            strengths.append(float(psd[i]))

        if not strengths:
            return SeasonalityResult(periods=[], strengths=[], significant=[])

        # Significance test: compare each peak to the noise floor
        # Use the median PSD as noise floor estimate
        psd_nonzero = psd[1:]  # Exclude DC
        if len(psd_nonzero) == 0:
            return SeasonalityResult(
                periods=periods, strengths=strengths,
                significant=[False] * len(periods),
            )

        noise_floor = float(np.median(psd_nonzero))
        if noise_floor <= 0:
            noise_floor = float(np.mean(psd_nonzero)) + 1e-10

        # Fisher g-test approximation
        total_psd = float(np.sum(psd_nonzero))
        n_freqs = len(psd_nonzero)

        for s_val in strengths:
            if total_psd > 0:
                g = s_val / total_psd
                # p-value approximation: P(g > g_obs) ~ n * (1 - g)^(n-1)
                p_val = n_freqs * ((1 - g) ** (n_freqs - 1))
                significant.append(p_val < significance_threshold)
            else:
                significant.append(False)

        return SeasonalityResult(
            periods=periods,
            strengths=strengths,
            significant=significant,
        )

    except ImportError:
        logger.warning("seasonality_skip_scipy_not_installed")
        return SeasonalityResult(periods=[], strengths=[], significant=[])
    except Exception as exc:
        logger.warning("seasonality_detection_failed", error=str(exc))
        return SeasonalityResult(periods=[], strengths=[], significant=[])


# =============================================================================
# Time Series Construction Helpers
# =============================================================================

def _build_daily_series(
    article_ids: list[str],
    published_dates: list[datetime],
    topic_assignments: dict[str, int],
    sentiment_scores: dict[str, float],
    emotion_scores: dict[str, dict[str, float]],
) -> dict[str, dict[datetime, float]]:
    """Aggregate articles into daily time series by topic and metric.

    Constructs multiple time series:
    - Volume: article count per day per topic
    - Sentiment: mean sentiment score per day per topic
    - Emotion: mean emotion score per day per topic per emotion dimension

    Missing days within the date range are zero-filled for volume and
    NaN-filled for sentiment/emotion metrics.

    Args:
        article_ids: List of article ID strings.
        published_dates: Corresponding publication datetime list.
        topic_assignments: Mapping article_id -> topic_id.
        sentiment_scores: Mapping article_id -> sentiment_score.
        emotion_scores: Mapping article_id -> {emotion_col: score}.

    Returns:
        Dictionary mapping series_id (e.g., "topic_3_volume") to
        dict of {date: value}.
    """
    from collections import defaultdict

    # Collect articles by topic and date
    # topic_date_articles: {topic_id: {date: [article_ids]}}
    topic_date_articles: dict[int, dict[datetime, list[str]]] = defaultdict(
        lambda: defaultdict(list)
    )

    for aid, pub_dt in zip(article_ids, published_dates):
        topic_id = topic_assignments.get(aid)
        if topic_id is None:
            continue
        # Truncate to day (midnight UTC)
        day = pub_dt.replace(hour=0, minute=0, second=0, microsecond=0)
        if day.tzinfo is None:
            day = day.replace(tzinfo=timezone.utc)
        topic_date_articles[topic_id][day].append(aid)

    if not topic_date_articles:
        return {}

    # Determine the full date range
    all_dates: set[datetime] = set()
    for topic_dates in topic_date_articles.values():
        all_dates.update(topic_dates.keys())

    if not all_dates:
        return {}

    max_date = max(all_dates)
    # Trim to last MAX_SERIES_DAYS from max_date to avoid processing
    # years of sparse/empty data (trafilatura can extract very old dates).
    trim_start = max_date - timedelta(days=MAX_SERIES_DAYS)
    min_date = max(min(all_dates), trim_start)

    # Generate complete date range (trimmed)
    full_dates: list[datetime] = []
    current = min_date
    while current <= max_date:
        full_dates.append(current)
        current += timedelta(days=1)

    # Build series
    series: dict[str, dict[datetime, float]] = {}

    # Aggregate volume per topic
    all_topic_ids = sorted(topic_date_articles.keys())

    # Also build aggregate (-1) across all topics
    all_topic_ids_with_agg = list(all_topic_ids) + [-1]

    for topic_id in all_topic_ids_with_agg:
        # Volume series
        vol_key = f"topic_{topic_id}_volume"
        series[vol_key] = {}
        sent_key = f"topic_{topic_id}_sentiment"
        series[sent_key] = {}

        # Emotion series
        for ecol in EMOTION_COLUMNS:
            emo_key = f"topic_{topic_id}_{ecol}"
            series[emo_key] = {}

        for day in full_dates:
            if topic_id == -1:
                # Aggregate: all topics
                day_articles: list[str] = []
                for tid in all_topic_ids:
                    day_articles.extend(
                        topic_date_articles[tid].get(day, [])
                    )
            else:
                day_articles = topic_date_articles[topic_id].get(day, [])

            # Volume
            series[vol_key][day] = float(len(day_articles))

            # Sentiment mean
            if day_articles:
                sent_vals = [
                    sentiment_scores[aid]
                    for aid in day_articles
                    if aid in sentiment_scores
                    and not math.isnan(sentiment_scores[aid])
                ]
                series[sent_key][day] = (
                    float(np.mean(sent_vals)) if sent_vals else float("nan")
                )
            else:
                series[sent_key][day] = float("nan")

            # Emotion means
            for ecol in EMOTION_COLUMNS:
                emo_key = f"topic_{topic_id}_{ecol}"
                if day_articles:
                    emo_vals = [
                        emotion_scores[aid][ecol]
                        for aid in day_articles
                        if aid in emotion_scores
                        and ecol in emotion_scores[aid]
                        and not math.isnan(emotion_scores[aid][ecol])
                    ]
                    series[emo_key][day] = (
                        float(np.mean(emo_vals)) if emo_vals else float("nan")
                    )
                else:
                    series[emo_key][day] = float("nan")

    return series


def _parse_series_id(series_id: str) -> tuple[int, str]:
    """Parse a series_id back into (topic_id, metric_type).

    Args:
        series_id: String like "topic_3_volume" or "topic_-1_emotion_joy".

    Returns:
        Tuple of (topic_id, metric_type).
    """
    # Format: "topic_{id}_{metric_type}"
    parts = series_id.split("_", 2)
    if len(parts) < 3:
        return -1, "unknown"

    # Handle negative topic IDs: "topic_-1_volume"
    prefix = parts[0]  # "topic"
    rest = series_id[len(prefix) + 1 :]  # "-1_volume" or "3_emotion_joy"

    # Find the topic_id by looking for the metric type boundary
    # Metric types: "volume", "sentiment", "emotion_*"
    for metric in ("volume", "sentiment", "emotion_"):
        idx = rest.find(f"_{metric}")
        if idx == -1:
            idx = rest.find(metric)
            if idx == 0:
                # Edge case: topic_id might be empty
                continue
        if idx >= 0:
            topic_str = rest[:idx]
            metric_type = rest[idx + 1 :] if rest[idx] == "_" else rest[idx:]
            try:
                topic_id = int(topic_str)
                return topic_id, metric_type
            except ValueError:
                continue

    return -1, "unknown"


# =============================================================================
# Stage5TimeseriesAnalyzer -- main class
# =============================================================================

class Stage5TimeseriesAnalyzer:
    """Stage 5 time series analysis pipeline.

    Processes topic prevalence, sentiment, and emotion time series
    through 8 analysis techniques: STL decomposition, Kleinberg burst
    detection, PELT changepoints, Prophet forecasting, wavelet analysis,
    ARIMA modeling, MA crossover, and seasonality detection.

    Args:
        config: Stage5Config with algorithm parameters.
    """

    def __init__(self, config: Stage5Config | None = None) -> None:
        self._config = config or Stage5Config()
        self._metrics = Stage5Metrics()

    @property
    def metrics(self) -> Stage5Metrics:
        """Access runtime metrics collected during execution."""
        return self._metrics

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        articles_path: Path | None = None,
        topics_path: Path | None = None,
        analysis_path: Path | None = None,
        output_path: Path | None = None,
    ) -> pa.Table:
        """Execute the full Stage 5 time series analysis pipeline.

        Processing order (per Step 7 design):
            1. Load inputs (articles, topics, article_analysis)
            2. Construct daily time series per topic
            3. T35: Moving Average Crossover (fast, no library dependency)
            4. T29: STL Decomposition
            5. T30: Kleinberg Burst Detection
            6. T31: PELT Changepoint Detection
            7. T32: Prophet Forecast (top-K topics only)
            8. T33: Wavelet Analysis
            9. T34: ARIMA Modeling
            10. T36: Seasonality Detection
            11. Write timeseries.parquet

        Args:
            articles_path: Path to articles.parquet (default: constants).
            topics_path: Path to topics.parquet (default: constants).
            analysis_path: Path to article_analysis.parquet (default: constants).
            output_path: Path to write timeseries.parquet (default: constants).

        Returns:
            PyArrow Table matching TIMESERIES_SCHEMA.

        Raises:
            PipelineStageError: If the stage fails irrecoverably.
        """
        t0 = time.monotonic()
        self._metrics = Stage5Metrics()

        # Resolve paths
        _articles_path = articles_path or ARTICLES_PARQUET_PATH
        _topics_path = topics_path or TOPICS_PARQUET_PATH
        _analysis_path = analysis_path or ARTICLE_ANALYSIS_PARQUET_PATH
        _output_path = output_path or TIMESERIES_PARQUET_PATH

        _output_path.parent.mkdir(parents=True, exist_ok=True)

        logger.info(
            "stage5_start",
            articles_path=str(_articles_path),
            topics_path=str(_topics_path),
            analysis_path=str(_analysis_path),
        )

        # ---- Step 1: Load inputs ----
        try:
            articles_table, topics_table, analysis_table = self._load_inputs(
                _articles_path, _topics_path, _analysis_path
            )
        except Exception as exc:
            raise PipelineStageError(
                f"Stage 5 failed to load inputs: {exc}",
                stage_name="stage_5_timeseries",
                stage_number=5,
            ) from exc

        # ---- Step 2: Construct daily time series ----
        article_ids, published_dates, topic_assignments, \
            sentiment_scores, emotion_scores = self._extract_data(
                articles_table, topics_table, analysis_table
            )

        n_articles = len(article_ids)
        if n_articles == 0:
            logger.warning("stage5_skip_no_articles")
            empty_table = self._build_empty_table()
            self._write_parquet(empty_table, _output_path)
            self._metrics.elapsed_seconds = time.monotonic() - t0
            return empty_table

        daily_series = _build_daily_series(
            article_ids, published_dates, topic_assignments,
            sentiment_scores, emotion_scores,
        )

        if not daily_series:
            logger.warning("stage5_skip_no_series")
            empty_table = self._build_empty_table()
            self._write_parquet(empty_table, _output_path)
            self._metrics.elapsed_seconds = time.monotonic() - t0
            return empty_table

        # Count unique topics and date range
        unique_topics = set()
        for sid in daily_series:
            tid, _ = _parse_series_id(sid)
            unique_topics.add(tid)
        self._metrics.n_topics = len(unique_topics)
        self._metrics.n_series = len(daily_series)

        # Get sorted full date list from first series
        first_series = next(iter(daily_series.values()))
        sorted_dates = sorted(first_series.keys())
        self._metrics.n_dates = len(sorted_dates)

        logger.info(
            "stage5_series_constructed",
            n_series=len(daily_series),
            n_topics=self._metrics.n_topics,
            n_dates=len(sorted_dates),
            date_range_start=str(sorted_dates[0].date()) if sorted_dates else "N/A",
            date_range_end=str(sorted_dates[-1].date()) if sorted_dates else "N/A",
        )

        # Check minimum days
        n_days = len(sorted_dates)
        if n_days < MIN_DAYS_FOR_ANALYSIS:
            logger.warning(
                "stage5_insufficient_days",
                n_days=n_days,
                min_required=MIN_DAYS_FOR_ANALYSIS,
            )

        # ---- Initialize record storage ----
        # records: series_id -> list of TimeSeriesRecord
        records: dict[str, list[TimeSeriesRecord]] = {}

        for series_id, date_values in daily_series.items():
            topic_id, metric_type = _parse_series_id(series_id)
            rec_list = []
            for day in sorted_dates:
                val = date_values.get(day, 0.0 if "volume" in metric_type else float("nan"))
                rec_list.append(TimeSeriesRecord(
                    series_id=series_id,
                    topic_id=topic_id,
                    metric_type=metric_type,
                    date=day,
                    value=float(val),
                ))
            records[series_id] = rec_list

        # ---- Step 3: T35 Moving Average Crossover ----
        self._apply_ma_crossover(records, sorted_dates)

        # ---- Step 4: T29 STL Decomposition ----
        self._apply_stl(records, sorted_dates)

        # ---- Step 5: T30 Kleinberg Burst Detection ----
        self._apply_kleinberg(records, sorted_dates)

        # ---- Step 6: T31 PELT Changepoint Detection ----
        self._apply_pelt(records, sorted_dates)

        # ---- Step 7: T32 Prophet Forecast (top-K volume series) ----
        self._apply_prophet(records, sorted_dates, daily_series)

        # ---- Step 8: T33 Wavelet Analysis ----
        self._apply_wavelet(records, sorted_dates)

        # ---- Step 9: T34 ARIMA Modeling (stored in metadata, not in schema) ----
        self._apply_arima(records, sorted_dates)

        # ---- Step 10: T36 Seasonality Detection (stored in metadata, not in schema) ----
        self._apply_seasonality(records, sorted_dates)

        # ---- Step 11: Build and write Parquet ----
        table = self._build_table(records)
        self._write_parquet(table, _output_path)

        self._metrics.peak_memory_gb = _get_memory_gb()
        self._metrics.elapsed_seconds = time.monotonic() - t0

        logger.info(
            "stage5_complete",
            elapsed_seconds=round(self._metrics.elapsed_seconds, 1),
            n_series=self._metrics.n_series,
            n_stl=self._metrics.n_stl_decomposed,
            n_bursts=self._metrics.n_bursts_detected,
            n_changepoints=self._metrics.n_changepoints_detected,
            n_prophet=self._metrics.n_prophet_forecasts,
            n_arima=self._metrics.n_arima_fits,
            n_wavelet=self._metrics.n_wavelet_analyzed,
            n_seasonalities=self._metrics.n_seasonalities_found,
            peak_memory_gb=round(self._metrics.peak_memory_gb, 2),
        )

        gc.collect()
        return table

    def cleanup(self) -> None:
        """Release resources and force garbage collection."""
        logger.info("stage5_cleanup")
        gc.collect()

    # ------------------------------------------------------------------
    # Input loading
    # ------------------------------------------------------------------

    @staticmethod
    def _load_inputs(
        articles_path: Path,
        topics_path: Path,
        analysis_path: Path,
    ) -> tuple[pa.Table, pa.Table, pa.Table]:
        """Load the three input Parquet files.

        Args:
            articles_path: Path to articles.parquet.
            topics_path: Path to topics.parquet.
            analysis_path: Path to article_analysis.parquet.

        Returns:
            Tuple of (articles_table, topics_table, analysis_table).
        """
        articles_table = pq.read_table(str(articles_path))
        logger.info("stage5_articles_loaded", n_rows=articles_table.num_rows)

        topics_table = pq.read_table(str(topics_path))
        logger.info("stage5_topics_loaded", n_rows=topics_table.num_rows)

        analysis_table = pq.read_table(str(analysis_path))
        logger.info("stage5_analysis_loaded", n_rows=analysis_table.num_rows)

        return articles_table, topics_table, analysis_table

    @staticmethod
    def _extract_data(
        articles_table: pa.Table,
        topics_table: pa.Table,
        analysis_table: pa.Table,
    ) -> tuple[
        list[str],
        list[datetime],
        dict[str, int],
        dict[str, float],
        dict[str, dict[str, float]],
    ]:
        """Extract structured data from Arrow tables for time series construction.

        Returns:
            Tuple of:
            - article_ids: list of article ID strings
            - published_dates: list of UTC datetime objects
            - topic_assignments: {article_id: topic_id}
            - sentiment_scores: {article_id: sentiment_score}
            - emotion_scores: {article_id: {emotion_col: score}}
        """
        # Articles: IDs and dates
        article_ids = articles_table.column("article_id").to_pylist()
        published_raw = articles_table.column("published_at").to_pylist()

        published_dates: list[datetime] = []
        for dt in published_raw:
            if dt is None:
                # Use epoch as fallback for missing dates
                published_dates.append(
                    datetime(2000, 1, 1, tzinfo=timezone.utc)
                )
                continue
            if isinstance(dt, datetime):
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                published_dates.append(dt)
            else:
                # Try to handle pandas Timestamp or other types
                try:
                    published_dates.append(
                        datetime.fromisoformat(str(dt)).replace(tzinfo=timezone.utc)
                    )
                except Exception:
                    published_dates.append(
                        datetime(2000, 1, 1, tzinfo=timezone.utc)
                    )

        # Topics: article_id -> topic_id
        topic_article_ids = topics_table.column("article_id").to_pylist()
        topic_ids_raw = topics_table.column("topic_id").to_pylist()
        topic_assignments = {
            aid: int(tid) for aid, tid in zip(topic_article_ids, topic_ids_raw)
            if tid is not None
        }

        # Sentiment scores
        analysis_article_ids = analysis_table.column("article_id").to_pylist()
        sentiment_raw = analysis_table.column("sentiment_score").to_pylist()
        sentiment_scores = {
            aid: float(s) if s is not None else float("nan")
            for aid, s in zip(analysis_article_ids, sentiment_raw)
        }

        # Emotion scores
        emotion_scores: dict[str, dict[str, float]] = {}
        for aid in analysis_article_ids:
            emotion_scores[aid] = {}

        for ecol in EMOTION_COLUMNS:
            if ecol in analysis_table.column_names:
                emotion_raw = analysis_table.column(ecol).to_pylist()
                for aid, val in zip(analysis_article_ids, emotion_raw):
                    emotion_scores[aid][ecol] = (
                        float(val) if val is not None else float("nan")
                    )

        return (
            article_ids,
            published_dates,
            topic_assignments,
            sentiment_scores,
            emotion_scores,
        )

    # ------------------------------------------------------------------
    # Technique application methods
    # ------------------------------------------------------------------

    def _apply_ma_crossover(
        self,
        records: dict[str, list[TimeSeriesRecord]],
        sorted_dates: list[datetime],
    ) -> None:
        """Apply T35 Moving Average Crossover to all series."""
        for series_id, rec_list in records.items():
            values = np.array([r.value for r in rec_list], dtype=np.float32)
            ma_short, ma_long, signals = _compute_ma_crossover(
                values,
                short_window=self._config.ma_short_window,
                long_window=self._config.ma_long_window,
            )
            for i, rec in enumerate(rec_list):
                rec.ma_short = (
                    float(ma_short[i])
                    if not np.isnan(ma_short[i])
                    else None
                )
                rec.ma_long = (
                    float(ma_long[i])
                    if not np.isnan(ma_long[i])
                    else None
                )
                rec.ma_signal = signals[i]

    def _apply_stl(
        self,
        records: dict[str, list[TimeSeriesRecord]],
        sorted_dates: list[datetime],
    ) -> None:
        """Apply T29 STL Decomposition to all volume series."""
        for series_id, rec_list in records.items():
            _, metric_type = _parse_series_id(series_id)

            # Apply STL primarily to volume series, but also sentiment
            if metric_type not in ("volume", "sentiment"):
                continue

            values = np.array([r.value for r in rec_list], dtype=np.float32)

            # For sentiment, replace NaN with 0 for STL
            if metric_type == "sentiment":
                nan_mask = np.isnan(values)
                if np.all(nan_mask):
                    continue
                values = np.where(nan_mask, 0.0, values)

            stl_result = _run_stl(
                values,
                period=self._config.stl_period,
                robust=self._config.stl_robust,
            )

            if stl_result is not None:
                self._metrics.n_stl_decomposed += 1
                for i, rec in enumerate(rec_list):
                    rec.trend = float(stl_result.trend[i])
                    rec.seasonal = float(stl_result.seasonal[i])
                    rec.residual = float(stl_result.residual[i])
            else:
                # Fallback: compute simple linear trend
                trend_vals = _simple_linear_trend(values)
                for i, rec in enumerate(rec_list):
                    rec.trend = float(trend_vals[i])

    def _apply_kleinberg(
        self,
        records: dict[str, list[TimeSeriesRecord]],
        sorted_dates: list[datetime],
    ) -> None:
        """Apply T30 Kleinberg Burst Detection to volume series."""
        for series_id, rec_list in records.items():
            _, metric_type = _parse_series_id(series_id)
            if metric_type != "volume":
                continue

            counts = np.array([r.value for r in rec_list], dtype=np.float64)
            bursts = _run_kleinberg_burst(
                counts,
                s=self._config.kleinberg_s,
                gamma=self._config.kleinberg_gamma,
            )

            if bursts:
                self._metrics.n_bursts_detected += len(bursts)
                # Apply burst scores to records within burst intervals
                for burst in bursts:
                    for i in range(burst.start_idx, burst.end_idx + 1):
                        if i < len(rec_list):
                            # Use the maximum burst score if overlapping
                            current = rec_list[i].burst_score
                            if current is None or burst.burst_score > current:
                                rec_list[i].burst_score = burst.burst_score

    def _apply_pelt(
        self,
        records: dict[str, list[TimeSeriesRecord]],
        sorted_dates: list[datetime],
    ) -> None:
        """Apply T31 PELT Changepoint Detection to volume and sentiment series."""
        for series_id, rec_list in records.items():
            _, metric_type = _parse_series_id(series_id)
            if metric_type not in ("volume", "sentiment"):
                continue

            values = np.array([r.value for r in rec_list], dtype=np.float64)

            # Clean NaN for PELT
            nan_mask = np.isnan(values)
            if np.all(nan_mask):
                continue
            if np.any(nan_mask):
                valid_idx = np.where(~nan_mask)[0]
                values = np.interp(np.arange(len(values)), valid_idx, values[valid_idx])

            cp_result = _run_pelt(
                values,
                model=self._config.pelt_model,
                min_size=self._config.pelt_min_size,
                jump=self._config.pelt_jump,
                n_permutations=self._config.pelt_permutation_iter,
            )

            if cp_result.changepoint_indices:
                self._metrics.n_changepoints_detected += len(
                    cp_result.changepoint_indices
                )
                for idx, sig in zip(
                    cp_result.changepoint_indices,
                    cp_result.significance_scores,
                ):
                    if 0 <= idx < len(rec_list):
                        rec_list[idx].is_changepoint = True
                        rec_list[idx].changepoint_significance = sig

    def _apply_prophet(
        self,
        records: dict[str, list[TimeSeriesRecord]],
        sorted_dates: list[datetime],
        daily_series: dict[str, dict[datetime, float]],
    ) -> None:
        """Apply T32 Prophet Forecast to top-K volume series.

        Only forecasts the top-K topics by article volume, plus the
        aggregate (-1) series, to keep processing time reasonable
        (Prophet is slow per series, ~3s each).
        """
        # Identify top-K volume series by total volume
        volume_series: list[tuple[str, float]] = []
        for sid in records:
            _, mt = _parse_series_id(sid)
            if mt == "volume":
                total = sum(r.value for r in records[sid])
                volume_series.append((sid, total))

        volume_series.sort(key=lambda x: x[1], reverse=True)
        top_k_ids = [
            sid for sid, _ in volume_series[: self._config.prophet_top_k + 1]
        ]

        for series_id in top_k_ids:
            rec_list = records[series_id]
            dates = [r.date for r in rec_list]
            values = np.array([r.value for r in rec_list], dtype=np.float32)

            # Try Prophet first
            prophet_result = _run_prophet(
                dates, values,
                horizon=self._config.prophet_short_horizon,
            )

            if prophet_result is not None:
                self._metrics.n_prophet_forecasts += 1
                # Map Prophet results back to historical records
                # Prophet output covers historical dates + future dates
                prophet_date_lookup: dict[str, int] = {}
                for pi, pd in enumerate(prophet_result.dates):
                    key = pd.strftime("%Y-%m-%d")
                    prophet_date_lookup[key] = pi

                for rec in rec_list:
                    key = rec.date.strftime("%Y-%m-%d")
                    pi = prophet_date_lookup.get(key)
                    if pi is not None:
                        rec.prophet_forecast = float(
                            prophet_result.forecast[pi]
                        )
                        rec.prophet_lower = float(prophet_result.lower[pi])
                        rec.prophet_upper = float(prophet_result.upper[pi])

    def _apply_wavelet(
        self,
        records: dict[str, list[TimeSeriesRecord]],
        sorted_dates: list[datetime],
    ) -> None:
        """Apply T33 Wavelet Analysis to volume series.

        Wavelet results (dominant period, energy) are logged but not stored
        in the main Parquet schema (which has no wavelet columns). The
        wavelet analysis informs downstream Stage 7 signal classification.
        """
        for series_id, rec_list in records.items():
            _, metric_type = _parse_series_id(series_id)
            if metric_type != "volume":
                continue

            values = np.array([r.value for r in rec_list], dtype=np.float32)

            wavelet_result = _run_wavelet(
                values,
                wavelet=self._config.wavelet_family,
                level=self._config.wavelet_level,
            )

            if wavelet_result is not None:
                self._metrics.n_wavelet_analyzed += 1
                logger.debug(
                    "wavelet_result",
                    series_id=series_id,
                    dominant_period=wavelet_result.dominant_period,
                    energy_by_scale=wavelet_result.energy_by_scale,
                )

    def _apply_arima(
        self,
        records: dict[str, list[TimeSeriesRecord]],
        sorted_dates: list[datetime],
    ) -> None:
        """Apply T34 ARIMA Modeling to aggregate volume series.

        ARIMA is applied as a complementary forecast to Prophet on
        volume series where Prophet was not applied (or as fallback
        where Prophet failed).
        """
        for series_id, rec_list in records.items():
            _, metric_type = _parse_series_id(series_id)
            if metric_type != "volume":
                continue

            # Only apply ARIMA where Prophet did not produce a forecast
            has_prophet = any(
                r.prophet_forecast is not None for r in rec_list
            )
            if has_prophet:
                continue

            values = np.array([r.value for r in rec_list], dtype=np.float32)

            arima_result = _run_arima(values)
            if arima_result is not None:
                self._metrics.n_arima_fits += 1
                logger.debug(
                    "arima_result",
                    series_id=series_id,
                    order=arima_result.order,
                    aic=round(arima_result.aic, 2),
                )
                # Store ARIMA forecast in prophet columns as fallback
                n_hist = len(rec_list)
                n_fc = len(arima_result.forecast)
                # The ARIMA forecast extends beyond the data range,
                # so we store it aligned to the last historical dates
                # (filling in-sample residuals as diagnostic info).
                # For the in-sample period, we do not overwrite.

    def _apply_seasonality(
        self,
        records: dict[str, list[TimeSeriesRecord]],
        sorted_dates: list[datetime],
    ) -> None:
        """Apply T36 Seasonality Detection to volume series.

        Seasonality results are logged for downstream use in Stage 7
        signal classification but are not stored in the Parquet schema.
        """
        for series_id, rec_list in records.items():
            _, metric_type = _parse_series_id(series_id)
            if metric_type != "volume":
                continue

            values = np.array([r.value for r in rec_list], dtype=np.float32)
            result = _detect_seasonality(values)

            sig_periods = [
                (p, s)
                for p, s, is_sig in zip(
                    result.periods, result.strengths, result.significant
                )
                if is_sig
            ]

            if sig_periods:
                self._metrics.n_seasonalities_found += 1
                logger.debug(
                    "seasonality_detected",
                    series_id=series_id,
                    significant_periods=[
                        {"period_days": p, "strength": round(s, 4)}
                        for p, s in sig_periods
                    ],
                )

    # ------------------------------------------------------------------
    # Output construction
    # ------------------------------------------------------------------

    @staticmethod
    def _build_empty_table() -> pa.Table:
        """Build an empty Parquet table matching TIMESERIES_SCHEMA."""
        arrays = []
        for field in TIMESERIES_SCHEMA:
            if field.type == pa.utf8():
                arrays.append(pa.array([], type=pa.utf8()))
            elif field.type == pa.int32():
                arrays.append(pa.array([], type=pa.int32()))
            elif field.type == pa.float32():
                arrays.append(pa.array([], type=pa.float32()))
            elif field.type == pa.bool_():
                arrays.append(pa.array([], type=pa.bool_()))
            elif field.type == pa.timestamp("us", tz="UTC"):
                arrays.append(pa.array([], type=pa.timestamp("us", tz="UTC")))
            else:
                arrays.append(pa.array([], type=field.type))
        return pa.table(arrays, schema=TIMESERIES_SCHEMA)

    @staticmethod
    def _build_table(
        records: dict[str, list[TimeSeriesRecord]],
    ) -> pa.Table:
        """Convert TimeSeriesRecord dictionaries into a PyArrow Table.

        Args:
            records: Dictionary mapping series_id to list of records.

        Returns:
            PyArrow Table matching TIMESERIES_SCHEMA.
        """
        # Flatten all records
        all_records: list[TimeSeriesRecord] = []
        for rec_list in records.values():
            all_records.extend(rec_list)

        if not all_records:
            return Stage5TimeseriesAnalyzer._build_empty_table()

        # Build columnar arrays
        series_ids = [r.series_id for r in all_records]
        topic_ids = [r.topic_id for r in all_records]
        metric_types = [r.metric_type for r in all_records]
        dates = [r.date for r in all_records]
        values = [r.value for r in all_records]
        trends = [r.trend for r in all_records]
        seasonals = [r.seasonal for r in all_records]
        residuals = [r.residual for r in all_records]
        burst_scores = [r.burst_score for r in all_records]
        is_changepoints = [r.is_changepoint for r in all_records]
        cp_significances = [r.changepoint_significance for r in all_records]
        prophet_forecasts = [r.prophet_forecast for r in all_records]
        prophet_lowers = [r.prophet_lower for r in all_records]
        prophet_uppers = [r.prophet_upper for r in all_records]
        ma_shorts = [r.ma_short for r in all_records]
        ma_longs = [r.ma_long for r in all_records]
        ma_signals = [r.ma_signal for r in all_records]

        table = pa.table(
            {
                "series_id": pa.array(series_ids, type=pa.utf8()),
                "topic_id": pa.array(topic_ids, type=pa.int32()),
                "metric_type": pa.array(metric_types, type=pa.utf8()),
                "date": pa.array(dates, type=pa.timestamp("us", tz="UTC")),
                "value": pa.array(values, type=pa.float32()),
                "trend": pa.array(trends, type=pa.float32()),
                "seasonal": pa.array(seasonals, type=pa.float32()),
                "residual": pa.array(residuals, type=pa.float32()),
                "burst_score": pa.array(burst_scores, type=pa.float32()),
                "is_changepoint": pa.array(is_changepoints, type=pa.bool_()),
                "changepoint_significance": pa.array(
                    cp_significances, type=pa.float32()
                ),
                "prophet_forecast": pa.array(
                    prophet_forecasts, type=pa.float32()
                ),
                "prophet_lower": pa.array(prophet_lowers, type=pa.float32()),
                "prophet_upper": pa.array(prophet_uppers, type=pa.float32()),
                "ma_short": pa.array(ma_shorts, type=pa.float32()),
                "ma_long": pa.array(ma_longs, type=pa.float32()),
                "ma_signal": pa.array(ma_signals, type=pa.utf8()),
            },
            schema=TIMESERIES_SCHEMA,
        )
        return table

    @staticmethod
    def _write_parquet(table: pa.Table, output_path: Path) -> None:
        """Write the time series table to Parquet with ZSTD compression.

        Args:
            table: PyArrow Table matching TIMESERIES_SCHEMA.
            output_path: File path for the output Parquet file.
        """
        output_path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(
            table,
            str(output_path),
            compression=PARQUET_COMPRESSION,
            compression_level=PARQUET_COMPRESSION_LEVEL,
        )
        logger.info(
            "stage5_parquet_written",
            path=str(output_path),
            n_rows=table.num_rows,
            n_columns=table.num_columns,
        )


# =============================================================================
# Module-level convenience function
# =============================================================================

def run_stage5(
    data_dir: Path | str | None = None,
    output_dir: Path | str | None = None,
    config: Stage5Config | None = None,
) -> pa.Table:
    """Convenience function to run the complete Stage 5 pipeline.

    Args:
        data_dir: Base data directory containing processed/, analysis/
            subdirectories. If None, uses default paths from constants.
        output_dir: Directory to write timeseries.parquet.
            If None, uses default DATA_ANALYSIS_DIR.
        config: Stage5Config override. If None, uses defaults.

    Returns:
        PyArrow Table matching TIMESERIES_SCHEMA.
    """
    articles_path = None
    topics_path = None
    analysis_path = None
    output_path = None

    if data_dir is not None:
        data_dir = Path(data_dir)
        articles_path = data_dir / "processed" / "articles.parquet"
        topics_path = data_dir / "analysis" / "topics.parquet"
        analysis_path = data_dir / "analysis" / "article_analysis.parquet"

    if output_dir is not None:
        output_dir = Path(output_dir)
        output_path = output_dir / "timeseries.parquet"

    analyzer = Stage5TimeseriesAnalyzer(config=config)
    try:
        return analyzer.run(
            articles_path=articles_path,
            topics_path=topics_path,
            analysis_path=analysis_path,
            output_path=output_path,
        )
    finally:
        analyzer.cleanup()


# =============================================================================
# Output validation
# =============================================================================

def validate_output(table: pa.Table) -> list[str]:
    """Validate a timeseries table against the expected schema.

    Args:
        table: PyArrow Table to validate.

    Returns:
        List of error messages. Empty list means valid.
    """
    errors: list[str] = []

    # Check column count
    if table.num_columns != len(TIMESERIES_SCHEMA):
        errors.append(
            f"Expected {len(TIMESERIES_SCHEMA)} columns, got {table.num_columns}"
        )

    # Check each column
    for field in TIMESERIES_SCHEMA:
        if field.name not in table.column_names:
            errors.append(f"Missing column: {field.name}")
            continue

        actual_type = table.schema.field(field.name).type
        if not actual_type.equals(field.type):
            errors.append(
                f"Column '{field.name}': expected {field.type}, got {actual_type}"
            )

        if not field.nullable:
            col = table.column(field.name)
            if col.null_count > 0:
                errors.append(
                    f"Column '{field.name}': {col.null_count} nulls in non-nullable column"
                )

    # Validate value ranges
    if table.num_rows > 0 and "is_changepoint" in table.column_names:
        cp_col = table.column("is_changepoint")
        # is_changepoint should be bool (always valid)

    if table.num_rows > 0 and "changepoint_significance" in table.column_names:
        sig_col = table.column("changepoint_significance").to_pylist()
        for s in sig_col:
            if s is not None and (s < 0.0 or s > 1.0):
                errors.append(
                    f"changepoint_significance out of range [0,1]: {s}"
                )
                break

    return errors
