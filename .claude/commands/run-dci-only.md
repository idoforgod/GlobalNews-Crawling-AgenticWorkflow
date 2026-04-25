Execute the DCI (Deep Content Intelligence) independent workflow under `@dci-execution-orchestrator` supervision.

## Purpose

DCI is a **standalone, independent workflow** orthogonal to the Meta-Orchestrator's W1→W2→W3→W4(Master) chain. It consumes only W1 raw articles (`data/raw/{date}/all_articles.jsonl`) and produces a doctoral-grade bilingual deep content analysis report with CE4 3-layer evidence chain and SG-Superhuman 10-gate verification.

**DCI is NOT part of `/run-chain`.** Master Integration (`/integrate-results`) continues to operate on the W1→W2→W3→W4 triple chain independently.

## When to Use

- You want a deep, multi-lens analysis of today's (or a historical date's) corpus — independent of whether W2/W3 have run
- A W1 crawl has completed and you want the ~14-layer DCI output without waiting for W2/W3/Master
- Re-running DCI on a prior date (backfill or research)

## When NOT to Use

- You want the cross-workflow Master synthesis — use `/integrate-results` instead
- The W1 corpus for `{date}` does not exist — run `/run-crawl-only` first
- You only need surface-level metrics — use `/run-analyze-only` (W2 NLP pipeline) instead

## Instructions

### Step 0: Ensure Domain Venv

DCI's Python layers (L-1 → L11) require the Python 3.13 venv (spaCy, Kiwi, pgmpy, NetworkX, DeBERTa-v3-MNLI). Hook scripts run on system python3.

```bash
.venv/bin/python -c "import spacy, kiwipiepy, networkx; print('venv ok')"
```

### Step 1: Pre-flight (Python-only — no agent hallucination risk)

```bash
python3 .claude/hooks/scripts/validate_dci_preflight.py \
  --date {date} --run-id dci-{date}-$(date +%H%M) --project-dir .
```

Parse the JSON output:
- `valid: true` → proceed
- `valid: false` → report violations; do NOT spawn the orchestrator

Display:
```
── DCI EXECUTION ──────────────────────────────
Run ID: dci-{date}-{HHMM}
Corpus date: {date}
Articles in corpus: {article_count from preflight}
Models: {deps list}
Claude CLI: {version}
Estimated duration: 1-3 hours (L6 Triadic + L10 narrator)
───────────────────────────────────────────────
```

### Step 2: Spawn `@dci-execution-orchestrator`

```
@dci-execution-orchestrator, execute DCI workflow for run_id=dci-{date}-{HHMM}, 
date={date}. Follow the 7-phase protocol in prompt/execution-workflows/dci.md. 
Respect the 5 P1 Hallucination Prevention Rules. Escalate only on retry budget 
exhaustion or mandatory layer failure.
```

### Step 3: Monitor

The orchestrator streams progress through per-phase logs:
```
logs/dci/{run_id}-{phase}.log
```

SOT state is visible via:
```bash
python3 scripts/sot_manager.py --read --project-dir . \
  --path execution.runs.{run_id}.workflows.dci
```

### Step 4: Final Artifacts

On completion, `workflows.dci.status == "completed"` and the following artifacts exist:
```
data/dci/runs/{run_id}/
├── final_report.md            # English doctoral prose
├── final_report.ko.md         # Korean translation
├── executive_summary.md
├── evidence_ledger.jsonl
├── sg_superhuman_verdict.json
└── phase6/
    ├── sg_review.md
    ├── evidence_review.md
    └── narrative_review.md
```

## Failure Recovery

If the orchestrator escalates:

1. Read the escalation message — it includes the failing P1 CLI name + JSON output verbatim
2. Check retry budget:
   ```bash
   python3 .claude/hooks/scripts/dci_retry_budget.py \
     --run-id {run_id} --gate {gate} --status --project-dir .
   ```
3. Diagnose via context (the orchestrator has already written `diagnosis-logs/dci-*.md` if applicable)
4. Either fix and re-invoke, or abort with `sot_manager.py --set-status failed`

## Resumability

DCI auto-resumes on re-invocation. The orchestrator's Phase 0 reads `src.dci.resume.resume_plan()` and jumps to the next incomplete layer. You do NOT need to manually specify "resume from L6" — the checkpoints tell the orchestrator.

To force a fresh run (discarding prior checkpoints):
```bash
rm -rf data/dci/runs/{run_id}/checkpoints/
```

## Trigger Patterns

| User phrase (Korean) | User phrase (English) | Action |
|---------------------|----------------------|--------|
| "DCI 실행", "심층 분석 시작" | "run DCI", "start deep content" | `/run-dci-only` with today's date |
| "DCI 재실행", "심층 분석 재개" | "resume DCI", "retry DCI" | `/run-dci-only` with existing run_id (auto-resumes via checkpoints) |
| "심층 분석 결과", "DCI 결과" | "DCI results", "show deep report" | Read `data/dci/runs/latest/final_report.md` |

## Distinction from Other Slash Commands

| Command | Scope | Writes to SOT |
|---------|-------|---------------|
| `/run-crawl-only` | W1 crawling | `workflows.crawling.*` |
| `/run-analyze-only` | W2 NLP analysis | `workflows.analysis.*` |
| `/run-insight-only` | W3 insight modules | `workflows.insight.*` |
| `/integrate-results` | W4 Master aggregation | `workflows.master.*` |
| `/run-chain` | W1→W2→W3→W4 chain | all of the above |
| **`/run-dci-only`** | **DCI standalone** | **`workflows.dci.*`** |

`/run-dci-only` is the ONLY slash command that writes to `workflows.dci.*`. It does NOT trigger any other workflow.
