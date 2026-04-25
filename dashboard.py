"""GlobalNews Pipeline — Interactive Dashboard (Multi-Period).

Launch:
    streamlit run dashboard.py

Reads Parquet/JSONL/SQLite outputs produced by the 8-stage analysis pipeline.
Supports daily, monthly, quarterly, and yearly aggregation via sidebar controls.

Tabs: Run Summary, Overview, Topics, Sentiment & Emotions, Word Cloud, 18 Questions.
"""

from __future__ import annotations

import datetime
import json
import re
import sqlite3
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from wordcloud import WordCloud

import dashboard_insights as di

# ---------------------------------------------------------------------------
# Base paths
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).parent
DATA_DIR = PROJECT_ROOT / "data"


# Sub-directory names that contain date-partitioned outputs
_DATE_PARTITIONED_DIRS = ("raw", "processed", "features", "analysis", "output")

# ---------------------------------------------------------------------------
# Date discovery
# ---------------------------------------------------------------------------


@st.cache_data(ttl=600)
def discover_dates() -> list[str]:
    """Scan data/raw/ for valid YYYY-MM-DD subdirectories and return sorted."""
    raw_dir = DATA_DIR / "raw"
    if not raw_dir.exists():
        return []
    dates: list[str] = []
    for p in sorted(raw_dir.iterdir()):
        if p.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.name):
            dates.append(p.name)
    return dates


def dates_for_period(
    all_dates: list[str], period: str, ref_date: str,
) -> list[str]:
    """Return the subset of *all_dates* that fall within the selected period.

    Parameters
    ----------
    all_dates : available date strings (YYYY-MM-DD), sorted ascending.
    period : "Daily" | "Monthly" | "Quarterly" | "Yearly"
    ref_date : reference date string chosen in the sidebar.
    """
    ref = datetime.date.fromisoformat(ref_date)

    if period == "Daily":
        return [ref_date] if ref_date in all_dates else []

    if period == "Monthly":
        return [d for d in all_dates
                if d[:7] == ref_date[:7]]  # same YYYY-MM

    if period == "Quarterly":
        q_start_month = ((ref.month - 1) // 3) * 3 + 1
        q_start = datetime.date(ref.year, q_start_month, 1)
        q_end_month = q_start_month + 2
        if q_end_month == 12:
            q_end = datetime.date(ref.year, 12, 31)
        else:
            q_end = datetime.date(ref.year, q_end_month + 1, 1) - datetime.timedelta(days=1)
        return [d for d in all_dates if q_start <= datetime.date.fromisoformat(d) <= q_end]

    if period == "Yearly":
        return [d for d in all_dates if d[:4] == ref_date[:4]]

    return [ref_date] if ref_date in all_dates else []


# ---------------------------------------------------------------------------
# Multi-date loaders
# ---------------------------------------------------------------------------


@st.cache_data(ttl=3600)
def load_multi_parquet(
    sub_dir: str, filename: str, dates: tuple[str, ...],
) -> pd.DataFrame | None:
    """Load and concatenate a parquet file from multiple date directories."""
    frames: list[pd.DataFrame] = []
    for d in dates:
        p = DATA_DIR / sub_dir / d / filename
        if p.exists():
            df = pd.read_parquet(str(p))
            df["_data_date"] = d
            frames.append(df)
    if not frames:
        return None
    combined = pd.concat(frames, ignore_index=True)
    # Deduplicate across dates if article_id column exists
    if "article_id" in combined.columns:
        combined = combined.drop_duplicates(subset=["article_id"], keep="last")
    return combined


@st.cache_data(ttl=3600)
def load_multi_jsonl(dates: tuple[str, ...]) -> pd.DataFrame | None:
    """Load and concatenate raw JSONL files from multiple date directories."""
    frames: list[pd.DataFrame] = []
    for d in dates:
        p = DATA_DIR / "raw" / d / "all_articles.jsonl"
        if not p.exists():
            continue
        records = []
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
        if records:
            df = pd.DataFrame(records)
            df["_data_date"] = d
            frames.append(df)
    if not frames:
        return None
    return pd.concat(frames, ignore_index=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Python/OS 예약 디렉터리 → 한국어 표기 사전
_SYSTEM_DIR_KO: dict[str, str] = {
    "__pycache__":  "파이썬 바이트코드 캐시",
    ".git":         "Git 저장소",
    ".venv":        "파이썬 가상환경",
    "node_modules": "Node.js 모듈 캐시",
    ".DS_Store":    "macOS 시스템 파일",
    "__init__.py":  "파이썬 패키지 초기화 파일",
}

def _ko_dirname(name: str) -> str:
    """파이썬/OS 예약 디렉터리명을 한국어로 변환. 일반 이름은 그대로 반환."""
    return _SYSTEM_DIR_KO.get(name, name)

def _is_date_dir(name: str) -> bool:
    """YYYY-MM-DD 형식의 날짜 디렉터리 여부 판별."""
    return (
        len(name) == 10
        and name[4] == "-"
        and name[7] == "-"
        and name[:4].isdigit()
        and name[5:7].isdigit()
        and name[8:].isdigit()
    )


def format_number(n: int | float) -> str:
    if isinstance(n, float):
        return f"{n:,.1f}"
    return f"{n:,}"


# Source -> Group mapping: derived from SOT (data/config/sources.yaml)
_VALID_GROUPS = frozenset("ABCDEFGHIJ")


def _load_source_groups() -> dict[str, str]:
    """Load site->group mapping from sources.yaml (SOT).

    P1: validates group values and minimum site count to detect parse failures.
    """
    import yaml

    sources_path = DATA_DIR / "config" / "sources.yaml"
    if not sources_path.exists():
        return {}

    with open(sources_path, encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}

    groups: dict[str, str] = {}
    for site_id, site_cfg in config.get("sources", {}).items():
        if isinstance(site_cfg, dict):
            g = site_cfg.get("group", "?")
            if g not in _VALID_GROUPS:
                g = "?"
            groups[site_id] = g

    # P1: parse failure detection — 116 sites expected, 50 is safe minimum
    if 0 < len(groups) < 50:
        import logging
        logging.getLogger(__name__).error(
            "source_groups_parse_suspect loaded=%d expected=100+", len(groups),
        )

    return groups


SOURCE_GROUPS = _load_source_groups()

GROUP_NAMES = {
    "A": "Korean Major",
    "B": "Korean Tech/Biz",
    "C": "Korean Specialty",
    "D": "Korean Tech",
    "E": "English Major",
    "F": "Asia-Pacific",
    "G": "Europe/ME",
    "H": "Africa",
    "I": "Latin America",
    "J": "Russia/Central Asia",
}

LANG_NAMES = {
    "ko": "Korean", "en": "English", "fr": "French", "de": "German",
    "ja": "Japanese", "ru": "Russian", "es": "Spanish", "it": "Italian",
    "pt": "Portuguese", "no": "Norwegian", "cs": "Czech", "sv": "Swedish",
    "pl": "Polish", "mn": "Mongolian",
}

# ---------------------------------------------------------------------------
# Word Cloud helpers
# ---------------------------------------------------------------------------

_KO_STOPWORDS = {
    "것", "수", "등", "이", "그", "저", "때", "중", "년", "월", "일",
    "위", "곳", "바", "뉴스", "기자", "연합뉴스", "서울", "제공",
    "사진", "대한", "관련", "이후", "올해", "현재", "경우", "이상",
    "이번", "지난", "전체", "가장", "오늘", "지금", "우리", "모든",
    "뉴스1", "한편", "또한", "기사", "무단", "전재", "배포", "금지",
    "특파원", "통신", "보도", "데일리", "저작권", "재배포", "헤럴드",
}

# Multilingual stopwords for word cloud (non-Korean languages).
# Korean is handled separately via kiwipiepy POS tagging (NNG/NNP only).
# Source: aligned with stage4_aggregation.py _MULTILINGUAL_STOP_WORDS.
_LATIN_STOPWORDS = {
    # English
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "shall", "may", "might", "can", "must", "need",
    "to", "of", "in", "for", "on", "with", "at", "by", "from", "as",
    "into", "about", "between", "through", "after", "before", "during",
    "above", "below", "up", "down", "out", "off", "over", "under",
    "and", "but", "or", "nor", "not", "so", "yet", "both", "either",
    "neither", "each", "every", "all", "any", "few", "more", "most",
    "other", "some", "such", "no", "only", "same", "than", "too",
    "very", "just", "also", "now", "then", "here", "there", "when",
    "where", "why", "how", "what", "which", "who", "whom", "this",
    "that", "these", "those", "it", "its", "he", "she", "they", "them",
    "his", "her", "their", "our", "my", "your", "we", "you", "i", "me",
    "us", "him", "if", "while", "because", "since", "until", "unless",
    "although", "though", "even", "still", "already", "never", "always",
    "often", "much", "many", "well", "however", "said", "says", "new",
    "like", "one", "two", "first", "last", "get", "got", "make", "made",
    "going", "come", "take", "know", "think", "see", "look", "want",
    "give", "use", "find", "tell", "ask", "work", "call", "try", "keep",
    "let", "put", "say", "go", "people", "time", "year", "day", "way",
    "man", "world", "life", "part", "back", "long", "great", "right",
    "old", "big", "high", "different", "small", "large", "next", "early",
    "young", "important", "public", "bad", "according", "reuters", "ap",
    "per", "set", "don", "didn", "won", "isn", "aren", "wasn", "weren",
    "haven", "hasn", "hadn", "doesn", "couldn", "shouldn", "wouldn",
    # Spanish
    "que", "de", "en", "el", "la", "los", "las", "del", "por", "con",
    "una", "para", "es", "al", "lo", "como", "pero", "sus", "su",
    "sin", "sobre", "este", "entre", "cuando", "muy", "ser", "hay",
    "fue", "son", "desde", "est", "esta", "hasta", "cada", "han",
    "tiene", "otro", "otra", "dos", "tres", "todo", "toda", "todos",
    "seg", "siendo", "puede", "hace", "donde", "parte", "contra",
    "tambi", "sido", "tiene", "mejor", "tras", "mismo",
    # German
    "der", "die", "das", "und", "ist", "von", "den", "des", "mit",
    "ein", "eine", "dem", "auf", "sich", "nicht", "auch", "als",
    "noch", "nach", "aus", "bei", "nur", "wie", "aber", "war",
    "wird", "sind", "hat", "vor", "oder", "bis", "mehr", "zum",
    "zur", "kann", "schon", "wenn", "wir", "sie", "ich",
    "seine", "unter", "haben", "diese", "einem", "einer", "grad",
    "zwei", "wurden", "worden", "hatte", "seit", "lange", "gibt",
    # French
    "les", "des", "est", "pas", "une", "par", "sur", "dans", "que",
    "pour", "qui", "son", "avec", "plus", "sont", "ses", "mais",
    "ont", "cette", "aux", "tout", "leur", "fait", "entre", "aussi",
    "tous", "elle", "comme", "peut", "autre", "apr",
    # Italian
    "che", "non", "per", "una", "del", "della", "dei", "nel", "nella",
    "con", "sono", "gli", "anche", "dal", "alla", "sul", "dello",
    "alle", "stato", "essere", "tra", "fra", "dopo", "suo", "suoi",
    "questo", "quella", "hanno", "fatto", "come", "quando", "cosa",
    # Portuguese
    "que", "dos", "das", "uma", "com", "para", "por", "mais", "foi",
    "ser", "como", "tem", "seu", "sua", "ele", "ela", "nos", "aos",
    "pela", "pelo", "entre", "sobre", "havia", "pode", "seus",
    "suas", "ainda", "todos", "esta", "esse", "essa", "isso",
    # Russian (transliterated — regex captures Latin chars only)
    # Russian Cyrillic is not captured by [a-zA-Z] regex, so no entries needed.
    # Norwegian
    "det", "som", "har", "med", "til", "den", "att", "han", "hun",
    "var", "vil", "kan", "fra", "mot", "ble", "ved", "mot",
    "eller", "etter", "skal", "alle", "over", "oss", "dem",
    # Swedish
    "det", "som", "och", "att", "med", "har", "den", "var",
    "kan", "ett", "ska", "alla", "sin", "sina", "mot", "vid",
    # Czech
    "tak", "ale", "jak", "pro", "pod", "nad", "aby", "jsou",
    "jeho", "jej", "nej", "jen", "byl", "kde", "kdy", "bez",
    # Polish
    "nie", "tak", "jak", "ale", "czy", "jest", "aby", "pod",
    "nad", "bez", "dla", "ich", "tej", "ten", "tym",
}


@st.cache_data(ttl=3600)
def extract_word_frequencies(
    texts: list[str], languages: list[str],
) -> dict[str, int]:
    """Extract word frequencies using kiwipiepy (Korean) + regex (English)."""
    ko_texts = [t for t, lang in zip(texts, languages) if lang == "ko" and t]
    en_texts = [t for t, lang in zip(texts, languages) if lang != "ko" and t]

    word_freq: dict[str, int] = {}

    if ko_texts:
        from kiwipiepy import Kiwi
        kiwi = Kiwi()
        for text in ko_texts:
            tokens = kiwi.tokenize(text)
            for token in tokens:
                if token.tag in ("NNG", "NNP") and len(token.form) >= 2:
                    w = token.form
                    if w not in _KO_STOPWORDS:
                        word_freq[w] = word_freq.get(w, 0) + 1

    if en_texts:
        pattern = re.compile(r"[a-zA-Z]{3,}")
        for text in en_texts:
            for m in pattern.finditer(text.lower()):
                w = m.group()
                if w not in _LATIN_STOPWORDS:
                    word_freq[w] = word_freq.get(w, 0) + 1

    return word_freq


# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="GlobalNews Dashboard",
    page_icon="🌐",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# Sidebar — Period selector
# ---------------------------------------------------------------------------

all_dates = discover_dates()

with st.sidebar:
    st.header("Period Selection")

    if not all_dates:
        st.warning("No data found in data/raw/")
        st.stop()

    period = st.selectbox(
        "Analysis Period",
        ["Daily", "Monthly", "Quarterly", "Yearly"],
        index=0,
    )

    if period == "Daily":
        selected_ref = st.selectbox("Date", all_dates, index=len(all_dates) - 1)
    elif period == "Monthly":
        months = sorted(set(d[:7] for d in all_dates))
        selected_month = st.selectbox("Month", months, index=len(months) - 1)
        selected_ref = selected_month + "-01"
    elif period == "Quarterly":
        quarters: list[str] = []
        seen: set[str] = set()
        for d in all_dates:
            dt = datetime.date.fromisoformat(d)
            q = (dt.month - 1) // 3 + 1
            label = f"{dt.year} Q{q}"
            if label not in seen:
                seen.add(label)
                quarters.append(label)
        selected_q = st.selectbox("Quarter", quarters, index=len(quarters) - 1)
        # Parse back to a ref date
        q_year, q_num = selected_q.split(" Q")
        q_month = (int(q_num) - 1) * 3 + 1
        selected_ref = f"{q_year}-{q_month:02d}-01"
    else:  # Yearly
        years = sorted(set(d[:4] for d in all_dates))
        selected_year = st.selectbox("Year", years, index=len(years) - 1)
        selected_ref = f"{selected_year}-01-01"

    active_dates = dates_for_period(all_dates, period, selected_ref)

    if not active_dates:
        st.warning("No data for the selected period.")
        st.stop()

    st.info(f"**{len(active_dates)}** day(s) selected: {active_dates[0]} — {active_dates[-1]}"
            if len(active_dates) > 1
            else f"**1** day: {active_dates[0]}")

    st.markdown("---")

# Convert to tuple for caching
_dates_key = tuple(active_dates)

# ---------------------------------------------------------------------------
# Load data for the selected period
# ---------------------------------------------------------------------------

raw_df = load_multi_jsonl(_dates_key)
articles_df = load_multi_parquet("processed", "articles.parquet", _dates_key)
analysis_df = load_multi_parquet("analysis", "article_analysis.parquet", _dates_key)
topics_df = load_multi_parquet("analysis", "topics.parquet", _dates_key)
timeseries_df = load_multi_parquet("analysis", "timeseries.parquet", _dates_key)
cross_df = load_multi_parquet("analysis", "cross_analysis.parquet", _dates_key)
networks_df = load_multi_parquet("analysis", "networks.parquet", _dates_key)
mood_df = load_multi_parquet("analysis", "mood_trajectory.parquet", _dates_key)
output_df = load_multi_parquet("output", "analysis.parquet", _dates_key)

# Merge articles + analysis + topics for unified view
if articles_df is not None and analysis_df is not None and topics_df is not None:
    _topic_cols = ["article_id", "topic_id", "topic_label", "topic_probability"]
    _topic_cols = [c for c in _topic_cols if c in topics_df.columns]
    merged_df = (
        articles_df
        .merge(analysis_df, on="article_id", how="left", suffixes=("", "_analysis"))
        .merge(topics_df[_topic_cols], on="article_id", how="left", suffixes=("", "_topic"))
    )
else:
    merged_df = articles_df

# ---------------------------------------------------------------------------
# Title
# ---------------------------------------------------------------------------

_period_label = (
    f"{active_dates[0]}" if period == "Daily"
    else f"{active_dates[0]} ~ {active_dates[-1]} ({len(active_dates)} days)"
)
st.title("🌐 GlobalNews Pipeline Dashboard")
st.caption(f"Period: **{period}** | {_period_label}")

# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------

(
    tab_summary, tab_overview, tab_topics, tab_sentiment,
    tab_wordcloud, tab_questions,
) = st.tabs([
    "📋 Run Summary",
    "📊 Overview",
    "🏷️ Topics",
    "😊 Sentiment & Emotions",
    "☁️ Word Cloud",
    "🔢 18 Questions",
])

# ========================= TAB 0: RUN SUMMARY (integrated) =================

with tab_summary:
    st.header("📋 Integrated Run Summary — W1 → W2 → W3 → W4")
    st.caption(
        "Consolidated view of every workflow artifact produced for the "
        "selected date. W1 Crawling → W2 Analysis → W3 Insight → W4 Master."
    )

    # ---- Date selector ----
    import json as _json
    from pathlib import Path as _Path
    from datetime import datetime as _dt

    _raw_dir = DATA_DIR / "raw"
    _available_dates = sorted(
        [p.name for p in _raw_dir.iterdir() if p.is_dir() and _is_date_dir(p.name)],
        reverse=True,
    ) if _raw_dir.exists() else []

    if not _available_dates:
        st.warning(
            "No crawl dates found under `data/raw/`. "
            "Run the pipeline first: `/run-chain` or `/run-crawl-only`."
        )
    else:
        sel_date = st.selectbox(
            "Select run date",
            options=_available_dates,
            index=0,
            key="run_summary_date",
        )
        run_date_iso = sel_date

        # ==================== 1) PIPELINE STATUS CARDS ====================
        st.subheader("🚦 Pipeline Stage Status")

        def _stage_status(path: _Path, min_size: int = 1) -> tuple[bool, int]:
            if not path.exists():
                return False, 0
            if path.is_dir():
                return True, sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
            return path.stat().st_size >= min_size, path.stat().st_size

        _w1_jsonl = DATA_DIR / "raw" / sel_date / "all_articles.jsonl"
        _w1_ok, _w1_size = _stage_status(_w1_jsonl)
        _w1_articles = 0
        if _w1_ok:
            try:
                with _w1_jsonl.open("r", encoding="utf-8") as f:
                    _w1_articles = sum(1 for line in f if line.strip())
            except Exception:
                _w1_articles = 0

        _w2_out = DATA_DIR / "output" / sel_date
        _w2_analysis = _w2_out / "analysis.parquet"
        _w2_signals = _w2_out / "signals.parquet"
        _w2_topics = _w2_out / "topics.parquet"
        _w2_sqlite = _w2_out / "index.sqlite"
        _w2_ok = all(p.exists() for p in [_w2_analysis, _w2_signals, _w2_topics, _w2_sqlite])

        # Best-effort W3 detection: look for any insights run that touches this date
        _insight_root = DATA_DIR / "insights"
        _w3_candidates = []
        if _insight_root.exists():
            for run_dir in sorted(_insight_root.iterdir(), reverse=True):
                report = run_dir / "synthesis" / "insight_report.md"
                if report.exists() and run_dir.is_dir():
                    mtime = _dt.fromtimestamp(report.stat().st_mtime).strftime("%Y-%m-%d")
                    if mtime == sel_date or run_dir.name.endswith(sel_date[:7]):
                        _w3_candidates.append(run_dir)
        _w3_run = _w3_candidates[0] if _w3_candidates else None
        _w3_ok = _w3_run is not None

        _w4_en = PROJECT_ROOT / "reports" / "final" / f"integrated-report-{sel_date}.md"
        _w4_ko = PROJECT_ROOT / "reports" / "final" / f"integrated-report-{sel_date}.ko.md"
        _w4_ok = _w4_en.exists() and _w4_ko.exists()

        col1, col2, col3, col4 = st.columns(4)
        col1.metric("W1 Crawling", "✅" if _w1_ok else "—", f"{_w1_articles:,} articles",
                    help="Workflow 1: 뉴스 크롤링 단계. 116개 사이트에서 기사를 수집하여 data/raw/{date}/all_articles.jsonl 에 저장.")
        col2.metric("W2 Analysis", "✅" if _w2_ok else "—",
                    f"{(_w2_analysis.stat().st_size // 1024) if _w2_analysis.exists() else 0:,} KB analysis",
                    help="Workflow 2: NLP 8단계 분석 파이프라인. 전처리→임베딩→감성→토픽→시계열→교차분석→신호분류→저장. Parquet+SQLite 출력.")
        col3.metric("W3 Insight", "✅" if _w3_ok else "—",
                    _w3_run.name if _w3_run else "not run",
                    help="Workflow 3: 7개 모듈(교차언어·서사·엔티티·시간·지정학·경제·종합)로 구조적 통찰 생산. 30일 윈도우 분석.")
        col4.metric("W4 Master", "✅" if _w4_ok else "—",
                    "EN + KO" if _w4_ok else "not built",
                    help="Workflow 4: W1+W2+W3 결과를 통합한 마스터 보고서. 영어 원본 + 한국어 번역 쌍으로 생성.")

        st.divider()

        # ==================== 1.3) BIG DATA ENGINE STATUS ================
        st.subheader("🔢 빅데이터 분석 엔진 — 종합 현황")
        st.caption(
            "매일 강제 생산되는 18개 핵심 질문 + GTI + Signal Portfolio + 주간 미래 맵 현황."
        )

        _enriched_path = DATA_DIR / "enriched" / sel_date / "articles_enriched.parquet"
        _answers_dir = DATA_DIR / "answers" / sel_date
        _gti_path = DATA_DIR / "gti" / sel_date / "gti_daily.json"

        # ── Row A: Enriched Parquet + Q-Engine KPIs ────────────────────
        _be1, _be2, _be3, _be4, _be5 = st.columns(5)

        _enriched_ok = _enriched_path.exists()
        _enrich_articles = 0
        _geo_pct = 0.0
        _noise_pct = 0.0
        if _enriched_ok:
            try:
                import pandas as _pd
                _edf = _pd.read_parquet(_enriched_path)
                _enrich_articles = len(_edf)
                _geo_pct = (_edf["geo_focus_primary"] != "UNKNOWN").mean() * 100 if "geo_focus_primary" in _edf.columns else 0.0
                _noise_pct = (_edf["signal_type"] == "NOISE").mean() * 100 if "signal_type" in _edf.columns else 0.0
            except Exception:
                pass

        _be1.metric(
            "Enriched 기사",
            f"{_enrich_articles:,}" if _enriched_ok else "—",
            "articles_enriched.parquet",
            help=(
                "**articles_enriched.parquet**\n\n"
                "원시 크롤링 데이터(JSONL)에 NLP 분석 결과를 모두 결합한 "
                "35개 필드짜리 통합 데이터셋. 18개 핵심 질문 엔진의 단일 입력 소스.\n\n"
                "포함 정보: STEEPS 분류, Geo Focus, 감성 점수, 신호 유형, "
                "출처 등급(GLOBAL/NATIONAL/REGIONAL/NICHE), 출처 성향(진보/보수), "
                "엔티티(인물·기관·국가)."
            ),
        )
        _be2.metric(
            "Geo 추출률",
            f"{_geo_pct:.0f}%" if _enriched_ok else "—",
            "source ≠ focus",
            help=(
                "**Geo Focus 추출률**\n\n"
                "기사가 *게재된 국가*(source_country)와 기사가 *다루는 국가*(geo_focus)를 "
                "분리해 추출한 비율. 0%이면 모든 기사가 'UNKNOWN'.\n\n"
                "활용: Q05(국가 감성), Q06(다크 코너), Q07(양국 긴장) 질문의 정확도에 직결.\n\n"
                "추출 방법: 제목·본문 키워드 매칭 (120개국, 14개 언어 사전) "
                "+ NER 지명 정규화."
            ),
        )

        # Q-Engine status
        _q_ans = _q_deg = _q_ins = 0
        _q_conf_sum = 0.0
        if _answers_dir.exists():
            for _qi in range(1, 19):
                _qp = _answers_dir / f"q{_qi:02d}.json"
                if _qp.exists():
                    try:
                        _qd = _json.loads(_qp.read_text(encoding="utf-8"))
                        _st = _qd.get("status", "")
                        _q_conf_sum += _qd.get("confidence", 0.0)
                        if _st == "answered":
                            _q_ans += 1
                        elif _st == "degraded":
                            _q_deg += 1
                        else:
                            _q_ins += 1
                    except Exception:
                        pass

        _q_total = _q_ans + _q_deg + _q_ins
        _be3.metric(
            "18문 답변",
            f"{_q_ans}/18" if _q_total > 0 else "—",
            f"deg={_q_deg} ins={_q_ins}",
            help=(
                "**18개 핵심 질문 응답 현황**\n\n"
                "매일 강제 생성되는 18개 빅데이터 분석 질문의 응답 상태:\n\n"
                "- 🟢 **answered**: 충분한 데이터로 완전 응답\n"
                "- 🟡 **degraded**: 데이터 부족으로 부분 응답 (제한된 신뢰도)\n"
                "- 🔴 **insufficient**: 최소 데이터 임계값 미달 — 누적 대기 중\n\n"
                "🔢 탭에서 각 질문별 상세 결과 확인 가능."
            ),
        )
        _be4.metric(
            "평균 신뢰도",
            f"{_q_conf_sum/_q_total:.0%}" if _q_total > 0 else "—",
            help=(
                "**18문 평균 신뢰도 (Confidence)**\n\n"
                "각 질문의 신뢰도(0~100%)를 평균한 값. 신뢰도는 "
                "알고리즘이 해당 답변을 얼마나 확신하는지를 나타내며, "
                "데이터 양·품질·분석 방법에 따라 결정됩니다.\n\n"
                "- **데이터 누적 기간**이 길어질수록 자동 상승\n"
                "- 외부 API 연결(경제지표 등) 시 추가 상승 가능\n"
                "- 'insufficient_data' 질문은 0%로 평균을 낮춤"
            ),
        )

        # GTI
        _gti_score = None
        _gti_label_val = "—"
        if _gti_path.exists():
            try:
                _gti_data = _json.loads(_gti_path.read_text(encoding="utf-8"))
                _gti_score = _gti_data.get("gti_score", 0)
                _gti_label_val = _gti_data.get("gti_label", "—")
            except Exception:
                pass
        _be5.metric(
            "GTI",
            f"{_gti_score:.1f}" if _gti_score is not None else "—",
            _gti_label_val,
            help=(
                "**Geopolitical Tension Index (GTI)**\n\n"
                "0~100 척도의 지정학적 긴장 종합 지수. 세 신호의 가중 합산:\n\n"
                "- **G1 (40%)** 보도 집중도 편차 — 특정 지역에 보도가 쏠릴수록 ↑\n"
                "- **G2 (35%)** 핫스팟 감성 — 분쟁 지역 국가의 부정 감성 강도\n"
                "- **G3 (25%)** 양국 긴장 — 국가 간 긴장 신호 강도\n\n"
                "등급: 🟢 LOW(<30) · 🟡 MEDIUM(30-60) · 🟠 HIGH(60-80) · 🔴 CRITICAL(>80)"
            ),
        )

        # ── Row B: STEEPS + Signal distribution mini-chart ─────────────
        if _enriched_ok and _enrich_articles > 0:
            try:
                import plotly.express as _px
                import pandas as _pd
                _bc1, _bc2 = st.columns(2)
                with _bc1:
                    _steeps_counts = _edf["steeps_primary"].value_counts().reset_index()
                    _steeps_counts.columns = ["STEEPS", "건수"]
                    _steeps_counts["카테고리"] = _steeps_counts["STEEPS"].map(
                        lambda s: {"SOC":"👥Social","TEC":"💻Tech","ECO":"💰Econ",
                                   "ENV":"🌿Env","POL":"🏛️Pol","SEC":"🛡️Sec",
                                   "SPI":"🙏Spi","CRS":"⚠️Crs"}.get(s, s)
                    )
                    _fig_steeps = _px.pie(
                        _steeps_counts, names="카테고리", values="건수",
                        title=f"STEEPS 분포 ({sel_date})",
                        color_discrete_sequence=_px.colors.qualitative.Set3,
                    )
                    _fig_steeps.update_layout(height=260, margin=dict(t=40, b=10, l=10, r=10))
                    _fig_steeps.update_traces(textposition="inside", textinfo="percent+label")
                    st.plotly_chart(_fig_steeps, use_container_width=True)
                with _bc2:
                    _geo_top = (
                        _edf[_edf["geo_focus_primary"] != "UNKNOWN"]["geo_focus_primary"]
                        .value_counts().head(10).reset_index()
                    )
                    _geo_top.columns = ["국가", "건수"]
                    if not _geo_top.empty:
                        _fig_geo = _px.bar(
                            _geo_top, x="건수", y="국가", orientation="h",
                            title=f"Geo Focus 상위 10국 ({sel_date})",
                            color="건수", color_continuous_scale="Blues",
                        )
                        _fig_geo.update_layout(
                            height=260, margin=dict(t=40, b=10, l=60, r=10),
                            showlegend=False,
                        )
                        st.plotly_chart(_fig_geo, use_container_width=True)
            except Exception as _exc:
                st.caption(f"차트 생성 실패: {_exc}")

        # ── Row C: 18문 빠른 상태 표 ───────────────────────────────────
        if _q_total > 0:
            _q_rows = []
            _q_meta_label = {
                "Q01":"버스트 탐지","Q02":"트렌드 추이","Q03":"사건 전후 변화",
                "Q04":"프레이밍 비교","Q05":"국가 감성","Q06":"다크 코너",
                "Q07":"양국 긴장","Q08":"약한 신호","Q09":"패러다임 전조",
                "Q10":"의제 이동","Q11":"의제 선점","Q12":"미디어 편향",
                "Q13":"언어권 의제","Q14":"보도 격차","Q15":"감성 선행",
                "Q16":"이슈 인과","Q17":"동시 급증","Q18":"핵심 엔티티",
            }
            _status_icon = {"answered":"🟢","degraded":"🟡","insufficient_data":"🔴"}
            for _qi in range(1, 19):
                _qid = f"Q{_qi:02d}"
                _qp = _answers_dir / f"q{_qi:02d}.json"
                if _qp.exists():
                    try:
                        _qd = _json.loads(_qp.read_text(encoding="utf-8"))
                        _st = _qd.get("status", "")
                        _q_rows.append({
                            "ID": _qid,
                            "질문": _q_meta_label.get(_qid, ""),
                            "상태": _status_icon.get(_st, "⚪") + " " + _st,
                            "신뢰도": f"{_qd.get('confidence', 0):.0%}",
                            "데이터 일수": f"{_qd.get('data_days_available', 0)}일",
                        })
                    except Exception:
                        pass
            if _q_rows:
                import pandas as _pd
                with st.expander("📋 18문 상태 일람표", expanded=False):
                    st.dataframe(
                        _pd.DataFrame(_q_rows),
                        use_container_width=True, hide_index=True,
                    )

        # ── Row D: Signal Portfolio + GTI 간략 카드 ────────────────────
        _port_path2 = DATA_DIR / "signal_portfolio.yaml"
        _wfm_dir2 = PROJECT_ROOT / "reports" / "weekly_future_map"

        _rd1, _rd2, _rd3 = st.columns(3)
        with _rd1:
            st.markdown("**📡 Signal Portfolio**")
            if _port_path2.exists():
                try:
                    import yaml as _yaml
                    _praw = _yaml.safe_load(_port_path2.read_text(encoding="utf-8")) or {}
                    _psigs = _praw.get("signals", {})
                    _pwatch = sum(1 for v in _psigs.values() if v.get("status") == "watching")
                    _pemerg = sum(1 for v in _psigs.values() if v.get("status") == "emerging")
                    _pconf = sum(1 for v in _psigs.values() if v.get("status") == "confirmed")
                    st.markdown(
                        f"총 **{len(_psigs)}**개 신호 추적  \n"
                        f"🔵 watching: {_pwatch} · 🟡 emerging: {_pemerg} · 🟢 confirmed: {_pconf}  \n"
                        f"마지막: {_praw.get('last_updated','?')}"
                    )
                except Exception:
                    st.caption("읽기 실패")
            else:
                st.caption("— 아직 생성 안 됨")
        with _rd2:
            st.markdown("**🌐 GTI 상세**")
            if _gti_path.exists():
                try:
                    _g = _gti_data
                    _comps = _g.get("components", {})
                    _dq = _g.get("data_quality", {})
                    st.markdown(
                        f"**{_g.get('gti_score',0):.1f}** — {_g.get('gti_label','')}  \n"
                        f"G1 보도집중: {_comps.get('g1_coverage_skew',0):.1f}  \n"
                        f"G2 핫스팟: {_comps.get('g2_sentiment_hotspot',0):.1f}  \n"
                        f"G3 양국긴장: {_comps.get('g3_bilateral_tension',0):.1f}"
                    )
                    _q5s = _dq.get("q05_status","—")
                    _q6s = _dq.get("q06_status","—")
                    _q7s = _dq.get("q07_status","—")
                    st.caption(f"입력 품질 — Q05: {_q5s} · Q06: {_q6s} · Q07: {_q7s}")
                except Exception:
                    st.caption("읽기 실패")
            else:
                st.caption("— GTI 미생성")
        with _rd3:
            st.markdown("**🗺️ 주간 미래 맵**")
            if _wfm_dir2.exists():
                _wfm_editions2 = sorted(
                    [p.name for p in _wfm_dir2.iterdir()
                     if p.is_dir() and not p.name.startswith("__")],
                    reverse=True,
                )
                if _wfm_editions2:
                    _latest_wfm = _wfm_editions2[0]
                    _wfm_m = _wfm_dir2 / _latest_wfm / "meta.json"
                    if _wfm_m.exists():
                        try:
                            _wm = _json.loads(_wfm_m.read_text(encoding="utf-8"))
                            st.markdown(
                                f"최신: **{_latest_wfm}**  \n"
                                f"기간: {_wm.get('start_date','')} ~ {_wm.get('end_date','')}  \n"
                                f"커버리지: {_wm.get('dates_with_data',0)}/{_wm.get('window_days',7)}일  \n"
                                f"GTI 평균: {_wm.get('gti_avg',0):.1f} — {_wm.get('gti_label','').split()[0]}"
                            )
                        except Exception:
                            st.caption(_latest_wfm)
                else:
                    st.caption("— 아직 생성 안 됨")
            else:
                st.caption("— 아직 생성 안 됨")

        st.divider()

        # ==================== 1.7) W2 / W3 NARRATIVE REPORTS ==============
        # ADR-081: wired via invoke_claude_agent.py in run_daily.sh.
        # These reports come from existing agents (@analysis-reporter,
        # @insight-narrator) that the pure-Python pipeline used to skip.

        _w2_report = PROJECT_ROOT / "workflows" / "analysis" / "outputs" / f"analysis-report-{sel_date}.md"
        # Find W3 insight run that matches date (weekly/monthly/quarterly)
        _w3_report = None
        if _w3_run:
            _w3_report = _w3_run / "synthesis" / "insight_report.md"

        _has_any_narrative = (_w2_report.exists() or (_w3_report and _w3_report.exists()))

        st.subheader("📚 Workflow Narrative Reports")

        if not _has_any_narrative:
            st.info(
                "W2/W3 내러티브 보고서가 아직 생성되지 않았습니다.  \n"
                "생성: `scripts/run_daily.sh` 다음 실행 시 자동 or "
                "`python3 scripts/reports/invoke_claude_agent.py --agent analysis-reporter ...`"
            )

        _narr_cols = st.columns(2)
        with _narr_cols[0]:
            st.markdown("**📊 W2 Analysis Report**")
            if _w2_report.exists():
                _size = _w2_report.stat().st_size
                st.caption(
                    f"{_size:,} bytes · "
                    f"{_dt.fromtimestamp(_w2_report.stat().st_mtime).strftime('%H:%M')}"
                )
                with st.expander("📄 analysis-report.md (CE3 pattern)",
                                 expanded=False):
                    st.markdown(_w2_report.read_text(encoding="utf-8"))
            else:
                st.caption("— (not generated)")
        with _narr_cols[1]:
            st.markdown("**🧠 W3 Insight Report (narrator-refined)**")
            if _w3_report and _w3_report.exists():
                _size = _w3_report.stat().st_size
                st.caption(
                    f"{_size:,} bytes · "
                    f"{_dt.fromtimestamp(_w3_report.stat().st_mtime).strftime('%H:%M')} · "
                    f"run: {_w3_run.name}"
                )
                with st.expander("📄 insight_report.md (doctoral)",
                                 expanded=False):
                    st.markdown(_w3_report.read_text(encoding="utf-8"))
            else:
                st.caption("— (not generated)")

        st.divider()

        # ==================== 2) W2 KPI ROW ====================
        if _w2_ok:
            st.subheader("📊 W2 NLP Pipeline Metrics")
            try:
                import pandas as _pd
                _analysis_df = _pd.read_parquet(_w2_analysis)
                _signals_df = _pd.read_parquet(_w2_signals)
                _topics_df = _pd.read_parquet(_w2_topics)

                k1, k2, k3, k4 = st.columns(4)
                k1.metric("Articles analyzed", f"{len(_analysis_df):,}",
                          help="W2 Stage 3에서 감성·감정·STEEPS 분류가 완료된 기사 수.")
                k2.metric("Signals detected", f"{len(_signals_df):,}",
                          help="W2 Stage 7에서 5계층 신호 분류를 통해 탐지된 신호 수 (BREAKING·TREND·WEAK·NOISE).")
                _topics_unique = (
                    _topics_df["topic_id"].nunique()
                    if "topic_id" in _topics_df.columns else len(_topics_df)
                )
                k3.metric("Unique topics", f"{_topics_unique:,}",
                          help="W2 Stage 4 BERTopic/HDBSCAN으로 클러스터링된 고유 토픽 수. -1은 노이즈 클러스터.")
                _sent_col = next(
                    (c for c in _analysis_df.columns
                     if "sentiment" in c.lower() and "score" in c.lower()),
                    None,
                )
                if _sent_col:
                    k4.metric(
                        "Mean sentiment",
                        f"{_analysis_df[_sent_col].mean():.3f}",
                        help="전체 기사의 평균 감성 점수. -1(극부정) ~ +1(극긍정). 0 근처는 중립.",
                    )
                else:
                    k4.metric("Columns", f"{len(_analysis_df.columns)}")
            except Exception as _exc:
                st.warning(f"W2 parquet read error: {_exc}")

        st.divider()

        # ==================== 3) W3 INSIGHT SNAPSHOT ====================
        if _w3_ok:
            st.subheader(f"🧠 W3 Insight — {_w3_run.name}")
            _w3_report = _w3_run / "synthesis" / "insight_report.md"
            _w3_findings = _w3_run / "synthesis" / "key_findings.json"
            _w3_modules_dir = _w3_run

            m1, m2, m3 = st.columns(3)
            _module_names = [
                d.name for d in _w3_modules_dir.iterdir()
                if d.is_dir() and d.name != "synthesis"
            ]
            m1.metric("Modules run", len(_module_names))
            if _w3_findings.exists():
                try:
                    _kf = _json.loads(_w3_findings.read_text(encoding="utf-8"))
                    _n_findings = len(_kf) if isinstance(_kf, list) else len(_kf.get("findings", []))
                    m2.metric("Key findings", _n_findings)
                except Exception:
                    m2.metric("Key findings", "?")
            m3.metric(
                "Report size",
                f"{_w3_report.stat().st_size // 1024} KB" if _w3_report.exists() else "—",
            )

            if _w3_report.exists():
                with st.expander("📄 insight_report.md (preview)", expanded=False):
                    st.markdown(_w3_report.read_text(encoding="utf-8"))

        st.divider()

        # ==================== 4) W4 MASTER REPORT ====================
        if _w4_ok:
            st.subheader("👑 W4 Master Integration Report")

            # Detect pACS/reviewer state from review-logs if available
            _review_logs = PROJECT_ROOT / "review-logs"
            _meta_log = _review_logs / f"phase-master-meta-{sel_date}.md"
            _narr_log = _review_logs / f"phase-master-narrative-{sel_date}.md"
            _ev_log = _review_logs / f"phase-master-evidence-{sel_date}.md"

            def _verdict_from_log(p: _Path) -> str:
                if not p.exists():
                    return "—"
                try:
                    head = p.read_text(encoding="utf-8")[:4000]
                    for verdict in ("PASS_WITH_WARNINGS", "PASS", "FAIL"):
                        if verdict in head.upper():
                            return verdict
                except Exception:
                    pass
                return "?"

            r1, r2, r3 = st.columns(3)
            r1.metric("@meta-reviewer", _verdict_from_log(_meta_log))
            r2.metric("@narrative-reviewer", _verdict_from_log(_narr_log))
            r3.metric("@evidence-reviewer", _verdict_from_log(_ev_log))

            # Language toggle
            _lang = st.radio(
                "Report language",
                options=["English", "한국어"],
                horizontal=True,
                key="w4_lang_toggle",
            )
            _report_path = _w4_en if _lang == "English" else _w4_ko
            st.caption(
                f"Path: `{_report_path.relative_to(PROJECT_ROOT)}` "
                f"— {_report_path.stat().st_size:,} bytes"
            )
            with st.expander("📄 Master report (full text)", expanded=True):
                st.markdown(_report_path.read_text(encoding="utf-8"))

        st.divider()

        # ==================== 5) ARTIFACT LIST ====================
        st.subheader("📁 Full Artifact Inventory")
        _artifacts: list[dict] = []

        def _add(role: str, label: str, path: _Path):
            if path.exists():
                size = path.stat().st_size if path.is_file() else sum(
                    f.stat().st_size for f in path.rglob("*") if f.is_file()
                )
                _artifacts.append({
                    "Workflow": role,
                    "Artifact": label,
                    "Path": str(path.relative_to(PROJECT_ROOT)),
                    "Size": f"{size:,} bytes" if size < 1024*1024
                            else f"{size/1024/1024:.2f} MB",
                })

        _add("W1", "all_articles.jsonl", _w1_jsonl)
        _add("W1", ".crawl_state.json",
             DATA_DIR / "raw" / sel_date / ".crawl_state.json")
        _add("W2", "analysis.parquet", _w2_analysis)
        _add("W2", "signals.parquet", _w2_signals)
        _add("W2", "topics.parquet", _w2_topics)
        _add("W2", "index.sqlite", _w2_sqlite)
        _add("W2", "checksums.md5", _w2_out / "checksums.md5")
        _add("W2", "analysis dir (stage 3-6)", DATA_DIR / "analysis" / sel_date)
        if _w3_run:
            _add("W3", "synthesis/insight_report.md",
                 _w3_run / "synthesis" / "insight_report.md")
            _add("W3", "synthesis/key_findings.json",
                 _w3_run / "synthesis" / "key_findings.json")
            _add("W3", "synthesis/insight_data.json",
                 _w3_run / "synthesis" / "insight_data.json")
            _add("W3", "synthesis/intelligence/",
                 _w3_run / "synthesis" / "intelligence")
            for mod in ["crosslingual", "economic", "entity",
                        "geopolitical", "narrative", "temporal"]:
                _add("W3", f"module: {mod}", _w3_run / mod)
        _add("W4", "final/EN", _w4_en)
        _add("W4", "final/KO", _w4_ko)
        _add("W4", "staging", PROJECT_ROOT / "reports" / "staging"
             / f"integrated-report-{sel_date}.md")
        _add("W4", "candidate", PROJECT_ROOT / "reports" / "candidate"
             / f"integrated-report-{sel_date}.md")
        _add("W4", "workflows/master/ingest/",
             PROJECT_ROOT / "workflows" / "master" / "ingest")
        _add("W4", "workflows/master/audit/",
             PROJECT_ROOT / "workflows" / "master" / "audit")
        _add("W4", "workflows/master/longitudinal/",
             PROJECT_ROOT / "workflows" / "master" / "longitudinal")
        _add("W4", "workflows/master/synthesis/",
             PROJECT_ROOT / "workflows" / "master" / "synthesis")
        _add("W4", "review-logs/meta",
             PROJECT_ROOT / "review-logs" / f"phase-master-meta-{sel_date}.md")
        _add("W4", "review-logs/narrative",
             PROJECT_ROOT / "review-logs" / f"phase-master-narrative-{sel_date}.md")
        _add("W4", "review-logs/evidence",
             PROJECT_ROOT / "review-logs" / f"phase-master-evidence-{sel_date}.md")
        # ── 빅데이터 분석 엔진 아티팩트 ──
        _add("BigData", "articles_enriched.parquet",
             DATA_DIR / "enriched" / sel_date / "articles_enriched.parquet")
        _add("BigData", "answers/ (18문 JSON)",
             DATA_DIR / "answers" / sel_date)
        _add("BigData", "gti_daily.json",
             DATA_DIR / "gti" / sel_date / "gti_daily.json")
        _add("BigData", "signal_portfolio.yaml",
             DATA_DIR / "signal_portfolio.yaml")

        if _artifacts:
            try:
                import pandas as _pd
                _df_art = _pd.DataFrame(_artifacts)
                st.dataframe(_df_art, use_container_width=True, hide_index=True)
                st.caption(f"Total: {len(_artifacts)} artifacts present for {sel_date}")
            except Exception:
                for a in _artifacts:
                    st.text(f"[{a['Workflow']}] {a['Artifact']}: {a['Path']} ({a['Size']})")
        else:
            st.info("No artifacts found yet for this date.")


# ========================= TAB 1: OVERVIEW =================================

with tab_overview:
    st.header("Crawling & Pipeline Overview")

    # ── 빅데이터 엔진 현황 (가장 최근 날짜 기준) ─────────────────────
    _ov_latest_date = (
        sorted([d for d in (DATA_DIR / "answers").iterdir()
                if d.is_dir() and _is_date_dir(d.name)],
               reverse=True)[0].name
        if (DATA_DIR / "answers").exists() and any(
            d for d in (DATA_DIR / "answers").iterdir()
            if d.is_dir() and _is_date_dir(d.name))
        else None
    )
    _ov_gti_hist = DATA_DIR / "gti" / "gti_history.jsonl"
    _ov_port = DATA_DIR / "signal_portfolio.yaml"

    if _ov_latest_date:
        st.caption(f"빅데이터 엔진 기준일: **{_ov_latest_date}** (최신 Q-Engine 결과)")
        _ov_c1, _ov_c2, _ov_c3, _ov_c4, _ov_c5, _ov_c6 = st.columns(6)

        # 18문 현황
        _ov_ans = _ov_deg = _ov_ins = 0
        for _i in range(1, 19):
            _p = DATA_DIR / "answers" / _ov_latest_date / f"q{_i:02d}.json"
            if _p.exists():
                try:
                    _s = json.loads(_p.read_text(encoding="utf-8")).get("status", "")
                    if _s == "answered": _ov_ans += 1
                    elif _s == "degraded": _ov_deg += 1
                    else: _ov_ins += 1
                except Exception: pass
        _ov_c1.metric("Q-Engine 답변", f"{_ov_ans}/18", f"deg={_ov_deg}",
                      help="18개 핵심 빅데이터 질문 중 완전 응답(answered) 수. 데이터 누적 기간이 길어질수록 자동으로 증가합니다.")

        # GTI (최신)
        _ov_gti_path = DATA_DIR / "gti" / _ov_latest_date / "gti_daily.json"
        if _ov_gti_path.exists():
            try:
                _ov_gti = json.loads(_ov_gti_path.read_text(encoding="utf-8"))
                _ov_c2.metric("GTI", f"{_ov_gti.get('gti_score',0):.1f}", _ov_gti.get("gti_label",""),
                              help="Geopolitical Tension Index. 보도 집중도(G1)·핫스팟 감성(G2)·양국 긴장(G3) 합성. 0=평온 / 100=극도 긴장.")
            except Exception:
                _ov_c2.metric("GTI", "—")
        else:
            _ov_c2.metric("GTI", "—")

        # GTI 추세 (직전 7일 평균 vs 오늘)
        if _ov_gti_hist.exists():
            try:
                _ov_hist_rows = [
                    json.loads(l) for l in _ov_gti_hist.read_text(encoding="utf-8").splitlines()
                    if l.strip()
                ]
                _ov_hist_df = pd.DataFrame(_ov_hist_rows).drop_duplicates("date", keep="last")
                _ov_hist_df = _ov_hist_df.sort_values("date")
                _ov_hist_recent = _ov_hist_df["gti_score"].tail(7).tolist()
                _ov_hist_avg = sum(_ov_hist_recent[:-1]) / max(len(_ov_hist_recent) - 1, 1) if len(_ov_hist_recent) > 1 else 0
                _ov_gti_delta = _ov_hist_recent[-1] - _ov_hist_avg if _ov_hist_recent else 0
                _ov_c3.metric("GTI 7일 평균", f"{_ov_hist_avg:.1f}", f"{_ov_gti_delta:+.1f} 오늘",
                              help="직전 7일 GTI 평균 대비 오늘 값의 차이. 양수(+)이면 긴장 상승, 음수(-)이면 완화.")
            except Exception:
                _ov_c3.metric("GTI 추세", "—")
        else:
            _ov_c3.metric("GTI 추세", "—")

        # Signal Portfolio
        if _ov_port.exists():
            try:
                import yaml as _ov_yaml
                _ov_p = _ov_yaml.safe_load(_ov_port.read_text(encoding="utf-8")) or {}
                _ov_sigs = _ov_p.get("signals", {})
                _ov_emerg = sum(1 for v in _ov_sigs.values() if v.get("status") in ("emerging","confirmed"))
                _ov_c4.metric("포트폴리오", f"{len(_ov_sigs)}개 신호", f"활성 {_ov_emerg}개",
                              help="Future Signal Portfolio. Q08(약한 신호)을 날짜별 누적 추적.\n\n"
                                   "watching → emerging(3일+) → confirmed(7일+) → dismissed(14일 미등장)")
            except Exception:
                _ov_c4.metric("포트폴리오", "—")
        else:
            _ov_c4.metric("포트폴리오", "—")

        # 축적 날짜 수
        _ov_dates_count = sum(
            1 for d in (DATA_DIR / "enriched").iterdir()
            if d.is_dir() and _is_date_dir(d.name)
        ) if (DATA_DIR / "enriched").exists() else 0
        _ov_c5.metric("누적 분석일", f"{_ov_dates_count}일",
                      help="articles_enriched.parquet가 생성된 날짜 수. 누적일이 많을수록 트렌드·인과 분석의 신뢰도가 높아집니다.\n\n"
                           "- Q03 활성화: 14일\n- Q10 활성화: 21일\n- Q16 Granger 분석: 30일")

        # 총 기사 수
        try:
            _ov_total_arts = sum(
                sum(1 for l in (DATA_DIR / "raw" / d.name / "all_articles.jsonl").open(encoding="utf-8") if l.strip())
                for d in (DATA_DIR / "enriched").iterdir()
                if d.is_dir() and _is_date_dir(d.name)
                and (DATA_DIR / "raw" / d.name / "all_articles.jsonl").exists()
            )
            _ov_c6.metric("총 기사 수", f"{_ov_total_arts:,}",
                          help="전체 누적 분석일의 크롤링 기사 합계. 빅데이터 분석의 실질적 표본 크기.")
        except Exception:
            _ov_c6.metric("총 기사 수", "—",
                          help="전체 누적 분析日의 크롤링 기사 합계.")

        st.divider()

    # ----- Auto-Insights -----
    st.subheader("🎯 Auto-Insights")
    di.render_insights(
        st, di.insight_overview_a(raw_df, articles_df, list(active_dates)),
    )
    st.divider()
    st.subheader("📊 Supporting Charts")

    if raw_df is not None:
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Total Articles Crawled", format_number(len(raw_df)))
        col2.metric("Unique Sources", format_number(raw_df["source_id"].nunique()))
        col3.metric("Languages", format_number(raw_df["language"].nunique()))
        if articles_df is not None:
            col4.metric("After Dedup/Processing", format_number(len(articles_df)))
        else:
            col4.metric("After Processing", "N/A")

        # Days covered
        if len(active_dates) > 1:
            col_d1, col_d2 = st.columns(2)
            col_d1.metric("Days in Period", len(active_dates))
            avg_per_day = len(raw_df) / len(active_dates)
            col_d2.metric("Avg Articles/Day", format_number(avg_per_day))

    st.subheader("Articles by Source")

    if raw_df is not None:
        source_counts = (
            raw_df.groupby("source_id")
            .size()
            .reset_index(name="articles")
            .sort_values("articles", ascending=False)
        )
        source_counts["group"] = source_counts["source_id"].map(SOURCE_GROUPS).fillna("?")
        source_counts["group_name"] = source_counts["group"].map(GROUP_NAMES).fillna("Unknown")

        fig_src = px.bar(
            source_counts,
            x="source_id",
            y="articles",
            color="group_name",
            title="Articles per Source (colored by Group)",
            labels={"source_id": "Source", "articles": "Articles", "group_name": "Group"},
            text_auto=True,
            height=450,
        )
        fig_src.update_layout(xaxis_tickangle=-45)
        st.plotly_chart(fig_src, use_container_width=True)

        col_left, col_right = st.columns(2)

        with col_left:
            group_counts = (
                source_counts.groupby(["group", "group_name"])["articles"]
                .sum()
                .reset_index()
                .sort_values("group")
            )
            fig_grp = px.pie(
                group_counts,
                values="articles",
                names="group_name",
                title="Articles by Group",
                hole=0.4,
            )
            st.plotly_chart(fig_grp, use_container_width=True)

        with col_right:
            lang_counts = raw_df["language"].value_counts().reset_index()
            lang_counts.columns = ["language", "count"]
            lang_counts["lang_name"] = lang_counts["language"].map(LANG_NAMES).fillna(lang_counts["language"])
            fig_lang = px.pie(
                lang_counts,
                values="count",
                names="lang_name",
                title="Articles by Language",
                hole=0.4,
            )
            st.plotly_chart(fig_lang, use_container_width=True)

        # Daily trend (useful for multi-day periods)
        if len(active_dates) > 1 and "_data_date" in raw_df.columns:
            st.subheader("Daily Article Volume")
            daily_vol = raw_df.groupby("_data_date").size().reset_index(name="articles")
            fig_daily = px.bar(
                daily_vol, x="_data_date", y="articles",
                title="Articles Collected per Day",
                labels={"_data_date": "Date", "articles": "Articles"},
                text_auto=True,
            )
            st.plotly_chart(fig_daily, use_container_width=True)

    # Pipeline stage summary
    st.subheader("Pipeline Output Files")
    file_info = []
    _file_defs = [
        ("Raw JSONL", "raw", "all_articles.jsonl"),
        ("Processed Articles", "processed", "articles.parquet"),
        ("Article Analysis", "analysis", "article_analysis.parquet"),
        ("Topics", "analysis", "topics.parquet"),
        ("Time Series", "analysis", "timeseries.parquet"),
        ("Cross Analysis", "analysis", "cross_analysis.parquet"),
        ("Networks", "analysis", "networks.parquet"),
        ("Output Analysis", "output", "analysis.parquet"),
        ("Signals", "output", "signals.parquet"),
    ]
    for label, sub, fname in _file_defs:
        total_size = 0.0
        found = 0
        for d in active_dates:
            p = DATA_DIR / sub / d / fname
            if p.exists():
                total_size += p.stat().st_size / (1024 * 1024)
                found += 1
        status = f"✅ ({found}/{len(active_dates)})" if found > 0 else "❌"
        file_info.append({
            "File": label,
            "Name": fname,
            "Total Size (MB)": round(total_size, 2),
            "Days Found": found,
            "Status": status,
        })
    st.dataframe(pd.DataFrame(file_info), use_container_width=True, hide_index=True)


# ========================= TAB 2: TOPICS ====================================

with tab_topics:
    st.header("Topic Analysis")

    # ----- Auto-Insights -----
    st.subheader("🎯 Auto-Insights")
    di.render_insights(st, di.insight_topics_a(topics_df, merged_df))
    st.divider()
    st.subheader("📊 Supporting Charts")

    if topics_df is not None:
        topic_counts = (
            topics_df[topics_df["topic_id"] != -1]
            .groupby(["topic_id", "topic_label"])
            .size()
            .reset_index(name="articles")
            .sort_values("articles", ascending=False)
        )

        col_info1, col_info2, col_info3 = st.columns(3)
        n_topics = topics_df[topics_df["topic_id"] != -1]["topic_id"].nunique()
        n_outliers = (topics_df["topic_id"] == -1).sum()
        outlier_pct = n_outliers / len(topics_df) * 100 if len(topics_df) > 0 else 0
        col_info1.metric("Topics Discovered", n_topics)
        col_info2.metric("Outlier Articles", f"{n_outliers} ({outlier_pct:.1f}%)")
        col_info3.metric("Total Articles", format_number(len(topics_df)))

        topic_counts["short_label"] = topic_counts["topic_label"].str[:40]

        fig_topics = px.bar(
            topic_counts.head(20),
            x="articles",
            y="short_label",
            orientation="h",
            title="Top 20 Topics by Article Count",
            labels={"short_label": "Topic", "articles": "Articles"},
            text_auto=True,
            height=600,
        )
        fig_topics.update_layout(yaxis=dict(autorange="reversed"))
        st.plotly_chart(fig_topics, use_container_width=True)

        st.subheader("Topic Assignment Confidence")
        fig_prob = px.histogram(
            topics_df[topics_df["topic_id"] != -1],
            x="topic_probability",
            nbins=50,
            title="Distribution of Topic Assignment Probability",
            labels={"topic_probability": "Probability"},
        )
        st.plotly_chart(fig_prob, use_container_width=True)

        if merged_df is not None and "steeps_category" in merged_df.columns:
            st.subheader("STEEPS Classification")
            steeps = merged_df["steeps_category"].value_counts().reset_index()
            steeps.columns = ["category", "count"]
            fig_steeps = px.bar(
                steeps,
                x="category",
                y="count",
                title="Articles by STEEPS Category",
                color="category",
                text_auto=True,
            )
            st.plotly_chart(fig_steeps, use_container_width=True)

        # Topic evolution across days (multi-day periods)
        if len(active_dates) > 1 and "_data_date" in topics_df.columns:
            st.subheader("Topic Trends Across Days")
            top5_topics = topic_counts.head(5)["topic_id"].tolist()
            topic_daily = (
                topics_df[topics_df["topic_id"].isin(top5_topics)]
                .groupby(["_data_date", "topic_label"])
                .size()
                .reset_index(name="articles")
            )
            if len(topic_daily) > 0:
                fig_topic_trend = px.line(
                    topic_daily, x="_data_date", y="articles", color="topic_label",
                    title="Top 5 Topics — Daily Trend",
                    labels={"_data_date": "Date", "articles": "Articles", "topic_label": "Topic"},
                )
                st.plotly_chart(fig_topic_trend, use_container_width=True)
    else:
        st.warning("Topics data not available.")


# ========================= TAB 3: SENTIMENT & EMOTIONS =====================

with tab_sentiment:
    st.header("Sentiment & Emotion Analysis")

    # ----- Auto-Insights -----
    st.subheader("🎯 Auto-Insights")
    di.render_insights(st, di.insight_sentiment_a(merged_df))
    st.divider()
    st.subheader("📊 Supporting Charts")

    if analysis_df is not None:
        col_s1, col_s2 = st.columns(2)

        with col_s1:
            sent_counts = analysis_df["sentiment_label"].value_counts().reset_index()
            sent_counts.columns = ["label", "count"]
            color_map = {"positive": "#2ecc71", "negative": "#e74c3c", "neutral": "#95a5a6"}
            fig_sent = px.pie(
                sent_counts,
                values="count",
                names="label",
                title="Sentiment Distribution",
                color="label",
                color_discrete_map=color_map,
                hole=0.4,
            )
            st.plotly_chart(fig_sent, use_container_width=True)

        with col_s2:
            fig_score = px.histogram(
                analysis_df,
                x="sentiment_score",
                nbins=50,
                title="Sentiment Score Distribution",
                labels={"sentiment_score": "Score (-1 to 1)"},
                color_discrete_sequence=["#3498db"],
            )
            st.plotly_chart(fig_score, use_container_width=True)

        # Sentiment trend across days
        if len(active_dates) > 1 and "_data_date" in analysis_df.columns:
            st.subheader("Sentiment Trend Across Days")
            sent_daily = (
                analysis_df.groupby(["_data_date", "sentiment_label"])
                .size()
                .reset_index(name="count")
            )
            fig_sent_trend = px.bar(
                sent_daily, x="_data_date", y="count", color="sentiment_label",
                title="Sentiment Distribution by Day",
                labels={"_data_date": "Date", "count": "Articles", "sentiment_label": "Sentiment"},
                color_discrete_map=color_map,
                barmode="stack",
            )
            st.plotly_chart(fig_sent_trend, use_container_width=True)

        # Emotion radar
        st.subheader("Emotion Profile (Average Across All Articles)")
        emotion_cols = [c for c in analysis_df.columns if c.startswith("emotion_")]
        if emotion_cols:
            avg_emotions = analysis_df[emotion_cols].mean()
            emotion_labels = [c.replace("emotion_", "").title() for c in emotion_cols]

            fig_radar = go.Figure()
            fig_radar.add_trace(go.Scatterpolar(
                r=avg_emotions.values.tolist() + [avg_emotions.values[0]],
                theta=emotion_labels + [emotion_labels[0]],
                fill="toself",
                name="Average",
                line_color="#3498db",
            ))
            fig_radar.update_layout(
                polar=dict(radialaxis=dict(visible=True, range=[0, 1])),
                title="Emotion Radar — Global Average",
                height=500,
            )
            st.plotly_chart(fig_radar, use_container_width=True)

            if merged_df is not None and "source" in merged_df.columns:
                st.subheader("Emotion Heatmap by Source")
                _emo_cols_merged = [c for c in merged_df.columns if c.startswith("emotion_")]
                if _emo_cols_merged:
                    emo_by_source = (
                        merged_df.groupby("source")[_emo_cols_merged]
                        .mean()
                        .sort_index()
                    )
                    emo_labels = [c.replace("emotion_", "").title() for c in _emo_cols_merged]
                    emo_by_source.columns = emo_labels

                    fig_heat = px.imshow(
                        emo_by_source.values,
                        x=emo_by_source.columns.tolist(),
                        y=emo_by_source.index.tolist(),
                        color_continuous_scale="RdYlBu_r",
                        title="Average Emotion Scores by News Source",
                        labels=dict(color="Score"),
                        aspect="auto",
                        height=max(400, len(emo_by_source) * 25),
                    )
                    st.plotly_chart(fig_heat, use_container_width=True)

        # Mood trajectory
        if mood_df is not None and len(mood_df) > 0:
            st.subheader("Mood Trajectory")
            fig_mood = px.line(
                mood_df,
                x="date",
                y="mood_index",
                color="source",
                title="Mood Index Over Time",
                labels={"mood_index": "Mood Index", "date": "Date"},
            )
            st.plotly_chart(fig_mood, use_container_width=True)
    else:
        st.warning("Analysis data not available.")


# ========================= TAB 5: WORD CLOUD ================================

with tab_wordcloud:
    st.header("Word Cloud Analysis")

    if raw_df is not None:
        wc_col1, wc_col2, wc_col3 = st.columns(3)

        with wc_col1:
            wc_lang_options = ["All", "Korean (ko)", "English (en)"]
            wc_lang = st.selectbox("Language Filter", wc_lang_options, key="wc_lang")

        with wc_col2:
            wc_group_options = ["All"] + [
                f"{k} — {v}" for k, v in sorted(GROUP_NAMES.items())
            ]
            wc_group = st.selectbox("Group Filter", wc_group_options, key="wc_group")

        with wc_col3:
            wc_max_words = st.slider("Max Words", 50, 300, 150, step=25, key="wc_max")

        wc_filtered = raw_df.copy()

        if wc_lang == "Korean (ko)":
            wc_filtered = wc_filtered[wc_filtered["language"] == "ko"]
        elif wc_lang == "English (en)":
            wc_filtered = wc_filtered[wc_filtered["language"] == "en"]

        if wc_group != "All":
            group_letter = wc_group.split(" — ")[0]
            group_sources = {
                sid for sid, g in SOURCE_GROUPS.items() if g == group_letter
            }
            wc_filtered = wc_filtered[wc_filtered["source_id"].isin(group_sources)]

        st.caption(f"Analyzing {len(wc_filtered):,} articles")

        if len(wc_filtered) == 0:
            st.warning("No articles match the selected filters.")
        else:
            texts = wc_filtered["body"].fillna("").tolist()
            langs = wc_filtered["language"].fillna("en").tolist()

            with st.spinner("Extracting words (Korean NLP + English tokenization)..."):
                word_freq = extract_word_frequencies(texts, langs)

            if not word_freq:
                st.warning("No words extracted. Try different filters.")
            else:
                st.success(f"Extracted {len(word_freq):,} unique words")

                st.subheader("Word Cloud")

                has_korean = any(
                    "\uac00" <= ch <= "\ud7a3"
                    for w in list(word_freq.keys())[:100]
                    for ch in w
                )
                font_path = None
                if has_korean:
                    for fp in [
                        "/System/Library/Fonts/AppleSDGothicNeo.ttc",
                        "/System/Library/Fonts/Supplemental/AppleGothic.ttf",
                        "/Library/Fonts/NanumGothic.ttf",
                    ]:
                        if Path(fp).exists():
                            font_path = fp
                            break

                wc = WordCloud(
                    width=1200,
                    height=600,
                    max_words=wc_max_words,
                    background_color="white",
                    colormap="viridis",
                    font_path=font_path,
                    prefer_horizontal=0.7,
                    min_font_size=10,
                    max_font_size=120,
                    relative_scaling=0.5,
                )
                wc.generate_from_frequencies(word_freq)

                fig_wc, ax_wc = plt.subplots(figsize=(14, 7))
                ax_wc.imshow(wc, interpolation="bilinear")
                ax_wc.axis("off")
                st.pyplot(fig_wc)
                plt.close(fig_wc)

                st.subheader("Top 30 Words by Frequency")
                top_words = sorted(
                    word_freq.items(), key=lambda x: x[1], reverse=True
                )[:30]
                top_df = pd.DataFrame(top_words, columns=["word", "count"])

                fig_top = px.bar(
                    top_df,
                    x="count",
                    y="word",
                    orientation="h",
                    title="Top 30 Most Frequent Words",
                    labels={"word": "Word", "count": "Frequency"},
                    text_auto=True,
                    height=700,
                    color="count",
                    color_continuous_scale="viridis",
                )
                fig_top.update_layout(
                    yaxis=dict(autorange="reversed"),
                    showlegend=False,
                )
                st.plotly_chart(fig_top, use_container_width=True)

                col_ws1, col_ws2, col_ws3 = st.columns(3)
                col_ws1.metric("Unique Words", f"{len(word_freq):,}")
                col_ws2.metric("Total Word Count", f"{sum(word_freq.values()):,}")
                col_ws3.metric(
                    "Most Frequent",
                    f"{top_words[0][0]} ({top_words[0][1]:,})" if top_words else "N/A",
                )
    else:
        st.warning("Raw article data not available.")


# ---------------------------------------------------------------------------
# Sidebar — Cross Analysis summary + meta
# ---------------------------------------------------------------------------

with st.sidebar:
    st.header("Cross Analysis")

    if cross_df is not None and len(cross_df) > 0:
        analysis_types = cross_df["analysis_type"].value_counts()
        st.markdown("**Technique Results**")
        for atype, cnt in analysis_types.items():
            st.text(f"  {atype}: {cnt:,}")

        st.markdown("---")
        st.markdown("**Top Entity Pairs (by effective strength)**")

        # D-7: saturation workaround. Legacy Stage 6 normalized frame/graphrag
        # strength with a linear cap that tied 93% of rows at 1.0, making
        # ranking order-of-insertion. For frame rows we parse KL from the
        # relationship string; for graphrag we compute from metadata chain
        # length. All other types use strength as-is. New Stage 6 writes
        # log-scaled strength directly, so this shim is a no-op going forward.
        def _effective_strength(row):
            base = row.get("strength")
            if pd.isna(base):
                return np.nan
            rel = str(row.get("relationship", ""))
            atype = row.get("analysis_type", "")
            # Frame: parse KL from relationship when saturated at 1.0.
            if atype == "frame" and base >= 0.999:
                import math as _m
                m = re.search(r"KL=([\d.]+)", rel)
                if m:
                    try:
                        kl = float(m.group(1))
                        return min(1.0, _m.log1p(kl) / _m.log1p(25.0))
                    except ValueError:
                        pass
            # GraphRAG: legacy min(1.0, len/10) saturates; parse entities
            # + articles from relationship for monotonic ordering.
            if atype == "graphrag" and base >= 0.999:
                import math as _m
                m = re.search(r"entities=(\d+),\s*articles=(\d+)", rel)
                if m:
                    try:
                        n_ent = int(m.group(1))
                        n_art = int(m.group(2))
                        return min(1.0, (
                            _m.log1p(n_ent) / _m.log1p(500.0) * 0.6
                            + _m.log1p(n_art) / _m.log1p(200.0) * 0.4
                        ))
                    except ValueError:
                        pass
            # Agenda: perfect correlation with lag=0 is almost always a
            # sparse-sample artifact (n=2-3 time points gives |r|=1 trivially).
            # Penalize to push them below genuine lagged relationships.
            if atype == "agenda" and base >= 0.999:
                lag = row.get("lag_days")
                try:
                    lag_f = float(lag) if lag is not None else 0.0
                except (TypeError, ValueError):
                    lag_f = 0.0
                if lag_f <= 0.0:
                    return 0.5  # trivial concurrent — demote
                # Give a meaningful lag gradient: lag=1 → 0.95, lag=7 → 0.99
                import math as _m
                return 0.9 + 0.09 * (_m.log1p(lag_f) / _m.log1p(30.0))
            return float(base)

        _scored = cross_df[cross_df["strength"].notna()].copy()
        _scored["_eff"] = _scored.apply(_effective_strength, axis=1)
        # Secondary: inverse p_value (smaller p → more robust). NaN → 1.0 (back).
        _scored["_p_key"] = _scored["p_value"].fillna(1.0).astype(float)
        # Tertiary: raw entity count from graphrag relationship string
        def _parse_chain_size(rel: str) -> int:
            m = re.search(r"entities=(\d+)", str(rel))
            return int(m.group(1)) if m else 0
        def _parse_chain_articles(rel: str) -> int:
            m = re.search(r"articles=(\d+)", str(rel))
            return int(m.group(1)) if m else 0
        _scored["_chain_ent"] = _scored["relationship"].apply(_parse_chain_size)
        _scored["_chain_art"] = _scored["relationship"].apply(_parse_chain_articles)
        # Stratified diversity: cap at 3 rows per analysis_type in top-10
        # so one saturated type (graphrag) doesn't monopolize the view.
        _scored = _scored.sort_values(
            by=["_eff", "_chain_ent", "_chain_art", "_p_key"],
            ascending=[False, False, False, True],
        )
        _per_type_cap = 3
        _acc = []
        _seen: dict[str, int] = {}
        for _, _row in _scored.iterrows():
            _t = _row["analysis_type"]
            if _seen.get(_t, 0) >= _per_type_cap:
                continue
            _acc.append(_row)
            _seen[_t] = _seen.get(_t, 0) + 1
            if len(_acc) >= 10:
                break
        top_cross = (
            pd.DataFrame(_acc)
            [["analysis_type", "source_entity", "target_entity",
              "relationship", "_eff"]]
            .rename(columns={"_eff": "strength"})
        )
        if len(top_cross) > 0:
            st.dataframe(top_cross, use_container_width=True, hide_index=True)
    else:
        st.info("No cross-analysis data.")

    if networks_df is not None and len(networks_df) > 0:
        st.markdown("---")
        st.markdown("**Network Stats**")
        st.text(f"  Edges: {len(networks_df):,}")
        st.text(f"  Unique entities: {pd.concat([networks_df['entity_a'], networks_df['entity_b']]).nunique():,}")
        st.text(f"  Communities: {networks_df['community_id'].nunique()}")

    st.markdown("---")
    st.caption("GlobalNews Crawling & Analysis Pipeline")
    st.caption(f"Available dates: {len(all_dates)} | Current: {period} view")


# ========================= TAB: 18 QUESTIONS ================================

_Q_META = {
    "Q01": ("버스트 탐지", "🔥"),
    "Q02": ("트렌드 추이", "📈"),
    "Q03": ("사건 전후 변화", "🔄"),
    "Q04": ("프레이밍 비교", "🗣️"),
    "Q05": ("국가 감성 변화", "🌐"),
    "Q06": ("다크 코너 탐지", "🕳️"),
    "Q07": ("양국 긴장/완화", "⚖️"),
    "Q08": ("약한 신호 탐지", "📡"),
    "Q09": ("패러다임 전환 전조", "🌀"),
    "Q10": ("의제 이동 패턴", "🔀"),
    "Q11": ("의제 선점 언론사", "🏁"),
    "Q12": ("진보/보수 강조점", "🔴🔵"),
    "Q13": ("언어권 독자 의제", "🌍"),
    "Q14": ("미디어 보도 격차", "📏"),
    "Q15": ("뉴스 감성 선행성", "📊"),
    "Q16": ("이슈 인과 연쇄", "🔗"),
    "Q17": ("동시 급증 클러스터", "💥"),
    "Q18": ("글로벌 의제 중심 엔티티", "🎯"),
}

_STATUS_COLOR = {
    "answered": "🟢",
    "degraded": "🟡",
    "insufficient_data": "🔴",
}

_STEEPS_EMOJI = {
    "SOC": "👥", "TEC": "💻", "ECO": "💰", "ENV": "🌿",
    "POL": "🏛️", "SEC": "🛡️", "SPI": "🙏", "CRS": "⚠️",
}


@st.cache_data(ttl=120)
def _load_all_answers(date: str) -> dict[str, dict]:
    """Load all 18 Q-Engine JSON files for a given date."""
    answers = {}
    base = DATA_DIR / "answers" / date
    if not base.exists():
        return answers
    for i in range(1, 19):
        p = base / f"q{i:02d}.json"
        if p.exists():
            try:
                answers[f"Q{i:02d}"] = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                pass
    return answers


@st.cache_data(ttl=300)
def _answer_history() -> pd.DataFrame:
    """Summary of answered/degraded/insufficient counts per date."""
    rows = []
    base = DATA_DIR / "answers"
    if not base.exists():
        return pd.DataFrame()
    for d in sorted(base.iterdir()):
        if not d.is_dir() or not _is_date_dir(d.name):
            continue
        ans = deg = ins = 0
        for i in range(1, 19):
            p = d / f"q{i:02d}.json"
            if p.exists():
                try:
                    s = json.loads(p.read_text(encoding="utf-8")).get("status", "")
                    if s == "answered":
                        ans += 1
                    elif s == "degraded":
                        deg += 1
                    else:
                        ins += 1
                except Exception:
                    pass
        rows.append({"date": d.name, "answered": ans, "degraded": deg, "insufficient": ins})
    return pd.DataFrame(rows)


def _render_question_card(qid: str, data: dict) -> None:
    status = data.get("status", "unknown")
    icon = _STATUS_COLOR.get(status, "⚪")
    short, emoji = _Q_META.get(qid, (qid, "🔢"))
    confidence = data.get("confidence", 0.0)
    findings = data.get("top_findings") or []
    elapsed = data.get("elapsed_ms", 0)

    with st.expander(f"{icon} {emoji} **{qid}** {data.get('question_ko', short)}", expanded=False):
        col_a, col_b, col_c = st.columns(3)
        col_a.metric("상태", status.upper(),
                     help="answered: 완전 응답 · degraded: 부분 응답 · insufficient_data: 데이터 부족으로 응답 불가")
        col_b.metric("신뢰도", f"{confidence:.0%}",
                     help="알고리즘이 이 답변을 얼마나 확신하는지. 데이터 양·품질·분석 방법에 따라 결정됩니다.")
        col_c.metric("데이터 일수", f"{data.get('data_days_available', 0)}일",
                     help="이 질문 답변에 활용된 과거 데이터 일수. 일수가 많을수록 트렌드 분석이 정확해집니다.")

        if findings:
            st.markdown("**핵심 발견:**")
            for f in findings[:5]:
                st.markdown(f"- {f}")

        answer = data.get("answer") or {}
        if isinstance(answer, dict) and answer and status != "insufficient_data":
            _render_answer_detail(qid, answer)

        if elapsed:
            st.caption(f"처리 시간: {elapsed:.0f}ms")


def _render_answer_detail(qid: str, answer: dict) -> None:
    """Render question-specific detail view."""
    st.markdown("---")

    if qid == "Q01" and "bursting_topics" in answer:
        rows = answer["bursting_topics"]
        if rows:
            df = pd.DataFrame(rows)
            df["category"] = df["steeps"].map(lambda s: f"{_STEEPS_EMOJI.get(s, '')} {s}")
            fig = px.bar(
                df, x="category", y="burst_score", color="burst_score",
                color_continuous_scale="Reds",
                labels={"burst_score": "버스트 점수", "category": "카테고리"},
                title="STEEPS 카테고리별 버스트 점수",
            )
            fig.update_layout(height=300, showlegend=False)
            st.plotly_chart(fig, use_container_width=True)

    elif qid == "Q02" and "rising" in answer:
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**📈 상승 트렌드**")
            for item in (answer.get("rising") or [])[:5]:
                st.markdown(f"- `{item.get('steeps','')}` {item.get('trend_direction','')} ({item.get('growth_pct',0):.0f}%)")
        with c2:
            st.markdown("**📉 하락 트렌드**")
            for item in (answer.get("falling") or [])[:5]:
                st.markdown(f"- `{item.get('steeps','')}` {item.get('trend_direction','')} ({item.get('growth_pct',0):.0f}%)")

    elif qid == "Q06" and "all_regions" in answer:
        rows = answer.get("all_regions") or []
        if rows:
            df = pd.DataFrame(rows[:20])
            if "region" in df.columns and "coverage_pct" in df.columns:
                fig = px.bar(
                    df, x="coverage_pct", y="region", orientation="h",
                    color="coverage_pct", color_continuous_scale="Blues",
                    title="지역별 보도 비율 (%)",
                    labels={"coverage_pct": "보도 비율 (%)", "region": "지역"},
                )
                fig.update_layout(height=400, showlegend=False)
                st.plotly_chart(fig, use_container_width=True)
        dark = answer.get("dark_corners_under3pct") or []
        if dark:
            dark_names = [
                d.get("region", str(d)) if isinstance(d, dict) else str(d)
                for d in dark[:10]
                if (d.get("region", "") if isinstance(d, dict) else d) not in ("UNKNOWN", "")
            ]
            if dark_names:
                st.markdown(f"**🕳️ 다크 코너** (보도율 3% 미만): {', '.join(dark_names)}")

    elif qid == "Q08" and "weak_signals" in answer:
        signals = answer.get("weak_signals") or []
        total = answer.get("total_detected", 0)
        st.caption(f"총 {total}개 약한 신호 탐지")
        for s in signals[:8]:
            geo = s.get("geo_focus") or s.get("geo_focus_primary", "")
            if isinstance(geo, dict):
                geo = ""
            st.markdown(
                f"- `{s.get('steeps','')}` **{s.get('title','')}** "
                f"{'· ' + str(geo) if geo and geo != 'UNKNOWN' else ''} "
                f"_{s.get('source_id','')}_ (novelty={s.get('novelty_score',0):.2f})"
            )

    elif qid == "Q11" and "agenda_setters" in answer:
        setters = answer.get("agenda_setters") or []
        if setters:
            df = pd.DataFrame(setters[:15])
            if "source_id" in df.columns and "articles_today" in df.columns:
                st.dataframe(
                    df[["source_id", "articles_today", "unique_steeps"]].rename(
                        columns={"source_id": "언론사", "articles_today": "기사수", "unique_steeps": "다양성"}
                    ),
                    use_container_width=True, hide_index=True,
                )

    elif qid == "Q12" and "lean_comparison" in answer:
        comp = answer.get("lean_comparison") or {}
        c1, c2 = st.columns(2)
        for col, lean, label in [(c1, "LEFT", "🔵 진보"), (c2, "RIGHT", "🔴 보수")]:
            with col:
                st.markdown(f"**{label}**")
                steeps_dist = comp.get(lean, {}).get("top_steeps") or {}
                for k, v in list(steeps_dist.items())[:4]:
                    st.markdown(f"- `{k}` {v:.0%}")

    elif qid == "Q17" and "clusters" in answer:
        clusters = answer.get("clusters") or []
        for idx, cl in enumerate(clusters[:5]):
            entities = cl.get("entities") or []
            steeps_list = cl.get("steeps_set") or []
            st.markdown(
                f"**클러스터 {idx+1}** — {' · '.join(steeps_list)} "
                f"({cl.get('article_count',0)}건)"
            )
            if entities:
                st.caption(", ".join(str(e) for e in entities[:8]))

    elif qid == "Q18" and "top_entities" in answer:
        by_type = answer.get("by_type") or {}
        tabs_ent = st.tabs(["인물", "기관", "국가"])
        for tab_e, etype in zip(tabs_ent, ["PERSON", "ORG", "GPE"]):
            with tab_e:
                ents = by_type.get(etype) or []
                for e in ents[:10]:
                    st.markdown(f"- **{e.get('entity','')}** ({e.get('mention_count',0)}회)")

    else:
        # Generic: show answer as JSON
        if answer and not (len(answer) == 1 and "reason" in answer):
            st.json(answer, expanded=False)


with tab_questions:
    st.header("🔢 18 Core Questions — Big Data Analysis Engine")
    st.caption(
        "뉴스 빅데이터 분석의 18개 핵심 질문에 대한 매일 자동 산출 결과. "
        "`data/answers/{date}/` 기반. 🟢 answered · 🟡 degraded · 🔴 insufficient"
    )

    # ---- date picker ----
    _ans_root = DATA_DIR / "answers"
    _ans_dates = sorted(
        [d.name for d in _ans_root.iterdir() if d.is_dir() and len(d.name) == 10],
        reverse=True,
    ) if _ans_root.exists() else []

    if not _ans_dates:
        st.warning(
            "아직 18-Question 결과가 없습니다. "
            "`python scripts/backfill_enriched.py` 를 실행하세요."
        )
    else:
        _sel_date = st.selectbox(
            "날짜 선택", _ans_dates, key="q_date_sel",
            format_func=lambda x: x,
        )
        _answers = _load_all_answers(_sel_date)
        _n_ans = sum(1 for v in _answers.values() if v.get("status") == "answered")
        _n_deg = sum(1 for v in _answers.values() if v.get("status") == "degraded")
        _n_ins = sum(1 for v in _answers.values() if v.get("status") == "insufficient_data")
        _avg_conf = (
            sum(v.get("confidence", 0) for v in _answers.values()) / len(_answers)
            if _answers else 0
        )

        # KPI row
        kc1, kc2, kc3, kc4 = st.columns(4)
        kc1.metric("🟢 Answered", _n_ans, f"/ {len(_answers)}",
                   help="충분한 데이터로 완전 응답된 질문 수. 알고리즘이 신뢰할 수 있는 결론을 도출함.")
        kc2.metric("🟡 Degraded", _n_deg,
                   help="데이터가 부족하거나 보조 수단으로 부분 답변한 질문 수. 결과는 있으나 신뢰도가 제한적.")
        kc3.metric("🔴 Insufficient", _n_ins,
                   help="최소 데이터 임계값에 미달해 아직 응답 불가한 질문 수. 데이터 누적이 더 필요합니다.\n\n"
                        "- Q03: 14일 이상 필요\n- Q10: 21일 이상 필요\n- Q16: 30일 이상 필요")
        kc4.metric("평균 신뢰도", f"{_avg_conf:.0%}",
                   help="18개 질문의 신뢰도(confidence) 평균. 신뢰도는 0~100%로, 해당 답변의 데이터 충분도와 "
                        "알고리즘 확신도를 나타냅니다. insufficient 질문(0%)이 평균을 낮춥니다.")

        st.markdown("---")

        # History sparkline
        _hist_df = _answer_history()
        if not _hist_df.empty:
            fig_hist = go.Figure()
            fig_hist.add_trace(go.Bar(
                x=_hist_df["date"], y=_hist_df["answered"],
                name="Answered", marker_color="#2ecc71",
            ))
            fig_hist.add_trace(go.Bar(
                x=_hist_df["date"], y=_hist_df["degraded"],
                name="Degraded", marker_color="#f1c40f",
            ))
            fig_hist.add_trace(go.Bar(
                x=_hist_df["date"], y=_hist_df["insufficient"],
                name="Insufficient", marker_color="#e74c3c",
            ))
            fig_hist.update_layout(
                barmode="stack", height=180, margin=dict(t=20, b=20, l=20, r=20),
                legend=dict(orientation="h", yanchor="bottom", y=1.0, xanchor="right", x=1),
                title="일별 질문 응답 현황 (18문 중)",
            )
            st.plotly_chart(fig_hist, use_container_width=True)

        st.markdown("---")
        st.markdown(f"**{_sel_date} 질문별 상세 결과** — 클릭해서 펼치기")

        # Render all 18 questions grouped by category
        _groups = [
            ("트렌드·신호 탐지", ["Q01", "Q02", "Q03", "Q08", "Q09", "Q10"]),
            ("지정학·지리 분석", ["Q05", "Q06", "Q07"]),
            ("미디어·프레이밍 분석", ["Q04", "Q11", "Q12", "Q13", "Q14"]),
            ("엔티티·구조 분석", ["Q17", "Q18"]),
            ("인과·시계열 분석", ["Q15", "Q16"]),
        ]

        for group_name, qids in _groups:
            st.subheader(group_name)
            for qid in qids:
                if qid in _answers:
                    _render_question_card(qid, _answers[qid])
                else:
                    st.markdown(f"⚪ **{qid}** — 데이터 없음")
            st.markdown("")

        # ── Geopolitical Tension Index ────────────────────────────────────
        st.markdown("---")
        st.subheader("🌐 Geopolitical Tension Index (GTI)")
        st.caption(
            "Q05(국가 감성) · Q06(보도 집중도) · Q07(양국 긴장) 합성 지수. "
            "0–100 척도: LOW < 30 · MEDIUM 30–60 · HIGH 60–80 · CRITICAL > 80"
        )

        _gti_hist_path = DATA_DIR / "gti" / "gti_history.jsonl"

        @st.cache_data(ttl=300)
        def _load_gti_history(path_str: str) -> pd.DataFrame:
            rows = []
            try:
                with open(path_str, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                rows.append(json.loads(line))
                            except Exception:
                                pass
            except FileNotFoundError:
                pass
            if not rows:
                return pd.DataFrame()
            df = pd.DataFrame(rows)
            # dedup: keep last per date
            df = df.drop_duplicates(subset="date", keep="last")
            return df.sort_values("date")

        @st.cache_data(ttl=300)
        def _load_gti_daily(date_str: str, gti_dir_str: str) -> dict:
            p = Path(gti_dir_str) / date_str / "gti_daily.json"
            if p.exists():
                try:
                    return json.loads(p.read_text(encoding="utf-8"))
                except Exception:
                    pass
            return {}

        _gti_dir = DATA_DIR / "gti"
        _gti_hist = _load_gti_history(str(_gti_hist_path))
        _gti_today = _load_gti_daily(_sel_date, str(_gti_dir))

        if _gti_today:
            gc1, gc2, gc3, gc4 = st.columns(4)
            _gti_score = _gti_today.get("gti_score", 0)
            _gti_label = _gti_today.get("gti_label", "N/A")
            _gti_color = _gti_today.get("gti_color", "#aaa")
            gc1.metric("GTI 점수", f"{_gti_score:.1f}",
                       help="Geopolitical Tension Index 종합 점수 (0~100).\n\n"
                            "G1×0.40 + G2×0.35 + G3×0.25 가중 합산.")
            gc2.metric("등급", _gti_label,
                       help="LOW: 0~30 (낮은 긴장) · MEDIUM: 30~60 · HIGH: 60~80 · CRITICAL: 80~100")
            _comps = _gti_today.get("components", {})
            gc3.metric("G1 보도집중도", f"{_comps.get('g1_coverage_skew', 0):.1f}",
                       help="G1: 지역별 보도 집중도 편차 지수 (0~100).\n\n"
                            "특정 지역에 보도가 집중될수록 높아집니다. "
                            "지니 계수 + 상위 3개 지역 집중도 + 다크 코너 비율로 산출.")
            gc4.metric("G2 핫스팟", f"{_comps.get('g2_sentiment_hotspot', 0):.1f}",
                       help="G2: 지정학적 핫스팟 국가(미국·중국·러시아·이란 등 18개국) 보도 집중도 + 부정 감성 강도.\n\n"
                            "핫스팟 국가 기사 비율(60%) + 부정 감성 점수(40%) 합산.")

        if not _gti_hist.empty:
            _label_color = {"LOW": "#2ecc71", "MEDIUM": "#f1c40f", "HIGH": "#e67e22", "CRITICAL": "#e74c3c"}
            fig_gti = go.Figure()
            fig_gti.add_trace(go.Scatter(
                x=_gti_hist["date"], y=_gti_hist["gti_score"],
                mode="lines+markers",
                line=dict(color="#3498db", width=2),
                name="GTI",
            ))
            fig_gti.add_hrect(y0=0, y1=30, fillcolor="#2ecc71", opacity=0.1, line_width=0, annotation_text="LOW", annotation_position="left")
            fig_gti.add_hrect(y0=30, y1=60, fillcolor="#f1c40f", opacity=0.1, line_width=0, annotation_text="MEDIUM", annotation_position="left")
            fig_gti.add_hrect(y0=60, y1=80, fillcolor="#e67e22", opacity=0.1, line_width=0, annotation_text="HIGH", annotation_position="left")
            fig_gti.add_hrect(y0=80, y1=100, fillcolor="#e74c3c", opacity=0.1, line_width=0, annotation_text="CRITICAL", annotation_position="left")
            fig_gti.update_layout(
                height=300, margin=dict(t=20, b=30, l=60, r=20),
                yaxis=dict(range=[0, 100], title="GTI 점수"),
                title="GTI 시계열 추이",
            )
            st.plotly_chart(fig_gti, use_container_width=True)

        # ── Future Signal Portfolio ──────────────────────────────────────
        st.markdown("---")
        st.subheader("📡 Future Signal Portfolio")
        st.caption(
            "Q08(약한 신호 탐지) 결과를 날짜별로 누적 추적. "
            "watching(신규) → emerging(3일+) → confirmed(7일+). "
            "`data/signal_portfolio.yaml` 기반."
        )

        _port_path = DATA_DIR / "signal_portfolio.yaml"

        @st.cache_data(ttl=300)
        def _load_portfolio_cached(path_str: str) -> dict:
            from src.analysis.signal_portfolio import load_portfolio as _lp
            return _lp(Path(path_str))

        if not _port_path.exists():
            st.info(
                "포트폴리오 아직 없습니다. "
                "`python src/analysis/signal_portfolio.py` 를 실행하세요."
            )
        else:
            _port = _load_portfolio_cached(str(_port_path))
            _sigs = _port.get("signals", {})
            _status_counts = {s: 0 for s in ("watching", "emerging", "confirmed", "dismissed")}
            for _e in _sigs.values():
                _st = _e.get("status", "watching")
                if _st in _status_counts:
                    _status_counts[_st] += 1

            pc1, pc2, pc3, pc4 = st.columns(4)
            pc1.metric("🔵 Watching", _status_counts["watching"],
                       help="최초 탐지된 신호. 아직 반복 등장 여부 확인 중. 새로 포착된 모든 약한 신호가 여기서 시작됩니다.")
            pc2.metric("🟡 Emerging (3d+)", _status_counts["emerging"],
                       help="3일 이상 연속 등장한 신호. 단발성이 아닌 지속적 흐름으로 부상 중.")
            pc3.metric("🟢 Confirmed (7d+)", _status_counts["confirmed"],
                       help="7일 이상 지속된 신호. 약한 신호에서 실질적 트렌드로 전환 확인된 항목.")
            pc4.metric("⬛ Dismissed", _status_counts["dismissed"],
                       help="14일 이상 재등장하지 않아 소멸로 판정된 신호. 반짝 이슈였거나 순환적 노이즈.")
            st.caption(f"마지막 업데이트: {_port.get('last_updated', '알 수 없음')} | 총 {len(_sigs)}개 신호 추적")

            _active_tab, _confirmed_tab = st.tabs(["활성 신호", "Confirmed"])
            with _active_tab:
                _rows = []
                for _sl, _e in _sigs.items():
                    if _e.get("status") in ("dismissed",):
                        continue
                    _rows.append({
                        "상태": _e.get("status", ""),
                        "제목": _e.get("title", "")[:60],
                        "STEEPS": _e.get("steeps", ""),
                        "지역": _e.get("geo_focus", ""),
                        "추적일수": len(set(_e.get("seen_dates", []))),
                        "최근등장": _e.get("last_seen", ""),
                        "novelty": round(_e.get("novelty_score", 0), 2),
                    })
                if _rows:
                    _port_df = pd.DataFrame(_rows).sort_values(
                        ["추적일수", "novelty"], ascending=[False, False]
                    )
                    st.dataframe(_port_df, use_container_width=True, hide_index=True)
                else:
                    st.info("활성 신호 없음")

            with _confirmed_tab:
                _conf_rows = [
                    {
                        "제목": _e.get("title", "")[:70],
                        "STEEPS": _e.get("steeps", ""),
                        "지역": _e.get("geo_focus", ""),
                        "추적일수": len(set(_e.get("seen_dates", []))),
                        "최초탐지": _e.get("first_detected", ""),
                        "확정일": _e.get("confirmed_date", ""),
                    }
                    for _e in _sigs.values() if _e.get("status") == "confirmed"
                ]
                if _conf_rows:
                    st.dataframe(pd.DataFrame(_conf_rows), use_container_width=True, hide_index=True)
                else:
                    st.info("아직 confirmed 신호 없음 (7일 이상 지속 시 자동 승격)")

        # ── Weekly Future Map ─────────────────────────────────────────────
        st.markdown("---")
        st.subheader("🗺️ 주간 미래 맵")
        st.caption(
            "18문 + GTI + Signal Portfolio 종합 주간 보고서. "
            "`reports/weekly_future_map/{YYYY-Www}/future_map.md` 기반."
        )

        _wfm_root = DATA_DIR.parent / "reports" / "weekly_future_map"

        @st.cache_data(ttl=300)
        def _list_weekly_maps(dir_str: str) -> list[str]:
            d = Path(dir_str)
            if not d.exists():
                return []
            return sorted(
                [p.name for p in d.iterdir()
                 if p.is_dir() and not p.name.startswith("__")],
                reverse=True,
            )

        _wfm_editions = _list_weekly_maps(str(_wfm_root))
        if not _wfm_editions:
            st.info(
                "주간 미래 맵이 아직 없습니다. "
                "`python src/analysis/weekly_future_map.py --end-date YYYY-MM-DD` 실행."
            )
        else:
            _sel_week = st.selectbox("주차 선택", _wfm_editions, key="wfm_week_sel")
            _wfm_path = _wfm_root / _sel_week / "future_map.md"
            _wfm_meta_path = _wfm_root / _sel_week / "meta.json"

            if _wfm_meta_path.exists():
                _wfm_meta = json.loads(_wfm_meta_path.read_text(encoding="utf-8"))
                wm1, wm2, wm3, wm4 = st.columns(4)
                wm1.metric("주차", _wfm_meta.get("week_label", ""),
                           help="ISO 주차 표기 (YYYY-Www). 해당 주의 마지막 날(종료일) 기준으로 생성됩니다.")
                wm2.metric("데이터 커버리지", f"{_wfm_meta.get('dates_with_data',0)}/{_wfm_meta.get('window_days',7)}일",
                           help="해당 주 7일 중 실제 분석 데이터(articles_enriched.parquet)가 존재하는 날짜 수. "
                                "커버리지가 낮을수록 보고서의 신뢰도가 낮아집니다.")
                wm3.metric("GTI 평균", f"{_wfm_meta.get('gti_avg',0):.1f}",
                           help="해당 주의 일별 GTI 점수 평균. 주간 지정학적 긴장 수준을 나타냅니다.")
                wm4.metric("GTI 등급", _wfm_meta.get("gti_label", "").split()[0],
                           help="주간 평균 GTI 기반 등급.\n\nLOW(<30) · MEDIUM(30-60) · HIGH(60-80) · CRITICAL(>80)")

            if _wfm_path.exists():
                st.markdown(_wfm_path.read_text(encoding="utf-8"))
