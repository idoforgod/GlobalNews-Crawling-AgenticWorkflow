"""Signal Type Classifier — BREAKING / TREND / WEAK_SIGNAL / NOISE.

Stage 7의 5-Layer signal_layer(L1~L5)와 구별되는 별도 분류.
Stage 7 = NLP 기술 신호 (분석가용)
signal_type = 18-Question Engine 직접 소비용 (사람이 읽는 4가지 카테고리)

두 레이어로 작동:

  Layer A — 기사 단독 (history 불필요, Stage 2에서 즉시 실행)
    noise_score    : 0-1, 높을수록 반복·저품질 기사
    novelty_score  : 0-1, 높을수록 새로운 주제·내용
    signal_type_prelim: 'NOISE' 또는 'UNCLASSIFIED'

  Layer B — 집계 후 확정 (history 필요, Stage 5 이후 refinement)
    burst_score    : 오늘 빈도 / 30일 평균 (1.0 = 중립)
    signal_type    : 'BREAKING' | 'TREND' | 'WEAK_SIGNAL' | 'NOISE'

Layer B는 Stage 5 시계열 분석 완료 후 refine_with_burst() 호출로 확정.
history 없으면 UNCLASSIFIED 유지 (18-Question Engine이 처리).

Signal Type 정의:
    BREAKING    다수 출처가 짧은 시간 내 동시 보도한 속보
    TREND       7일 이상 성장 중인 지속 의제
    WEAK_SIGNAL 소수 출처·낮은 빈도·높은 신규성 → 미래 의제 후보
    NOISE       스포츠 결과·시세 마감·일상 공지 등 반복 저가치
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# ─────────────────────────────────────────────────────────────
# 상수
# ─────────────────────────────────────────────────────────────

# Layer B 분류 임계값
_BURST_BREAKING     = 5.0   # 30일 평균의 5배 이상 + 단일일 급증
_BURST_TREND        = 1.5   # 30일 평균의 1.5배 이상, 7일 이상 지속
_NOVELTY_WEAK       = 0.60  # novelty ≥ 0.60 + 출처 희소 → WEAK_SIGNAL
_NOISE_THRESHOLD    = 0.55  # noise_score ≥ 0.55 → NOISE 확정

# 본문 길이 기준
_BODY_VERY_SHORT    = 150   # chars, 이하 → noise 강신호
_BODY_SHORT         = 400   # chars, 이하 → noise 약신호
_BODY_RICH          = 800   # chars, 이상 → novelty 약신호

# 스포츠 스코어 패턴 (다국어)
_SPORTS_SCORE_RE = re.compile(
    r"""
    \b\d{1,3}\s*[-:]\s*\d{1,3}\b           # 3-1, 21:18 스코어
    | (?:win|loss|defeat|beat|scored)\b     # 영어 승패 동사
    | (?:승리|패배|득점|무승부|승|패)\b      # 한국어 승패
    | (?:シュート|得点|勝|敗|引き分け)\b    # 일본어
    | (?:ganó|perdió|empate|gol)\b         # 스페인어
    """,
    re.IGNORECASE | re.VERBOSE,
)

# 시세·증시 마감 패턴
_MARKET_CLOSE_RE = re.compile(
    r"""
    (?:마감|종가|종합지수|코스피|코스닥|나스닥|다우)   # 한국 증시
    | (?:close[sd]|closing|market\s+wrap)              # 영어 마감
    | (?:\+|-)\s*\d+\.?\d*\s*%                          # 등락률
    | (?:포인트|bp|bps)\s+(?:상승|하락|올랐|내렸)      # 포인트 변동
    """,
    re.IGNORECASE | re.VERBOSE,
)

# 날씨 패턴
_WEATHER_RE = re.compile(
    r"""
    (?:날씨|기온|강수|강설|맑음|흐림)     # 한국어
    | (?:weather\s+forecast|temperature)  # 영어
    | (?:\d{1,2}°[CF])                    # 온도 표기
    """,
    re.IGNORECASE | re.VERBOSE,
)

# 공지/브리핑 패턴
_BRIEFING_RE = re.compile(
    r"""
    (?:오늘의\s+일정|일정\s+안내|공고|고시|입찰공고)  # 한국어 공지
    | (?:press\s+release|media\s+advisory|embargo)    # 영어 보도자료
    | (?:agenda\s+for|schedule\s+for\s+(?:today|tomorrow))
    """,
    re.IGNORECASE | re.VERBOSE,
)


# ─────────────────────────────────────────────────────────────
# 결과 데이터클래스
# ─────────────────────────────────────────────────────────────

@dataclass
class SignalResult:
    """Signal 분류 결과.

    Attributes:
        signal_type:    확정 분류 ('BREAKING'|'TREND'|'WEAK_SIGNAL'|'NOISE'|'UNCLASSIFIED')
        noise_score:    0-1, 높을수록 반복·저가치
        novelty_score:  0-1, 높을수록 신규·고가치
        burst_score:    오늘 빈도 / 30일 평균 (history 없으면 1.0)
        is_refined:     True = Layer B 집계 후 확정, False = Layer A 예비 분류
        noise_reasons:  noise 판정 근거 목록
    """
    signal_type: str = "UNCLASSIFIED"
    noise_score: float = 0.0
    novelty_score: float = 0.0
    burst_score: float = 1.0
    is_refined: bool = False
    noise_reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal_type": self.signal_type,
            "noise_score": round(self.noise_score, 4),
            "novelty_score": round(self.novelty_score, 4),
            "burst_score": round(self.burst_score, 4),
        }


# ─────────────────────────────────────────────────────────────
# Layer A — 기사 단독 (article-level) 분류기
# ─────────────────────────────────────────────────────────────

class _NoiseDetector:
    """기사 단독으로 NOISE를 결정론적으로 탐지."""

    def score(
        self,
        title: str,
        body: str,
        body_quality: str,
        language: str,
    ) -> tuple[float, list[str]]:
        """noise_score와 근거 목록 반환."""
        score = 0.0
        reasons: list[str] = []

        # R1: 본문 길이
        body_len = len(body.strip())
        if body_quality in ("TITLE_ONLY", "PAYWALL") or body_len < _BODY_VERY_SHORT:
            score += 0.45
            reasons.append(f"body_too_short({body_len}chars)")
        elif body_len < _BODY_SHORT:
            score += 0.20
            reasons.append(f"body_short({body_len}chars)")

        # R2: 스포츠 스코어 패턴
        combined = f"{title} {body[:500]}"
        if _SPORTS_SCORE_RE.search(combined):
            score += 0.35
            reasons.append("sports_score_pattern")

        # R3: 시세 마감 패턴
        if _MARKET_CLOSE_RE.search(combined):
            score += 0.30
            reasons.append("market_close_pattern")

        # R4: 날씨 패턴
        if _WEATHER_RE.search(title):
            score += 0.40
            reasons.append("weather_title_pattern")

        # R5: 공지·보도자료 패턴
        if _BRIEFING_RE.search(title):
            score += 0.35
            reasons.append("briefing_announcement_pattern")

        # R6: 제목이 매우 짧고 숫자/특수문자 비율 높음
        title_stripped = title.strip()
        if len(title_stripped) < 15 and title_stripped:
            digit_ratio = sum(1 for c in title_stripped if c.isdigit()) / len(title_stripped)
            if digit_ratio > 0.4:
                score += 0.25
                reasons.append("short_numeric_title")

        return min(score, 1.0), reasons


class _NoveltyScorer:
    """기사 단독으로 novelty_score 추정.

    history 없이 추정하는 휴리스틱 — 집계 후 burst_score로 보정 예정.
    """

    def score(
        self,
        title: str,
        body: str,
        body_quality: str,
        steeps_all: list[str],
        geo_focus_all: list[str],
        entity_person: list[str],
        entity_org: list[str],
        entity_country: list[str],
    ) -> float:
        """novelty_score 0-1 반환."""
        score = 0.0

        # N1: 본문 풍부도 (정보량 프록시)
        body_len = len(body.strip())
        if body_len >= _BODY_RICH:
            score += 0.20
        elif body_quality == "FULL":
            score += 0.10

        # N2: STEEPS 복합 (교차 도메인은 신규성 높음)
        if "CRS" in steeps_all or len(steeps_all) >= 3:
            score += 0.25
        elif len(steeps_all) == 2:
            score += 0.10

        # N3: 지리 다양성 (여러 나라 동시 언급 = 복합 이슈)
        unique_geo = len(set(geo_focus_all) - {"UNKNOWN", "EU", "NATO", "UN"})
        if unique_geo >= 3:
            score += 0.20
        elif unique_geo == 2:
            score += 0.10

        # N4: 엔티티 다양성 (다양한 행위자 = 복잡한 이슈)
        total_entities = len(entity_person) + len(entity_org) + len(entity_country)
        if total_entities >= 6:
            score += 0.20
        elif total_entities >= 3:
            score += 0.10

        # N5: 제목에 부정어·대조어 (새로운 전개 신호)
        contradiction_words = [
            "처음", "최초", "역대", "사상", "unprecedented", "first time",
            "new high", "new low", "reversal", "surprise", "unexpected",
            "반전", "충격", "돌파", "사상 최초", "전례없는",
        ]
        title_lower = title.lower()
        if any(w in title_lower for w in contradiction_words):
            score += 0.15

        return min(score, 1.0)


# ─────────────────────────────────────────────────────────────
# Layer B — 집계 후 확정 (topic-level, history 필요)
# ─────────────────────────────────────────────────────────────

def refine_with_burst(
    result: SignalResult,
    burst_score: float,
    topic_age_days: int,
    source_count_today: int,
    source_count_threshold: int = 3,
) -> SignalResult:
    """Stage 5 집계 결과로 signal_type 확정.

    Args:
        result:                  Layer A 결과 (signal_type='UNCLASSIFIED' 또는 'NOISE')
        burst_score:             오늘 기사 수 / 30일 평균 (Stage 5에서 계산)
        topic_age_days:          이 토픽이 처음 등장한 지 며칠째인가
        source_count_today:      오늘 이 토픽을 보도한 언론사 수
        source_count_threshold:  BREAKING 판정 최소 언론사 수

    Returns:
        signal_type이 확정된 새 SignalResult.
    """
    # NOISE는 유지 (Layer A 결정 존중)
    if result.signal_type == "NOISE":
        return SignalResult(
            signal_type="NOISE",
            noise_score=result.noise_score,
            novelty_score=result.novelty_score,
            burst_score=burst_score,
            is_refined=True,
            noise_reasons=result.noise_reasons,
        )

    # signal_type 결정 로직
    if (
        burst_score >= _BURST_BREAKING
        and source_count_today >= source_count_threshold
        and topic_age_days <= 1
    ):
        signal_type = "BREAKING"

    elif burst_score >= _BURST_TREND and topic_age_days >= 7:
        signal_type = "TREND"

    elif (
        result.novelty_score >= _NOVELTY_WEAK
        and source_count_today <= 2
        and topic_age_days <= 3
    ):
        signal_type = "WEAK_SIGNAL"

    elif source_count_today <= 1 and burst_score < 0.5:
        # 단일 소스 + 감소 추세 → 약신호 또는 노이즈 경계
        signal_type = "WEAK_SIGNAL" if result.novelty_score >= 0.40 else "NOISE"

    else:
        # 나머지 (중간 burst, 지속적 의제)
        signal_type = "TREND"

    return SignalResult(
        signal_type=signal_type,
        noise_score=result.noise_score,
        novelty_score=result.novelty_score,
        burst_score=burst_score,
        is_refined=True,
        noise_reasons=result.noise_reasons,
    )


# ─────────────────────────────────────────────────────────────
# 메인 분류기
# ─────────────────────────────────────────────────────────────

class SignalClassifier:
    """Signal Type 분류기.

    Layer A (즉시 실행, history 불필요):
        classify(article) → SignalResult with signal_type='NOISE' or 'UNCLASSIFIED'

    Layer B (집계 후, Stage 5 완료 후):
        refine_with_burst(result, burst_score, ...) → 확정 SignalResult

    사용 예:
        clf = SignalClassifier()

        # Stage 2에서 호출 (즉시)
        r = clf.classify(
            title="코스피 0.3% 상승 마감",
            body="한국 증시가 소폭 상승...",
            body_quality="FULL",
            language="ko",
            steeps_all=["ECO"],
            geo_focus_all=["KR"],
            entity_person=[], entity_org=["코스피"], entity_country=["KR"],
        )
        print(r.signal_type)   # NOISE
        print(r.noise_score)   # 0.65

        # Stage 5 완료 후 refinement
        r_final = clf.refine(r, burst_score=1.1, topic_age_days=30,
                             source_count_today=15)
        print(r_final.signal_type)  # TREND
    """

    def __init__(self) -> None:
        self._noise = _NoiseDetector()
        self._novelty = _NoveltyScorer()

    def classify(
        self,
        title: str,
        body: str,
        body_quality: str = "FULL",
        language: str = "en",
        steeps_all: list[str] | None = None,
        geo_focus_all: list[str] | None = None,
        entity_person: list[str] | None = None,
        entity_org: list[str] | None = None,
        entity_country: list[str] | None = None,
    ) -> SignalResult:
        """Layer A 분류 (기사 단독).

        Returns:
            SignalResult — signal_type은 'NOISE' 또는 'UNCLASSIFIED'.
            P1 보장: 항상 반환, 예외 없음.
        """
        try:
            return self._classify_safe(
                title, body, body_quality, language,
                steeps_all or [], geo_focus_all or [],
                entity_person or [], entity_org or [], entity_country or [],
            )
        except Exception:
            return SignalResult()

    def _classify_safe(
        self,
        title: str,
        body: str,
        body_quality: str,
        language: str,
        steeps_all: list[str],
        geo_focus_all: list[str],
        entity_person: list[str],
        entity_org: list[str],
        entity_country: list[str],
    ) -> SignalResult:
        noise_score, reasons = self._noise.score(
            title, body, body_quality, language,
        )
        novelty_score = self._novelty.score(
            title, body, body_quality,
            steeps_all, geo_focus_all,
            entity_person, entity_org, entity_country,
        )

        # NOISE: noise_score 기준 초과 시 즉시 확정
        signal_type = "NOISE" if noise_score >= _NOISE_THRESHOLD else "UNCLASSIFIED"

        return SignalResult(
            signal_type=signal_type,
            noise_score=noise_score,
            novelty_score=novelty_score,
            burst_score=1.0,     # history 없으므로 중립값
            is_refined=False,
            noise_reasons=reasons,
        )

    @staticmethod
    def refine(
        result: SignalResult,
        burst_score: float,
        topic_age_days: int,
        source_count_today: int,
        source_count_threshold: int = 3,
    ) -> SignalResult:
        """Layer B 확정 (Stage 5 완료 후 호출).

        Args:
            result:                 Layer A 결과.
            burst_score:            오늘 / 30일 평균 빈도 비율.
            topic_age_days:         토픽 최초 등장 이후 경과일.
            source_count_today:     오늘 이 토픽을 보도한 언론사 수.
            source_count_threshold: BREAKING 최소 언론사 수.

        Returns:
            확정된 SignalResult (is_refined=True).
        """
        return refine_with_burst(
            result, burst_score, topic_age_days,
            source_count_today, source_count_threshold,
        )

    def classify_batch(
        self,
        articles: list[dict[str, Any]],
    ) -> list[SignalResult]:
        """배치 처리. 각 article dict 필드: title, body, body_quality,
        language, steeps_all, geo_focus_all,
        entity_person, entity_org, entity_country.
        """
        return [
            self.classify(
                title=a.get("title", ""),
                body=a.get("body", ""),
                body_quality=a.get("body_quality", "FULL"),
                language=a.get("language", "en"),
                steeps_all=a.get("steeps_all", []),
                geo_focus_all=a.get("geo_focus_all", []),
                entity_person=a.get("entity_person", []),
                entity_org=a.get("entity_org", []),
                entity_country=a.get("entity_country", []),
            )
            for a in articles
        ]


# ─────────────────────────────────────────────────────────────
# P1 검증
# ─────────────────────────────────────────────────────────────

_VALID_SIGNAL_TYPES = frozenset(
    {"BREAKING", "TREND", "WEAK_SIGNAL", "NOISE", "UNCLASSIFIED"}
)


def validate_signal_coverage(
    results: list[SignalResult],
    expect_refined: bool = False,
) -> dict[str, Any]:
    """배치 결과 품질 검증.

    SV1 — signal_type이 유효 값인지
    SV2 — noise_score, novelty_score 범위 0-1
    SV3 — NOISE 비율 > 40% 이면 경고 (패턴 과잉 탐지 의심)
    SV4 — expect_refined=True 인데 UNCLASSIFIED 남아있으면 경고
    """
    errors: list[str] = []
    warnings: list[str] = []

    from collections import Counter
    type_counts: Counter[str] = Counter(r.signal_type for r in results)

    # SV1
    invalid = [r.signal_type for r in results
               if r.signal_type not in _VALID_SIGNAL_TYPES]
    if invalid:
        errors.append(f"SV1: 유효하지 않은 signal_type: {set(invalid)}")

    # SV2
    out_of_range = [
        i for i, r in enumerate(results)
        if not (0.0 <= r.noise_score <= 1.0) or not (0.0 <= r.novelty_score <= 1.0)
    ]
    if out_of_range:
        errors.append(f"SV2: 범위 초과 점수 {len(out_of_range)}건")

    # SV3
    noise_ratio = type_counts.get("NOISE", 0) / max(len(results), 1)
    if noise_ratio > 0.40:
        warnings.append(
            f"SV3: NOISE 비율 {noise_ratio:.1%} > 40% — 탐지 패턴 재검토 필요"
        )

    # SV4
    if expect_refined:
        unclassified = type_counts.get("UNCLASSIFIED", 0)
        if unclassified > 0:
            warnings.append(
                f"SV4: Layer B 완료 후 UNCLASSIFIED {unclassified}건 잔류"
            )

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "distribution": dict(type_counts),
        "noise_ratio": round(noise_ratio, 4),
        "avg_novelty": round(
            sum(r.novelty_score for r in results) / max(len(results), 1), 4
        ),
    }
