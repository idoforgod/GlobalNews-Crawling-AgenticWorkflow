Generate 3-layer chart interpretations (해석 · 인사이트 · 미래통찰) for each dashboard tab. Consumes W2/W3/W4 + Public Narrative + M4 Temporal; emits `data/analysis/{date}/interpretations.json` that the dashboard auto-renders.

## When to Use

- After `/run-chain` or `scripts/run_daily.sh` — interpretations are auto-generated in Step 6.6
- Backfill for older runs
- Re-generation after template fix or glossary update

## Instructions

### Step 1: Ensure prerequisites

Required minimums for a meaningful output:
```bash
test -f "data/raw/$DATE/all_articles.jsonl" || echo "W1 missing"
test -f "data/output/$DATE/analysis.parquet" || echo "W2 missing"
```

Optional but recommended (richer facts):
- `reports/public/$DATE/facts_pool.json` (Public Narrative already ran)
- `data/insights/{latest}/synthesis/insight_report.md` (W3 narrator-refined)
- `data/insights/{latest}/temporal/velocity_map.parquet` (M4 Temporal)

### Step 2: Run the orchestrator

```bash
python3 scripts/reports/generate_chart_interpretations.py \
  --date "$DATE" --project-dir .
```

Pipeline:
1. Baseline build (~1s) — 7d/30d/all-history p25/50/75/90 per key metric
2. 6 tab fact extraction (pure Python, ~2s total)
3. Future linking — cherry-pick from Public L3 + W3 Forward + M4 Temporal
4. Prose generation — Claude CLI ×6 (~3-5 min), CI1-CI6 validation + 3-retry
5. Consolidate → `data/analysis/$DATE/interpretations.json`

Exit 0 = all 6 tabs PASS. Exit 1 = any tab FAIL. Exit 2 = script error.

### Step 3: Review (optional Agent Team)

```bash
python3 scripts/reports/review_chart_interpretations.py \
  --date "$DATE" --project-dir .
# → data/analysis/$DATE/interp-review-interp-fact-auditor-{ts}.md
```

Output is non-blocking — dashboard uses interpretations.json regardless.

### Step 4: View in dashboard

Open http://localhost:8501/ — each of 6 tabs (Overview/Topics/Sentiment/Time Series/Word Cloud/W3 Insight) now has a 📖 **해석 · 인사이트 · 미래통찰** expander at the top. Overview is default-expanded.

## Single-Tab Regeneration

```bash
python3 scripts/reports/generate_chart_interpretations.py \
  --date 2026-04-14 --only overview
```

Or click "🔁 재생성" button on the dashboard (async subprocess, refresh page to see new output).

## Dry-Run

```bash
python3 scripts/reports/generate_chart_interpretations.py \
  --date 2026-04-14 --dry-run
```

Composes prompts + extracts facts, skips Claude CLI. Useful for validating template changes.

## CI (validator) Contract

| Check | Inherits | Enforces |
|---|---|---|
| CI1 | new | 3-layer section presence (해석/인사이트/미래통찰) |
| CI2 | Public Narrative PUB2 | Korean-aware FKGL per layer |
| CI3 | Public Narrative PUB4 | Number parity (facts_pool whitelist) |
| CI4 | Public Narrative PUB5 | `[ev:xxx]` whitelist |
| CI5 | Public Narrative PUB7 | Forbidden phrases (반드시/100%/확실히) |
| CI6 | new | cross_tab_refs validity |

## Trigger Patterns

| Korean | English | Action |
|--------|---------|--------|
| "차트 해석 만들어줘", "대시보드 해석" | "generate chart interpretations" | `/generate-chart-interpretations --date today` |
| "특정 탭 재생성" | "regenerate X tab" | with `--only {tab_id}` |
| "해석이 없는데" | "dashboard missing interpretation" | same command |

## Distinction

| Artefact | Audience | Scope |
|---|---|---|
| Public Narrative L1/L2/L3 | General public | Run-wide (whole-day synthesis) |
| W4 Master final | Researcher/strategist | Cross-workflow integration (doctoral) |
| DCI final + appendix | Researcher (deep) | 14-layer deep content |
| **Chart Interpretations** | **Dashboard user** | **Per-tab local interpretation** |

Chart Interpretations **references** (not duplicates) Public L3 / W3 Forward / M4 Temporal.
