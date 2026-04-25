Run only the W2 Analysis Execution workflow under agent orchestration + P1 gates.

## Purpose

Executes W2 (Analysis Pipeline Stage 1-8) in isolation: `@analysis-execution-orchestrator` drives `main.py --mode analyze` and applies all W2 quality gates (L0, L1, L1.5, L2, SG2, Evidence Chain passthrough).

Prerequisite: W1 must have produced valid JSONL (check `data/raw/<date>/all_articles.jsonl`).

Useful when:
- Re-running analysis after fixing NLP code
- Running W2 on existing W1 data without re-crawling
- Debugging analysis stage failures

## Execution Protocol

### Step 0: Preflight

```bash
python3 scripts/execution/p1/validate_p1_scripts.py --check manifest
.venv/bin/python scripts/preflight_check.py --project-dir . --mode analyze --json
```

### Step 1: Verify W1 Input Available

```bash
ls data/raw/$(date +%Y-%m-%d)/all_articles.jsonl
```

If missing, abort and suggest `/run-crawl-only` first.

### Step 2: Verify W1 Evidence Chain

```bash
python3 scripts/execution/p1/evidence_chain.py --check generate \
  --jsonl data/raw/$(date +%Y-%m-%d)/all_articles.jsonl
```

If evidence_id coverage is < 100%, warn the user. Phase 0.4-compliant W1 output should always pass this check.

### Step 3: Invoke Meta-Orchestrator with W2-only scope

```
@meta-orchestrator, execute ONLY W2 Analysis for today's run using existing W1 data. Do NOT advance to W3.
```

Meta-Orchestrator:
1. Confirms `current_workflow = crawling, status = completed` in SOT
2. Runs `meta_gates.py --check transition --from crawling --to analysis`
3. On PASS, advances to W2 and delegates to `@analysis-execution-orchestrator`
4. Waits for Stage 1-8 completion
5. Runs SG2 Semantic Gate (`sg2_analysis_quality.py --check all`)
6. Runs Evidence Chain passthrough check
7. STOPS at `current_workflow = analysis, status = completed`

### Step 4: Report

- Stages completed / failed
- Stage-wise processing time
- Peak memory
- Signal layer distribution (L1-L5)
- Sentiment mean/std
- Topic coherence median
- SG2 decision
- W2 pACS score
- Output files

## Mapping User Intent

| User says | Action |
|---|---|
| "분석만 에이전트로 실행" | `/run-analyze-only` |
| "W2만", "analyze only" | `/run-analyze-only` |
| "NLP 파이프라인 재실행" | `/run-analyze-only` |
