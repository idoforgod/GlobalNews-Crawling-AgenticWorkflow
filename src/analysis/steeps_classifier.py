"""STEEPSS 7-category + Cross-domain classifier for multilingual news.

Categories: SOC / TEC / ECO / ENV / POL / SEC / SPI / CRS
Method: Hybrid — keyword tier (< 1ms) → model tier (150ms) on ambiguous.

SPI(Spiritual) 추가 이유:
  종교·영성·가치관은 POL·SOC로 흡수되면 미래학적 약신호에서 소실된다.
  "AI 윤리 논쟁", "종교적 정체성의 부상", "의미 추구 경향" 같은 패러다임
  전환 신호는 SPI 없이는 포착 불가하다.

Supports 14 languages via:
  - Language-specific keyword dictionaries (Tier 1)
  - Multilingual zero-shot NLI via XLM-RoBERTa (Tier 2, optional)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# Category Definition
# ─────────────────────────────────────────────────────────────

class STEEPS(str, Enum):
    SOC = "SOC"   # Social
    TEC = "TEC"   # Technology
    ECO = "ECO"   # Economic
    ENV = "ENV"   # Environmental
    POL = "POL"   # Political
    SEC = "SEC"   # Security
    SPI = "SPI"   # Spiritual
    CRS = "CRS"   # Cross-domain (auto-assigned)

    @property
    def label_ko(self) -> str:
        return {
            "SOC": "사회", "TEC": "기술", "ECO": "경제",
            "ENV": "환경", "POL": "정치", "SEC": "안보",
            "SPI": "영성", "CRS": "복합",
        }[self.value]

    @property
    def hypothesis_en(self) -> str:
        return {
            "SOC": "This article covers social issues, demographics, education, culture, health, or community.",
            "TEC": "This article covers technology, innovation, AI, science, or digital transformation.",
            "ECO": "This article covers economics, business, finance, trade, markets, or industry.",
            "ENV": "This article covers environmental issues, climate change, energy, or natural disasters.",
            "POL": "This article covers politics, government policy, elections, diplomacy, or legislation.",
            "SEC": "This article covers military, war, terrorism, cybersecurity, weapons, or national security.",
            "SPI": "This article covers religion, spirituality, faith, ethics, values, or the search for meaning.",
            "CRS": "This article covers multiple domains simultaneously.",
        }[self.value]


# ─────────────────────────────────────────────────────────────
# Keyword Dictionaries
# ─────────────────────────────────────────────────────────────

_KEYWORDS: dict[str, dict[str, list[tuple[str, int]]]] = {
    "SOC": {
        "ko": [
            ("인구", 3), ("출생률", 3), ("고령화", 3), ("저출산", 3),
            ("교육", 2), ("학교", 2), ("복지", 2), ("의료", 2), ("건강", 2),
            ("문화", 2), ("사회", 1), ("가족", 2), ("이민", 2), ("여성", 2),
            ("청년", 2), ("노인", 2), ("불평등", 3), ("빈곤", 2),
            ("다양성", 2), ("인권", 2), ("노동", 2), ("임금", 2),
        ],
        "en": [
            ("population", 3), ("birth rate", 3), ("aging", 3), ("elderly", 2),
            ("education", 2), ("school", 2), ("welfare", 2), ("healthcare", 2),
            ("culture", 2), ("social", 1), ("family", 2), ("immigration", 2),
            ("gender", 2), ("youth", 2), ("inequality", 3), ("poverty", 2),
            ("diversity", 2), ("human rights", 2), ("labor", 2), ("wage", 2),
            ("community", 2), ("public health", 3), ("census", 3),
        ],
    },
    "TEC": {
        "ko": [
            ("인공지능", 3), ("AI", 3), ("반도체", 3), ("우주", 2),
            ("기술", 2), ("디지털", 2), ("로봇", 3), ("자율주행", 3),
            ("양자", 3), ("바이오", 2), ("유전자", 3), ("IT", 2),
            ("소프트웨어", 2), ("플랫폼", 2), ("스타트업", 2),
            ("혁신", 2), ("전기차", 3), ("배터리", 2), ("5G", 3), ("6G", 3),
        ],
        "en": [
            ("artificial intelligence", 3), ("AI", 3), ("semiconductor", 3),
            ("space", 2), ("technology", 2), ("digital", 2), ("robot", 3),
            ("autonomous", 3), ("quantum", 3), ("biotech", 2), ("gene", 3),
            ("software", 2), ("platform", 2), ("startup", 2), ("innovation", 2),
            ("electric vehicle", 3), ("battery", 2), ("5G", 3), ("6G", 3),
            ("drone", 2), ("chip", 3), ("LLM", 3), ("machine learning", 3),
        ],
    },
    "ECO": {
        "ko": [
            ("GDP", 3), ("금리", 3), ("주가", 3), ("인플레이션", 3),
            ("무역", 3), ("경제", 2), ("기업", 2), ("시장", 2),
            ("투자", 2), ("산업", 2), ("수출", 3), ("수입", 2),
            ("환율", 3), ("실업", 3), ("고용", 2), ("부동산", 3),
            ("주식", 3), ("채권", 3), ("예산", 2), ("세금", 2),
            ("공급망", 3), ("물가", 3),
        ],
        "en": [
            ("GDP", 3), ("interest rate", 3), ("stock", 3), ("inflation", 3),
            ("trade", 3), ("economy", 2), ("company", 2), ("market", 2),
            ("investment", 2), ("industry", 2), ("export", 3), ("import", 2),
            ("exchange rate", 3), ("unemployment", 3), ("employment", 2),
            ("real estate", 3), ("bond", 3), ("budget", 2), ("tax", 2),
            ("supply chain", 3), ("price", 2), ("recession", 3),
            ("earnings", 3), ("revenue", 3),
        ],
    },
    "ENV": {
        "ko": [
            ("기후", 3), ("탄소", 3), ("온난화", 3), ("환경", 2),
            ("재생에너지", 3), ("태양광", 3), ("풍력", 3), ("핵발전", 2),
            ("원전", 2), ("홍수", 2), ("가뭄", 2), ("산불", 2),
            ("오염", 2), ("생태계", 3), ("멸종", 3), ("탄소중립", 3),
            ("COP", 3), ("녹색", 2), ("해수면", 3), ("빙하", 3),
        ],
        "en": [
            ("climate", 3), ("carbon", 3), ("global warming", 3),
            ("environment", 2), ("renewable", 3), ("solar", 3), ("wind", 3),
            ("nuclear", 2), ("flood", 2), ("drought", 2), ("wildfire", 2),
            ("pollution", 2), ("ecosystem", 3), ("extinction", 3),
            ("net zero", 3), ("COP", 3), ("sea level", 3), ("glacier", 3),
            ("biodiversity", 3), ("deforestation", 3),
        ],
    },
    "POL": {
        "ko": [
            ("선거", 3), ("대통령", 3), ("정부", 2), ("국회", 2),
            ("정치", 2), ("외교", 3), ("정책", 2), ("법안", 3),
            ("여당", 3), ("야당", 3), ("국무", 2), ("총리", 2),
            ("정상회담", 3), ("유엔", 3), ("NATO", 3), ("제재", 3),
            ("협정", 2), ("조약", 3), ("의회", 2), ("투표", 3),
        ],
        "en": [
            ("election", 3), ("president", 3), ("government", 2),
            ("parliament", 2), ("politics", 2), ("diplomacy", 3),
            ("policy", 2), ("legislation", 3), ("ruling party", 3),
            ("opposition", 3), ("prime minister", 2), ("summit", 3),
            ("UN", 3), ("NATO", 3), ("sanctions", 3), ("treaty", 3),
            ("vote", 3), ("congress", 2), ("senate", 2), ("minister", 2),
        ],
    },
    "SEC": {
        "ko": [
            ("전쟁", 3), ("군사", 3), ("미사일", 3), ("핵", 3),
            ("테러", 3), ("사이버", 3), ("해킹", 3), ("안보", 3),
            ("군대", 2), ("국방", 3), ("무기", 3), ("분쟁", 3),
            ("폭발", 2), ("공격", 2), ("침공", 3), ("방위", 2),
            ("스파이", 3), ("정보기관", 3),
        ],
        "en": [
            ("war", 3), ("military", 3), ("missile", 3), ("nuclear", 3),
            ("terrorism", 3), ("cyber", 3), ("hacking", 3), ("security", 3),
            ("army", 2), ("defense", 3), ("weapon", 3), ("conflict", 3),
            ("explosion", 2), ("attack", 2), ("invasion", 3), ("spy", 3),
            ("intelligence", 3), ("airstrike", 3), ("troops", 3),
        ],
    },
    "SPI": {
        "ko": [
            ("종교", 3), ("신앙", 3), ("교회", 3), ("성당", 3),
            ("절", 2), ("사원", 2), ("모스크", 3), ("불교", 3),
            ("기독교", 3), ("이슬람", 3), ("유대교", 3), ("힌두교", 3),
            ("목사", 3), ("신부", 3), ("스님", 3), ("이맘", 3),
            ("기도", 3), ("예배", 3), ("명상", 3), ("영성", 3),
            ("윤리", 2), ("도덕", 2), ("가치관", 2), ("철학", 2),
            ("신학", 3), ("성지", 3), ("종교 자유", 3),
        ],
        "en": [
            ("religion", 3), ("religious", 3), ("faith", 3), ("church", 3),
            ("mosque", 3), ("temple", 3), ("synagogue", 3),
            ("buddhism", 3), ("christianity", 3), ("islam", 3),
            ("judaism", 3), ("hinduism", 3),
            ("pastor", 3), ("priest", 3), ("monk", 3), ("imam", 3),
            ("pope", 3), ("bishop", 3),
            ("prayer", 3), ("worship", 3), ("meditation", 3),
            ("spirituality", 3), ("spiritual", 3),
            ("ethics", 2), ("moral", 2), ("values", 2), ("philosophy", 2),
            ("theology", 3), ("holy", 2), ("sacred", 3),
            ("interfaith", 3), ("religious freedom", 3),
            ("meaning of life", 3), ("existential", 2),
        ],
        "de": [
            ("Religion", 3), ("Kirche", 3), ("Glaube", 3), ("Moschee", 3),
            ("Ethik", 2), ("Moral", 2), ("Spiritualität", 3),
        ],
        "ar": [
            ("دين", 3), ("إسلام", 3), ("مسجد", 3), ("صلاة", 3),
            ("إيمان", 3), ("روحانية", 3),
        ],
    },
}

_FALLBACK_LANG = "en"


# ─────────────────────────────────────────────────────────────
# Result
# ─────────────────────────────────────────────────────────────

@dataclass
class STEEPSResult:
    primary: str
    secondary: list[str]
    scores: dict[str, float]
    confidence: float
    method: str
    is_cross_domain: bool
    all_tags: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        tags = [self.primary]
        tags.extend(s for s in self.secondary if s != self.primary)
        if self.is_cross_domain and "CRS" not in tags:
            tags.append("CRS")
        self.all_tags = tags

    def to_dict(self) -> dict[str, Any]:
        return {
            "steeps_primary":    self.primary,
            "steeps_secondary":  self.secondary,
            "steeps_all":        self.all_tags,
            "steeps_scores":     {k: round(v, 4) for k, v in self.scores.items()},
            "steeps_confidence": round(self.confidence, 4),
            "steeps_method":     self.method,
        }


# ─────────────────────────────────────────────────────────────
# Tier 1: Keyword Matcher
# ─────────────────────────────────────────────────────────────

class _KeywordMatcher:
    _TITLE_WEIGHT = 3.0
    _BODY_WEIGHT  = 1.0
    CONFIDENT_THRESHOLD  = 0.45
    SECONDARY_THRESHOLD  = 0.28
    CROSS_DOMAIN_THRESHOLD = 0.35

    def score(self, title: str, body: str, language: str) -> dict[str, float]:
        lang = language[:2].lower()
        title_lower = title.lower()
        body_lower  = body.lower()
        raw: dict[str, float] = {c.value: 0.0 for c in STEEPS if c != STEEPS.CRS}

        for cat_code, lang_dict in _KEYWORDS.items():
            keywords = lang_dict.get(lang) or lang_dict.get(_FALLBACK_LANG, [])
            for kw, weight in keywords:
                kw_l = kw.lower()
                t_hits = len(re.findall(
                    r"(?<![가-힣a-zA-Z])" + re.escape(kw_l) + r"(?![가-힣a-zA-Z])",
                    title_lower, re.IGNORECASE,
                ))
                b_hits = len(re.findall(
                    r"(?<![가-힣a-zA-Z])" + re.escape(kw_l) + r"(?![가-힣a-zA-Z])",
                    body_lower[:3000], re.IGNORECASE,
                ))
                raw[cat_code] += (
                    t_hits * weight * self._TITLE_WEIGHT
                    + b_hits * weight * self._BODY_WEIGHT
                )

        total = sum(raw.values()) or 1.0
        return {k: v / total for k, v in raw.items()}


# ─────────────────────────────────────────────────────────────
# Tier 2: Zero-Shot NLI
# ─────────────────────────────────────────────────────────────

class _ZeroShotNLI:
    _MODEL_NAME = "cross-encoder/nli-MiniLM2-L6-H768"
    _pipe = None

    @classmethod
    def _get_pipeline(cls) -> Any:
        if cls._pipe is None:
            from transformers import pipeline as hf_pipeline
            cls._pipe = hf_pipeline(
                "zero-shot-classification",
                model=cls._MODEL_NAME,
                device=-1,
                batch_size=8,
            )
            logger.info("steeps_nli_model_loaded model=%s", cls._MODEL_NAME)
        return cls._pipe

    def score(self, text: str) -> dict[str, float]:
        pipe = self._get_pipeline()
        cats = [c for c in STEEPS if c != STEEPS.CRS]
        hypotheses = [c.hypothesis_en for c in cats]
        try:
            result = pipe(text[:512], candidate_labels=hypotheses, multi_label=True)
        except Exception as e:
            logger.warning("steeps_nli_failed error=%s", e)
            return {c.value: 1.0 / 7 for c in cats}
        hyp_to_code = {c.hypothesis_en: c.value for c in cats}
        scores: dict[str, float] = {}
        for label, score in zip(result["labels"], result["scores"]):
            code = hyp_to_code.get(label)
            if code:
                scores[code] = float(score)
        total = sum(scores.values()) or 1.0
        return {k: v / total for k, v in scores.items()}


# ─────────────────────────────────────────────────────────────
# Main Classifier
# ─────────────────────────────────────────────────────────────

class STEEPSClassifier:
    """STEEPSS 7+1 카테고리 분류기.

    사용법:
        clf = STEEPSClassifier()
        r = clf.classify(title="연준 금리 동결", body="...", language="en")
        print(r.primary)    # "ECO"
        print(r.all_tags)   # ["ECO"]
    """

    _KEYWORD_CONFIDENT   = 0.45
    _SECONDARY_THRESHOLD = 0.28
    _CROSS_DOMAIN_COUNT  = 2

    def __init__(self, use_model: bool = True) -> None:
        self._kw  = _KeywordMatcher()
        self._nli: _ZeroShotNLI | None = _ZeroShotNLI() if use_model else None

    def classify(
        self,
        title: str,
        body: str,
        language: str = "en",
        body_quality: str = "FULL",
    ) -> STEEPSResult:
        try:
            return self._classify_safe(title, body, language, body_quality)
        except Exception as e:
            logger.error("steeps_classify_failed error=%s", e)
            return self._fallback()

    def _classify_safe(
        self,
        title: str,
        body: str,
        language: str,
        body_quality: str,
    ) -> STEEPSResult:
        effective_body = body if body_quality in ("FULL", "PARTIAL") else ""
        kw_scores  = self._kw.score(title, effective_body, language)
        top_score  = max(kw_scores.values())
        final_scores = kw_scores
        method = "keyword"

        if (
            self._nli is not None
            and top_score < self._KEYWORD_CONFIDENT
            and effective_body
        ):
            nli_scores = self._nli.score(f"{title}. {effective_body[:400]}")
            final_scores = {
                k: 0.4 * kw_scores.get(k, 0.0) + 0.6 * nli_scores.get(k, 0.0)
                for k in kw_scores
            }
            method = "hybrid"

        primary    = max(final_scores, key=final_scores.__getitem__)
        confidence = final_scores[primary]
        secondary  = [
            k for k, v in sorted(final_scores.items(), key=lambda x: -x[1])
            if k != primary and v >= self._SECONDARY_THRESHOLD
        ]
        is_cross = (
            sum(1 for v in final_scores.values()
                if v >= _KeywordMatcher.CROSS_DOMAIN_THRESHOLD)
            >= self._CROSS_DOMAIN_COUNT
        )
        return STEEPSResult(
            primary=primary, secondary=secondary,
            scores=final_scores, confidence=confidence,
            method=method, is_cross_domain=is_cross,
        )

    @staticmethod
    def _fallback() -> STEEPSResult:
        eq = 1.0 / 7
        return STEEPSResult(
            primary="SOC", secondary=[],
            scores={c.value: eq for c in STEEPS if c != STEEPS.CRS},
            confidence=eq, method="fallback", is_cross_domain=False,
        )

    def classify_batch(
        self,
        articles: list[dict[str, str]],
    ) -> list[STEEPSResult]:
        return [
            self.classify(
                title=a.get("title", ""),
                body=a.get("body", ""),
                language=a.get("language", "en"),
                body_quality=a.get("body_quality", "FULL"),
            )
            for a in articles
        ]
