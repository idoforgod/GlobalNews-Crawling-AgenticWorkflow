"""Future Signal Portfolio — 약한 신호 추적 시스템.

Q08(약한 신호 탐지) 결과를 날짜별로 집계하여
신호의 생애 주기(watching → emerging → confirmed → dismissed)를 추적한다.

data/signal_portfolio.yaml — 단일 영구 SOT
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

import yaml

# ---------------------------------------------------------------------------
# 상수
# ---------------------------------------------------------------------------

_SIGNAL_STATUSES = ("watching", "emerging", "confirmed", "dismissed")

# 일수 임계값
_EMERGE_DAYS = 3     # 3일 이상 지속 → emerging
_CONFIRM_DAYS = 7    # 7일 이상 지속 → confirmed
_DISMISS_DAYS = 14   # 14일 비등장 → dismissed


def _today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _slug(text: str) -> str:
    """텍스트에서 간단한 슬러그 생성 (중복 탐지용)."""
    text = re.sub(r"[^\w\s가-힣]", "", text.lower())
    text = re.sub(r"\s+", "_", text.strip())
    return text[:60]


# ---------------------------------------------------------------------------
# Portfolio loader / saver
# ---------------------------------------------------------------------------

def load_portfolio(portfolio_path: Path) -> dict:
    """YAML 포트폴리오를 로드. 없으면 빈 구조 반환."""
    if portfolio_path.exists():
        try:
            raw = yaml.safe_load(portfolio_path.read_text(encoding="utf-8")) or {}
            return raw if isinstance(raw, dict) else {}
        except Exception:
            return {}
    return {"version": "1.0", "signals": {}, "last_updated": ""}


def save_portfolio(portfolio: dict, portfolio_path: Path) -> None:
    """포트폴리오를 YAML로 저장."""
    portfolio["last_updated"] = _today_str()
    portfolio_path.parent.mkdir(parents=True, exist_ok=True)
    portfolio_path.write_text(
        yaml.dump(portfolio, allow_unicode=True, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Core update logic
# ---------------------------------------------------------------------------

def update_portfolio(
    portfolio_path: Path,
    answers_dir: Path,
    lookback_days: int = 30,
) -> dict:
    """Q08 결과를 스캔하여 포트폴리오를 갱신한다.

    Returns:
        dict with keys: added, promoted, dismissed, total
    """
    portfolio = load_portfolio(portfolio_path)
    signals: dict[str, dict] = portfolio.setdefault("signals", {})

    # 최근 lookback_days 날짜에서 Q08 결과 수집
    cutoff = datetime.now() - timedelta(days=lookback_days)
    q08_by_date: dict[str, list[dict]] = {}

    for date_dir in sorted(answers_dir.iterdir()):
        if not date_dir.is_dir() or len(date_dir.name) != 10:
            continue
        try:
            date_dt = datetime.strptime(date_dir.name, "%Y-%m-%d")
        except ValueError:
            continue
        if date_dt < cutoff:
            continue
        q08_path = date_dir / "q08.json"
        if not q08_path.exists():
            continue
        try:
            data = json.loads(q08_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if data.get("status") not in ("answered", "degraded"):
            continue
        weak = (data.get("answer") or {}).get("weak_signals") or []
        q08_by_date[date_dir.name] = weak

    # 신호 슬러그 → 등장 날짜 목록 구축
    slug_dates: dict[str, list[str]] = {}
    slug_meta: dict[str, dict] = {}
    for date_str, signals_list in q08_by_date.items():
        for sig in signals_list:
            title = sig.get("title") or ""
            if not title:
                continue
            sl = _slug(title)
            slug_dates.setdefault(sl, []).append(date_str)
            # 최신 메타 저장
            if sl not in slug_meta or date_str > slug_meta[sl].get("last_date", ""):
                slug_meta[sl] = {
                    "title": title,
                    "steeps": sig.get("steeps", ""),
                    "geo_focus": sig.get("geo_focus_primary", "UNKNOWN"),
                    "novelty_score": sig.get("novelty_score", 0.0),
                    "source_id": sig.get("source_id", ""),
                    "last_date": date_str,
                }

    stats = {"added": 0, "promoted": 0, "dismissed": 0, "total": len(signals)}

    today_str = _today_str()
    all_seen_slugs: set[str] = set(slug_dates.keys())

    # 기존 신호 업데이트
    for sl, entry in signals.items():
        if entry.get("status") == "dismissed":
            continue
        dates_seen = sorted(slug_dates.get(sl, []))
        if dates_seen:
            entry["seen_dates"] = sorted(set(entry.get("seen_dates", []) + dates_seen))
            entry["last_seen"] = entry["seen_dates"][-1]
            if sl in slug_meta:
                entry.update({k: v for k, v in slug_meta[sl].items() if k != "last_date"})
        else:
            # 최근에 안 보임 → dismissed 후보
            last_seen_dt = datetime.strptime(entry.get("last_seen", "2000-01-01"), "%Y-%m-%d")
            if (datetime.now() - last_seen_dt).days >= _DISMISS_DAYS:
                entry["status"] = "dismissed"
                entry["dismissed_date"] = today_str
                stats["dismissed"] += 1
                continue

        # 상태 승격
        n_days = len(set(entry.get("seen_dates", [])))
        old_status = entry.get("status", "watching")
        if old_status == "watching" and n_days >= _EMERGE_DAYS:
            entry["status"] = "emerging"
            entry["emerged_date"] = today_str
            stats["promoted"] += 1
        elif old_status == "emerging" and n_days >= _CONFIRM_DAYS:
            entry["status"] = "confirmed"
            entry["confirmed_date"] = today_str
            stats["promoted"] += 1

    # 신규 신호 추가 (포트폴리오에 없는 것)
    for sl in all_seen_slugs:
        if sl in signals:
            continue
        meta = slug_meta[sl]
        signals[sl] = {
            "title": meta["title"],
            "slug": sl,
            "steeps": meta["steeps"],
            "geo_focus": meta["geo_focus"],
            "novelty_score": meta["novelty_score"],
            "source_id": meta["source_id"],
            "status": "watching",
            "first_detected": slug_dates[sl][0],
            "last_seen": slug_dates[sl][-1],
            "seen_dates": sorted(set(slug_dates[sl])),
            "added_date": today_str,
        }
        stats["added"] += 1

    stats["total"] = len(signals)
    save_portfolio(portfolio, portfolio_path)
    return stats


# ---------------------------------------------------------------------------
# Query helpers (for dashboard)
# ---------------------------------------------------------------------------

def get_active_signals(portfolio_path: Path, status: str | None = None) -> list[dict]:
    """포트폴리오에서 활성 신호 목록 반환."""
    portfolio = load_portfolio(portfolio_path)
    signals = portfolio.get("signals", {})
    result = []
    for sl, entry in signals.items():
        if status and entry.get("status") != status:
            continue
        if not status and entry.get("status") == "dismissed":
            continue
        days = len(set(entry.get("seen_dates", [])))
        result.append({**entry, "slug": sl, "days_tracked": days})
    return sorted(result, key=lambda x: (
        {"confirmed": 0, "emerging": 1, "watching": 2}.get(x.get("status", ""), 3),
        -x.get("days_tracked", 0),
    ))


def portfolio_summary(portfolio_path: Path) -> dict:
    """상태별 요약 딕셔너리 반환."""
    portfolio = load_portfolio(portfolio_path)
    signals = portfolio.get("signals", {})
    summary: dict[str, int] = {s: 0 for s in _SIGNAL_STATUSES}
    for entry in signals.values():
        s = entry.get("status", "watching")
        if s in summary:
            summary[s] += 1
    summary["total"] = len(signals)
    summary["last_updated"] = portfolio.get("last_updated", "")
    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="Future Signal Portfolio 업데이트")
    p.add_argument("--project-dir", default=".", type=str)
    p.add_argument("--lookback", default=30, type=int)
    args = p.parse_args()

    root = Path(args.project_dir).resolve()
    port_path = root / "data" / "signal_portfolio.yaml"
    ans_dir = root / "data" / "answers"

    result = update_portfolio(port_path, ans_dir, args.lookback)
    summ = portfolio_summary(port_path)
    print(f"Portfolio updated: +{result['added']} added, +{result['promoted']} promoted, {result['dismissed']} dismissed")
    print(f"Total: {summ['total']} | watching={summ['watching']} | emerging={summ['emerging']} | confirmed={summ['confirmed']} | dismissed={summ['dismissed']}")
