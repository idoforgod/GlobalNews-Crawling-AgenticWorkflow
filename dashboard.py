"""GlobalNews Pipeline — Interactive Dashboard (Multi-Period).

Launch:
    streamlit run dashboard.py

Reads Parquet/JSONL/SQLite outputs produced by the 8-stage analysis pipeline.
Supports daily, monthly, quarterly, and yearly aggregation via sidebar controls.

Tabs: Overview, Topics, Sentiment & Emotions, Time Series, Word Cloud,
      Article Explorer.
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


# ---- Chart Interpretations loader + renderer (ADR-082) --------------------
# Loads data/analysis/{date}/interpretations.json and renders a standardized
# 3-layer card (해석 / 인사이트 / 미래통찰) above existing charts.


@st.cache_data(ttl=300)
def _load_interpretations(date: str) -> dict:
    """Cached loader for interpretations.json. Returns {} on miss."""
    p = DATA_DIR / "analysis" / date / "interpretations.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _render_interpretation_card(
    tab_id: str, interpretations: dict, *, default_expanded: bool = False,
) -> None:
    """Render the 3-layer card at the top of a tab.

    Contract: reads data only (no writes). Graceful when entry is missing,
    FAILED, or empty — surfaces a clear status message + regenerate hint.
    """
    tabs = (interpretations or {}).get("tabs") or {}
    entry = tabs.get(tab_id)

    # Missing: no interpretations generated yet for this tab
    if not entry:
        st.info(
            f"**{tab_id}** 탭의 해석이 아직 생성되지 않았습니다. "
            f"`python3 scripts/reports/generate_chart_interpretations.py "
            f"--date {(interpretations or {}).get('date', '{date}')} "
            f"--only {tab_id}` 또는 `/generate-chart-interpretations` 실행."
        )
        return

    status = entry.get("status")
    if status != "PASS":
        reason = entry.get("reason") or status or "unknown"
        st.warning(
            f"해석 생성 실패 · {status} · {reason}. "
            "🔁 버튼으로 재생성할 수 있습니다."
        )
        if st.button(f"🔁 {tab_id} 재생성", key=f"regen_{tab_id}"):
            import subprocess as _sub
            _log = (
                PROJECT_ROOT / "logs"
                / f"chart-interp-{tab_id}-{interpretations.get('date','run')}.log"
            )
            _log.parent.mkdir(parents=True, exist_ok=True)
            _proc = _sub.Popen([
                sys.executable,
                str(PROJECT_ROOT / "scripts" / "reports"
                    / "generate_chart_interpretations.py"),
                "--date", interpretations.get("date", ""),
                "--only", tab_id,
                "--project-dir", str(PROJECT_ROOT),
            ], stdout=_log.open("w"), stderr=_sub.STDOUT)
            st.toast(f"재생성 시작 (PID {_proc.pid}) — 페이지 새로고침 권장")
        return

    # PASS path — render 3-layer card
    with st.expander("📖 해석 · 인사이트 · 미래통찰",
                     expanded=default_expanded):
        c1, c2, c3 = st.columns([1.1, 1.5, 1.3])

        with c1:
            st.markdown("**🌱 해석**")
            interp_md = (entry.get("interpretation") or {}).get("md", "")
            if interp_md:
                st.markdown(interp_md)
            else:
                st.caption("_해석 미생성_")

        with c2:
            st.markdown("**💡 인사이트**")
            insight = entry.get("insight") or {}
            for b in insight.get("bullets", []):
                st.markdown(f"- {b}")
            refs = insight.get("cross_tab_refs") or []
            if refs:
                st.caption(
                    "교차 참조: " + ", ".join(r for r in refs if r)
                )

        with c3:
            st.markdown("**🔮 미래통찰**")
            future = entry.get("future") or {}
            for b in future.get("bullets", []):
                st.markdown(f"- {b}")
            src_refs = future.get("source_refs") or []
            for r in src_refs:
                label = r.get("type") or ""
                section = r.get("section") or r.get("item_index") or ""
                st.caption(f"→ {label}: {section}")

        meta = entry.get("metadata") or {}
        st.caption(
            f"Generated · attempts {meta.get('attempts', '?')} · "
            f"{meta.get('elapsed_seconds', '?')}s · "
            f"model {meta.get('model', '?')} · "
            f"template {interpretations.get('template_version', '?')}"
        )

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
    tab_summary, tab_overview, tab_topics, tab_sentiment, tab_timeseries,
    tab_wordcloud, tab_explorer, tab_w3_insight, tab_dci, tab_newspaper,
) = st.tabs([
    "📋 Run Summary",
    "📊 Overview",
    "🏷️ Topics",
    "😊 Sentiment & Emotions",
    "📈 Time Series",
    "☁️ Word Cloud",
    "🔍 Article Explorer",
    "🧠 W3 Insight Brief",
    "🔬 DCI (Independent Workflow)",
    "📰 Newspaper (WF5)",
])

# ========================= TAB 0: RUN SUMMARY (integrated) =================

with tab_summary:
    st.header("📋 Integrated Run Summary — W1 → W2 → W3 → W4 + DCI")
    st.caption(
        "Consolidated view of every workflow artifact produced for the "
        "selected date. Reflects the independent-DCI track alongside the "
        "W1→W2→W3→W4(Master) chain."
    )

    # ---- Date selector ----
    import json as _json
    from pathlib import Path as _Path
    from datetime import datetime as _dt

    _raw_dir = DATA_DIR / "raw"
    _available_dates = sorted(
        [p.name for p in _raw_dir.iterdir() if p.is_dir()],
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

        _dci_root = DATA_DIR / "dci" / "runs"
        _dci_runs_for_date = []
        if _dci_root.exists():
            for r in _dci_root.iterdir():
                if r.is_dir() and sel_date in r.name:
                    _dci_runs_for_date.append(r)
        _dci_run = _dci_runs_for_date[0] if _dci_runs_for_date else None
        _dci_ok = _dci_run is not None and (_dci_run / "final_report.md").exists()

        col1, col2, col3, col4, col5 = st.columns(5)
        col1.metric("W1 Crawling", "✅" if _w1_ok else "—", f"{_w1_articles:,} articles")
        col2.metric("W2 Analysis", "✅" if _w2_ok else "—",
                    f"{(_w2_analysis.stat().st_size // 1024) if _w2_analysis.exists() else 0:,} KB analysis")
        col3.metric("W3 Insight", "✅" if _w3_ok else "—",
                    _w3_run.name if _w3_run else "not run")
        col4.metric("W4 Master", "✅" if _w4_ok else "—",
                    "EN + KO" if _w4_ok else "not built")
        col5.metric("DCI", "✅" if _dci_ok else "—",
                    _dci_run.name if _dci_run else "not run")

        st.divider()

        # ==================== 1.5) PUBLIC NARRATIVE 3-LAYER ==============
        st.subheader("📖 일반인용 3-Layer 해석")
        st.caption(
            "해석(Interpretation) · 통찰(Insight) · 미래(Future) — 전문용어 없이, "
            "모든 숫자는 `facts_pool.json`에서 검증됨."
        )

        _public_dir = PROJECT_ROOT / "reports" / "public" / sel_date
        _pub_meta = _public_dir / "generation_metadata.json"

        _layers_def = [
            ("L1", "🌱 해석 (Interpretation)", "interpretation", "이게 무슨 뜻?"),
            ("L2", "💡 통찰 (Insight)",        "insight",        "무슨 패턴?"),
            ("L3", "🔮 미래 (Future Insight)", "future",         "앞으로는?"),
        ]

        if not _pub_meta.exists():
            st.info(
                "아직 공개 레이어가 생성되지 않았습니다.  \n"
                "생성: `/generate-public-layers` 또는 "
                f"`python3 .claude/hooks/scripts/generate_public_layers.py --date {sel_date}`"
            )
            if st.button("🔁 지금 생성 (background)", key="gen_public_now"):
                import subprocess as _sub
                _log_path = PROJECT_ROOT / "logs" / f"public-layers-{sel_date}.log"
                _log_path.parent.mkdir(parents=True, exist_ok=True)
                _proc = _sub.Popen(
                    ["python3",
                     str(PROJECT_ROOT / ".claude" / "hooks" / "scripts"
                         / "generate_public_layers.py"),
                     "--date", sel_date, "--project-dir", str(PROJECT_ROOT)],
                    stdout=_log_path.open("w"), stderr=_sub.STDOUT,
                )
                st.success(
                    f"생성 시작됨 (PID {_proc.pid}). "
                    f"로그: `{_log_path.relative_to(PROJECT_ROOT)}`  \n"
                    "페이지를 새로 고치면 진행 상황이 반영됩니다."
                )
        else:
            try:
                _meta = _json.loads(_pub_meta.read_text(encoding="utf-8"))
            except Exception as _exc:
                _meta = {"status": f"meta parse error: {_exc}", "layers": []}

            _status_label = _meta.get("status", "?")
            _status_icon = {
                "full_pass": "✅",
                "partial_pass_l3_failed": "⚠️",
                "failed": "❌",
            }.get(_status_label, "—")
            st.caption(
                f"생성 상태: {_status_icon} **{_status_label}** "
                f"· 소요 {_meta.get('total_elapsed_seconds', '?')}s "
                f"· model `{_meta.get('model', '?')}`"
            )

            _per_layer = {e["layer"]: e for e in _meta.get("layers", [])
                          if "layer" in e}

            # 3 cards
            _cards = st.columns(3)
            for (_lid, _title, _slug, _question), _col in zip(_layers_def, _cards):
                _card_entry = _per_layer.get(_lid, {})
                _card_status = _card_entry.get("status", "—")
                _md_path = _public_dir / f"{_slug}.md"
                _ko_path = _public_dir / f"{_slug}.ko.md"
                with _col:
                    st.markdown(f"**{_title}**")
                    st.caption(_question)
                    _color = {"PASS": "🟢", "FAIL": "🔴"}.get(_card_status, "⚪")
                    st.markdown(f"{_color} **{_card_status}**")
                    if _card_status == "PASS":
                        _size = _md_path.stat().st_size if _md_path.exists() else 0
                        st.caption(f"{_size:,} bytes · "
                                   f"{_card_entry.get('attempts', '?')}회 시도")
                        if _ko_path.exists():
                            st.caption("🇰🇷 한국어 번역 있음")

            st.markdown("---")

            # Full-text expanders (EN/KO toggle)
            _pub_lang = st.radio(
                "언어 / Language",
                options=["한국어", "English"],
                horizontal=True,
                key="pub_lang_toggle",
            )
            for _lid, _title, _slug, _question in _layers_def:
                _md_path = _public_dir / f"{_slug}.md"
                _ko_path = _public_dir / f"{_slug}.ko.md"
                _show_path = _ko_path if _pub_lang == "한국어" and _ko_path.exists() else _md_path
                if not _show_path.exists():
                    continue
                with st.expander(f"{_title} — 본문 ({_show_path.name})",
                                 expanded=(_lid == "L1")):
                    st.markdown(_show_path.read_text(encoding="utf-8"))

            # Regenerate button
            _cols_btn = st.columns([1, 1, 3])
            with _cols_btn[0]:
                if st.button("🔁 재생성", key="pub_regen"):
                    import subprocess as _sub
                    _log_path = PROJECT_ROOT / "logs" / f"public-layers-{sel_date}.log"
                    _proc = _sub.Popen(
                        ["python3",
                         str(PROJECT_ROOT / ".claude" / "hooks" / "scripts"
                             / "generate_public_layers.py"),
                         "--date", sel_date, "--project-dir", str(PROJECT_ROOT)],
                        stdout=_log_path.open("w"), stderr=_sub.STDOUT,
                    )
                    st.success(f"재생성 시작 (PID {_proc.pid})")
            with _cols_btn[1]:
                if st.button("📄 facts_pool 보기", key="pub_facts"):
                    _facts = _public_dir / "facts_pool.json"
                    if _facts.exists():
                        st.json(_json.loads(_facts.read_text(encoding="utf-8")))

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
                k1.metric("Articles analyzed", f"{len(_analysis_df):,}")
                k2.metric("Signals detected", f"{len(_signals_df):,}")
                _topics_unique = (
                    _topics_df["topic_id"].nunique()
                    if "topic_id" in _topics_df.columns else len(_topics_df)
                )
                k3.metric("Unique topics", f"{_topics_unique:,}")
                _sent_col = next(
                    (c for c in _analysis_df.columns
                     if "sentiment" in c.lower() and "score" in c.lower()),
                    None,
                )
                if _sent_col:
                    k4.metric(
                        "Mean sentiment",
                        f"{_analysis_df[_sent_col].mean():.3f}",
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

        # ==================== 5) DCI SUMMARY (if present) ====================
        if _dci_ok:
            st.subheader(f"🔬 DCI — {_dci_run.name}")
            _dci_report = _dci_run / "final_report.md"
            _dci_report_ko = _dci_run / "final_report.ko.md"
            _dci_verdict = _dci_run / "sg_superhuman_verdict.json"
            _dci_ledger = _dci_run / "evidence_ledger.jsonl"

            d1, d2, d3 = st.columns(3)
            if _dci_verdict.exists():
                try:
                    _v = _json.loads(_dci_verdict.read_text(encoding="utf-8"))
                    d1.metric("SG decision", _v.get("decision", "?"))
                    _gates = _v.get("gates", [])
                    _pass = sum(1 for g in _gates if g.get("status") == "pass")
                    d2.metric("Gates PASS", f"{_pass}/{len(_gates)}")
                except Exception:
                    d1.metric("SG decision", "parse error")
            if _dci_ledger.exists():
                try:
                    _n_markers = sum(
                        1 for _ in _dci_ledger.open("r", encoding="utf-8") if _.strip()
                    )
                    d3.metric("Evidence markers", f"{_n_markers:,}")
                except Exception:
                    d3.metric("Evidence markers", "?")

            if _dci_report.exists():
                with st.expander("📄 DCI final_report.md", expanded=False):
                    st.markdown(_dci_report.read_text(encoding="utf-8"))

        st.divider()

        # ==================== 6) ARTIFACT LIST ====================
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
        if _dci_run:
            _add("DCI", "final_report.md", _dci_run / "final_report.md")
            _add("DCI", "final_report.ko.md", _dci_run / "final_report.ko.md")
            _add("DCI", "evidence_ledger.jsonl",
                 _dci_run / "evidence_ledger.jsonl")
            _add("DCI", "sg_superhuman_verdict.json",
                 _dci_run / "sg_superhuman_verdict.json")
            _add("DCI", "executive_summary.md",
                 _dci_run / "executive_summary.md")

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
    # ADR-082: Chart Interpretations (Overview tab default-expanded)
    _interp_date = period if period and re.match(r"\d{4}-\d{2}-\d{2}", str(period)) else (
        max((p.name for p in (DATA_DIR / "analysis").iterdir()
             if p.is_dir() and re.match(r"\d{4}-\d{2}-\d{2}", p.name)),
            default="")
        if (DATA_DIR / "analysis").exists() else ""
    )
    if _interp_date:
        _render_interpretation_card(
            "overview", _load_interpretations(_interp_date),
            default_expanded=True,
        )
    st.header("Crawling & Pipeline Overview")

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
    if _interp_date:
        _render_interpretation_card(
            "topics", _load_interpretations(_interp_date),
            default_expanded=False,
        )
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
    if _interp_date:
        _render_interpretation_card(
            "sentiment", _load_interpretations(_interp_date),
            default_expanded=False,
        )
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


# ========================= TAB 4: TIME SERIES ===============================

with tab_timeseries:
    if _interp_date:
        _render_interpretation_card(
            "time_series", _load_interpretations(_interp_date),
            default_expanded=False,
        )
    st.header("Time Series Analysis")

    # ----- Auto-Insights -----
    st.subheader("🎯 Auto-Insights")
    di.render_insights(
        st, di.insight_timeseries_a(timeseries_df, list(active_dates)),
    )
    st.divider()
    st.subheader("📊 Supporting Charts")

    if timeseries_df is not None:
        col_f1, col_f2 = st.columns(2)

        metric_types = sorted(timeseries_df["metric_type"].unique())
        with col_f1:
            selected_metric = st.selectbox("Metric Type", metric_types, index=0)

        topic_ids = sorted(timeseries_df["topic_id"].unique())
        with col_f2:
            selected_topics = st.multiselect(
                "Topic IDs (leave empty for aggregate -1)",
                topic_ids,
                default=[-1] if -1 in topic_ids else topic_ids[:1],
            )

        if not selected_topics:
            selected_topics = [-1] if -1 in topic_ids else topic_ids[:1]

        mask = (
            (timeseries_df["metric_type"] == selected_metric) &
            (timeseries_df["topic_id"].isin(selected_topics))
        )
        ts_filtered = timeseries_df[mask].copy()

        if len(ts_filtered) > 0:
            ts_filtered["date"] = pd.to_datetime(ts_filtered["date"])
            ts_filtered = ts_filtered.sort_values("date")

            fig_ts = go.Figure()
            for tid in selected_topics:
                tid_data = ts_filtered[ts_filtered["topic_id"] == tid]
                fig_ts.add_trace(go.Scatter(
                    x=tid_data["date"],
                    y=tid_data["value"],
                    mode="lines",
                    name=f"Topic {tid} — Value",
                    opacity=0.6,
                ))
                if tid_data["trend"].notna().any():
                    fig_ts.add_trace(go.Scatter(
                        x=tid_data["date"],
                        y=tid_data["trend"],
                        mode="lines",
                        name=f"Topic {tid} — Trend",
                        line=dict(dash="dash", width=2),
                    ))

                burst_data = tid_data[tid_data["burst_score"].notna() & (tid_data["burst_score"] > 0)]
                if len(burst_data) > 0:
                    fig_ts.add_trace(go.Scatter(
                        x=burst_data["date"],
                        y=burst_data["value"],
                        mode="markers",
                        name=f"Topic {tid} — Bursts",
                        marker=dict(size=10, symbol="star", color="red"),
                    ))

            fig_ts.update_layout(
                title=f"Time Series: {selected_metric}",
                xaxis_title="Date",
                yaxis_title="Value",
                height=500,
            )
            st.plotly_chart(fig_ts, use_container_width=True)

            if ts_filtered["ma_short"].notna().any():
                st.subheader("Moving Average Crossover")
                fig_ma = go.Figure()
                for tid in selected_topics:
                    tid_data = ts_filtered[ts_filtered["topic_id"] == tid]
                    fig_ma.add_trace(go.Scatter(
                        x=tid_data["date"], y=tid_data["ma_short"],
                        name=f"Topic {tid} — MA Short (3d)",
                        line=dict(width=1),
                    ))
                    fig_ma.add_trace(go.Scatter(
                        x=tid_data["date"], y=tid_data["ma_long"],
                        name=f"Topic {tid} — MA Long (14d)",
                        line=dict(width=1, dash="dash"),
                    ))
                fig_ma.update_layout(height=400, title="Short vs Long Moving Average")
                st.plotly_chart(fig_ma, use_container_width=True)

            if ts_filtered["prophet_forecast"].notna().any():
                st.subheader("Prophet Forecast")
                for tid in selected_topics:
                    tid_data = ts_filtered[ts_filtered["topic_id"] == tid]
                    forecast_data = tid_data[tid_data["prophet_forecast"].notna()]
                    if len(forecast_data) > 0:
                        fig_prophet = go.Figure()
                        fig_prophet.add_trace(go.Scatter(
                            x=tid_data["date"], y=tid_data["value"],
                            name="Actual", line=dict(color="#3498db"),
                        ))
                        fig_prophet.add_trace(go.Scatter(
                            x=forecast_data["date"], y=forecast_data["prophet_forecast"],
                            name="Forecast", line=dict(color="#e74c3c", dash="dash"),
                        ))
                        if forecast_data["prophet_lower"].notna().any():
                            fig_prophet.add_trace(go.Scatter(
                                x=forecast_data["date"], y=forecast_data["prophet_upper"],
                                mode="lines", line=dict(width=0), showlegend=False,
                            ))
                            fig_prophet.add_trace(go.Scatter(
                                x=forecast_data["date"], y=forecast_data["prophet_lower"],
                                mode="lines", line=dict(width=0), showlegend=False,
                                fill="tonexty", fillcolor="rgba(231,76,60,0.15)",
                            ))
                        fig_prophet.update_layout(
                            title=f"Prophet Forecast — Topic {tid}",
                            height=400,
                        )
                        st.plotly_chart(fig_prophet, use_container_width=True)
        else:
            st.info("No data for the selected filters.")

        st.subheader("Time Series Statistics")
        ts_stats = {
            "Total Series": timeseries_df["series_id"].nunique(),
            "Date Range": f"{timeseries_df['date'].min()} -> {timeseries_df['date'].max()}",
            "Data Points": format_number(len(timeseries_df)),
            "Burst Events": int((timeseries_df["burst_score"].notna() & (timeseries_df["burst_score"] > 0)).sum()),
            "Changepoints": int(timeseries_df["is_changepoint"].sum()),
        }
        for k, v in ts_stats.items():
            st.text(f"  {k}: {v}")
    else:
        st.warning("Time series data not available.")


# ========================= TAB 5: WORD CLOUD ================================

with tab_wordcloud:
    if _interp_date:
        _render_interpretation_card(
            "word_cloud", _load_interpretations(_interp_date),
            default_expanded=False,
        )
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


# ========================= TAB 6: ARTICLE EXPLORER =========================

with tab_explorer:
    st.header("Article Explorer")

    if merged_df is not None:
        col_e1, col_e2, col_e3 = st.columns(3)

        with col_e1:
            sources = ["All"] + sorted(merged_df["source"].dropna().unique().tolist())
            selected_source = st.selectbox("Source", sources)

        with col_e2:
            languages = ["All"] + sorted(merged_df["language"].dropna().unique().tolist())
            selected_lang = st.selectbox("Language", languages)

        with col_e3:
            search_query = st.text_input("Search in title", "")

        filtered = merged_df.copy()
        if selected_source != "All":
            filtered = filtered[filtered["source"] == selected_source]
        if selected_lang != "All":
            filtered = filtered[filtered["language"] == selected_lang]
        if search_query:
            filtered = filtered[
                filtered["title"].str.contains(search_query, case=False, na=False)
            ]

        st.caption(f"Showing {len(filtered)} of {len(merged_df)} articles")

        sort_col = st.selectbox(
            "Sort by",
            ["published_at", "importance_score", "sentiment_score", "topic_probability"],
            index=0,
        )
        sort_asc = st.checkbox("Ascending", value=False)

        if sort_col in filtered.columns:
            filtered = filtered.sort_values(sort_col, ascending=sort_asc, na_position="last")

        display_cols = [
            "title", "source", "language", "published_at",
            "sentiment_label", "sentiment_score",
            "topic_id", "topic_label",
            "steeps_category", "importance_score",
        ]
        display_cols = [c for c in display_cols if c in filtered.columns]

        st.dataframe(
            filtered[display_cols].head(100),
            use_container_width=True,
            hide_index=True,
            height=500,
        )

        st.subheader("Article Detail")
        if len(filtered) > 0:
            article_titles = filtered["title"].head(50).tolist()
            selected_title = st.selectbox("Select an article", article_titles)
            row = filtered[filtered["title"] == selected_title].iloc[0]

            col_d1, col_d2 = st.columns([2, 1])
            with col_d1:
                st.markdown(f"**{row['title']}**")
                st.caption(f"Source: {row.get('source', 'N/A')} | "
                           f"Language: {row.get('language', 'N/A')} | "
                           f"Published: {row.get('published_at', 'N/A')}")
                body = row.get("body", "")
                if isinstance(body, str) and body:
                    st.text_area("Body", body[:3000], height=300, disabled=True)

            with col_d2:
                st.markdown("**Analysis**")
                for field in ["sentiment_label", "sentiment_score", "steeps_category",
                              "importance_score", "topic_id", "topic_label", "topic_probability"]:
                    if field in row.index and pd.notna(row[field]):
                        label = field.replace("_", " ").title()
                        st.text(f"{label}: {row[field]}")

                emotion_cols = [c for c in row.index if c.startswith("emotion_")]
                if emotion_cols:
                    st.markdown("**Emotions**")
                    emo_data = {c.replace("emotion_", "").title(): row[c]
                                for c in emotion_cols if pd.notna(row[c])}
                    if emo_data:
                        fig_emo = px.bar(
                            x=list(emo_data.keys()),
                            y=list(emo_data.values()),
                            labels={"x": "", "y": "Score"},
                            height=250,
                        )
                        fig_emo.update_layout(margin=dict(t=10, b=10))
                        st.plotly_chart(fig_emo, use_container_width=True)

    elif articles_df is not None:
        st.dataframe(articles_df.head(100), use_container_width=True, hide_index=True)
    else:
        st.warning("Article data not available.")


# ========================= TAB 7: W3 INSIGHT BRIEF =========================

with tab_w3_insight:
    if _interp_date:
        _render_interpretation_card(
            "w3_insight", _load_interpretations(_interp_date),
            default_expanded=False,
        )
    st.header("🧠 W3 Insight Brief — Cross-Module Synthesis")

    insights_root = DATA_DIR / "insights"
    # Auto-discover the most recent run directory (monthly/weekly/quarterly)
    run_dirs = [
        p for p in insights_root.iterdir()
        if p.is_dir() and p.name.startswith(("monthly-", "weekly-", "quarterly-"))
    ] if insights_root.exists() else []
    run_dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    if not run_dirs:
        st.warning(
            "No W3 insight runs found. Run `python main.py --mode insight "
            "--window 30` to generate."
        )
    else:
        default_idx = 0
        selected = st.selectbox(
            "Select insight run",
            options=[p.name for p in run_dirs],
            index=default_idx,
        )
        run_dir = insights_root / selected
        synthesis_dir = run_dir / "synthesis"

        # --- Header: state metadata ---
        state_path = insights_root / "insight_state.json"
        if state_path.exists():
            state = json.loads(state_path.read_text())
            cov = state.get("data_coverage", {})
            col1, col2, col3, col4 = st.columns(4)
            col1.metric(
                "Data coverage",
                f"{cov.get('available_days', 0)} / {cov.get('window_days', 0)} days",
                f"{cov.get('coverage_ratio', 0)*100:.0f}%",
            )
            col2.metric(
                "Modules completed",
                f"{len(state.get('modules_completed', []))} / 7",
            )
            col3.metric(
                "Validation",
                "PASS" if state.get("validation_passed") else "FAIL",
            )
            col4.metric(
                "Total elapsed",
                f"{state.get('total_elapsed_seconds', 0):.1f}s",
            )

        st.divider()

        # --- Executive Summary + full report (collapsible) ---
        report_path = synthesis_dir / "insight_report.md"
        if report_path.exists():
            report_md = report_path.read_text(encoding="utf-8")
            # Extract Executive Summary section for primary display
            lines = report_md.splitlines()
            exec_section: list[str] = []
            capturing = False
            for ln in lines:
                if ln.startswith("## Executive Summary"):
                    capturing = True
                    continue
                if capturing and ln.startswith("## "):
                    break
                if capturing:
                    exec_section.append(ln)
            if exec_section:
                st.subheader("Executive Summary")
                st.markdown("\n".join(exec_section).strip())

            with st.expander("📄 Full Insight Report (Markdown)", expanded=False):
                st.markdown(report_md)
        else:
            st.warning(f"insight_report.md not found at {report_path}")

        st.divider()

        # --- Key Findings from insight_data.json ---
        data_path = synthesis_dir / "insight_data.json"
        if data_path.exists():
            insight_data = json.loads(data_path.read_text())
            st.subheader(f"🎯 Top Findings — {insight_data.get('total_findings', 0)} total")

            top_findings = insight_data.get("top_findings", [])
            if top_findings:
                top_df = pd.DataFrame([
                    {
                        "Module": f.get("module", ""),
                        "Metric": f.get("metric", ""),
                        "Finding": f.get("description", ""),
                        "Magnitude": round(f.get("magnitude", 0), 3),
                    }
                    for f in top_findings
                ])
                st.dataframe(top_df, use_container_width=True, hide_index=True)

            # Per-module breakdown chart
            modules = insight_data.get("modules_available", [])
            if modules:
                mod_counts = {}
                kf_path = synthesis_dir / "key_findings.json"
                if kf_path.exists():
                    kf = json.loads(kf_path.read_text())
                    for f in kf.get("top_5", []):
                        m = f.get("module", "unknown")
                        mod_counts[m] = mod_counts.get(m, 0) + 1

        # --- Per-module deep dive ---
        st.divider()
        st.subheader("🔬 Per-Module Deep Dive")

        module_files = {
            "crosslingual": [
                ("asymmetry_index.parquet", "JSD Asymmetry (per language pair × date)"),
                ("filter_bubble.parquet", "Filter Bubble (Jaccard overlap)"),
                ("attention_gaps.parquet", "Attention Gaps (per topic)"),
            ],
            "narrative": [
                ("voice_dominance.parquet", "Voice Dominance (HHI per topic)"),
                ("media_health.parquet", "Media Health Score"),
                ("frame_evolution.parquet", "Frame Evolution (STEEPS over time)"),
            ],
            "entity": [
                ("trajectories.parquet", "Entity Trajectories"),
                ("hidden_connections.parquet", "Hidden Entity Connections"),
            ],
            "temporal": [
                ("velocity_map.parquet", "Cross-Lingual Velocity Map"),
                ("decay_curves.parquet", "Topic Decay Curves"),
            ],
            "geopolitical": [
                ("bilateral_index.parquet", "Bilateral Relations Index (BRI)"),
                ("soft_power.parquet", "Soft Power Scores"),
            ],
            "economic": [
                ("epu_index.parquet", "Economic Policy Uncertainty"),
                ("sector_sentiment.parquet", "Sector Sentiment"),
                ("narrative_economics.parquet", "Narrative Economics"),
            ],
        }

        selected_mod = st.selectbox(
            "Select module to explore",
            options=list(module_files.keys()),
        )

        mod_dir = run_dir / selected_mod
        if mod_dir.exists():
            for fn, label in module_files[selected_mod]:
                fpath = mod_dir / fn
                if fpath.exists():
                    try:
                        df = pd.read_parquet(fpath)
                        with st.expander(
                            f"{label} — {len(df)} rows", expanded=False,
                        ):
                            if len(df) > 0:
                                st.dataframe(
                                    df.head(100),
                                    use_container_width=True,
                                    hide_index=True,
                                )

                                # Simple visualization for numeric-heavy tables
                                numeric_cols = df.select_dtypes(
                                    include=["number"]
                                ).columns.tolist()
                                if len(numeric_cols) >= 1 and len(df) <= 5000:
                                    try:
                                        primary = numeric_cols[0]
                                        fig = px.histogram(
                                            df,
                                            x=primary,
                                            nbins=30,
                                            title=f"Distribution: {primary}",
                                            height=300,
                                        )
                                        fig.update_layout(margin=dict(t=30, b=20))
                                        st.plotly_chart(
                                            fig, use_container_width=True,
                                        )
                                    except Exception:
                                        pass
                            else:
                                st.info(f"{label}: empty (no qualifying data)")
                    except Exception as e:
                        st.error(f"{label}: read error — {e}")
        else:
            st.warning(f"Module directory {selected_mod} not found.")


# ========================= TAB 8: DCI (Independent Workflow) ================

with tab_dci:
    st.header("🔬 Deep Content Intelligence — Independent Workflow")
    st.caption(
        "14-layer analytic pipeline with char_coverage=1.00 guarantee. "
        "Run via `python main.py --mode dci --date YYYY-MM-DD`."
    )

    # Layer status from technique_registry
    try:
        from src.config.constants import (
            DCI_LAYERS,
            DCI_TECHNIQUES_TOTAL,
            DCI_TECHNIQUES_P_MODE,
            DCI_TECHNIQUES_H_MODE,
            DCI_TECHNIQUES_L_MODE,
        )
        from src.dci.orchestrator import (
            ensure_layers_registered,
            registered_layer_ids,
        )
        ensure_layers_registered()
        registered = set(registered_layer_ids())
    except Exception as exc:
        st.error(f"DCI package import failed: {exc}")
        DCI_LAYERS = ()
        registered = set()
        DCI_TECHNIQUES_TOTAL = 0
        DCI_TECHNIQUES_P_MODE = 0
        DCI_TECHNIQUES_H_MODE = 0
        DCI_TECHNIQUES_L_MODE = 0

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Layers wired", f"{len(registered)} / {len(DCI_LAYERS)}")
    col2.metric("Techniques total", DCI_TECHNIQUES_TOTAL)
    col3.metric("P-mode (Pure Python)", DCI_TECHNIQUES_P_MODE)
    col4.metric("H/L-mode (LLM-bound)",
                DCI_TECHNIQUES_H_MODE + DCI_TECHNIQUES_L_MODE)

    st.divider()
    st.subheader("14-Layer Architecture")
    if DCI_LAYERS:
        rows = []
        for layer_id in DCI_LAYERS:
            rows.append({
                "Layer": layer_id,
                "Wired": "✓" if layer_id in registered else "—",
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    st.divider()
    st.subheader("Latest Run Output")

    dci_run_dir = DATA_DIR / "dci" / "runs"
    if dci_run_dir.exists():
        runs = sorted(
            [p.name for p in dci_run_dir.iterdir() if p.is_dir()],
            reverse=True,
        )
    else:
        runs = []

    if not runs:
        st.info(
            "No DCI runs found yet. Execute "
            "`python main.py --mode dci --date YYYY-MM-DD` "
            "to generate one. Use `--dry-run` for a zero-cost smoke run."
        )
    else:
        selected_run = st.selectbox(
            "Select run date", options=runs, index=0,
        )
        run_path = dci_run_dir / selected_run
        report_path = run_path / "final_report.md"
        if report_path.exists():
            st.markdown(report_path.read_text(encoding="utf-8"))
        else:
            st.warning(f"No final_report.md at {report_path}")

        kg_path = run_path / "kg.gexf"
        if kg_path.exists():
            st.divider()
            st.subheader("🕸️ Knowledge Graph (3D force layout)")
            try:
                import networkx as nx
                g = nx.read_gexf(kg_path)
                if g.number_of_nodes() == 0:
                    st.info("Knowledge graph is empty for this run.")
                elif g.number_of_nodes() > 500:
                    st.warning(
                        f"KG has {g.number_of_nodes()} nodes — "
                        f"rendering top-100 by degree for responsiveness."
                    )
                    # Trim to top-100 nodes by degree
                    degs = sorted(g.degree(weight="weight"), key=lambda x: -x[1])
                    keep = {n for n, _ in degs[:100]}
                    g = g.subgraph(keep).copy()

                if g.number_of_nodes() > 0:
                    layout = nx.spring_layout(
                        g, dim=3, seed=42, k=0.5,
                        iterations=50,
                    )
                    edge_x, edge_y, edge_z = [], [], []
                    for u, v in g.edges():
                        xa, ya, za = layout[u]
                        xb, yb, zb = layout[v]
                        edge_x.extend([xa, xb, None])
                        edge_y.extend([ya, yb, None])
                        edge_z.extend([za, zb, None])
                    node_x = [layout[n][0] for n in g.nodes()]
                    node_y = [layout[n][1] for n in g.nodes()]
                    node_z = [layout[n][2] for n in g.nodes()]
                    node_text = [
                        f"{n}<br>articles={g.nodes[n].get('article_count', 0)}"
                        for n in g.nodes()
                    ]
                    node_size = [
                        8 + 2 * g.degree(n, weight="weight")
                        for n in g.nodes()
                    ]
                    fig = go.Figure()
                    fig.add_trace(go.Scatter3d(
                        x=edge_x, y=edge_y, z=edge_z,
                        mode="lines",
                        line=dict(width=1, color="rgba(125,125,125,0.3)"),
                        hoverinfo="none",
                        showlegend=False,
                    ))
                    fig.add_trace(go.Scatter3d(
                        x=node_x, y=node_y, z=node_z,
                        mode="markers",
                        marker=dict(
                            size=node_size,
                            color=node_size,
                            colorscale="Viridis",
                            opacity=0.85,
                        ),
                        text=node_text,
                        hoverinfo="text",
                        showlegend=False,
                    ))
                    fig.update_layout(
                        scene=dict(
                            xaxis=dict(visible=False),
                            yaxis=dict(visible=False),
                            zaxis=dict(visible=False),
                        ),
                        height=600,
                        margin=dict(l=0, r=0, t=20, b=0),
                        hovermode="closest",
                    )
                    st.plotly_chart(fig, use_container_width=True)
                    st.caption(
                        f"{g.number_of_nodes()} nodes · "
                        f"{g.number_of_edges()} edges · "
                        f"spring_layout seed=42"
                    )
            except Exception as exc:
                st.warning(f"Could not render KG: {exc}")

    st.divider()
    st.subheader("SG-Superhuman Thresholds")
    try:
        from src.config.constants import (
            DCI_SG_CHAR_COVERAGE_MIN,
            DCI_SG_TRIPLE_LENS_COVERAGE_MIN,
            DCI_SG_LLM_BODY_INJECTION_RATIO_MIN,
            DCI_SG_NLI_VERIFICATION_PASS_RATE_MIN,
            DCI_SG_TRIADIC_CONSENSUS_RATE_MIN,
            DCI_SG_ADVERSARIAL_CRITIC_PASS_MIN,
            DCI_SG_MULTILINGUAL_COVERAGE_MIN,
        )
        st.dataframe(pd.DataFrame([
            {"Gate": "char_coverage",           "Threshold": DCI_SG_CHAR_COVERAGE_MIN},
            {"Gate": "triple_lens_coverage",    "Threshold": DCI_SG_TRIPLE_LENS_COVERAGE_MIN},
            {"Gate": "llm_body_injection",      "Threshold": DCI_SG_LLM_BODY_INJECTION_RATIO_MIN},
            {"Gate": "nli_pass_rate",           "Threshold": DCI_SG_NLI_VERIFICATION_PASS_RATE_MIN},
            {"Gate": "triadic_consensus",       "Threshold": DCI_SG_TRIADIC_CONSENSUS_RATE_MIN},
            {"Gate": "adversarial_critic_pass", "Threshold": DCI_SG_ADVERSARIAL_CRITIC_PASS_MIN},
            {"Gate": "multilingual_coverage",   "Threshold": DCI_SG_MULTILINGUAL_COVERAGE_MIN},
        ]), use_container_width=True, hide_index=True)
    except ImportError:
        st.warning("DCI constants unavailable")


# ========================= TAB 9: Newspaper (WF5) ============================

with tab_newspaper:
    st.header("📰 Personal Newspaper — The Global Ledger")
    st.caption(
        "WF5 Independent Workflow (ADR-083). 135,000-word daily + "
        "205,000-word weekly. 17 editorial agents · 15 principles."
    )

    _np_root = DATA_DIR.parent / "newspaper"
    _daily_root = _np_root / "daily"
    _weekly_root = _np_root / "weekly"

    _daily_editions = sorted(
        [p.name for p in _daily_root.iterdir() if p.is_dir()],
        reverse=True,
    ) if _daily_root.exists() else []
    _weekly_editions = sorted(
        [p.name for p in _weekly_root.iterdir() if p.is_dir()],
        reverse=True,
    ) if _weekly_root.exists() else []

    np_tab_daily, np_tab_weekly = st.tabs(["📅 Daily", "🗓️ Weekly"])

    with np_tab_daily:
        if not _daily_editions:
            st.info(
                "No daily editions yet. Run "
                "`python3 scripts/reports/generate_newspaper_daily.py "
                "--date YYYY-MM-DD` or `/run-newspaper-only`."
            )
        else:
            sel_np_date = st.selectbox(
                "Select daily edition", _daily_editions,
                key="np_daily_sel",
            )
            _ed = _daily_root / sel_np_date
            _meta = _ed / "newspaper_metadata.json"
            if _meta.exists():
                try:
                    _m = json.loads(_meta.read_text(encoding="utf-8"))
                    _stats = _m.get("stats") or {}
                    c1, c2, c3, c4, c5 = st.columns(5)
                    c1.metric("기사", f"{_stats.get('articles_total', 0):,}")
                    c2.metric("클러스터", f"{_stats.get('clusters_total', 0):,}")
                    c3.metric("3각검증", f"{_stats.get('clusters_triangulated', 0):,}")
                    c4.metric("Dark Corners", f"{_stats.get('countries_dark', 0):,}")
                    c5.metric("증거 앵커", f"{_stats.get('evidence_markers', 0):,}")
                    st.caption(
                        f"Generated {_m.get('generated_at', '')} · "
                        f"elapsed {_m.get('total_elapsed_seconds', 0)}s · "
                        f"template {_m.get('template_version', '?')}"
                    )
                except Exception as _exc:
                    st.warning(f"metadata parse: {_exc}")
            # iframe the actual HTML
            _idx = _ed / "index.html"
            if _idx.exists():
                try:
                    html = _idx.read_text(encoding="utf-8")
                    # Inline asset fetch: browser can't access filesystem,
                    # so inline CSS so iframe renders correctly.
                    _css = _ed / "assets" / "style.css"
                    if _css.exists():
                        html = html.replace(
                            '<link rel="stylesheet" href="assets/style.css">',
                            f"<style>{_css.read_text(encoding='utf-8')}</style>",
                        )
                    st.components.v1.html(html, height=900, scrolling=True)
                except Exception as _exc:
                    st.error(f"render failed: {_exc}")
            else:
                st.warning("index.html missing — run the orchestrator first.")

    with np_tab_weekly:
        if not _weekly_editions:
            st.info(
                "No weekly editions yet. Requires ≥ 4 daily editions in the "
                "ISO week, then run `/run-newspaper-weekly --week YYYY-W##`."
            )
        else:
            sel_week = st.selectbox(
                "Select weekly edition", _weekly_editions,
                key="np_weekly_sel",
            )
            _wed = _weekly_root / sel_week
            _idx = _wed / "index.html"
            if _idx.exists():
                html = _idx.read_text(encoding="utf-8")
                _css = _wed / "assets" / "style.css"
                if _css.exists():
                    html = html.replace(
                        '<link rel="stylesheet" href="assets/style.css">',
                        f"<style>{_css.read_text(encoding='utf-8')}</style>",
                    )
                st.components.v1.html(html, height=900, scrolling=True)


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
