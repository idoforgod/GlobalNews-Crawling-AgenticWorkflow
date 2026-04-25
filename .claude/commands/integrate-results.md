Run only the W4 Master Integration workflow. Assumes W1, W2, W3 are all complete.

## Purpose

Executes Master Integration (the 5-phase synthesis pipeline) on top of existing W1+W2+W3 outputs. `@master-integrator` invokes the full assembly chain: ingestion → cross-audit → longitudinal analysis → doctoral synthesis → review and staging.

Prerequisites:
- W1: `data/raw/<date>/all_articles.jsonl` exists and SG1 passed
- W2: `data/output/*.parquet` exists and SG2 passed
- W3: `data/insights/<insight_run_id>/synthesis/insight_report.md` exists and SG3 passed

## Execution Protocol

### Step 0: Preflight

```bash
python3 scripts/execution/p1/validate_p1_scripts.py --check manifest
```

### Step 1: Verify All Three Workflow Outputs

```bash
python3 scripts/execution/p1/master_assembly.py --check validate_inputs \
  --w1-summary workflows/crawling/outputs/w1-summary-$(date +%Y-%m-%d).md \
  --w2-summary workflows/analysis/outputs/w2-summary-$(date +%Y-%m-%d).md \
  --w3-summary workflows/insight/outputs/w3-summary-$(date +%Y-%m-%d).md
```

If FAIL, report which summaries are missing and abort.

### Step 2: Invoke Meta-Orchestrator with Master-only scope

```
@meta-orchestrator, execute ONLY W4 Master Integration for today's run.
```

Meta-Orchestrator:
1. Confirms `current_workflow = insight, status = completed`
2. Runs `meta_gates.py --check transition --from insight --to master`
3. On PASS, advances to master, delegates to `@master-integrator`
4. Master Integrator runs 5 phases:
   - **Ingestion**: `w1_metrics.py --extract` + `w2_metrics.py --extract` + `w3_metrics.py --extract` → structured JSON
   - **Cross-Audit**: `master-audit-team` (Agent Team) validates consistency across W1/W2/W3
   - **Longitudinal**: `longitudinal-analysis-team` runs DoD/WoW/MoM comparisons against `execution.history`
   - **Synthesis**: `@master-synthesizer` writes doctoral narrative with Python-injected metrics + `[ev:xxx]` markers
   - **Review & Staging**: `@meta-reviewer`, `@narrative-reviewer`, `@evidence-reviewer` all audit; master_assembly chunks + injects + validates structure
5. Promotes report: `reports/staging/` → `reports/candidate/` → `reports/final/`

### Step 3: Evidence Chain Final Verification

```bash
python3 scripts/execution/p1/evidence_chain.py --check master_chain \
  --report reports/candidate/integrated-report-$(date +%Y-%m-%d).md \
  --jsonl data/raw/$(date +%Y-%m-%d)/all_articles.jsonl
```

All `[ev:xxx]` markers must resolve to raw JSONL. Orphan count = 0 is a hard requirement.

### Step 4: Translation

Invoke `@translator` to produce the Korean version:
```
@translator, translate reports/final/integrated-report-$(date +%Y-%m-%d).md to Korean.
```

Output: `reports/final/integrated-report-<date>.ko.md`

### Step 5: Autopilot-Aware Promotion

- If autopilot is ON, promote candidate → final automatically
- If autopilot is OFF, stop at candidate and prompt user: "Master report ready for review. Use `/approve-report` to promote to final."

### Step 6: Report

- Phase completion status (5/5)
- Cross-audit findings
- Longitudinal delta summary (DoD/WoW/MoM)
- Claim count + evidence coverage
- Reviewer verdicts (@meta-reviewer, @narrative-reviewer, @evidence-reviewer)
- Final report path (staging / candidate / final)

## Mapping User Intent

| User says | Action |
|---|---|
| "통합 보고서 생성" | `/integrate-results` |
| "master 통합만" | `/integrate-results` |
| "master integration only" | `/integrate-results` |
