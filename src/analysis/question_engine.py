"""18-Question Engine — 매일 18개 분석 질문에 강제 응답하는 엔진.

입력:  data/enriched/{date}/articles_enriched.parquet  (오늘)
       data/enriched/{prev_date}/articles_enriched.parquet (이전 N일, 선택)

출력:  data/answers/{date}/q01.json ~ q18.json
       data/answers/{date}/summary.json  (18개 요약)

P1 보장: 18개 파일 전부 반드시 생성. 빈 파일·null 금지.
          데이터 부족 시 status='insufficient_data'로 최소 응답 출력.

통합: pipeline.py Stage 8 완료 후 post-processing으로 자동 호출.
      main.py --mode analyze 또는 --mode full 시 포함.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _to_list(val: Any) -> list:
    """pandas row에서 꺼낸 list 컬럼을 Python list로 안전 변환.

    parquet list 컬럼은 pandas에서 numpy.ndarray로 로딩되므로
    isinstance(val, list) 체크 대신 이 함수를 사용한다.
    """
    if val is None:
        return []
    if isinstance(val, list):
        return val
    try:
        # numpy.ndarray, pyarrow array, etc.
        return list(val)
    except Exception:
        return []


# ─────────────────────────────────────────────────────────────
# 상수
# ─────────────────────────────────────────────────────────────

_MIN_ARTICLES_FOR_FULL = 50     # 이하 → degraded 응답
_HISTORY_DAYS_SHORT   = 7
_HISTORY_DAYS_LONG    = 30
_TOP_N                = 10      # 대부분의 질문에서 상위 N개

_VALID_STATUS = frozenset({"answered", "insufficient_data", "degraded"})


# ─────────────────────────────────────────────────────────────
# 결과 데이터클래스
# ─────────────────────────────────────────────────────────────

@dataclass
class QuestionAnswer:
    """단일 질문의 응답.

    Attributes:
        question_id:        Q01 ~ Q18
        question_ko:        질문 (한국어)
        date:               분석 날짜
        status:             answered | insufficient_data | degraded
        confidence:         0-1, 데이터 충분도
        data_days_available: 사용 가능한 과거 데이터 일수
        answer:             질문별 구조화된 답 dict
        top_findings:       자연어 핵심 발견 (1-3개)
        next_watch:         다음에 주목할 주제 (0-3개)
        elapsed_ms:         계산 소요 시간
    """
    question_id: str
    question_ko: str
    date: str
    status: str = "answered"
    confidence: float = 1.0
    data_days_available: int = 1
    answer: dict[str, Any] = field(default_factory=dict)
    top_findings: list[str] = field(default_factory=list)
    next_watch: list[str] = field(default_factory=list)
    elapsed_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


# ─────────────────────────────────────────────────────────────
# 헬퍼: 데이터 로딩
# ─────────────────────────────────────────────────────────────

def _load_enriched(path: Path) -> "pd.DataFrame | None":
    """enriched parquet → DataFrame. 실패 시 None."""
    try:
        import pandas as pd
        import pyarrow.parquet as pq
        if not path.exists():
            return None
        table = pq.read_table(str(path))
        df = table.to_pandas()
        return df
    except Exception as exc:
        logger.warning("load_enriched_failed path=%s error=%s", path, exc)
        return None


def _load_history(
    base_dir: Path,
    today_date: str,
    days: int,
) -> "pd.DataFrame | None":
    """지난 N일 enriched parquet 합산. 없으면 None."""
    try:
        import pandas as pd
        today = date.fromisoformat(today_date)
        frames: list[pd.DataFrame] = []
        for d in range(1, days + 1):
            dt = (today - timedelta(days=d)).isoformat()
            path = base_dir / dt / "articles_enriched.parquet"
            df = _load_enriched(path)
            if df is not None:
                df["_hist_date"] = dt
                frames.append(df)
        if not frames:
            return None
        return pd.concat(frames, ignore_index=True)
    except Exception as exc:
        logger.warning("load_history_failed error=%s", exc)
        return None


def _insufficient(q_id: str, q_ko: str, date: str, reason: str) -> QuestionAnswer:
    """데이터 부족 시 표준 응답."""
    return QuestionAnswer(
        question_id=q_id,
        question_ko=q_ko,
        date=date,
        status="insufficient_data",
        confidence=0.0,
        data_days_available=0,
        answer={"reason": reason},
        top_findings=[reason],
        next_watch=[],
    )


# ─────────────────────────────────────────────────────────────
# 18-Question Engine
# ─────────────────────────────────────────────────────────────

class QuestionEngine:
    """18개 분석 질문에 매일 강제 응답하는 엔진.

    사용법:
        engine = QuestionEngine(date="2026-04-25", project_root=PROJECT_ROOT)
        results = engine.run_all()
        # data/answers/2026-04-25/q01.json ~ q18.json 생성

    영구 파이프라인 통합:
        pipeline.py → AnalysisPipeline.run() Stage 8 완료 후 자동 호출
        main.py cmd_analyze() → run_analysis_pipeline() 내부에서 호출
    """

    def __init__(
        self,
        date: str | None = None,
        project_root: str | Path | None = None,
    ) -> None:
        from pathlib import Path as _P
        self._root = _P(project_root) if project_root else _P(__file__).parents[2]
        self._date = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")

        self._enriched_dir = self._root / "data" / "enriched"
        self._answers_dir  = self._root / "data" / "answers" / self._date

        self._today_path = self._enriched_dir / self._date / "articles_enriched.parquet"

        # 지연 로딩 — 처음 접근 시 로드
        self._today_df:   "pd.DataFrame | None" = None
        self._hist7_df:   "pd.DataFrame | None" = None
        self._hist30_df:  "pd.DataFrame | None" = None
        self._loaded_today = False
        self._loaded_hist7 = False
        self._loaded_hist30 = False

    # ── Public API ───────────────────────────────────────────

    def run_all(self) -> list[QuestionAnswer]:
        """18개 질문 전부 실행. 각각 JSON 파일로 저장. P1 보장."""
        self._answers_dir.mkdir(parents=True, exist_ok=True)
        results: list[QuestionAnswer] = []
        t_total = time.monotonic()

        handlers = [
            self._q01, self._q02, self._q03, self._q04, self._q05,
            self._q06, self._q07, self._q08, self._q09, self._q10,
            self._q11, self._q12, self._q13, self._q14, self._q15,
            self._q16, self._q17, self._q18,
        ]

        for i, handler in enumerate(handlers, start=1):
            q_id = f"Q{i:02d}"
            t0 = time.monotonic()
            try:
                ans = handler()
            except Exception as exc:
                logger.error("question_engine_handler_failed q=%s error=%s", q_id, exc)
                ans = _insufficient(q_id, "오류로 인한 미응답", self._date, str(exc))
            ans.elapsed_ms = round((time.monotonic() - t0) * 1000, 1)
            out_path = self._answers_dir / f"q{i:02d}.json"
            ans.save(out_path)
            results.append(ans)
            logger.debug(
                "question_engine_q%02d status=%s confidence=%.2f elapsed_ms=%.0f",
                i, ans.status, ans.confidence, ans.elapsed_ms,
            )

        # 요약 파일
        self._save_summary(results)
        logger.info(
            "question_engine_complete date=%s elapsed=%.1fs",
            self._date, time.monotonic() - t_total,
        )
        return results

    def run_single(self, q_num: int) -> QuestionAnswer:
        """단일 질문 실행 (재생성·디버깅용)."""
        handlers = [
            None,  # 0-indexed placeholder
            self._q01, self._q02, self._q03, self._q04, self._q05,
            self._q06, self._q07, self._q08, self._q09, self._q10,
            self._q11, self._q12, self._q13, self._q14, self._q15,
            self._q16, self._q17, self._q18,
        ]
        if not 1 <= q_num <= 18:
            raise ValueError(f"q_num must be 1-18, got {q_num}")
        ans = handlers[q_num]()
        ans.save(self._answers_dir / f"q{q_num:02d}.json")
        return ans

    # ── Data Accessors (지연 로딩) ────────────────────────────

    @property
    def _today(self) -> "pd.DataFrame | None":
        if not self._loaded_today:
            self._today_df = _load_enriched(self._today_path)
            self._loaded_today = True
        return self._today_df

    @property
    def _hist7(self) -> "pd.DataFrame | None":
        if not self._loaded_hist7:
            self._hist7_df = _load_history(self._enriched_dir, self._date, _HISTORY_DAYS_SHORT)
            self._loaded_hist7 = True
        return self._hist7_df

    @property
    def _hist30(self) -> "pd.DataFrame | None":
        if not self._loaded_hist30:
            self._hist30_df = _load_history(self._enriched_dir, self._date, _HISTORY_DAYS_LONG)
            self._loaded_hist30 = True
        return self._hist30_df

    def _hist_days(self) -> int:
        """사용 가능한 과거 데이터 일수."""
        if self._hist30 is not None:
            return int(self._hist30["_hist_date"].nunique())
        if self._hist7 is not None:
            return int(self._hist7["_hist_date"].nunique())
        return 0

    # ── Q01: 버스트 탐지 ─────────────────────────────────────

    def _q01(self) -> QuestionAnswer:
        """오늘 갑자기 급증한 주제는 무엇인가?"""
        df = self._today
        if df is None or len(df) < 5:
            return _insufficient("Q01", "이 주제는 언제 갑자기 터졌나? (버스트 탐지)",
                                  self._date, "오늘 데이터 없음")

        import pandas as pd

        # 오늘 STEEPS별 기사 수
        today_counts = df.groupby("steeps_primary").size().to_dict()

        # 과거 30일 평균 (있으면)
        hist = self._hist30
        bursting: list[dict] = []

        if hist is not None:
            hist_avg = (hist.groupby(["_hist_date", "steeps_primary"])
                        .size().reset_index(name="cnt")
                        .groupby("steeps_primary")["cnt"].mean().to_dict())
            for steeps, count in sorted(today_counts.items(), key=lambda x: -x[1]):
                avg = hist_avg.get(steeps, 0)
                burst_score = count / avg if avg > 0 else float(count)
                bursting.append({
                    "steeps": steeps,
                    "article_count_today": int(count),
                    "avg_30d": round(float(avg), 1),
                    "burst_score": round(float(burst_score), 2),
                })
        else:
            for steeps, count in sorted(today_counts.items(), key=lambda x: -x[1]):
                bursting.append({
                    "steeps": steeps,
                    "article_count_today": int(count),
                    "avg_30d": None,
                    "burst_score": None,
                })

        top = sorted(bursting, key=lambda x: x["burst_score"] or 0, reverse=True)
        findings = []
        for b in top[:3]:
            if b["burst_score"] and b["burst_score"] > 2.0:
                findings.append(
                    f'{b["steeps"]}: 오늘 {b["article_count_today"]}건'
                    f' (30일 평균 대비 {b["burst_score"]:.1f}배)'
                )
            elif b["avg_30d"] is None:
                findings.append(
                    f'{b["steeps"]}: 오늘 {b["article_count_today"]}건 (기준선 없음)'
                )

        conf = 0.90 if hist is not None else 0.40
        return QuestionAnswer(
            question_id="Q01",
            question_ko="이 주제는 언제 갑자기 터졌나? (버스트 탐지)",
            date=self._date,
            status="answered",
            confidence=conf,
            data_days_available=self._hist_days() + 1,
            answer={"bursting_topics": top[:_TOP_N]},
            top_findings=findings or [f"오늘 기사 {len(df)}건, 버스트 분석 가능"],
            next_watch=[b["steeps"] for b in top[:3] if (b["burst_score"] or 0) > 3.0],
        )

    # ── Q02: 트렌드 추이 ─────────────────────────────────────

    def _q02(self) -> QuestionAnswer:
        """지금 성장 중인 트렌드 vs 소멸 중인 트렌드는?"""
        df = self._today
        if df is None:
            return _insufficient("Q02", "성장/소멸 트렌드 추이", self._date, "오늘 데이터 없음")

        hist = self._hist7
        days_avail = self._hist_days() + 1

        if hist is None:
            # history 없으면 오늘 STEEPS 분포만 출력
            counts = df["steeps_primary"].value_counts().head(_TOP_N)
            return QuestionAnswer(
                question_id="Q02",
                question_ko="성장 중인 트렌드 vs 소멸 중인 트렌드는?",
                date=self._date,
                status="degraded",
                confidence=0.20,
                data_days_available=1,
                answer={"today_distribution": counts.to_dict(), "trend": "기준선 없음 — 7일 후 활성화"},
                top_findings=["트렌드 분석: 7일 누적 데이터 필요"],
                next_watch=[],
            )

        import pandas as pd
        all_df = pd.concat([hist.assign(_hist_date=hist["_hist_date"]),
                            df.assign(_hist_date=self._date)], ignore_index=True)
        daily = (all_df.groupby(["_hist_date", "steeps_primary"])
                 .size().reset_index(name="cnt"))
        trend_data: dict[str, dict] = {}
        for steeps, grp in daily.groupby("steeps_primary"):
            grp = grp.sort_values("_hist_date")
            counts = grp["cnt"].tolist()
            if len(counts) >= 2:
                # 단순 기울기: (마지막 - 평균) / 평균
                avg = sum(counts[:-1]) / len(counts[:-1])
                latest = counts[-1]
                slope = (latest - avg) / (avg or 1)
                trend_data[str(steeps)] = {
                    "trend_score": round(float(slope), 3),
                    "today": int(latest),
                    "avg_prev_7d": round(float(avg), 1),
                }

        rising  = [{"steeps": k, **v} for k, v in trend_data.items() if v["trend_score"] > 0.2]
        falling = [{"steeps": k, **v} for k, v in trend_data.items() if v["trend_score"] < -0.2]
        stable  = [{"steeps": k, **v} for k, v in trend_data.items()
                   if -0.2 <= v["trend_score"] <= 0.2]

        rising  = sorted(rising,  key=lambda x: -x["trend_score"])
        falling = sorted(falling, key=lambda x: x["trend_score"])

        findings = []
        for r in rising[:2]:
            findings.append(f'{r["steeps"]} 상승: 오늘 {r["today"]}건 (7일 평균 {r["avg_prev_7d"]:.0f}건)')
        for f in falling[:1]:
            findings.append(f'{f["steeps"]} 하락: 오늘 {f["today"]}건 (7일 평균 {f["avg_prev_7d"]:.0f}건)')

        conf = 0.75 + min(0.15, (days_avail - 7) * 0.01) if days_avail >= 7 else 0.75
        return QuestionAnswer(
            question_id="Q02",
            question_ko="성장 중인 트렌드 vs 소멸 중인 트렌드는?",
            date=self._date,
            status="answered",
            confidence=round(conf, 2),
            data_days_available=days_avail,
            answer={"rising": rising, "falling": falling, "stable": stable},
            top_findings=findings or ["오늘 트렌드 변동 없음"],
            next_watch=[r["steeps"] for r in rising[:2]],
        )

    # ── Q03: 사건 전후 변화 ──────────────────────────────────

    def _q03(self) -> QuestionAnswer:
        """특정 사건 전후로 보도 구조가 어떻게 바뀌었나?"""
        hist = self._hist30
        days = self._hist_days()

        if hist is None or days < 14:
            return QuestionAnswer(
                question_id="Q03",
                question_ko="사건 전후 보도 구조 변화",
                date=self._date,
                status="insufficient_data",
                confidence=0.0,
                data_days_available=days,
                answer={"reason": f"기준선 구축 중 — 14일 후 활성화 (현재 {days}일)"},
                top_findings=[f"기준선 구축 중 ({days}/14일)"],
                next_watch=[],
            )

        import pandas as pd
        today = date.fromisoformat(self._date)
        midpoint = (today - timedelta(days=7)).isoformat()

        before = hist[hist["_hist_date"] < midpoint]
        after  = hist[hist["_hist_date"] >= midpoint]

        before_dist = before["steeps_primary"].value_counts().to_dict() if len(before) else {}
        after_dist  = after["steeps_primary"].value_counts().to_dict() if len(after) else {}

        all_cats = set(before_dist) | set(after_dist)
        deltas: list[dict] = []
        for cat in sorted(all_cats):
            b = before_dist.get(cat, 0)
            a = after_dist.get(cat, 0)
            pct = ((a - b) / (b or 1)) * 100
            deltas.append({"steeps": cat, "before_7d": int(b), "after_7d": int(a),
                           "change_pct": round(pct, 1)})
        deltas.sort(key=lambda x: abs(x["change_pct"]), reverse=True)

        findings = []
        for d in deltas[:3]:
            if abs(d["change_pct"]) > 20:
                trend = "증가" if d["change_pct"] > 0 else "감소"
                findings.append(f'{d["steeps"]}: 최근 7일 대비 {abs(d["change_pct"]):.0f}% {trend}')

        conf = 0.70 + min(0.20, (days - 14) * 0.01) if days >= 14 else 0.70
        return QuestionAnswer(
            question_id="Q03",
            question_ko="사건 전후 보도 구조 변화",
            date=self._date,
            status="answered",
            confidence=round(conf, 2),
            data_days_available=days,
            answer={"period_comparison": deltas, "before_period": "D-14 ~ D-7", "after_period": "D-7 ~ 오늘"},
            top_findings=findings or ["7일 전후 보도 구조 변화 없음"],
            next_watch=[d["steeps"] for d in deltas[:2] if abs(d["change_pct"]) > 30],
        )

    # ── Q04: 프레이밍 차이 ──────────────────────────────────

    def _q04(self) -> QuestionAnswer:
        """같은 주제를 A/B 언어 언론이 어떻게 다르게 프레이밍하는가?"""
        df = self._today
        if df is None or len(df) < 20:
            return _insufficient("Q04", "언어별 프레이밍 차이", self._date, "데이터 부족")

        frames: list[dict] = []
        top_steeps = df["steeps_primary"].value_counts().head(3).index.tolist()

        for steeps in top_steeps:
            sub = df[df["steeps_primary"] == steeps]
            lang_kw: dict[str, list[str]] = {}
            for lang, grp in sub.groupby("language"):
                if len(grp) < 3:
                    continue
                # 제목에서 단어 빈도 추출 (단순 tokenization)
                all_words: list[str] = []
                for title in grp["title"].fillna("").tolist():
                    words = re.findall(r"[가-힣]{2,}|[a-zA-Z]{3,}", title.lower())
                    all_words.extend(words)
                stop_words = {"and", "the", "in", "of", "to", "a", "is", "for",
                              "그", "이", "에", "를", "이", "가", "의", "은", "는"}
                top_words = [w for w, _ in Counter(all_words).most_common(8)
                             if w not in stop_words]
                if top_words:
                    lang_kw[str(lang)] = top_words

            if len(lang_kw) >= 2:
                frames.append({
                    "steeps": steeps,
                    "article_count": int(len(sub)),
                    "language_frames": lang_kw,
                })

        findings = []
        for f in frames[:2]:
            langs = list(f["language_frames"].keys())
            if len(langs) >= 2:
                findings.append(
                    f'{f["steeps"]}: {"/".join(langs)} 프레이밍 비교 가능'
                )

        return QuestionAnswer(
            question_id="Q04",
            question_ko="같은 주제를 A/B 언어 언론이 어떻게 다르게 프레이밍하는가?",
            date=self._date,
            status="answered" if frames else "degraded",
            confidence=0.65 if frames else 0.20,
            data_days_available=self._hist_days() + 1,
            answer={"framing_comparison": frames[:_TOP_N]},
            top_findings=findings or ["오늘 언어별 프레이밍 차이 추출 가능 주제 없음"],
            next_watch=[f["steeps"] for f in frames[:2]],
        )

    # ── Q05: 국가별 감성 ─────────────────────────────────────

    def _q05(self) -> QuestionAnswer:
        """특정 국가에 대한 글로벌 감성이 변화하고 있나?"""
        df = self._today
        if df is None:
            return _insufficient("Q05", "국가별 글로벌 감성", self._date, "데이터 없음")

        # source_country 기준 감성 집계
        if "sentiment_score" not in df.columns or df["sentiment_score"].isnull().all():
            # 감성 없으면 기사 수만 (키를 "country"로 통일)
            counts = (df.groupby("source_country")
                      .size().reset_index(name="article_count")
                      .rename(columns={"source_country": "country"})
                      .sort_values("article_count", ascending=False)
                      .head(_TOP_N))
            result = counts.to_dict("records")
            status = "degraded"
            conf = 0.30
            findings = ["sentiment 미산출 — 출처 국가별 기사 수만 출력"]
        else:
            agg = (df[df["sentiment_score"].notna()]
                   .groupby("source_country")
                   .agg(
                       avg_sentiment=("sentiment_score", "mean"),
                       article_count=("sentiment_score", "count"),
                   )
                   .reset_index()
                   .sort_values("article_count", ascending=False)
                   .head(_TOP_N))
            result = [
                {
                    "country": row["source_country"],
                    "avg_sentiment": round(float(row["avg_sentiment"]), 3),
                    "article_count": int(row["article_count"]),
                    "sentiment_label": ("POSITIVE" if row["avg_sentiment"] > 0.1
                                        else "NEGATIVE" if row["avg_sentiment"] < -0.1
                                        else "NEUTRAL"),
                }
                for _, row in agg.iterrows()
            ]
            status = "answered"
            conf = 0.80
            top = sorted(result, key=lambda x: x["avg_sentiment"])
            findings = []
            if top:
                findings.append(f'부정 감성 최고: {top[0]["country"]} ({top[0]["avg_sentiment"]:.2f})')
            if len(top) > 1:
                findings.append(f'긍정 감성 최고: {top[-1]["country"]} ({top[-1]["avg_sentiment"]:.2f})')

        return QuestionAnswer(
            question_id="Q05",
            question_ko="특정 국가에 대한 글로벌 감성이 변화하고 있나?",
            date=self._date,
            status=status,
            confidence=conf,
            data_days_available=self._hist_days() + 1,
            answer={"country_sentiments": result},
            top_findings=findings,
            next_watch=[r["country"] for r in sorted(result, key=lambda x: abs(x.get("avg_sentiment", 0)), reverse=True)[:2]],
        )

    # ── Q06: 어두운 구석 ─────────────────────────────────────

    def _q06(self) -> QuestionAnswer:
        """국제 뉴스에서 주목받지 못하는 지역은 어디인가?"""
        df = self._today
        if df is None:
            return _insufficient("Q06", "보도 사각지대 (어두운 구석)", self._date, "데이터 없음")

        region_counts = df.groupby("source_region").agg(
            article_count=("source_id", "count"),
            source_count=("source_id", "nunique"),
        ).reset_index()

        total = len(df)
        dark_corners = []
        for _, row in region_counts.iterrows():
            region = str(row["source_region"])
            count = int(row["article_count"])
            coverage_pct = count / total if total > 0 else 0
            dark_corners.append({
                "region": region,
                "article_count": count,
                "source_count": int(row["source_count"]),
                "coverage_pct": round(coverage_pct * 100, 1),
            })

        dark_corners.sort(key=lambda x: x["coverage_pct"])
        bottom20 = [d for d in dark_corners if d["coverage_pct"] < 3.0]

        findings = []
        for d in bottom20[:3]:
            findings.append(
                f'{d["region"]}: 전체 보도의 {d["coverage_pct"]:.1f}% '
                f'({d["article_count"]}건, {d["source_count"]}개 출처)'
            )

        return QuestionAnswer(
            question_id="Q06",
            question_ko="국제 뉴스에서 주목받지 못하는 '어두운 구석'은 어디인가?",
            date=self._date,
            status="answered",
            confidence=0.85,
            data_days_available=self._hist_days() + 1,
            answer={
                "all_regions": dark_corners,
                "dark_corners_under3pct": bottom20,
                "most_covered": dark_corners[-1] if dark_corners else {},
                "least_covered": dark_corners[0] if dark_corners else {},
            },
            top_findings=findings or ["보도 사각지대 없음 (모든 지역 3% 이상)"],
            next_watch=[d["region"] for d in bottom20[:2]],
        )

    # ── Q07: 양국 관계 긴장 ─────────────────────────────────

    def _q07(self) -> QuestionAnswer:
        """어떤 양국 관계가 급격히 긴장 또는 완화되고 있나?"""
        df = self._today
        if df is None:
            return _insufficient("Q07", "양국 관계 긴장/완화", self._date, "데이터 없음")

        # geo_focus_all에서 국가 쌍 추출
        pairs: Counter = Counter()
        pair_sentiments: dict[str, list[float]] = defaultdict(list)

        has_sentiment = ("sentiment_score" in df.columns
                         and not df["sentiment_score"].isnull().all())

        for _, row in df.iterrows():
            geo_all = _to_list(row.get("geo_focus_all"))
            countries = [g for g in geo_all if isinstance(g, str)
                         and len(g) == 2 and g.isupper()]
            countries = list(set(countries))
            if len(countries) < 2:
                continue
            for i in range(len(countries)):
                for j in range(i + 1, len(countries)):
                    pair = tuple(sorted([countries[i], countries[j]]))
                    pairs[pair] += 1
                    if has_sentiment and row.get("sentiment_score") is not None:
                        pair_sentiments[str(pair)].append(float(row["sentiment_score"]))

        top_pairs = pairs.most_common(_TOP_N)
        result = []
        for pair, count in top_pairs:
            sents = pair_sentiments.get(str(pair), [])
            avg_sent = sum(sents) / len(sents) if sents else None
            result.append({
                "pair": list(pair),
                "article_count": count,
                "avg_sentiment": round(avg_sent, 3) if avg_sent is not None else None,
            })

        result.sort(key=lambda x: abs(x["avg_sentiment"] or 0), reverse=True)
        findings = []
        for r in result[:2]:
            if r["avg_sentiment"] is not None:
                tone = "부정적" if r["avg_sentiment"] < -0.1 else "긍정적" if r["avg_sentiment"] > 0.1 else "중립"
                findings.append(
                    f'{"-".join(r["pair"])}: {r["article_count"]}건, 논조 {tone} ({r["avg_sentiment"]:.2f})'
                )

        days = self._hist_days()
        status = "answered" if result else "degraded"
        conf = 0.75 if result and has_sentiment else 0.45

        return QuestionAnswer(
            question_id="Q07",
            question_ko="어떤 양국 관계가 급격히 긴장 또는 완화되고 있나?",
            date=self._date,
            status=status,
            confidence=conf,
            data_days_available=days + 1,
            answer={"country_pairs": result},
            top_findings=findings or ["오늘 양국 관계 주목 이슈 없음"],
            next_watch=["-".join(r["pair"]) for r in result[:2]
                        if r["avg_sentiment"] is not None and abs(r["avg_sentiment"]) > 0.2],
        )

    # ── Q08: 약한 신호 ──────────────────────────────────────

    def _q08(self) -> QuestionAnswer:
        """아직 주류가 아니지만 부상하는 약한 신호는?"""
        df = self._today
        if df is None:
            return _insufficient("Q08", "약한 신호 (Weak Signal) 탐지", self._date, "데이터 없음")

        # WEAK_SIGNAL 또는 novelty_score >= 0.6
        ws_mask = (
            (df.get("signal_type", "") == "WEAK_SIGNAL") |
            (df.get("novelty_score", 0) >= 0.60)
        )
        ws_df = df[ws_mask].copy() if "signal_type" in df.columns else df[df.get("novelty_score", 0) >= 0.60].copy()

        if len(ws_df) == 0:
            # UNCLASSIFIED이면서 novelty 높은 것
            if "novelty_score" in df.columns:
                ws_df = df.nlargest(5, "novelty_score")

        signals = []
        for _, row in ws_df.head(_TOP_N).iterrows():
            signals.append({
                "title": str(row.get("title", ""))[:80],
                "source_id": str(row.get("source_id", "")),
                "steeps": str(row.get("steeps_primary", "")),
                "language": str(row.get("language", "")),
                "geo_focus": str(row.get("geo_focus_primary", "")),
                "novelty_score": round(float(row.get("novelty_score", 0) or 0), 3),
                "signal_type": str(row.get("signal_type", "UNCLASSIFIED")),
            })

        findings = []
        for s in signals[:3]:
            findings.append(
                f'[{s["steeps"]}] {s["title"]} '
                f'(novelty={s["novelty_score"]:.2f})'
            )

        return QuestionAnswer(
            question_id="Q08",
            question_ko="아직 주류가 아니지만 부상하는 약한 신호는?",
            date=self._date,
            status="answered" if signals else "degraded",
            confidence=0.65 if signals else 0.30,
            data_days_available=self._hist_days() + 1,
            answer={"weak_signals": signals, "total_detected": len(signals)},
            top_findings=findings or ["오늘 약한 신호 탐지 없음 (임계값 미달)"],
            next_watch=[s["steeps"] for s in signals[:3]],
        )

    # ── Q09: 패러다임 전환 전조 ──────────────────────────────

    def _q09(self) -> QuestionAnswer:
        """어떤 이슈가 패러다임 전환의 전조를 보이는가?"""
        df = self._today
        if df is None:
            return _insufficient("Q09", "패러다임 전환 전조 탐지", self._date, "데이터 없음")

        # 복수 STEEPS (3개 이상) + 복수 언어 보도
        candidates = []
        for _, row in df.iterrows():
            steeps_all = _to_list(row.get("steeps_all"))
            if "CRS" in steeps_all and len(steeps_all) >= 3:
                candidates.append({
                    "title": str(row.get("title", ""))[:80],
                    "steeps_all": steeps_all,
                    "language": str(row.get("language", "")),
                    "steeps_count": len([s for s in steeps_all if s != "CRS"]),
                    "novelty_score": float(row.get("novelty_score", 0) or 0),
                })

        # 같은 geo_focus에 다국어 커버리지 높은 것
        geo_lang: dict[str, set] = defaultdict(set)
        for _, row in df.iterrows():
            geo = str(row.get("geo_focus_primary", "UNKNOWN"))
            lang = str(row.get("language", ""))
            if geo != "UNKNOWN" and lang:
                geo_lang[geo].add(lang)

        multi_lang_geo = [
            {"geo": geo, "language_count": len(langs), "languages": list(langs)}
            for geo, langs in geo_lang.items()
            if len(langs) >= 3
        ]
        multi_lang_geo.sort(key=lambda x: -x["language_count"])

        findings = []
        for c in candidates[:2]:
            findings.append(
                f'복합 도메인: {"/".join(c["steeps_all"])} '
                f'({c["title"][:40]}...)'
            )
        for g in multi_lang_geo[:1]:
            findings.append(
                f'{g["geo"]}: {g["language_count"]}개 언어권 동시 보도'
            )

        return QuestionAnswer(
            question_id="Q09",
            question_ko="어떤 이슈가 패러다임 전환의 전조를 보이는가?",
            date=self._date,
            status="answered",
            confidence=0.55 if (candidates or multi_lang_geo) else 0.30,
            data_days_available=self._hist_days() + 1,
            answer={
                "cross_domain_articles": candidates[:_TOP_N],
                "multi_language_geo": multi_lang_geo[:_TOP_N],
            },
            top_findings=findings or ["오늘 패러다임 전환 전조 없음"],
            next_watch=[c["steeps_all"][0] for c in candidates[:2]],
        )

    # ── Q10: 의제 이동 패턴 ─────────────────────────────────

    def _q10(self) -> QuestionAnswer:
        """비주류→주류로 이동하는 의제 패턴은?"""
        df = self._today
        days = self._hist_days()

        if days < 21:
            return QuestionAnswer(
                question_id="Q10",
                question_ko="비주류→주류 의제 이동 패턴",
                date=self._date,
                status="insufficient_data",
                confidence=0.0,
                data_days_available=days,
                answer={"reason": f"이동 패턴 분석: 21일 필요 (현재 {days}일)"},
                top_findings=[f"누적 중 ({days}/21일)"],
                next_watch=[],
            )

        hist = self._hist30
        import pandas as pd
        all_df = pd.concat([hist.assign(_is_hist=True),
                            df.assign(_hist_date=self._date, _is_hist=False)], ignore_index=True)

        # 소스 수 추이로 비주류→주류 탐지 (임계값 1.2x로 낮춤)
        source_trend = (all_df.groupby(["_hist_date", "steeps_primary"])
                        .agg(source_count=("source_id", "nunique"))
                        .reset_index())
        movements = []
        stable_steeps = []
        for steeps, grp in source_trend.groupby("steeps_primary"):
            grp = grp.sort_values("_hist_date")
            first_half = grp.head(len(grp) // 2)["source_count"].mean()
            second_half = grp.tail(len(grp) // 2)["source_count"].mean()
            ratio = second_half / first_half if first_half > 0 else 1.0
            if first_half > 0 and ratio > 1.2:
                movements.append({
                    "steeps": str(steeps),
                    "first_half_sources": round(float(first_half), 1),
                    "second_half_sources": round(float(second_half), 1),
                    "growth_ratio": round(float(ratio), 2),
                    "trend_direction": "rising",
                })
            elif first_half > 0 and ratio < 0.8:
                movements.append({
                    "steeps": str(steeps),
                    "first_half_sources": round(float(first_half), 1),
                    "second_half_sources": round(float(second_half), 1),
                    "growth_ratio": round(float(ratio), 2),
                    "trend_direction": "falling",
                })
            else:
                stable_steeps.append(str(steeps))
        movements.sort(key=lambda x: -abs(x["growth_ratio"] - 1.0))

        findings = [
            f'{m["steeps"]}: {m["trend_direction"]} — 출처 {m["first_half_sources"]:.0f}→{m["second_half_sources"]:.0f}건 ({m["growth_ratio"]:.1f}×)'
            for m in movements[:3]
        ]
        if not findings:
            findings = [f"의제 안정 구간: {', '.join(stable_steeps[:4])}"]

        conf_q10 = 0.70 + min(0.20, (days - 21) * 0.01) if days >= 21 else 0.70
        return QuestionAnswer(
            question_id="Q10",
            question_ko="비주류→주류 의제 이동 패턴은?",
            date=self._date,
            status="answered",
            confidence=round(conf_q10, 2),
            data_days_available=days + 1,
            answer={"agenda_movements": movements[:_TOP_N]},
            top_findings=findings or ["의제 이동 패턴 없음 (안정적)"],
            next_watch=[m["steeps"] for m in movements[:2]],
        )

    # ── Q11: 의제 선점 ──────────────────────────────────────

    def _q11(self) -> QuestionAnswer:
        """어떤 언론사가 오늘 의제를 선점(최초 보도)하는가?"""
        df = self._today
        if df is None:
            return _insufficient("Q11", "의제 선점 (최초 보도 출처)", self._date, "데이터 없음")

        hist = self._hist30

        if hist is None:
            # history 없으면 오늘 source별 기사 수만
            top_sources = (df.groupby(["source_id", "source_tier"])
                           .size().reset_index(name="count")
                           .sort_values("count", ascending=False)
                           .head(_TOP_N))
            return QuestionAnswer(
                question_id="Q11",
                question_ko="어떤 언론사가 오늘 의제를 선점하는가?",
                date=self._date,
                status="degraded",
                confidence=0.30,
                data_days_available=1,
                answer={
                    "top_sources_today": top_sources.to_dict("records"),
                    "agenda_setters": [],
                    "note": "의제 선점 분석: 30일 이력 필요",
                },
                top_findings=["의제 선점 분석: 30일 이력 필요"],
                next_watch=[],
            )

        # 30일 이력에 없는 주제 = 오늘 신규 = 선점 후보
        import pandas as pd
        hist_steeps = set(hist["steeps_primary"].unique())
        new_today = df[~df["steeps_primary"].isin(hist_steeps)]

        setters = (new_today.groupby(["source_id", "source_tier"])
                   .size().reset_index(name="new_topic_count")
                   .sort_values("new_topic_count", ascending=False)
                   .head(_TOP_N))

        findings = []
        for _, row in setters.head(3).iterrows():
            findings.append(
                f'{row["source_id"]} ({row["source_tier"]}): '
                f'신규 {row["new_topic_count"]}개 주제 선도'
            )

        return QuestionAnswer(
            question_id="Q11",
            question_ko="어떤 언론사가 오늘 의제를 선점하는가?",
            date=self._date,
            status="answered",
            confidence=0.75,
            data_days_available=self._hist_days() + 1,
            answer={"agenda_setters": setters.to_dict("records")},
            top_findings=findings or ["오늘 신규 의제 선점 없음 (기존 주제 지속)"],
            next_watch=[],
        )

    # ── Q12: 진보/보수 차이 ─────────────────────────────────

    def _q12(self) -> QuestionAnswer:
        """진보/보수 미디어의 강조점 차이는?"""
        df = self._today
        if df is None:
            return _insufficient("Q12", "진보/보수 미디어 프레이밍 차이", self._date, "데이터 없음")

        lean_map = {"LEFT": "진보", "CENTER_LEFT": "진보", "CENTER": "중도",
                    "CENTER_RIGHT": "보수", "RIGHT": "보수"}

        classified = df[df["source_lean"].isin(lean_map.keys())].copy()
        if len(classified) < 10:
            return QuestionAnswer(
                question_id="Q12",
                question_ko="진보/보수 미디어 강조점 차이",
                date=self._date,
                status="degraded",
                confidence=0.15,
                data_days_available=self._hist_days() + 1,
                answer={"note": f"lean 분류 기사 {len(classified)}건 — 충분하지 않음 (최소 10건)"},
                top_findings=["이념 성향 분류 출처 부족 — source_lean.yaml 큐레이션 필요"],
                next_watch=[],
            )

        classified["lean_group"] = classified["source_lean"].map(lean_map)
        lean_steeps: dict[str, dict] = {}
        for group, grp in classified.groupby("lean_group"):
            steeps_dist = grp["steeps_primary"].value_counts().head(5).to_dict()
            lean_steeps[str(group)] = {
                "article_count": len(grp),
                "steeps_distribution": {str(k): int(v) for k, v in steeps_dist.items()},
            }

        findings = []
        if "진보" in lean_steeps and "보수" in lean_steeps:
            l_top = max(lean_steeps["진보"]["steeps_distribution"],
                        key=lean_steeps["진보"]["steeps_distribution"].__getitem__)
            r_top = max(lean_steeps["보수"]["steeps_distribution"],
                        key=lean_steeps["보수"]["steeps_distribution"].__getitem__)
            if l_top != r_top:
                findings.append(f'진보 최다 주제: {l_top}, 보수 최다 주제: {r_top}')
            else:
                findings.append(f'진보·보수 공통 최다 주제: {l_top}')

        return QuestionAnswer(
            question_id="Q12",
            question_ko="진보/보수 미디어의 강조점 차이는?",
            date=self._date,
            status="answered",
            confidence=0.65,
            data_days_available=self._hist_days() + 1,
            answer={"lean_comparison": lean_steeps, "classified_articles": len(classified)},
            top_findings=findings or ["진보/보수 차이 없음 — 공통 의제 지배"],
            next_watch=[],
        )

    # ── Q13: 언어권 독자 의제 ───────────────────────────────

    def _q13(self) -> QuestionAnswer:
        """특정 언어권에서만 다루는 독자적 의제는?"""
        df = self._today
        if df is None:
            return _insufficient("Q13", "언어권 독자 의제", self._date, "데이터 없음")

        # 언어별 STEEPS 분포
        lang_steeps: dict[str, set] = defaultdict(set)
        for _, row in df.iterrows():
            lang = str(row.get("language", ""))
            steeps = str(row.get("steeps_primary", ""))
            if lang and steeps:
                lang_steeps[lang].add(steeps)

        all_langs = list(lang_steeps.keys())
        if len(all_langs) < 2:
            return QuestionAnswer(
                question_id="Q13",
                question_ko="언어권 독자 의제",
                date=self._date,
                status="degraded",
                confidence=0.20,
                data_days_available=self._hist_days() + 1,
                answer={"note": f"단일 언어 데이터 ({all_langs})"},
                top_findings=["단일 언어 — 비교 불가"],
                next_watch=[],
            )

        # 독자적: 다른 언어 2개 이상에 없는 STEEPS
        exclusives: list[dict] = []
        for lang, steeps_set in lang_steeps.items():
            other_steeps = set()
            for other_lang, other_set in lang_steeps.items():
                if other_lang != lang:
                    other_steeps |= other_set
            unique = steeps_set - other_steeps
            if unique:
                lang_articles = df[df["language"] == lang]
                count = int(lang_articles[lang_articles["steeps_primary"].isin(unique)].shape[0])
                exclusives.append({
                    "language": lang,
                    "exclusive_steeps": list(unique),
                    "article_count": count,
                })
        exclusives.sort(key=lambda x: -x["article_count"])

        # 언어 커버리지 겹침 (Jaccard)
        coverage: list[dict] = []
        for i, la in enumerate(all_langs):
            for lb in all_langs[i+1:]:
                inter = len(lang_steeps[la] & lang_steeps[lb])
                union = len(lang_steeps[la] | lang_steeps[lb])
                jaccard = inter / union if union > 0 else 0
                coverage.append({
                    "lang_a": la, "lang_b": lb,
                    "jaccard": round(jaccard, 3),
                    "shared_steeps": list(lang_steeps[la] & lang_steeps[lb]),
                })
        coverage.sort(key=lambda x: x["jaccard"])

        findings = []
        for ex in exclusives[:2]:
            findings.append(
                f'{ex["language"]} 전용 주제: {", ".join(ex["exclusive_steeps"])} '
                f'({ex["article_count"]}건)'
            )

        return QuestionAnswer(
            question_id="Q13",
            question_ko="특정 언어권에서만 다루는 독자적 의제는?",
            date=self._date,
            status="answered",
            confidence=0.75,
            data_days_available=self._hist_days() + 1,
            answer={
                "exclusive_by_language": exclusives[:_TOP_N],
                "coverage_similarity": coverage[:_TOP_N],
            },
            top_findings=findings or ["언어 간 의제 격차 없음 — 공통 의제 지배"],
            next_watch=[ex["language"] for ex in exclusives[:2]],
        )

    # ── Q14: 보도 격차 ──────────────────────────────────────

    def _q14(self) -> QuestionAnswer:
        """어떤 주제가 미디어 간 보도 격차가 가장 큰가?"""
        df = self._today
        if df is None:
            return _insufficient("Q14", "미디어 보도 격차", self._date, "데이터 없음")

        # 언어권별 출처 수로 격차 측정
        lang_source: dict[str, set] = defaultdict(set)
        for _, row in df.iterrows():
            lang = str(row.get("language", ""))
            src  = str(row.get("source_id", ""))
            if lang and src:
                lang_source[lang].add(src)

        tier_dist = df.groupby("source_tier").size().to_dict()
        lang_dist  = df.groupby("language").size().sort_values(ascending=False).head(10).to_dict()

        gaps: list[dict] = []
        langs = list(lang_source.keys())
        for i, la in enumerate(langs):
            for lb in langs[i+1:]:
                count_a = len(lang_source[la])
                count_b = len(lang_source[lb])
                if max(count_a, count_b) > 0:
                    gap = abs(count_a - count_b) / max(count_a, count_b)
                    gaps.append({
                        "lang_a": la, "lang_b": lb,
                        "source_count_a": count_a,
                        "source_count_b": count_b,
                        "gap_ratio": round(gap, 3),
                    })
        gaps.sort(key=lambda x: -x["gap_ratio"])

        findings = [
            f'{g["lang_a"]}({g["source_count_a"]}출처) vs '
            f'{g["lang_b"]}({g["source_count_b"]}출처): 격차 {g["gap_ratio"]:.2f}'
            for g in gaps[:2]
        ]

        return QuestionAnswer(
            question_id="Q14",
            question_ko="어떤 주제가 미디어 간 보도 격차가 가장 큰가?",
            date=self._date,
            status="answered",
            confidence=0.70,
            data_days_available=self._hist_days() + 1,
            answer={
                "language_source_counts": {k: len(v) for k, v in lang_source.items()},
                "language_gaps": gaps[:_TOP_N],
                "tier_distribution": {str(k): int(v) for k, v in tier_dist.items()},
                "language_distribution": {str(k): int(v) for k, v in lang_dist.items()},
            },
            top_findings=findings or ["언어별 보도 격차 균등"],
            next_watch=[g["lang_a"] for g in gaps[:1] if g["gap_ratio"] > 0.5],
        )

    # ── Q15: 감성-경제 선행 ─────────────────────────────────

    def _q15(self) -> QuestionAnswer:
        """뉴스 감성이 경제 지표를 선행하는가, 후행하는가?"""
        df = self._today
        if df is None:
            return _insufficient("Q15", "감성-경제 선행 관계", self._date, "데이터 없음")

        eco_df = df[df["steeps_primary"] == "ECO"].copy()
        days = self._hist_days()

        if "sentiment_score" not in eco_df.columns or eco_df["sentiment_score"].isnull().all():
            return QuestionAnswer(
                question_id="Q15",
                question_ko="뉴스 감성이 경제 지표를 선행하는가?",
                date=self._date,
                status="degraded",
                confidence=0.10,
                data_days_available=days + 1,
                answer={"reason": "sentiment 미산출 — Stage 3 실행 후 재계산 필요"},
                top_findings=["경제 기사 감성 미산출"],
                next_watch=[],
            )

        today_eco_sent = float(eco_df["sentiment_score"].mean()) if len(eco_df) > 0 else 0.0

        # 30일 경제 감성 시계열
        series: list[dict] = [{"date": self._date, "avg_sentiment": round(today_eco_sent, 3),
                                "article_count": len(eco_df)}]
        if self._hist30 is not None:
            eco_hist = self._hist30[self._hist30["steeps_primary"] == "ECO"]
            if "sentiment_score" in eco_hist.columns:
                daily = (eco_hist.groupby("_hist_date")
                         .agg(avg_sentiment=("sentiment_score", "mean"),
                              article_count=("sentiment_score", "count"))
                         .reset_index())
                for _, row in daily.sort_values("_hist_date").iterrows():
                    series.append({
                        "date": str(row["_hist_date"]),
                        "avg_sentiment": round(float(row["avg_sentiment"]), 3),
                        "article_count": int(row["article_count"]),
                    })

        series.sort(key=lambda x: x["date"])
        trend_str = ("부정 강화" if today_eco_sent < -0.2
                     else "부정" if today_eco_sent < -0.05
                     else "긍정" if today_eco_sent > 0.1
                     else "중립")

        return QuestionAnswer(
            question_id="Q15",
            question_ko="뉴스 감성이 경제 지표를 선행하는가?",
            date=self._date,
            status="answered" if days >= 7 else "degraded",
            confidence=0.60 + min(0.30, (days - 7) * 0.01) if days >= 7 else 0.30,
            data_days_available=days + 1,
            answer={
                "eco_sentiment_today": today_eco_sent,
                "eco_article_count": len(eco_df),
                "eco_sentiment_30d_series": series[-30:],
                "external_economic_data": "미연결 — 수동 연결 필요 (FRED/IMF API)",
                "trend": trend_str,
            },
            top_findings=[
                f'오늘 경제 기사 {len(eco_df)}건, 평균 감성 {today_eco_sent:.2f} ({trend_str})'
            ],
            next_watch=[],
        )

    # ── Q16: 이슈 인과 연쇄 ─────────────────────────────────

    def _q16(self) -> QuestionAnswer:
        """A 사건 보도가 B 사건에 영향을 미치는가?"""
        days = self._hist_days()

        if days < 30:
            # 30일 미만: co-occurrence 대리 지표 + degraded 상태
            pass  # 아래 로직으로 계속

        # STEEPS 소스 수준 공기 — 같은 언론사가 같은 날 다루는 STEEPS 쌍
        # (기사 내 다중 STEEPS 레이블이 없어도 동작하는 robust 접근법)
        df = self._today
        if df is None:
            return _insufficient("Q16", "이슈 인과 연쇄", self._date, "오늘 데이터 없음")

        steeps_pairs: Counter = Counter()
        src_steeps: dict[str, list[str]] = defaultdict(list)
        for _, row in df.iterrows():
            sid = str(row.get("source_id", ""))
            s = str(row.get("steeps_primary", ""))
            if sid and s and s != "CRS":
                src_steeps[sid].append(s)

        for sid, cats in src_steeps.items():
            unique_cats = list(set(cats))
            for i in range(len(unique_cats)):
                for j in range(i + 1, len(unique_cats)):
                    pair = tuple(sorted([unique_cats[i], unique_cats[j]]))
                    steeps_pairs[pair] += 1

        # 히스토리로 STEEPS 간 지연 관계 (간단 lead-lag)
        lag_signals: list[dict] = []
        if self._hist7 is not None:
            import pandas as pd
            hist_daily = (self._hist7.groupby(["_hist_date", "steeps_primary"])
                          .size().reset_index(name="cnt"))
            today_steeps = df["steeps_primary"].value_counts().to_dict()
            for steeps, hist_grp in hist_daily.groupby("steeps_primary"):
                prev_max = hist_grp["cnt"].max()
                today_cnt = today_steeps.get(steeps, 0)
                if prev_max > 0 and today_cnt / prev_max > 1.3:
                    lag_signals.append({"steeps": str(steeps), "prev_max": int(prev_max),
                                        "today": int(today_cnt), "lag_ratio": round(today_cnt/prev_max, 2)})
            lag_signals.sort(key=lambda x: -x["lag_ratio"])

        co_occur = [
            {"pair": list(p), "co_occurrence_count": int(c)}
            for p, c in steeps_pairs.most_common(_TOP_N)
        ]

        findings = [
            f'{"/".join(c["pair"])}: {c["co_occurrence_count"]}개 언론사 동시 보도'
            for c in co_occur[:3]
        ]
        for ls in lag_signals[:2]:
            findings.append(
                f'{ls["steeps"]}: 7일 최대({ls["prev_max"]})→오늘({ls["today"]}) {ls["lag_ratio"]:.1f}× 급증'
            )

        if days >= 30:
            conf_q16 = 0.70 + min(0.20, (days - 30) * 0.01)
            q16_status = "answered"
        else:
            conf_q16 = 0.40  # co-occurrence 대리 지표는 Granger 아님
            q16_status = "degraded"
        return QuestionAnswer(
            question_id="Q16",
            question_ko="A 사건 보도가 B 사건에 영향을 미치는가?",
            date=self._date,
            status=q16_status,
            confidence=round(conf_q16, 2),
            data_days_available=days + 1,
            answer={
                "steeps_co_occurrence": co_occur,
                "granger_causality": "30일+ 데이터 기반 고도화 예정 (현재: 공기 분석)",
                "note": "STEEPS 공기(co-occurrence) 기반 인과 대리 지표",
            },
            top_findings=findings or ["STEEPS 간 공기 패턴 없음"],
            next_watch=["/".join(c["pair"]) for c in co_occur[:2]],
        )

    # ── Q17: 이슈 클러스터 ──────────────────────────────────

    def _q17(self) -> QuestionAnswer:
        """동시에 급증하는 이슈 클러스터는 무엇인가?"""
        df = self._today
        if df is None:
            return _insufficient("Q17", "이슈 클러스터", self._date, "데이터 없음")

        # STEEPS × 엔티티 공기 기반 클러스터
        clusters: list[dict] = []
        for steeps, grp in df.groupby("steeps_primary"):
            # 상위 엔티티 수집
            persons: list[str] = []
            orgs:    list[str] = []
            for _, row in grp.iterrows():
                persons.extend(_to_list(row.get("entity_person"))[:3])
                orgs.extend(_to_list(row.get("entity_org"))[:3])
            top_persons = [e for e, _ in Counter(p for p in persons if p).most_common(5)]
            top_orgs    = [e for e, _ in Counter(o for o in orgs if o).most_common(5)]

            # 감성
            avg_sent = None
            if "sentiment_score" in grp.columns and not grp["sentiment_score"].isnull().all():
                avg_sent = round(float(grp["sentiment_score"].mean()), 3)

            clusters.append({
                "steeps": str(steeps),
                "article_count": int(len(grp)),
                "top_persons": top_persons,
                "top_orgs": top_orgs,
                "avg_sentiment": avg_sent,
                "languages": sorted(grp["language"].unique().tolist()),
                "source_count": int(grp["source_id"].nunique()),
            })

        clusters.sort(key=lambda x: -x["article_count"])
        findings = [
            f'{c["steeps"]}: {c["article_count"]}건, '
            f'주요 기관 {c["top_orgs"][:2]}'
            for c in clusters[:3] if c["top_orgs"]
        ]

        return QuestionAnswer(
            question_id="Q17",
            question_ko="동시에 급증하는 이슈 클러스터는?",
            date=self._date,
            status="answered",
            confidence=0.80,
            data_days_available=self._hist_days() + 1,
            answer={"clusters": clusters[:_TOP_N]},
            top_findings=findings or [f"오늘 {len(clusters)}개 STEEPS 클러스터 식별"],
            next_watch=[c["steeps"] for c in clusters[:3]],
        )

    # ── Q18: 엔티티 지도 ─────────────────────────────────────

    def _q18(self) -> QuestionAnswer:
        """어떤 인물·기관·국가가 지금 글로벌 의제의 중심에 있나?"""
        df = self._today
        if df is None:
            return _insufficient("Q18", "글로벌 엔티티 지도", self._date, "데이터 없음")

        has_sentiment = (
            "sentiment_score" in df.columns
            and not df["sentiment_score"].isnull().all()
        )

        entity_stats: dict[str, dict] = {}

        for _, row in df.iterrows():
            sent = float(row.get("sentiment_score") or 0.0) if has_sentiment else None
            steeps = str(row.get("steeps_primary", ""))
            geo = str(row.get("geo_focus_primary", ""))

            for etype, col in [("PERSON", "entity_person"), ("ORG", "entity_org"),
                                ("COUNTRY", "entity_country")]:
                entities = _to_list(row.get(col))
                for ent in list(entities)[:5]:
                    if not ent or not isinstance(ent, str):
                        continue
                    key = f"{etype}:{ent}"
                    if key not in entity_stats:
                        entity_stats[key] = {
                            "name": ent, "type": etype,
                            "count": 0, "sentiments": [],
                            "steeps": Counter(), "geo": Counter(),
                        }
                    entity_stats[key]["count"] += 1
                    if sent is not None:
                        entity_stats[key]["sentiments"].append(sent)
                    if steeps:
                        entity_stats[key]["steeps"][steeps] += 1
                    if geo and geo != "UNKNOWN":
                        entity_stats[key]["geo"][geo] += 1

        entities_out: list[dict] = []
        for key, stat in entity_stats.items():
            avg_sent = (sum(stat["sentiments"]) / len(stat["sentiments"])
                        if stat["sentiments"] else None)
            entities_out.append({
                "name": stat["name"],
                "type": stat["type"],
                "mention_count": stat["count"],
                "avg_sentiment": round(avg_sent, 3) if avg_sent is not None else None,
                "top_steeps": stat["steeps"].most_common(1)[0][0] if stat["steeps"] else "",
                "top_geo": stat["geo"].most_common(1)[0][0] if stat["geo"] else "",
            })

        entities_out.sort(key=lambda x: -x["mention_count"])
        top10 = entities_out[:_TOP_N]

        findings = []
        for e in top10[:3]:
            s = f'({e["avg_sentiment"]:.2f})' if e["avg_sentiment"] is not None else ""
            findings.append(
                f'{e["type"]} {e["name"]}: {e["mention_count"]}회 언급 {s}'
            )

        return QuestionAnswer(
            question_id="Q18",
            question_ko="어떤 인물·기관·국가가 글로벌 의제의 중심에 있나?",
            date=self._date,
            status="answered",
            confidence=0.80,
            data_days_available=self._hist_days() + 1,
            answer={
                "top_entities": top10,
                "total_unique_entities": len(entities_out),
                "by_type": {
                    t: [e for e in top10 if e["type"] == t]
                    for t in ("PERSON", "ORG", "COUNTRY")
                },
            },
            top_findings=findings or ["오늘 주요 엔티티 없음"],
            next_watch=[e["name"] for e in top10[:3]],
        )

    # ── 요약 파일 ────────────────────────────────────────────

    def _save_summary(self, results: list[QuestionAnswer]) -> None:
        """18개 질문 요약 JSON 저장."""
        summary = {
            "date": self._date,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "total_questions": len(results),
            "answered":           sum(1 for r in results if r.status == "answered"),
            "degraded":           sum(1 for r in results if r.status == "degraded"),
            "insufficient_data":  sum(1 for r in results if r.status == "insufficient_data"),
            "avg_confidence":     round(sum(r.confidence for r in results) / len(results), 3),
            "questions": [
                {
                    "id": r.question_id,
                    "question_ko": r.question_ko,
                    "status": r.status,
                    "confidence": r.confidence,
                    "top_finding": r.top_findings[0] if r.top_findings else "",
                }
                for r in results
            ],
        }
        path = self._answers_dir / "summary.json"
        path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        logger.info(
            "question_engine_summary answered=%d degraded=%d insufficient=%d",
            summary["answered"], summary["degraded"], summary["insufficient_data"],
        )


# ─────────────────────────────────────────────────────────────
# P1 검증
# ─────────────────────────────────────────────────────────────

def validate_question_answers(answers_dir: str | Path) -> dict[str, Any]:
    """P1 게이트: 18개 파일 전부 존재·유효 확인.

    QA1 — q01.json ~ q18.json 전부 존재
    QA2 — 각 파일이 유효 JSON
    QA3 — status 필드가 유효 값
    QA4 — 빈 answer dict 없음
    QA5 — answered 비율 보고
    """
    errors: list[str] = []
    warnings: list[str] = []
    path = Path(answers_dir)

    results: list[dict] = []
    for i in range(1, 19):
        fpath = path / f"q{i:02d}.json"
        # QA1
        if not fpath.exists():
            errors.append(f"QA1: q{i:02d}.json 없음")
            continue
        # QA2
        try:
            data = json.loads(fpath.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            errors.append(f"QA2: q{i:02d}.json JSON 파싱 실패")
            continue
        # QA3
        if data.get("status") not in _VALID_STATUS:
            errors.append(f"QA3: q{i:02d} status 유효하지 않음: {data.get('status')}")
        # QA4
        if not data.get("answer"):
            warnings.append(f"QA4: q{i:02d} answer 비어있음")
        results.append(data)

    answered = sum(1 for r in results if r.get("status") == "answered")
    degraded = sum(1 for r in results if r.get("status") == "degraded")
    insufficient = sum(1 for r in results if r.get("status") == "insufficient_data")

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "files_found": len(results),
        "answered": answered,
        "degraded": degraded,
        "insufficient_data": insufficient,
        "coverage_pct": round(len(results) / 18 * 100, 1),
    }


# ─────────────────────────────────────────────────────────────
# 편의 함수 (pipeline.py에서 호출)
# ─────────────────────────────────────────────────────────────

def run_question_engine(
    date: str,
    project_root: str | Path | None = None,
) -> dict[str, Any]:
    """외부 진입점 — pipeline.py post-processing에서 호출."""
    engine = QuestionEngine(date=date, project_root=project_root)
    results = engine.run_all()
    answers_dir = Path(project_root or Path(__file__).parents[2]) / "data" / "answers" / date
    validation = validate_question_answers(answers_dir)
    return {
        "answers_dir": str(answers_dir),
        "question_count": len(results),
        "validation": validation,
        "summary": {
            "answered": validation["answered"],
            "degraded": validation["degraded"],
            "insufficient_data": validation["insufficient_data"],
        },
    }
