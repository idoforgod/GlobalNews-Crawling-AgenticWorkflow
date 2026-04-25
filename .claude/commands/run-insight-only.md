Run only the W3 Insight Execution workflow under agent orchestration + P1 gates.

## Purpose

Executes W3 (Insight modules M1-M7) in isolation: `@insight-execution-orchestrator` drives `main.py --mode insight` and applies all W3 quality gates (L0, L1, L1.5, L2, SG3, Evidence Chain claim_ratio + trace).

Prerequisite: W2 must have produced valid parquet outputs (analysis.parquet, signals.parquet, topics.parquet).

## Execution Protocol

### Step 0: Preflight

```bash
python3 scripts/execution/p1/validate_p1_scripts.py --check manifest
```

### Step 1: Verify W2 Input Available

```bash
ls data/output/*.parquet data/output/index.sqlite
```

If missing, abort and suggest `/run-analyze-only` first.

### Step 2: Invoke Meta-Orchestrator with W3-only scope

```
@meta-orchestrator, execute ONLY W3 Insight for today's run using existing W2 data.
```

Meta-Orchestrator:
1. Confirms `current_workflow = analysis, status = completed`
2. Runs `meta_gates.py --check transition --from analysis --to insight`
3. On PASS, advances to W3, delegates to `@insight-execution-orchestrator`
4. W3 orchestrator determines window (7/30/90) and insight_run_id (weekly-*/monthly-*/quarterly-*)
5. Runs M1-M7 modules sequentially (or in parallel per module)
6. Applies entity resolution via `resolve_entities.py`
7. Runs SG3 Semantic Gate:
   ```bash
   python3 scripts/execution/p1/sg3_insight_quality.py --check all \
     --report data/insights/{insight_run_id}/synthesis/insight_report.md \
     --jsonl data/raw/$(date +%Y-%m-%d)/all_articles.jsonl
   ```
8. Runs Evidence Chain trace verification
9. STOPS at `current_workflow = insight, status = completed`

### Step 3: Report

- M1-M7 modules completed
- Claim count
- Evidence marker count / unique
- Median evidence per claim
- SG3 decision
- W3 pACS score
- Insight report path

## Mapping User Intent

| User says | Action |
|---|---|
| "인사이트만 에이전트로 실행" | `/run-insight-only` |
| "W3만", "insight only" | `/run-insight-only` |
| "통찰 분석 재실행" | `/run-insight-only` |
| "주간 인사이트" | `/run-insight-only --window 7` |
| "월간 인사이트" | `/run-insight-only --window 30` |
| "분기 인사이트" | `/run-insight-only --window 90` |
