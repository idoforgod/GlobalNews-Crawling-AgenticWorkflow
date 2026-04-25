"""articles_enriched.parquet 조립기 — 18-Question Engine의 단일 입력 소스.

입력:
    data/raw/{date}/all_articles.jsonl           (원시 크롤링)
    data/processed/{date}/articles.parquet       (Stage 1: 전처리)
    data/features/{date}/ner.parquet             (Stage 2: NER + geo + signal)
    data/analysis/{date}/article_analysis.parquet (Stage 3: 감성 + STEEPS)
    data/config/sources.yaml                     (출처 메타)
    data/config/source_lean.yaml                 (이념 성향, 선택)

출력:
    data/enriched/{date}/articles_enriched.parquet

스키마 (18개 질문 커버리지):
    identity:   evidence_id, url, source_id, published_date, published_at, crawled_at
    content:    title, body, language, body_char_count, body_quality
    source:     source_country, source_region, source_tier, source_lean
    geo:        geo_focus_primary, geo_focus_all, geo_confidence
    steeps:     steeps_primary, steeps_all, steeps_confidence
    sentiment:  sentiment_score, sentiment_label
    signal:     signal_type, noise_score, novelty_score, burst_score
    entities:   entity_person, entity_org, entity_location, entity_country
    meta:       crawl_method, is_paywall_truncated, source_domain
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# 스키마 정의
# ─────────────────────────────────────────────────────────────

def _enriched_schema():
    """articles_enriched.parquet 전체 스키마."""
    pa, _ = _ensure_pyarrow()
    return pa.schema([
        # ── 식별자 ──
        pa.field("evidence_id",        pa.utf8(),              nullable=False),
        pa.field("url",                pa.utf8(),              nullable=False),
        pa.field("source_id",          pa.utf8(),              nullable=False),
        pa.field("published_date",     pa.date32(),            nullable=True),
        pa.field("published_at",       pa.timestamp("ms", tz="UTC"), nullable=True),
        pa.field("crawled_at",         pa.timestamp("ms", tz="UTC"), nullable=False),
        # ── 원문 텍스트 ──
        pa.field("title",              pa.utf8(),              nullable=False),
        pa.field("body",               pa.utf8(),              nullable=False),
        pa.field("language",           pa.utf8(),              nullable=False),
        pa.field("body_char_count",    pa.int32(),             nullable=False),
        pa.field("body_quality",       pa.utf8(),              nullable=False),
        # ── 출처 메타 (Q5,Q6,Q11,Q12,Q13,Q14) ──
        pa.field("source_country",     pa.utf8(),              nullable=False),
        pa.field("source_region",      pa.utf8(),              nullable=False),
        pa.field("source_tier",        pa.utf8(),              nullable=False),
        pa.field("source_lean",        pa.utf8(),              nullable=False),
        # ── 지리 초점 (Q4,Q5,Q6,Q7) ──
        pa.field("geo_focus_primary",  pa.utf8(),              nullable=False),
        pa.field("geo_focus_all",      pa.list_(pa.utf8()),    nullable=False),
        pa.field("geo_confidence",     pa.float32(),           nullable=False),
        # ── 주제 분류 (Q1~Q3,Q8~Q10,Q17) ──
        pa.field("steeps_primary",     pa.utf8(),              nullable=False),
        pa.field("steeps_all",         pa.list_(pa.utf8()),    nullable=False),
        pa.field("steeps_confidence",  pa.float32(),           nullable=False),
        # ── 감성 (Q4,Q5,Q7,Q15,Q18) ──
        pa.field("sentiment_score",    pa.float32(),           nullable=True),
        pa.field("sentiment_label",    pa.utf8(),              nullable=False),
        # ── 신호 분류 (Q8,Q9,Q10,Q11) ──
        pa.field("signal_type",        pa.utf8(),              nullable=False),
        pa.field("noise_score",        pa.float32(),           nullable=False),
        pa.field("novelty_score",      pa.float32(),           nullable=False),
        pa.field("burst_score",        pa.float32(),           nullable=False),
        # ── 엔티티 (Q7,Q16,Q17,Q18) ──
        pa.field("entity_person",      pa.list_(pa.utf8()),    nullable=False),
        pa.field("entity_org",         pa.list_(pa.utf8()),    nullable=False),
        pa.field("entity_location",    pa.list_(pa.utf8()),    nullable=False),
        pa.field("entity_country",     pa.list_(pa.utf8()),    nullable=False),
        # ── 크롤링 메타 ──
        pa.field("crawl_method",       pa.utf8(),              nullable=False),
        pa.field("is_paywall_truncated", pa.bool_(),           nullable=False),
        pa.field("source_domain",      pa.utf8(),              nullable=False),
        pa.field("content_hash",       pa.utf8(),              nullable=False),
    ])


def _ensure_pyarrow():
    import pyarrow as pa
    import pyarrow.parquet as pq
    return pa, pq


# ─────────────────────────────────────────────────────────────
# 본문 품질 등급
# ─────────────────────────────────────────────────────────────

def _body_quality(body: str, is_paywall: bool) -> str:
    if is_paywall:
        return "PAYWALL"
    n = len(body.strip())
    if n >= 500:
        return "FULL"
    if n >= 100:
        return "PARTIAL"
    return "TITLE_ONLY"


# ─────────────────────────────────────────────────────────────
# 어셈블러
# ─────────────────────────────────────────────────────────────

class ArticlesEnrichedAssembler:
    """articles_enriched.parquet 조립기.

    사용법:
        assembler = ArticlesEnrichedAssembler(date="2026-04-25")
        result = assembler.run()
        # result["output_path"], result["article_count"]
    """

    def __init__(
        self,
        date: str | None = None,
        project_root: str | Path | None = None,
    ) -> None:
        from pathlib import Path as _P
        self._root = _P(project_root) if project_root else _P(__file__).parents[2]
        self._date = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # 입력 경로
        self._raw_jsonl    = self._root / "data" / "raw"    / self._date / "all_articles.jsonl"
        self._articles_pq  = self._root / "data" / "processed" / self._date / "articles.parquet"
        self._ner_pq       = self._root / "data" / "features"  / self._date / "ner.parquet"
        self._analysis_pq  = self._root / "data" / "analysis"  / self._date / "article_analysis.parquet"

        # 출력 경로
        self._output_dir   = self._root / "data" / "enriched" / self._date
        self._output_path  = self._output_dir / "articles_enriched.parquet"

    # ── Public API ───────────────────────────────────────────

    def run(self) -> dict[str, Any]:
        """조립 실행. 모든 입력 파일을 JOIN하여 enriched parquet 생성."""
        t0 = time.monotonic()
        self._output_dir.mkdir(parents=True, exist_ok=True)

        logger.info("enriched_assemble_start date=%s", self._date)

        # 1. 기사 기본 데이터 로드 (JSONL이 최우선, articles.parquet 폴백)
        articles = self._load_base_articles()
        if not articles:
            logger.warning("enriched_no_articles date=%s", self._date)
            return {"output_path": None, "article_count": 0, "elapsed": 0.0}

        logger.info("enriched_base_loaded count=%d", len(articles))

        # 2. NER + Geo + Signal 조인
        ner_data = self._load_ner_data()
        logger.info("enriched_ner_loaded count=%d", len(ner_data))

        # 3. 감성 + STEEPS 조인
        analysis_data = self._load_analysis_data()
        logger.info("enriched_analysis_loaded count=%d", len(analysis_data))

        # 4. 출처 메타 조인
        from src.analysis.source_metadata_joiner import SourceMetadataJoiner
        joiner = SourceMetadataJoiner()

        # 5. 조립
        rows = self._assemble(articles, ner_data, analysis_data, joiner)

        # 6. Parquet 저장
        self._write_parquet(rows)

        elapsed = time.monotonic() - t0
        logger.info(
            "enriched_assemble_done date=%s count=%d elapsed=%.1fs path=%s",
            self._date, len(rows["evidence_id"]), elapsed, self._output_path,
        )

        return {
            "output_path": str(self._output_path),
            "article_count": len(rows["evidence_id"]),
            "elapsed": round(elapsed, 2),
        }

    # ── Private: 로드 ────────────────────────────────────────

    def _load_base_articles(self) -> list[dict[str, Any]]:
        """JSONL → 기사 목록. articles.parquet 폴백."""
        if self._raw_jsonl.exists():
            return self._load_jsonl(self._raw_jsonl)
        if self._articles_pq.exists():
            return self._load_parquet_as_dicts(self._articles_pq)
        logger.error("no_base_data date=%s", self._date)
        return []

    def _load_jsonl(self, path: Path) -> list[dict[str, Any]]:
        import json
        articles = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    articles.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return articles

    def _load_parquet_as_dicts(self, path: Path) -> list[dict[str, Any]]:
        import pyarrow.parquet as pq
        table = pq.read_table(str(path))
        return table.to_pydict()  # type: ignore

    def _load_ner_data(self) -> dict[str, dict[str, Any]]:
        """ner.parquet → {article_id: {...}} 인덱스."""
        if not self._ner_pq.exists():
            logger.warning("ner_parquet_missing path=%s", self._ner_pq)
            return {}
        import pyarrow.parquet as pq
        table = pq.read_table(str(self._ner_pq))
        col_names = table.schema.names
        result: dict[str, dict[str, Any]] = {}
        ids = table.column("article_id").to_pylist()
        for i, aid in enumerate(ids):
            row: dict[str, Any] = {}
            for col in col_names:
                if col == "article_id":
                    continue
                row[col] = table.column(col)[i].as_py()
            result[aid] = row
        return result

    def _load_analysis_data(self) -> dict[str, dict[str, Any]]:
        """article_analysis.parquet → {evidence_id: {...}} 인덱스.

        Stage 3은 article_id(UUID)로 저장하지만 enriched는 evidence_id(ev:...)를
        키로 사용한다. Stage 1 articles.parquet에서 article_id→evidence_id 역매핑을
        구축하여 키를 통일한다.
        """
        if not self._analysis_pq.exists():
            logger.warning("analysis_parquet_missing path=%s", self._analysis_pq)
            return {}
        import pyarrow.parquet as pq

        # Stage 1 역매핑: article_id(UUID) → evidence_id(ev:...)
        uuid_to_evid: dict[str, str] = {}
        if self._articles_pq.exists():
            try:
                s1 = pq.read_table(str(self._articles_pq),
                                   columns=["article_id", "evidence_id"])
                s1_ids   = s1.column("article_id").to_pylist()
                s1_evids = s1.column("evidence_id").to_pylist()
                uuid_to_evid = {a: e for a, e in zip(s1_ids, s1_evids) if a and e}
                logger.info("stage1_id_map_loaded count=%d", len(uuid_to_evid))
            except Exception as exc:
                logger.warning("stage1_id_map_failed error=%s", exc)

        table = pq.read_table(str(self._analysis_pq))
        col_names = table.schema.names
        id_col = "article_id" if "article_id" in col_names else "evidence_id"
        if id_col not in col_names:
            return {}

        result: dict[str, dict[str, Any]] = {}
        ids = table.column(id_col).to_pylist()
        for i, raw_id in enumerate(ids):
            # UUID → evidence_id 변환 (변환 실패 시 원본 키 유지)
            key = uuid_to_evid.get(raw_id, raw_id) if uuid_to_evid else raw_id
            row: dict[str, Any] = {}
            for col in col_names:
                if col == id_col:
                    continue
                row[col] = table.column(col)[i].as_py()
            result[key] = row

        matched = sum(1 for k in result if k.startswith("ev:"))
        logger.info(
            "analysis_data_loaded total=%d evidence_id_matched=%d",
            len(result), matched,
        )
        return result

    # ── Private: 조립 ────────────────────────────────────────

    def _assemble(
        self,
        articles: list[dict[str, Any]],
        ner_data: dict[str, dict[str, Any]],
        analysis_data: dict[str, dict[str, Any]],
        joiner: Any,
    ) -> dict[str, list[Any]]:
        """모든 소스를 JOIN하여 enriched 컬럼 dict 반환."""
        rows: dict[str, list[Any]] = {f.name: [] for f in _enriched_schema()}

        # NER parquet에 geo/signal 컬럼이 없으면 직접 분류 (Stage 2 구버전 대응)
        _ner_has_geo = any(
            "geo_focus_primary" in v for v in list(ner_data.values())[:10]
        )
        _ner_has_signal = any(
            "signal_type" in v for v in list(ner_data.values())[:10]
        )
        _geo_extractor = None
        _signal_classifier = None
        if not _ner_has_geo:
            try:
                from src.analysis.geo_focus_extractor import GeoFocusExtractor
                _geo_extractor = GeoFocusExtractor()
                logger.info("enriched_inline_geo_enabled reason=ner_parquet_missing_columns")
            except Exception as exc:
                logger.warning("geo_extractor_init_failed error=%s", exc)
        if not _ner_has_signal:
            try:
                from src.analysis.signal_classifier import SignalClassifier
                _signal_classifier = SignalClassifier()
                logger.info("enriched_inline_signal_enabled reason=ner_parquet_missing_columns")
            except Exception as exc:
                logger.warning("signal_classifier_init_failed error=%s", exc)

        for art in articles:
            aid = art.get("evidence_id") or art.get("article_id") or ""
            ner  = ner_data.get(aid, {})
            anl  = analysis_data.get(aid, {})
            sid  = art.get("source_id", "")
            meta = joiner.get(sid)

            # 본문 품질
            body  = art.get("body", "") or ""
            is_pw = bool(art.get("is_paywall_truncated", False))
            bq    = _body_quality(body, is_pw)
            lang  = art.get("language", "en") or "en"

            # 발행일 파싱
            pub_at = self._parse_ts(art.get("published_at"))
            pub_dt = pub_at.date() if pub_at else None
            crawl_at = self._parse_ts(art.get("crawled_at")) or datetime.now(timezone.utc)

            # STEEPS (Stage 3 우선, 없으면 STEEPS 분류기 즉시 실행)
            steeps_primary = anl.get("steeps_primary") or self._classify_steeps(
                art.get("title", ""), body, lang,
            )
            steeps_all = anl.get("steeps_all") or [steeps_primary]
            steeps_conf = float(anl.get("steeps_confidence", 0.5))

            # 감성
            sentiment_score = anl.get("sentiment_score")
            sentiment_label = anl.get("sentiment_label") or self._score_to_label(sentiment_score)

            # Geo — NER parquet 우선, 없으면 인라인 분류
            if "geo_focus_primary" in ner:
                geo_primary = ner["geo_focus_primary"]
                geo_all = ner.get("geo_focus_all") or []
                geo_conf = float(ner.get("geo_confidence", 0.0))
            elif _geo_extractor is not None:
                try:
                    gr = _geo_extractor.extract(
                        title=art.get("title", ""),
                        body=body,
                        language=lang,
                        ner_locations=ner.get("entities_location") or [],
                        source_country=meta.source_country if meta else "",
                    )
                    geo_primary = gr.primary
                    geo_all = gr.all_codes
                    geo_conf = gr.confidence
                except Exception:
                    geo_primary, geo_all, geo_conf = "UNKNOWN", [], 0.0
            else:
                geo_primary, geo_all, geo_conf = "UNKNOWN", [], 0.0

            # Signal — NER parquet 우선, 없으면 인라인 분류
            if "signal_type" in ner:
                sig_type = ner["signal_type"]
                noise_score = float(ner.get("noise_score", 0.0))
                novelty_score = float(ner.get("novelty_score", 0.0))
                burst_score = float(ner.get("burst_score", 1.0))
            elif _signal_classifier is not None:
                try:
                    sr = _signal_classifier.classify(
                        title=art.get("title", ""),
                        body=body,
                        language=lang,
                        steeps_all=steeps_all,
                        geo_focus_all=geo_all,
                        body_quality=bq,
                    )
                    sig_type = sr.signal_type
                    noise_score = sr.noise_score
                    novelty_score = sr.novelty_score
                    burst_score = sr.burst_score
                except Exception:
                    sig_type, noise_score, novelty_score, burst_score = "UNCLASSIFIED", 0.0, 0.0, 1.0
            else:
                sig_type, noise_score, novelty_score, burst_score = "UNCLASSIFIED", 0.0, 0.0, 1.0

            rows["evidence_id"].append(aid)
            rows["url"].append(art.get("url", ""))
            rows["source_id"].append(sid)
            rows["published_date"].append(pub_dt)
            rows["published_at"].append(pub_at)
            rows["crawled_at"].append(crawl_at)
            rows["title"].append(art.get("title", ""))
            rows["body"].append(body)
            rows["language"].append(lang)
            rows["body_char_count"].append(len(body))
            rows["body_quality"].append(bq)
            # 출처 메타
            rows["source_country"].append(meta.source_country if meta else "")
            rows["source_region"].append(meta.source_region if meta else "")
            rows["source_tier"].append(meta.source_tier if meta else "UNKNOWN")
            rows["source_lean"].append(meta.source_lean if meta else "UNKNOWN")
            # Geo
            rows["geo_focus_primary"].append(geo_primary)
            rows["geo_focus_all"].append(geo_all)
            rows["geo_confidence"].append(geo_conf)
            # STEEPS
            rows["steeps_primary"].append(steeps_primary)
            rows["steeps_all"].append(steeps_all)
            rows["steeps_confidence"].append(steeps_conf)
            # 감성
            rows["sentiment_score"].append(
                float(sentiment_score) if sentiment_score is not None else None
            )
            rows["sentiment_label"].append(sentiment_label)
            # 신호
            rows["signal_type"].append(sig_type)
            rows["noise_score"].append(noise_score)
            rows["novelty_score"].append(novelty_score)
            rows["burst_score"].append(burst_score)
            # 엔티티
            rows["entity_person"].append(ner.get("entities_person") or [])
            rows["entity_org"].append(ner.get("entities_org") or [])
            rows["entity_location"].append(ner.get("entities_location") or [])
            rows["entity_country"].append(geo_all)
            # 크롤링 메타
            rows["crawl_method"].append(art.get("crawl_method", ""))
            rows["is_paywall_truncated"].append(is_pw)
            rows["source_domain"].append(art.get("source_domain", ""))
            rows["content_hash"].append(art.get("content_hash", ""))

        return rows

    def _write_parquet(self, rows: dict[str, list[Any]]) -> None:
        pa, pq = _ensure_pyarrow()
        schema = _enriched_schema()
        arrays = []
        for field in schema:
            col_data = rows[field.name]
            try:
                arrays.append(pa.array(col_data, type=field.type))
            except Exception as exc:
                logger.warning(
                    "column_cast_failed col=%s type=%s error=%s — using null array",
                    field.name, field.type, exc,
                )
                arrays.append(pa.nulls(len(col_data), type=field.type))
        table = pa.table(dict(zip(schema.names, arrays)), schema=schema)
        pq.write_table(
            table, str(self._output_path),
            compression="zstd", compression_level=3,
        )

    @staticmethod
    def _parse_ts(val: Any) -> datetime | None:
        if val is None:
            return None
        if isinstance(val, datetime):
            return val.replace(tzinfo=timezone.utc) if val.tzinfo is None else val
        if isinstance(val, str):
            try:
                dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
                return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
            except ValueError:
                return None
        return None

    @staticmethod
    def _classify_steeps(title: str, body: str, language: str) -> str:
        """Stage 3 분석 없을 때 즉석 STEEPS 분류 (STEEPSClassifier 재사용)."""
        try:
            from src.analysis.steeps_classifier import STEEPSClassifier
            r = STEEPSClassifier(use_model=False).classify(
                title=title, body=body[:300], language=language,
            )
            return r.primary
        except Exception:
            return "SOC"

    @staticmethod
    def _score_to_label(score: float | None) -> str:
        if score is None:
            return "NEUTRAL"
        if score > 0.1:
            return "POSITIVE"
        if score < -0.1:
            return "NEGATIVE"
        return "NEUTRAL"


# ─────────────────────────────────────────────────────────────
# P1 검증
# ─────────────────────────────────────────────────────────────

_REQUIRED_FIELDS_NOT_NULL = [
    "evidence_id", "url", "source_id", "title", "language",
    "body_quality", "source_tier", "geo_focus_primary",
    "steeps_primary", "sentiment_label", "signal_type",
]


def validate_enriched(output_path: str | Path) -> dict[str, Any]:
    """articles_enriched.parquet P1 품질 검증.

    EN1 — 파일 존재
    EN2 — 최소 1건 이상
    EN3 — 필수 필드 null 없음
    EN4 — body_quality 유효 값
    EN5 — steeps_primary 유효 값
    EN6 — signal_type 유효 값
    EN7 — geo_focus_primary UNKNOWN 비율 보고
    EN8 — sentiment_score null 비율 보고 (Stage 3 미실행 경우 높음)
    """
    errors: list[str] = []
    warnings: list[str] = []
    path = Path(output_path)

    # EN1
    if not path.exists():
        return {"valid": False, "errors": ["EN1: 파일 없음"], "warnings": []}

    import pyarrow.parquet as pq
    table = pq.read_table(str(path))
    n = table.num_rows

    # EN2
    if n == 0:
        errors.append("EN2: 기사 0건")
        return {"valid": False, "errors": errors, "warnings": warnings, "count": 0}

    col_names = set(table.schema.names)

    # EN3
    for f in _REQUIRED_FIELDS_NOT_NULL:
        if f not in col_names:
            errors.append(f"EN3: 필드 누락 — {f}")
            continue
        null_count = table.column(f).null_count
        if null_count > 0:
            errors.append(f"EN3: {f} null {null_count}건")

    # EN4
    valid_bq = {"FULL", "PARTIAL", "TITLE_ONLY", "PAYWALL"}
    if "body_quality" in col_names:
        bq_set = set(table.column("body_quality").to_pylist())
        invalid = bq_set - valid_bq
        if invalid:
            errors.append(f"EN4: body_quality 유효하지 않은 값 — {invalid}")

    # EN5
    valid_steeps = {"SOC", "TEC", "ECO", "ENV", "POL", "SEC", "SPI", "CRS"}
    if "steeps_primary" in col_names:
        sp_set = set(table.column("steeps_primary").to_pylist())
        invalid = sp_set - valid_steeps
        if invalid:
            errors.append(f"EN5: steeps_primary 유효하지 않은 값 — {invalid}")

    # EN6
    valid_sig = {"BREAKING", "TREND", "WEAK_SIGNAL", "NOISE", "UNCLASSIFIED"}
    if "signal_type" in col_names:
        sig_set = set(table.column("signal_type").to_pylist())
        invalid = sig_set - valid_sig
        if invalid:
            errors.append(f"EN6: signal_type 유효하지 않은 값 — {invalid}")

    # EN7
    if "geo_focus_primary" in col_names:
        geo_vals = table.column("geo_focus_primary").to_pylist()
        unknown_ratio = sum(1 for v in geo_vals if v == "UNKNOWN") / n
        if unknown_ratio > 0.70:
            warnings.append(f"EN7: geo_focus UNKNOWN {unknown_ratio:.1%} > 70%")

    # EN8
    if "sentiment_score" in col_names:
        null_ratio = table.column("sentiment_score").null_count / n
        if null_ratio > 0.50:
            warnings.append(
                f"EN8: sentiment_score null {null_ratio:.1%} — Stage 3 미실행 가능"
            )

    # 요약 통계
    from collections import Counter
    stats: dict[str, Any] = {"count": n}
    for col in ("steeps_primary", "signal_type", "source_tier", "body_quality",
                "geo_focus_primary", "sentiment_label"):
        if col in col_names:
            vals = table.column(col).to_pylist()
            stats[col + "_dist"] = dict(Counter(v for v in vals if v).most_common(8))

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        **stats,
    }


# ─────────────────────────────────────────────────────────────
# CLI 진입점
# ─────────────────────────────────────────────────────────────

def run_assembly(date: str, project_root: str | None = None) -> dict[str, Any]:
    """외부에서 호출하는 단일 진입점."""
    assembler = ArticlesEnrichedAssembler(date=date, project_root=project_root)
    result = assembler.run()
    if result.get("output_path"):
        v = validate_enriched(result["output_path"])
        result["validation"] = v
    return result
