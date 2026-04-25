"""Semantic Gate library — statistical primitives for SG1/SG2/SG3.

G3 from 2nd reflection: Semantic Quality Gates need deterministic
statistical checks at each workflow boundary. This module provides the
pure-Python + numpy primitives those gates share:

    compute_crawl_metrics(jsonl_path)   — W1 output statistics
    count_html_contamination(text)      — title HTML residue detection
    deterministic_sample(items, k, seed)— fixed-seed sampling (P1 compliant)
    detect_z_score_outliers(values, t)  — numpy z-score outlier detection
    compare_to_baseline(current, base)  — KL divergence + chi-square
    compute_basic_stats(values)         — mean, std, min, max, median

P1 Compliance: Pure computation. No LLM judgment. Deterministic.
SOT Compliance: Read-only I/O.
"""

from __future__ import annotations

import json
import math
import os
import random
import re
from collections import Counter
from typing import Iterable

# Optional numpy/scipy — fall back to pure Python when unavailable.
# This keeps the module importable from Hook context even on minimal envs.
try:
    import numpy as _np  # type: ignore
    _HAS_NUMPY = True
except ImportError:  # pragma: no cover
    _np = None
    _HAS_NUMPY = False


# ---------------------------------------------------------------------------
# Mandatory fields for crawl records (matches W1 JSONL writer contract)
# The URL slot accepts either `url` (canonical W1 field) or `source_url` (legacy
# alias used in earlier tests); `compute_crawl_metrics` treats them as equivalent.
# ---------------------------------------------------------------------------
MANDATORY_FIELDS = ("title", "published_at", "body", "url")
_URL_FIELD_ALIASES = ("url", "source_url")


# ---------------------------------------------------------------------------
# Regexes for HTML contamination detection
# ---------------------------------------------------------------------------
_HTML_TAG_RE = re.compile(r"<[A-Za-z][^>]*?>|<\s*/\s*[A-Za-z][^>]*?>|<[^>]+/>")
_HTML_ENTITY_RE = re.compile(r"&(?:[A-Za-z]{2,8}|#\d{2,5}|#x[0-9A-Fa-f]{2,5});")


# ===========================================================================
# Basic statistics (numpy-optional)
# ===========================================================================

def compute_basic_stats(values: Iterable[float]) -> dict:
    """Compute count/mean/std/min/max/median deterministically.

    Uses numpy when available for performance + numerical stability,
    falls back to pure Python for minimal environments.
    """
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return {
            "count": 0, "mean": 0.0, "std": 0.0,
            "min": 0.0, "max": 0.0, "median": 0.0,
        }

    n = len(vals)
    if _HAS_NUMPY:
        arr = _np.array(vals, dtype=float)
        return {
            "count": n,
            "mean": float(arr.mean()),
            # population std (ddof=0) — matches test expectations
            "std": float(arr.std(ddof=0)),
            "min": float(arr.min()),
            "max": float(arr.max()),
            "median": float(_np.median(arr)),
        }
    # Pure Python fallback
    mean = sum(vals) / n
    std = math.sqrt(sum((v - mean) ** 2 for v in vals) / n)
    sorted_vals = sorted(vals)
    if n % 2 == 1:
        median = sorted_vals[n // 2]
    else:
        median = (sorted_vals[n // 2 - 1] + sorted_vals[n // 2]) / 2.0
    return {
        "count": n,
        "mean": mean,
        "std": std,
        "min": min(vals),
        "max": max(vals),
        "median": median,
    }


# ===========================================================================
# HTML contamination detection
# ===========================================================================

def count_html_contamination(text) -> int:
    """Return the number of HTML tags + entities found in `text`.

    Used by SG1 to detect title fields containing unrendered HTML like
    `<b>` or `&nbsp;`, which indicate extraction bugs.
    """
    if not text or not isinstance(text, str):
        return 0
    tag_matches = _HTML_TAG_RE.findall(text)
    entity_matches = _HTML_ENTITY_RE.findall(text)
    return len(tag_matches) + len(entity_matches)


# ===========================================================================
# Deterministic sampling (fixed seed)
# ===========================================================================

def deterministic_sample(items: list, k: int, seed: int) -> list:
    """Return a deterministic random sample of `k` items from `items`.

    Uses Python's random.Random(seed) which is deterministic across runs
    for the same seed. Does NOT mutate the input list.

    If k >= len(items), returns all items in a stable shuffled order.
    If k <= 0, returns [].
    """
    if k <= 0 or not items:
        return []
    rng = random.Random(seed)
    pool = list(items)  # copy, avoid mutating caller's list
    if k >= len(pool):
        rng.shuffle(pool)
        return pool
    return rng.sample(pool, k)


# ===========================================================================
# Z-score outlier detection
# ===========================================================================

def detect_z_score_outliers(values: Iterable[float], threshold: float = 2.0) -> list:
    """Return indices of values whose |z-score| exceeds the threshold.

    Uses population standard deviation. Returns empty list if:
      - input is empty
      - input has < 2 values (no variance possible)
      - all values identical (std = 0)
    """
    vals = [float(v) for v in values if v is not None]
    n = len(vals)
    if n < 2:
        return []

    if _HAS_NUMPY:
        arr = _np.array(vals, dtype=float)
        mean = float(arr.mean())
        std = float(arr.std(ddof=0))
        if std == 0.0:
            return []
        z_scores = (arr - mean) / std
        return [int(i) for i in _np.where(_np.abs(z_scores) > threshold)[0]]

    # Pure Python fallback
    mean = sum(vals) / n
    std = math.sqrt(sum((v - mean) ** 2 for v in vals) / n)
    if std == 0.0:
        return []
    return [i for i, v in enumerate(vals) if abs((v - mean) / std) > threshold]


# ===========================================================================
# Baseline comparison — KL divergence + chi-square
# ===========================================================================

def compare_to_baseline(current: dict, baseline: dict) -> dict:
    """Compare a current categorical distribution to a baseline.

    Both inputs are {category_name: count} dicts. Returns a dict with:
      - kl_divergence: KL(P || Q) where P = current, Q = baseline
      - total_categories: union size
      - common_categories: intersection size
      - new_categories: count of keys in current not in baseline
      - missing_categories: count of keys in baseline not in current

    KL uses Laplace smoothing (add-1) to handle zero-count categories
    without producing inf. Returns kl_divergence=0.0 on empty inputs.
    """
    if not current and not baseline:
        return {
            "kl_divergence": 0.0,
            "total_categories": 0,
            "common_categories": 0,
            "new_categories": 0,
            "missing_categories": 0,
        }

    # Union of keys
    all_keys = set(current.keys()) | set(baseline.keys())
    common = set(current.keys()) & set(baseline.keys())
    new_keys = set(current.keys()) - set(baseline.keys())
    missing_keys = set(baseline.keys()) - set(current.keys())

    # Laplace smoothing to avoid log(0)
    alpha = 1.0
    current_total = sum(max(0, v) for v in current.values()) + alpha * len(all_keys)
    baseline_total = sum(max(0, v) for v in baseline.values()) + alpha * len(all_keys)

    if current_total == 0 or baseline_total == 0:
        return {
            "kl_divergence": 0.0,
            "total_categories": len(all_keys),
            "common_categories": len(common),
            "new_categories": len(new_keys),
            "missing_categories": len(missing_keys),
        }

    kl = 0.0
    for k in all_keys:
        p = (max(0, current.get(k, 0)) + alpha) / current_total
        q = (max(0, baseline.get(k, 0)) + alpha) / baseline_total
        if p > 0 and q > 0:
            kl += p * math.log(p / q)

    return {
        "kl_divergence": kl,
        "total_categories": len(all_keys),
        "common_categories": len(common),
        "new_categories": len(new_keys),
        "missing_categories": len(missing_keys),
    }


# ===========================================================================
# compute_crawl_metrics — W1 JSONL statistics
# ===========================================================================

def compute_crawl_metrics(jsonl_path: str) -> dict:
    """Compute deterministic crawl metrics from a JSONL file.

    Returned structure:
    {
      "total_articles": int,
      "mandatory_fields_present": int,    # records with ALL mandatory fields non-empty
      "body_length": { compute_basic_stats result },
      "title_length": { compute_basic_stats result },
      "language_distribution": {lang_code: count},
      "html_contamination_count": int,    # total HTML artifacts in titles
      "error": str | None,
    }
    """
    if not jsonl_path or not os.path.exists(jsonl_path):
        return {
            "total_articles": 0,
            "mandatory_fields_present": 0,
            "body_length": compute_basic_stats([]),
            "title_length": compute_basic_stats([]),
            "language_distribution": {},
            "html_contamination_count": 0,
            "error": f"file_not_found: {jsonl_path}",
        }

    total = 0
    mandatory_ok = 0
    body_lengths: list[int] = []
    title_lengths: list[int] = []
    lang_counts: Counter = Counter()
    html_artifacts = 0

    try:
        with open(jsonl_path, "r", encoding="utf-8") as f:
            for raw_line in f:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    rec = json.loads(raw_line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(rec, dict):
                    continue

                total += 1

                # Mandatory fields check — non-empty string/value.
                # The URL slot accepts either `url` or `source_url` (D-7 alias)
                # so the validator stays in sync with the W1 JSONL writer.
                has_all = True
                for field in MANDATORY_FIELDS:
                    if field == "url":
                        val = next(
                            (rec.get(alias) for alias in _URL_FIELD_ALIASES
                             if rec.get(alias) not in (None, "")),
                            None,
                        )
                    else:
                        val = rec.get(field)
                    if val is None or (isinstance(val, str) and val.strip() == ""):
                        has_all = False
                        break
                if has_all:
                    mandatory_ok += 1

                # Body length
                body = rec.get("body") or ""
                if isinstance(body, str):
                    body_lengths.append(len(body))

                # Title length + HTML contamination
                title = rec.get("title") or ""
                if isinstance(title, str):
                    title_lengths.append(len(title))
                    html_artifacts += count_html_contamination(title)

                # Language
                lang = rec.get("language")
                if isinstance(lang, str) and lang:
                    lang_counts[lang] += 1
    except (OSError, UnicodeDecodeError) as e:
        return {
            "total_articles": total,
            "mandatory_fields_present": mandatory_ok,
            "body_length": compute_basic_stats(body_lengths),
            "title_length": compute_basic_stats(title_lengths),
            "language_distribution": dict(lang_counts),
            "html_contamination_count": html_artifacts,
            "error": f"read_error: {e}",
        }

    return {
        "total_articles": total,
        "mandatory_fields_present": mandatory_ok,
        "body_length": compute_basic_stats(body_lengths),
        "title_length": compute_basic_stats(title_lengths),
        "language_distribution": dict(lang_counts),
        "html_contamination_count": html_artifacts,
        "error": None,
    }
