Generate the 3-layer Public Narrative (Interpretation · Insight · Future) for a given run date. Produces plain-reader prose that cites only facts pre-registered in `facts_pool.json` — hallucination impossible by construction.

## When to Use

- After W4 Master Integration completes (automatic in full chain)
- To regenerate public layers for an older run (research, backfill)
- After fixing glossary or template to re-run without changing upstream data

## Instructions

### Step 1: Confirm prerequisites

```bash
test -f "data/raw/$DATE/all_articles.jsonl" || echo "W1 missing — /run-crawl-only first"
test -f "data/output/$DATE/analysis.parquet" || echo "W2 missing — /run-analyze-only"
```

W3 and W4 are nice-to-have; the generator degrades gracefully.

### Step 2: Run the orchestrator

```bash
python3 .claude/hooks/scripts/generate_public_layers.py \
  --date "$DATE" --project-dir .
```

The orchestrator does (all deterministic):

1. `facts_extractor.build_facts_pool()` scans W1/W2/W3/W4/DCI → `reports/public/$DATE/facts_pool.json`
2. **L1 Interpretation** via Claude CLI + 5-attempt retry against PUB2-PUB7
3. **L2 Insight** sees L1 as "upper context" + same retry
4. **L3 Future** sees L1+L2 + same retry (L3 failure is non-blocking)
5. Korean translation via Claude CLI for each passing layer
6. PUB8 EN↔KO structure parity re-verify

### Step 3: Verify

```bash
# Per-layer deterministic P1 check
python3 .claude/hooks/scripts/validate_public_readability.py \
  --layer L1 --date "$DATE" --project-dir .
python3 .claude/hooks/scripts/validate_public_readability.py \
  --layer L2 --date "$DATE" --project-dir .
python3 .claude/hooks/scripts/validate_public_readability.py \
  --layer L3 --date "$DATE" --project-dir .
```

Each CLI returns exit 0 on PASS, exit 1 on FAIL (with detailed JSON).

### Step 4: View results

Open the integrated dashboard:

```bash
.venv/bin/python -m streamlit run dashboard.py
```

→ `📋 Run Summary` tab → `📖 일반인용 3-Layer 해석` section.

Or read files directly:
- `reports/public/$DATE/interpretation.md` (+ `.ko.md`)
- `reports/public/$DATE/insight.md` (+ `.ko.md`)
- `reports/public/$DATE/future.md` (+ `.ko.md`)
- `reports/public/$DATE/facts_pool.json`
- `reports/public/$DATE/generation_metadata.json`

## Failure Modes

| Layer failed | Next action |
|---|---|
| L1 | Abort (downstream layers would inherit broken framing). Inspect `facts_pool.json` + validator output. |
| L2 | Abort L3. L1 output retained. Fix the FAIL reason then `--only L2 L3` to resume. |
| L3 | Report labeled `partial_pass_l3_failed`. L1+L2 retained. Retry with `--only L3`. |
| Translation | Warning only (PUB8 SKIP). EN-only release still valid. |

## P1 Contract

- All 8 PUB checks are deterministic Python (no LLM judgment)
- Facts pool is built before any narration — narrators cannot add numbers
- Marker whitelist is pulled from W4 report's actual `[ev:xxx]` markers
- Retry budget = 5 per layer (matches `dci_retry_budget.py` convention)
- Korean translation reuses Claude CLI with structural-parity contract

## Trigger Patterns

| Korean | English | Action |
|--------|---------|--------|
| "일반인 해석 만들어줘", "공개 보고서 생성" | "generate public layers", "public narrative" | `/generate-public-layers --date today` |
| "해석/통찰/미래 다시" | "regenerate L2", etc. | with `--only L2` or as needed |
| "대시보드에 해석 없는데" | "dashboard missing narrative" | same command |

## Distinction from Other Workflows

| Workflow | Audience | Format |
|---|---|---|
| W4 Master final report | Researcher · strategist | Doctoral prose (13KB, technical) |
| W3 insight report | Analyst | Metric-heavy markdown |
| DCI final report | Research · 전략가 | Doctoral + CE4 evidence |
| **Public Narrative (L1/L2/L3)** | **General public → decision-maker** | **Layered plain prose + futurist** |

Public Narrative is **not** a replacement — it is a *translation layer* atop the existing analytical artifacts.
