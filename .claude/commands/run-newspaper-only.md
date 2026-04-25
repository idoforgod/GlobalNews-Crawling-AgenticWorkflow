Execute WF5 Personal Newspaper — daily edition (ADR-083). Consumes W1-W4 artefacts, emits a 135,000-word NYTimes-style HTML edition under `newspaper/daily/{date}/`.

## Prerequisites

- W1 raw corpus: `data/raw/{date}/all_articles.jsonl`
- (recommended) W2 analysis parquet: `data/output/{date}/analysis.parquet`
- (recommended) W3 insight + W4 master + Public L3 + Chart Interp

## Instructions

### Step 1: Check prerequisites

```bash
test -f "data/raw/$DATE/all_articles.jsonl" || echo "W1 missing — /run-crawl-only first"
```

### Step 2: Run the orchestrator

```bash
python3 scripts/reports/generate_newspaper_daily.py \
  --date "$DATE" --project-dir .
```

7-Phase pipeline (~60-90 min full, ~3 sec skeleton-only):
1. Ingest W1-W4 + Public + Chart Interp
2. Story clustering (DCI simhash + entity overlap)
3. Organization (country/continent/STEEPS/tier/dark/budget)
4. 14 editorial desks × Claude CLI (parallel-ish)
5. Chief Editor assembly (integration + editorial column + deep analysis)
6. Copy Editor review (P9/P10/P14/P15 enforcement)
7. HTML rendering (NYTimes-style, 16+ pages)

### Step 3: Skeleton-only (fast, no LLM)

```bash
python3 scripts/reports/generate_newspaper_daily.py \
  --date "$DATE" --skeleton-only --project-dir .
```

~3 seconds. Useful for layout iteration.

### Step 4: Validate

```bash
python3 .claude/hooks/scripts/validate_newspaper.py \
  --kind daily --date "$DATE" --project-dir .
```

NP1-NP12 checks. Exit 0 = all PASS.

### Step 5: Open

```bash
open "newspaper/daily/$DATE/index.html"
```

or view via dashboard "📰 Newspaper" tab (once integrated).

## Trigger Patterns

| Korean | English | Action |
|--------|---------|--------|
| "오늘 신문 만들어줘" | "generate today's newspaper" | `/run-newspaper-only --date today` |
| "내 신문 다시 만들기" | "regenerate newspaper" | same command |
| "일간 신문 발행" | "publish daily edition" | same command |

## Distinction

| Artefact | Audience | Scope |
|---|---|---|
| W4 Master final | Researcher | Cross-workflow integration |
| DCI final + appendix | Researcher (deep) | 14-layer deep content |
| Public Narrative L1/L2/L3 | General public | Run-wide synthesis |
| Chart Interpretations | Dashboard user | Per-tab interpretation |
| **Personal Newspaper (WF5)** | **신문 독자 (일반)** | **9-hour read, NYTimes-style HTML** |
