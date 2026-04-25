"""Geopolitical Tension Index (GTI) — Q05·Q06·Q07 합성 지수.

GTI = 3개 신호의 가중 합성:
  G1 (40%)  — 국가별 보도 집중도 편차 (Q06)
  G2 (35%)  — 국가 감성 / 보도량 변화율 (Q05)
  G3 (25%)  — 양국 긴장 신호 강도 (Q07)

0~100 척도. 임계값:
  < 30 : LOW    (낮은 지정학적 긴장)
  30-60: MEDIUM (보통 긴장)
  60-80: HIGH   (높은 긴장)
  > 80 : CRITICAL (심각)

출력:
  data/gti/{date}/gti_daily.json   — 날짜별 GTI 값
  data/gti/gti_history.jsonl       — 시계열 히스토리
"""

from __future__ import annotations

import json
import logging
import math
import time
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_SCORE_LABELS = {(0, 30): "LOW", (30, 60): "MEDIUM", (60, 80): "HIGH", (80, 101): "CRITICAL"}
_COLOR_MAP = {"LOW": "#2ecc71", "MEDIUM": "#f1c40f", "HIGH": "#e67e22", "CRITICAL": "#e74c3c"}

# Geopolitical hotspot countries (ISO2) — amplify signals from these
_HOTSPOT_COUNTRIES = frozenset({
    "US", "CN", "RU", "IR", "KP", "UA", "IL", "PS", "SY", "IQ",
    "SA", "PK", "IN", "TW", "KR", "TR", "AZ", "AM",
})


def _score_label(score: float) -> str:
    for (lo, hi), label in _SCORE_LABELS.items():
        if lo <= score < hi:
            return label
    return "CRITICAL"


def _safe_float(v, default: float = 0.0) -> float:
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


# ─────────────────────────────────────────────────────────────
# G1: 보도 집중도 편차 (Q06 기반)
# ─────────────────────────────────────────────────────────────

def _compute_g1(q06: dict) -> tuple[float, dict]:
    """지역별 보도 집중도 편차 → 0~100.

    특정 지역에 보도가 집중될수록 (지정학적 이벤트 신호) G1이 높다.
    어두운 구석(보도 < 3%) 비율도 반영.
    """
    regions = q06.get("answer", {}).get("all_regions") or []
    if not regions:
        return 0.0, {"reason": "no_region_data"}

    coverages = [_safe_float(r.get("coverage_pct", 0)) for r in regions]
    if not coverages:
        return 0.0, {}

    # 지니 계수 (불균등 지수)
    n = len(coverages)
    total = sum(coverages)
    if total == 0:
        return 0.0, {}
    mean = total / n
    gini = sum(abs(a - b) for a in coverages for b in coverages) / (2 * n * n * mean)

    # 상위 3개 지역 집중도
    top3_share = sum(sorted(coverages, reverse=True)[:3]) / total

    # 다크 코너 비율
    dark = q06.get("answer", {}).get("dark_corners_under3pct") or []
    dark_ratio = len(dark) / max(n, 1)

    # 합성: gini(50%) + top3(30%) + dark(20%)
    g1 = min(100.0, (gini * 50) + (top3_share * 30) + (dark_ratio * 20))
    return round(g1, 2), {
        "gini": round(gini, 3),
        "top3_share": round(top3_share, 3),
        "dark_ratio": round(dark_ratio, 3),
        "region_count": n,
    }


# ─────────────────────────────────────────────────────────────
# G2: 국가 감성 변화 (Q05 기반)
# ─────────────────────────────────────────────────────────────

def _compute_g2(q05: dict) -> tuple[float, dict]:
    """국가별 감성 / 보도량 이상 변화 → 0~100.

    sentiment_score 있으면 부정 감성 가중.
    없으면 보도량 자체를 핫스팟 기준으로 평가 (degraded 경로).
    """
    sents = q05.get("answer", {}).get("country_sentiments") or []
    if not sents:
        return 0.0, {"reason": "no_country_data"}

    total_articles = sum(_safe_float(s.get("article_count", 0)) for s in sents)
    hotspot_articles = sum(
        _safe_float(s.get("article_count", 0))
        for s in sents if s.get("country", "") in _HOTSPOT_COUNTRIES
    )

    # 핫스팟 국가 집중도 (기본 신호)
    hotspot_ratio = hotspot_articles / max(total_articles, 1)

    # 감성 신호 (있으면 부정 감성 비율 반영)
    neg_ratio = 0.0
    has_sentiment = any("sentiment_score" in s for s in sents)
    if has_sentiment:
        neg_scores = [
            abs(min(_safe_float(s.get("sentiment_score", 0)), 0))
            for s in sents if s.get("country", "") in _HOTSPOT_COUNTRIES
        ]
        neg_ratio = sum(neg_scores) / max(len(neg_scores), 1)

    g2 = min(100.0, (hotspot_ratio * 60) + (neg_ratio * 40))
    return round(g2, 2), {
        "hotspot_ratio": round(hotspot_ratio, 3),
        "neg_ratio": round(neg_ratio, 3),
        "has_sentiment_data": has_sentiment,
        "total_articles": int(total_articles),
    }


# ─────────────────────────────────────────────────────────────
# G3: 양국 긴장 신호 (Q07 기반)
# ─────────────────────────────────────────────────────────────

def _compute_g3(q07: dict) -> tuple[float, dict]:
    """양국 관계 긴장 신호 → 0~100.

    country_pairs에서 긴장(tension_score > 0) 쌍의 강도 합산.
    데이터 없으면 0 반환.
    """
    pairs = q07.get("answer", {}).get("country_pairs") or []
    if not pairs:
        return 0.0, {"reason": "no_pair_data", "pair_count": 0}

    tension_scores = []
    hotspot_pairs = []
    for p in pairs:
        ts = _safe_float(p.get("tension_score", 0))
        if ts <= 0:
            continue
        a = p.get("country_a", "")
        b = p.get("country_b", "")
        # 핫스팟 쌍 가중
        multiplier = 1.5 if (a in _HOTSPOT_COUNTRIES or b in _HOTSPOT_COUNTRIES) else 1.0
        tension_scores.append(ts * multiplier)
        hotspot_pairs.append(f"{a}-{b}")

    if not tension_scores:
        return 0.0, {"pair_count": len(pairs), "tension_pairs": 0}

    # 정규화: 최대 가능 점수 대비
    raw = sum(tension_scores) / max(len(pairs), 1)
    g3 = min(100.0, raw * 100)
    return round(g3, 2), {
        "tension_pairs": len(tension_scores),
        "total_pairs": len(pairs),
        "hotspot_pairs": hotspot_pairs[:5],
    }


# ─────────────────────────────────────────────────────────────
# GTI 합성
# ─────────────────────────────────────────────────────────────

_W_G1, _W_G2, _W_G3 = 0.40, 0.35, 0.25


def compute_gti(q05: dict, q06: dict, q07: dict) -> dict:
    """Q05·Q06·Q07 → GTI 딕셔너리."""
    g1, d1 = _compute_g1(q06)
    g2, d2 = _compute_g2(q05)
    g3, d3 = _compute_g3(q07)

    score = _W_G1 * g1 + _W_G2 * g2 + _W_G3 * g3
    score = round(min(100.0, score), 2)
    label = _score_label(score)

    return {
        "gti_score": score,
        "gti_label": label,
        "gti_color": _COLOR_MAP[label],
        "components": {
            "g1_coverage_skew": g1,
            "g2_sentiment_hotspot": g2,
            "g3_bilateral_tension": g3,
        },
        "weights": {"g1": _W_G1, "g2": _W_G2, "g3": _W_G3},
        "details": {"g1": d1, "g2": d2, "g3": d3},
        "data_quality": {
            "q05_status": q05.get("status", "missing"),
            "q06_status": q06.get("status", "missing"),
            "q07_status": q07.get("status", "missing"),
        },
    }


# ─────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────

def run_gti(date: str, project_root: str | Path = ".") -> dict:
    """지정 날짜 GTI를 계산하고 파일로 저장."""
    root = Path(project_root)
    answers_dir = root / "data" / "answers" / date

    def _load(qid: str) -> dict:
        p = answers_dir / f"{qid}.json"
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}

    q05 = _load("q05")
    q06 = _load("q06")
    q07 = _load("q07")

    result = compute_gti(q05, q06, q07)
    result["date"] = date
    result["computed_at"] = datetime.now().isoformat()

    # 저장
    out_dir = root / "data" / "gti" / date
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "gti_daily.json"
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    # 히스토리 append
    hist_path = root / "data" / "gti" / "gti_history.jsonl"
    with open(hist_path, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "date": date,
            "gti_score": result["gti_score"],
            "gti_label": result["gti_label"],
            "g1": result["components"]["g1_coverage_skew"],
            "g2": result["components"]["g2_sentiment_hotspot"],
            "g3": result["components"]["g3_bilateral_tension"],
        }, ensure_ascii=False) + "\n")

    logger.info("gti_done date=%s score=%.1f label=%s", date, result["gti_score"], result["gti_label"])
    return result


def backfill_gti(project_root: str | Path = ".") -> list[dict]:
    """모든 answer 날짜에 대해 GTI 재계산."""
    root = Path(project_root)
    answers_base = root / "data" / "answers"
    results = []
    for d in sorted(answers_base.iterdir()):
        if not d.is_dir() or len(d.name) != 10:
            continue
        try:
            r = run_gti(d.name, root)
            results.append(r)
            print(f"  {d.name}: GTI={r['gti_score']:.1f} ({r['gti_label']})")
        except Exception as exc:
            print(f"  {d.name}: FAIL {exc}")
    return results


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="GTI 계산")
    p.add_argument("--date", type=str, default=None)
    p.add_argument("--backfill", action="store_true")
    p.add_argument("--project-dir", default=".", type=str)
    args = p.parse_args()

    if args.backfill:
        backfill_gti(args.project_dir)
    else:
        date = args.date or datetime.now().strftime("%Y-%m-%d")
        r = run_gti(date, args.project_dir)
        print(f"GTI {date}: {r['gti_score']:.1f} ({r['gti_label']})")
        print(f"  G1(coverage): {r['components']['g1_coverage_skew']:.1f}")
        print(f"  G2(sentiment): {r['components']['g2_sentiment_hotspot']:.1f}")
        print(f"  G3(bilateral): {r['components']['g3_bilateral_tension']:.1f}")
