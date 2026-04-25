"""과거 JSONL → articles_enriched.parquet + 18-Question 소급 변환 스크립트.

이미 enriched가 있는 날짜는 건너뛴다 (--force 로 재처리 강제).
NLP 모델 없이 키워드 기반 STEEPS + geo_focus + signal 만 산출.
Stage 2/3 parquet 있으면 자동 병합.

Usage:
    python scripts/backfill_enriched.py
    python scripts/backfill_enriched.py --force        # 전체 재처리
    python scripts/backfill_enriched.py --dry-run      # 처리 대상만 출력
    python scripts/backfill_enriched.py --date 2026-03-25  # 단일 날짜
    python scripts/backfill_enriched.py --min-articles 100 # 최소 기사 수 필터
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


def _count_jsonl(path: Path) -> int:
    """JSONL 파일 유효 줄 수."""
    try:
        count = 0
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    count += 1
        return count
    except Exception:
        return 0


def _discover_dates(raw_dir: Path, min_articles: int) -> list[str]:
    """처리 가능한 날짜 목록 (기사 수 필터 적용)."""
    dates: list[str] = []
    for d in sorted(raw_dir.iterdir()):
        if not d.is_dir():
            continue
        name = d.name
        # YYYY-MM-DD 형식만
        if len(name) != 10 or name[4] != "-" or name[7] != "-":
            continue
        jsonl = d / "all_articles.jsonl"
        if not jsonl.exists():
            continue
        n = _count_jsonl(jsonl)
        if n >= min_articles:
            dates.append(name)
    return dates


def process_date(
    date: str,
    project_root: Path,
    force: bool = False,
    dry_run: bool = False,
) -> dict:
    """단일 날짜 처리: enriched 조립 + 18문 엔진."""
    enriched_path = project_root / "data" / "enriched" / date / "articles_enriched.parquet"
    answers_summary = project_root / "data" / "answers" / date / "summary.json"

    result = {
        "date": date,
        "skipped": False,
        "enriched_ok": False,
        "questions_ok": False,
        "article_count": 0,
        "elapsed_s": 0.0,
        "error": None,
    }

    # 이미 처리됨 (--force 없으면 건너뜀)
    if not force and enriched_path.exists() and answers_summary.exists():
        result["skipped"] = True
        return result

    if dry_run:
        result["skipped"] = False  # dry-run은 건너뜀 표시 안 함
        return result

    t0 = time.monotonic()
    try:
        # Step 1: enriched 조립
        from src.analysis.articles_enriched_assembler import ArticlesEnrichedAssembler
        assembler = ArticlesEnrichedAssembler(
            date=date,
            project_root=str(project_root),
        )
        r = assembler.run()
        result["article_count"] = r.get("article_count", 0)
        result["enriched_ok"] = r.get("article_count", 0) > 0

        # Step 2: 18-Question 엔진
        if result["enriched_ok"]:
            from src.analysis.question_engine import QuestionEngine
            engine = QuestionEngine(date=date, project_root=str(project_root))
            engine.run_all()
            result["questions_ok"] = True

    except Exception as exc:
        result["error"] = str(exc)

    result["elapsed_s"] = round(time.monotonic() - t0, 1)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="과거 데이터 소급 변환")
    parser.add_argument("--force", action="store_true", help="기존 enriched 재처리")
    parser.add_argument("--dry-run", action="store_true", help="처리 대상만 출력")
    parser.add_argument("--date", type=str, default=None, help="단일 날짜 (YYYY-MM-DD)")
    parser.add_argument("--min-articles", type=int, default=50, help="최소 기사 수 (기본: 50)")
    parser.add_argument("--project-dir", type=str, default=None, help="프로젝트 루트 경로")
    args = parser.parse_args()

    root = Path(args.project_dir).resolve() if args.project_dir else PROJECT_ROOT
    raw_dir = root / "data" / "raw"

    if args.date:
        dates = [args.date]
    else:
        dates = _discover_dates(raw_dir, args.min_articles)

    print(f"=== 소급 변환 시작 ===")
    print(f"대상: {len(dates)}개 날짜 | min_articles={args.min_articles}")
    print(f"force={args.force} | dry_run={args.dry_run}\n")

    total_ok = 0
    total_skip = 0
    total_fail = 0
    grand_total_articles = 0
    t_all = time.monotonic()

    for i, date in enumerate(dates, 1):
        r = process_date(
            date=date,
            project_root=root,
            force=args.force,
            dry_run=args.dry_run,
        )

        if r["skipped"]:
            total_skip += 1
            print(f"  [{i:02d}/{len(dates)}] {date} SKIP (이미 처리됨)")
            continue

        if args.dry_run:
            jsonl = raw_dir / date / "all_articles.jsonl"
            n = _count_jsonl(jsonl)
            print(f"  [{i:02d}/{len(dates)}] {date} → {n}건 처리 예정")
            continue

        if r["error"]:
            total_fail += 1
            print(f"  [{i:02d}/{len(dates)}] {date} FAIL | {r['error'][:60]}")
        else:
            total_ok += 1
            grand_total_articles += r["article_count"]
            q_ok = "Q✓" if r["questions_ok"] else "Q?"
            print(
                f"  [{i:02d}/{len(dates)}] {date} OK | "
                f"{r['article_count']}건 | {r['elapsed_s']}s | {q_ok}"
            )

    elapsed_total = time.monotonic() - t_all
    print()
    print(f"=== 완료 ===")
    print(f"성공: {total_ok} | 건너뜀: {total_skip} | 실패: {total_fail}")
    print(f"총 기사: {grand_total_articles:,}건 | 총 소요: {elapsed_total:.0f}s")

    if not args.dry_run and total_ok > 0:
        print()
        print("이제 18개 질문 신뢰도가 향상됩니다:")
        print("  Q02 트렌드 추이      → 7일+ 데이터로 answered")
        print("  Q03 사건 전후 변화   → 14일+ 데이터로 answered")
        print("  Q10 의제 이동 패턴   → 21일+ 데이터로 answered")
        print("  Q16 이슈 인과 연쇄   → 30일+ 데이터로 answered")
        print("  Q11 의제 선점        → 이력 보유로 answered")


if __name__ == "__main__":
    main()
