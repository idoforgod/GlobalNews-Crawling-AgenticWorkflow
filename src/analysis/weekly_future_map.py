"""주간 미래 맵 자동 생성 — 18문 + GTI + Signal Portfolio 종합.

7일치 18-Question 결과 + GTI 히스토리 + Signal Portfolio를
Claude CLI 없이 순수 Python으로 종합 Markdown 보고서를 생성한다.

출력:
  reports/weekly_future_map/YYYY-W{WW}/future_map.md
  reports/weekly_future_map/YYYY-W{WW}/future_map_ko.md
  reports/weekly_future_map/YYYY-W{WW}/meta.json

사용법:
  python src/analysis/weekly_future_map.py
  python src/analysis/weekly_future_map.py --week 2026-W16
  python src/analysis/weekly_future_map.py --end-date 2026-04-25
"""

from __future__ import annotations

import json
import logging
import math
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# 헬퍼
# ─────────────────────────────────────────────────────────────

def _load_json(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _safe(v, default=0.0):
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _week_label(end_date: datetime) -> str:
    """ISO 주차 레이블 (YYYY-Www)."""
    iso = end_date.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


_STEEPS_NAME = {
    "SOC": "Social", "TEC": "Technology", "ECO": "Economy",
    "ENV": "Environment", "POL": "Politics", "SEC": "Security",
    "SPI": "Spiritual", "CRS": "Crisis",
}

_GTI_EMOJI = {"LOW": "🟢", "MEDIUM": "🟡", "HIGH": "🟠", "CRITICAL": "🔴"}


# ─────────────────────────────────────────────────────────────
# 주간 데이터 수집
# ─────────────────────────────────────────────────────────────

def collect_weekly_data(
    end_date: datetime,
    project_root: Path,
    window_days: int = 7,
) -> dict:
    """end_date 기준 최근 window_days일치 데이터 수집."""
    root = project_root
    answers_base = root / "data" / "answers"
    gti_base = root / "data" / "gti"
    port_path = root / "data" / "signal_portfolio.yaml"

    # 날짜 범위
    date_range = [
        (end_date - timedelta(days=i)).strftime("%Y-%m-%d")
        for i in range(window_days)
    ]
    date_range.reverse()

    # 각 날짜별 Q01~Q18 + GTI 수집
    daily: list[dict] = []
    for date_str in date_range:
        d = {"date": date_str}
        # Questions
        for i in range(1, 19):
            p = answers_base / date_str / f"q{i:02d}.json"
            d[f"q{i:02d}"] = _load_json(p)
        # GTI
        d["gti"] = _load_json(gti_base / date_str / "gti_daily.json")
        daily.append(d)

    # Signal portfolio
    try:
        import yaml
        port_raw = yaml.safe_load(port_path.read_text(encoding="utf-8")) if port_path.exists() else {}
    except Exception:
        port_raw = {}

    return {
        "date_range": date_range,
        "end_date": end_date.strftime("%Y-%m-%d"),
        "daily": daily,
        "portfolio": port_raw,
    }


# ─────────────────────────────────────────────────────────────
# 섹션 생성기
# ─────────────────────────────────────────────────────────────

def _section_gti_summary(daily: list[dict]) -> str:
    """GTI 주간 요약 섹션."""
    scores = [_safe(d["gti"].get("gti_score")) for d in daily if d.get("gti")]
    if not scores:
        return "### 🌐 GTI 주간 요약\n\n데이터 없음.\n"

    avg = statistics.mean(scores)
    trend = scores[-1] - scores[0] if len(scores) >= 2 else 0
    peak = max(scores)
    peak_date = daily[[_safe(d["gti"].get("gti_score")) for d in daily].index(peak)]["date"] if scores else ""

    trend_desc = "상승" if trend > 3 else "하락" if trend < -3 else "횡보"
    trend_arrow = "↑" if trend > 3 else "↓" if trend < -3 else "→"

    lines = [
        "### 🌐 Geopolitical Tension Index — 주간 요약",
        "",
        f"- **주간 평균 GTI**: {avg:.1f} ({_gti_label_str(avg)})",
        f"- **추세**: {trend_arrow} {trend_desc} ({scores[0]:.1f} → {scores[-1]:.1f})",
        f"- **주중 최고**: {peak:.1f} ({peak_date})",
        "",
    ]
    return "\n".join(lines) + "\n"


def _gti_label_str(score: float) -> str:
    if score < 30:
        return "LOW 🟢"
    elif score < 60:
        return "MEDIUM 🟡"
    elif score < 80:
        return "HIGH 🟠"
    else:
        return "CRITICAL 🔴"


def _section_burst_trends(daily: list[dict]) -> str:
    """Q01 버스트 집계 섹션."""
    steeps_burst: dict[str, list[float]] = defaultdict(list)
    for d in daily:
        q01 = d.get("q01", {})
        for item in (q01.get("answer") or {}).get("bursting_topics", []):
            steeps_burst[item["steeps"]].append(_safe(item.get("burst_score", 0)))

    if not steeps_burst:
        return "### 🔥 주간 버스트 신호\n\n데이터 없음.\n"

    lines = ["### 🔥 주간 버스트 신호 (Q01)", ""]
    for steeps, scores in sorted(steeps_burst.items(), key=lambda x: -statistics.mean(x[1])):
        avg_burst = statistics.mean(scores)
        name = _STEEPS_NAME.get(steeps, steeps)
        lines.append(f"- **{steeps} {name}**: 주간 평균 버스트 {avg_burst:.2f}x")
    lines.append("")
    return "\n".join(lines) + "\n"


def _section_trend_shifts(daily: list[dict]) -> str:
    """Q02 트렌드 상승/하락 섹션."""
    rising_counter: Counter = Counter()
    falling_counter: Counter = Counter()
    for d in daily:
        q02 = d.get("q02", {})
        ans = q02.get("answer") or {}
        for item in ans.get("rising", []):
            rising_counter[item.get("steeps", "")] += 1
        for item in ans.get("falling", []):
            falling_counter[item.get("steeps", "")] += 1

    lines = ["### 📈 주간 트렌드 흐름 (Q02)", ""]
    if rising_counter:
        lines.append("**상승 트렌드 (일 횟수 기준):**")
        for steeps, cnt in rising_counter.most_common(5):
            lines.append(f"  - {steeps} {_STEEPS_NAME.get(steeps,'')}: {cnt}일")
    if falling_counter:
        lines.append("")
        lines.append("**하락 트렌드:**")
        for steeps, cnt in falling_counter.most_common(5):
            lines.append(f"  - {steeps} {_STEEPS_NAME.get(steeps,'')}: {cnt}일")
    lines.append("")
    return "\n".join(lines) + "\n"


def _section_weak_signals(daily: list[dict], portfolio: dict) -> str:
    """Q08 약한 신호 + 포트폴리오 섹션."""
    all_signals: list[dict] = []
    for d in daily:
        q08 = d.get("q08", {})
        sigs = (q08.get("answer") or {}).get("weak_signals", [])
        for s in sigs:
            all_signals.append({**s, "date": d["date"]})

    lines = ["### 📡 주간 약한 신호 — 미래 전조 (Q08)", ""]

    # Top novel signals
    top = sorted(all_signals, key=lambda x: -_safe(x.get("novelty_score")))[:8]
    for s in top:
        steeps = s.get("steeps", "")
        title = s.get("title", "")[:70]
        novelty = _safe(s.get("novelty_score"))
        geo = s.get("geo_focus_primary", "")
        lines.append(f"- `{steeps}` **{title}** (novelty={novelty:.2f}{'| ' + geo if geo and geo != 'UNKNOWN' else ''})")

    # Emerging signals from portfolio
    port_sigs = portfolio.get("signals", {})
    emerging = [
        v for v in port_sigs.values()
        if v.get("status") in ("emerging", "confirmed")
    ]
    if emerging:
        lines.append("")
        lines.append("**포트폴리오: 부상 중인 신호 (3일+ 지속):**")
        for e in sorted(emerging, key=lambda x: -len(x.get("seen_dates", [])))[:5]:
            days = len(set(e.get("seen_dates", [])))
            status = e.get("status", "")
            lines.append(f"  - [{status.upper()}] {e.get('title','')[:60]} ({days}일 추적)")

    lines.append("")
    return "\n".join(lines) + "\n"


def _section_paradigm_shifts(daily: list[dict]) -> str:
    """Q09 패러다임 전환 전조 섹션."""
    cross_domain_total = 0
    multilang_total = 0
    for d in daily:
        q09 = d.get("q09", {})
        ans = q09.get("answer") or {}
        cross_domain_total += len(ans.get("cross_domain_articles", []))
        multilang_total += len(ans.get("multi_language_geo", []))

    lines = ["### 🌀 패러다임 전환 전조 (Q09)", ""]
    lines.append(f"- **교차도메인 기사**: 주간 총 {cross_domain_total}건")
    lines.append(f"- **다국어 동시 보도 이슈**: 주간 총 {multilang_total}건")
    lines.append("")
    return "\n".join(lines) + "\n"


def _section_agenda_landscape(daily: list[dict]) -> str:
    """Q11 의제 선점 + Q12 미디어 편향 섹션."""
    setter_counter: Counter = Counter()
    for d in daily:
        q11 = d.get("q11", {})
        for item in (q11.get("answer") or {}).get("agenda_setters", [])[:3]:
            setter_counter[item.get("source_id", "")] += 1

    lines = ["### 🏁 주간 의제 지형 (Q11·Q12)", ""]
    if setter_counter:
        lines.append("**의제 선점 언론사 (이번 주 상위):**")
        for src, cnt in setter_counter.most_common(8):
            lines.append(f"  - {src}: {cnt}일 등장")
    lines.append("")
    return "\n".join(lines) + "\n"


def _section_dark_corners(daily: list[dict]) -> str:
    """Q06 다크 코너 섹션."""
    all_dark: Counter = Counter()
    for d in daily:
        q06 = d.get("q06", {})
        dark = (q06.get("answer") or {}).get("dark_corners_under3pct", [])
        for region in dark:
            # region may be a string or dict {'region': ..., 'coverage_pct': ...}
            region_name = region.get("region", str(region)) if isinstance(region, dict) else str(region)
            all_dark[region_name] += 1

    lines = ["### 🕳️ 글로벌 다크 코너 — 지속 미보도 지역 (Q06)", ""]
    if all_dark:
        lines.append(f"이번 주 7일 중 보도율 3% 미만 지역 (등장 일수):")
        for region, cnt in all_dark.most_common(12):
            lines.append(f"  - {region}: {cnt}일")
    else:
        lines.append("데이터 없음.")
    lines.append("")
    return "\n".join(lines) + "\n"


def _section_entities(daily: list[dict]) -> str:
    """Q18 글로벌 핵심 엔티티 섹션."""
    entity_mentions: dict[str, Counter] = {"PERSON": Counter(), "ORG": Counter(), "GPE": Counter()}
    for d in daily:
        q18 = d.get("q18", {})
        by_type = (q18.get("answer") or {}).get("by_type", {})
        for etype in ("PERSON", "ORG", "GPE"):
            for e in by_type.get(etype, []):
                entity_mentions[etype][e.get("entity", "")] += _safe(e.get("mention_count", 1))

    lines = ["### 🎯 주간 핵심 엔티티 (Q18)", ""]
    for etype, label in [("PERSON", "인물"), ("ORG", "기관"), ("GPE", "국가/지역")]:
        top = entity_mentions[etype].most_common(6)
        if top:
            lines.append(f"**{label}:** " + " · ".join(f"{e}({c:.0f})" for e, c in top))
    lines.append("")
    return "\n".join(lines) + "\n"


def _section_outlook(daily: list[dict], gti_avg: float) -> str:
    """다음 주 전망 섹션 (데이터 기반 추론)."""
    # Q02 rising 트렌드 중 다음 주 지속 가능성
    rising_set: set[str] = set()
    for d in daily[-3:]:  # 최근 3일
        q02 = d.get("q02", {})
        for item in (q02.get("answer") or {}).get("rising", []):
            rising_set.add(item.get("steeps", ""))

    lines = ["### 🔮 다음 주 전망", ""]
    lines.append(f"**GTI 베이스라인**: {_gti_label_str(gti_avg)} ({gti_avg:.1f})")
    lines.append("")
    if rising_set:
        names = [f"{s} {_STEEPS_NAME.get(s,'')}" for s in sorted(rising_set)]
        lines.append(f"**지속 가능성 높은 트렌드**: {', '.join(names)}")
    lines.append("")
    lines.append("> ⚠️ 이 전망은 데이터 패턴 기반이며 실제 미래를 예측하지 않습니다.")
    lines.append("")
    return "\n".join(lines) + "\n"


# ─────────────────────────────────────────────────────────────
# 보고서 생성
# ─────────────────────────────────────────────────────────────

def generate_weekly_future_map(
    end_date: datetime,
    project_root: Path,
    window_days: int = 7,
) -> dict:
    """주간 미래 맵 Markdown 보고서 생성."""
    data = collect_weekly_data(end_date, project_root, window_days)
    daily = data["daily"]
    portfolio = data["portfolio"]
    week_label = _week_label(end_date)
    start_date = data["date_range"][0] if data["date_range"] else ""
    end_str = data["end_date"]

    # GTI 주간 평균
    gti_scores = [_safe(d["gti"].get("gti_score")) for d in daily if d.get("gti")]
    gti_avg = statistics.mean(gti_scores) if gti_scores else 0.0

    # 데이터 커버리지
    dates_with_data = sum(
        1 for d in daily
        if any(d.get(f"q{i:02d}", {}).get("status") in ("answered", "degraded")
               for i in range(1, 19))
    )

    # 보고서 조립
    header = f"""# 주간 미래 맵 — {week_label}
**기간**: {start_date} ~ {end_str}
**데이터 커버리지**: {dates_with_data}/{window_days}일
**생성 시각**: {datetime.now().strftime('%Y-%m-%d %H:%M')}

> 이 보고서는 18개 핵심 분석 질문 + GTI + Signal Portfolio를 종합하여
> 자동 생성된 주간 미래 전망 맵입니다. Claude CLI 미사용 — 순수 데이터 기반.

---

"""

    sections = [
        header,
        _section_gti_summary(daily),
        _section_burst_trends(daily),
        _section_trend_shifts(daily),
        _section_weak_signals(daily, portfolio),
        _section_paradigm_shifts(daily),
        _section_agenda_landscape(daily),
        _section_dark_corners(daily),
        _section_entities(daily),
        _section_outlook(daily, gti_avg),
    ]
    report_md = "\n".join(sections)

    # 저장
    out_dir = project_root / "reports" / "weekly_future_map" / week_label
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "future_map.md").write_text(report_md, encoding="utf-8")

    # 메타
    meta = {
        "week_label": week_label,
        "start_date": start_date,
        "end_date": end_str,
        "dates_with_data": dates_with_data,
        "window_days": window_days,
        "gti_avg": round(gti_avg, 2),
        "gti_label": _gti_label_str(gti_avg),
        "generated_at": datetime.now().isoformat(),
        "report_path": str(out_dir / "future_map.md"),
    }
    (out_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    logger.info(
        "weekly_future_map_done week=%s dates=%d/%d gti_avg=%.1f path=%s",
        week_label, dates_with_data, window_days, gti_avg, out_dir / "future_map.md",
    )
    return meta


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="주간 미래 맵 생성")
    p.add_argument("--end-date", type=str, default=None, help="YYYY-MM-DD")
    p.add_argument("--week", type=str, default=None, help="YYYY-Www (예: 2026-W16)")
    p.add_argument("--window", type=int, default=7, help="집계 일수 (기본: 7)")
    p.add_argument("--project-dir", default=".", type=str)
    args = p.parse_args()

    root = Path(args.project_dir).resolve()

    if args.end_date:
        end_dt = datetime.strptime(args.end_date, "%Y-%m-%d")
    else:
        end_dt = datetime.now()

    meta = generate_weekly_future_map(end_dt, root, args.window)
    print(f"주간 미래 맵 생성 완료: {meta['week_label']}")
    print(f"  기간: {meta['start_date']} ~ {meta['end_date']}")
    print(f"  커버리지: {meta['dates_with_data']}/{meta['window_days']}일")
    print(f"  GTI 평균: {meta['gti_avg']:.1f} ({meta['gti_label']})")
    print(f"  파일: {meta['report_path']}")
