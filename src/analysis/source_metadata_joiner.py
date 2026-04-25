"""Source Metadata Joiner — sources.yaml 메타데이터를 기사 데이터에 결합.

기사의 source_id → source_country, source_tier, source_lean 조회 제공.

source_tier (자동 도출 — sources.yaml group + 특성 기반):
    GLOBAL    : 국제적 영향력 높은 매체 (BBC, NYT, Al Jazeera 등)
    NATIONAL  : 각국 주요 전국지 (조선일보, 주요 유럽 신문 등)
    REGIONAL  : 지역 특화 매체 또는 중간 규모
    NICHE     : 전문 분야 특화 (IT, 과학, 종교 등)

source_lean (수동 설정 — data/config/source_lean.yaml):
    LEFT / CENTER_LEFT / CENTER / CENTER_RIGHT / RIGHT / UNKNOWN
    정치적 민감성으로 인해 자동 도출 불가. 파일 없으면 전부 UNKNOWN.
    수동 큐레이션 권고. Q12 분석에서 UNKNOWN 제외 후 사용.

Q6(어두운 구석), Q11(의제 선점), Q12(진보/보수), Q14(보도 격차) 직접 사용.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# GLOBAL Tier 고정 목록 (국제적 영향력 기준)
# sources.yaml group/daily_article_estimate 와 무관하게 강제 지정
# ─────────────────────────────────────────────────────────────

_GLOBAL_SITES: frozenset[str] = frozenset({
    # 영미권
    "bbc", "theguardian", "nytimes", "wsj", "ft", "bloomberg",
    "cnn", "npr", "pbsnewshour", "axios",
    # 유럽
    "spiegel", "lemonde", "lefigaro", "elpais", "repubblica",
    "sueddeutsche", "faz",
    # 중동·글로벌
    "aljazeera", "france24_en", "france24_fr",
    # 아시아 영문
    "scmp", "thehindu", "economictimes", "koreatimes",
    # 통신사
    "yna",  # 연합뉴스 (국내 통신사, 글로벌 배포)
})

_NICHE_SITES: frozenset[str] = frozenset({
    # 한국 IT/기술
    "bloter", "zdnet_kr", "aitimes", "techneedle", "irobotnews",
    "sciencetimes", "insight_kr",
    # 영어 IT
    "wired", "techmeme", "stratechery", "arstechnica", "techcrunch", "theverge",
    # 기타 전문
    "north38",       # 북한 전문
    "dailynk",       # 북한 전문
    "propublica",    # 조사저널리즘
    "theintercept",  # 조사저널리즘
    "reason",        # 자유주의 논평
    "balkaninsight", # 발칸 전문
    "intellinews",   # 동유럽 전문
    "centraleuropeantimes",
    "intellinews",
    "icelandmonitor", "icelandreview",
    "sofiaglobe",
    "panapress",     # 아프리카 통신사
})


# ─────────────────────────────────────────────────────────────
# source_lean 기본값 (수동 큐레이션 시작점)
# data/config/source_lean.yaml 로 덮어쓰기 가능
# 정치적 판단이 포함 — 사용 전 박사님 검토 권고
# ─────────────────────────────────────────────────────────────

_DEFAULT_LEAN: dict[str, str] = {
    # 영국
    "theguardian": "CENTER_LEFT",
    "thetimes": "CENTER_RIGHT",
    "telegraph": "RIGHT",
    "bbc": "CENTER",
    "thesun": "RIGHT",
    "independent": "CENTER_LEFT",
    # 미국
    "nytimes": "CENTER_LEFT",
    "wsj": "CENTER_RIGHT",
    "ft": "CENTER",
    "huffpost": "LEFT",
    "npr": "CENTER_LEFT",
    "reason": "RIGHT",    # 자유주의
    "theintercept": "LEFT",
    # 독일
    "spiegel": "CENTER_LEFT",
    "sueddeutsche": "CENTER_LEFT",
    "faz": "CENTER_RIGHT",
    "taz": "LEFT",
    "bild": "RIGHT",
    # 프랑스
    "lemonde": "CENTER_LEFT",
    "lefigaro": "CENTER_RIGHT",
    "liberation": "LEFT",
    # 스페인
    "elpais": "CENTER_LEFT",
    "elmundo": "CENTER_RIGHT",
    # 이탈리아
    "repubblica": "CENTER_LEFT",
    "corriere": "CENTER",
    # 일본
    "asahi": "CENTER_LEFT",
    "yomiuri": "CENTER_RIGHT",
    "mainichi": "CENTER_LEFT",
    # 중국
    "globaltimes": "RIGHT",  # 중국 국영 + 민족주의
    "people": "RIGHT",       # 중국 국영
    # 러시아
    "ria": "RIGHT",          # 국영
    "tass": "RIGHT",         # 국영
    "themoscowtimes": "CENTER_LEFT",  # 독립
    # 이스라엘
    "haaretz": "LEFT",
    "jpost": "CENTER_RIGHT",
    "israelhayom": "RIGHT",
    # 한국 (매우 민감 — UNKNOWN 권고)
    # 필요 시 data/config/source_lean.yaml 에서 수동 지정
    "hani": "UNKNOWN",
    "chosun": "UNKNOWN",
    "joongang": "UNKNOWN",
    "donga": "UNKNOWN",
    "mk": "UNKNOWN",
    "hankyung": "UNKNOWN",
    # 중동
    "aljazeera": "CENTER_LEFT",
    "arabnews": "CENTER_RIGHT",
    "haaretz": "LEFT",
}


# ─────────────────────────────────────────────────────────────
# 결과 데이터클래스
# ─────────────────────────────────────────────────────────────

class SourceMetadata:
    """단일 출처의 메타데이터."""
    __slots__ = (
        "source_id", "source_name", "source_country",
        "source_region", "source_tier", "source_lean",
        "language", "group", "daily_estimate",
    )

    def __init__(
        self,
        source_id: str,
        source_name: str = "",
        source_country: str = "UNKNOWN",
        source_region: str = "UNKNOWN",
        source_tier: str = "NATIONAL",
        source_lean: str = "UNKNOWN",
        language: str = "en",
        group: str = "?",
        daily_estimate: int = 0,
    ) -> None:
        self.source_id = source_id
        self.source_name = source_name
        self.source_country = source_country.upper()
        self.source_region = source_region
        self.source_tier = source_tier
        self.source_lean = source_lean
        self.language = language
        self.group = group
        self.daily_estimate = daily_estimate

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_country":  self.source_country,
            "source_region":   self.source_region,
            "source_tier":     self.source_tier,
            "source_lean":     self.source_lean,
        }


# 지역 코드 → 대륙/지역명 매핑
_REGION_MAP: dict[str, str] = {
    "kr": "East-Asia", "jp": "East-Asia", "cn": "East-Asia",
    "tw": "East-Asia", "hk": "East-Asia", "mn": "East-Asia",
    "in": "South-Asia", "pk": "South-Asia", "bd": "South-Asia",
    "np": "South-Asia", "lk": "South-Asia",
    "id": "Southeast-Asia", "my": "Southeast-Asia", "th": "Southeast-Asia",
    "vn": "Southeast-Asia", "ph": "Southeast-Asia", "sg": "Southeast-Asia",
    "mm": "Southeast-Asia",
    "us": "North-America", "ca": "North-America",
    "mx": "Latin-America", "br": "Latin-America", "ar": "Latin-America",
    "cl": "Latin-America", "co": "Latin-America", "pe": "Latin-America",
    "gb": "Europe", "de": "Europe", "fr": "Europe", "it": "Europe",
    "es": "Europe", "nl": "Europe", "be": "Europe", "se": "Europe",
    "no": "Europe", "dk": "Europe", "fi": "Europe", "pl": "Europe",
    "cz": "Europe", "hu": "Europe", "ro": "Europe", "gr": "Europe",
    "pt": "Europe", "at": "Europe", "ch": "Europe", "is": "Europe",
    "ie": "Europe",
    "ru": "Russia-CIS", "ua": "Russia-CIS", "by": "Russia-CIS",
    "kz": "Russia-CIS", "uz": "Russia-CIS", "az": "Russia-CIS",
    "ge": "Russia-CIS", "am": "Russia-CIS",
    # sources.yaml에서 "me"는 Middle East 지역 코드로 사용됨 (ISO2 Montenegro ≠)
    "me": "Middle-East",
    "il": "Middle-East", "tr": "Middle-East", "sa": "Middle-East",
    "ir": "Middle-East", "iq": "Middle-East", "sy": "Middle-East",
    "lb": "Middle-East", "jo": "Middle-East", "ae": "Middle-East",
    "qa": "Middle-East", "kw": "Middle-East",
    "eg": "Africa", "ng": "Africa", "za": "Africa", "ke": "Africa",
    "et": "Africa", "tz": "Africa", "sn": "Africa", "gh": "Africa",
    "au": "Oceania", "nz": "Oceania",
    "eu": "Europe",
}


# ─────────────────────────────────────────────────────────────
# 메인 조이너
# ─────────────────────────────────────────────────────────────

class SourceMetadataJoiner:
    """sources.yaml 기반 출처 메타데이터 조이너.

    사용법:
        joiner = SourceMetadataJoiner()  # 자동으로 sources.yaml 로드
        meta = joiner.get("chosun")
        print(meta.source_tier)     # "NATIONAL"
        print(meta.source_country)  # "KR"
        print(meta.source_lean)     # "UNKNOWN"
    """

    def __init__(
        self,
        sources_yaml_path: str | Path | None = None,
        lean_yaml_path: str | Path | None = None,
    ) -> None:
        from pathlib import Path as _Path
        if sources_yaml_path is None:
            # 프로젝트 루트 자동 탐지
            _here = _Path(__file__).resolve()
            sources_yaml_path = _here.parents[2] / "data" / "config" / "sources.yaml"
        if lean_yaml_path is None:
            _here = _Path(__file__).resolve()
            lean_yaml_path = _here.parents[2] / "data" / "config" / "source_lean.yaml"

        self._cache: dict[str, SourceMetadata] = {}
        self._load(Path(sources_yaml_path), Path(lean_yaml_path))

    def get(self, source_id: str) -> SourceMetadata:
        """source_id → SourceMetadata. 미등록 ID는 기본값 반환."""
        return self._cache.get(
            source_id,
            SourceMetadata(source_id=source_id),
        )

    def enrich_article(self, article: dict[str, Any]) -> dict[str, Any]:
        """기사 dict에 source_* 필드를 추가한 새 dict 반환."""
        sid = article.get("source_id", "")
        meta = self.get(sid)
        return {**article, **meta.to_dict()}

    def enrich_batch(
        self,
        articles: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """배치 enrichment."""
        return [self.enrich_article(a) for a in articles]

    def source_ids(self) -> list[str]:
        """등록된 source_id 목록."""
        return sorted(self._cache.keys())

    # ── Private ─────────────────────────────────────────────

    def _load(self, sources_path: Path, lean_path: Path) -> None:
        """sources.yaml + source_lean.yaml 로드."""
        try:
            import yaml
            with open(sources_path, encoding="utf-8") as f:
                cfg = yaml.safe_load(f)
        except Exception as exc:
            logger.error("sources_yaml_load_failed path=%s error=%s", sources_path, exc)
            return

        # source_lean.yaml (없으면 기본값만 사용)
        lean_overrides: dict[str, str] = {}
        try:
            if lean_path.exists():
                import yaml
                with open(lean_path, encoding="utf-8") as f:
                    lean_overrides = yaml.safe_load(f) or {}
        except Exception as exc:
            logger.warning("source_lean_yaml_load_failed error=%s", exc)

        for site_id, site_cfg in cfg.get("sources", {}).items():
            region_raw = (site_cfg.get("region") or "").lower()
            self._cache[site_id] = SourceMetadata(
                source_id=site_id,
                source_name=site_cfg.get("name", site_id),
                source_country=self._derive_country(region_raw),
                source_region=_REGION_MAP.get(region_raw, "Unknown"),
                source_tier=self._derive_tier(
                    site_id,
                    site_cfg.get("group", ""),
                    site_cfg.get("meta", {}).get("daily_article_estimate", 0) or 0,
                ),
                source_lean=lean_overrides.get(site_id)
                            or _DEFAULT_LEAN.get(site_id, "UNKNOWN"),
                language=site_cfg.get("language", "en"),
                group=site_cfg.get("group", "?"),
                daily_estimate=site_cfg.get("meta", {}).get("daily_article_estimate", 0) or 0,
            )

        logger.info(
            "source_metadata_loaded sites=%d lean_overrides=%d",
            len(self._cache), len(lean_overrides),
        )

    @staticmethod
    def _derive_country(region_raw: str) -> str:
        """region 코드 → ISO2 국가 코드 (없으면 원본 대문자)."""
        _region_to_iso2 = {
            "kr": "KR", "jp": "JP", "cn": "CN", "tw": "TW",
            "us": "US", "gb": "GB", "de": "DE", "fr": "FR",
            "it": "IT", "es": "ES", "nl": "NL", "be": "BE",
            "se": "SE", "no": "NO", "dk": "DK", "fi": "FI",
            "pl": "PL", "cz": "CZ", "hu": "HU", "ro": "RO",
            "gr": "GR", "pt": "PT", "at": "AT", "ch": "CH",
            "is": "IS", "ie": "IE",
            "ru": "RU", "ua": "UA", "by": "BY",
            "kz": "KZ", "uz": "UZ", "az": "AZ", "ge": "GE",
            "il": "IL", "tr": "TR", "sa": "SA", "ir": "IR",
            "iq": "IQ", "sy": "SY", "lb": "LB", "jo": "JO",
            "ae": "AE", "qa": "QA", "kw": "KW",
            "me": "ME_REGION",   # sources.yaml 중동 지역 코드
            "in": "IN", "pk": "PK", "bd": "BD", "np": "NP",
            "id": "ID", "my": "MY", "th": "TH", "vn": "VN",
            "ph": "PH", "sg": "SG", "mm": "MM",
            "eg": "EG", "ng": "NG", "za": "ZA", "ke": "KE",
            "et": "ET", "tz": "TZ", "sn": "SN", "gh": "GH",
            "au": "AU", "nz": "NZ",
            "ca": "CA", "mx": "MX", "br": "BR", "ar": "AR",
            "cl": "CL", "co": "CO", "pe": "PE",
            "mn": "MN", "eu": "EU",
        }
        return _region_to_iso2.get(region_raw, region_raw.upper() or "UNKNOWN")

    @staticmethod
    def _derive_tier(
        site_id: str,
        group: str,
        daily_estimate: int,
    ) -> str:
        """site_id + group + daily_estimate → source_tier."""
        if site_id in _GLOBAL_SITES:
            return "GLOBAL"
        if site_id in _NICHE_SITES:
            return "NICHE"
        # Group D = 한국 IT (NICHE에 없으면)
        if group == "D":
            return "NICHE"
        # Group E (English Western) 높은 추정치 → GLOBAL 후보
        if group == "E" and daily_estimate >= 40:
            return "GLOBAL"
        # 나머지 A/B/C (한국 주요) → NATIONAL
        if group in ("A", "B"):
            return "NATIONAL"
        # F-J 국제 → NATIONAL (자국 내 영향력 기준)
        if group in ("F", "G", "H", "I", "J"):
            # 소규모 지역 언론 → REGIONAL
            if daily_estimate <= 10:
                return "REGIONAL"
            return "NATIONAL"
        return "NATIONAL"


# ─────────────────────────────────────────────────────────────
# P1 검증
# ─────────────────────────────────────────────────────────────

_VALID_TIERS = frozenset({"GLOBAL", "NATIONAL", "REGIONAL", "NICHE"})
_VALID_LEANS = frozenset({
    "LEFT", "CENTER_LEFT", "CENTER", "CENTER_RIGHT", "RIGHT", "UNKNOWN"
})


def validate_source_metadata(joiner: SourceMetadataJoiner) -> dict[str, Any]:
    """조이너 메타데이터 품질 검증.

    SM1 — source_tier가 유효 값인지
    SM2 — source_lean이 유효 값인지
    SM3 — source_country가 비어있지 않은지
    SM4 — UNKNOWN lean 비율 보고 (Q12 분석 가능 출처 수 파악)
    """
    errors: list[str] = []
    warnings: list[str] = []

    metas = [joiner.get(sid) for sid in joiner.source_ids()]
    invalid_tier = [m.source_id for m in metas if m.source_tier not in _VALID_TIERS]
    invalid_lean = [m.source_id for m in metas if m.source_lean not in _VALID_LEANS]
    no_country = [m.source_id for m in metas if not m.source_country or m.source_country == "UNKNOWN"]

    if invalid_tier:
        errors.append(f"SM1: 유효하지 않은 source_tier — {invalid_tier[:5]}")
    if invalid_lean:
        errors.append(f"SM2: 유효하지 않은 source_lean — {invalid_lean[:5]}")
    if no_country:
        warnings.append(f"SM3: source_country 미확정 — {no_country[:5]}")

    from collections import Counter
    tier_dist = Counter(m.source_tier for m in metas)
    lean_dist = Counter(m.source_lean for m in metas)
    unknown_lean_ratio = lean_dist.get("UNKNOWN", 0) / max(len(metas), 1)

    if unknown_lean_ratio > 0.80:
        warnings.append(
            f"SM4: source_lean UNKNOWN {unknown_lean_ratio:.0%} "
            f"— Q12 분석 가능 출처 {len(metas) - lean_dist.get('UNKNOWN', 0)}개만"
        )

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "total_sources": len(metas),
        "tier_distribution": dict(tier_dist),
        "lean_distribution": dict(lean_dist),
        "lean_classified_count": len(metas) - lean_dist.get("UNKNOWN", 0),
    }
