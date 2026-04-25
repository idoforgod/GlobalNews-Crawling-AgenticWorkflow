Run only the W1 Crawling Execution workflow under agent orchestration + P1 gates.

## Purpose

Executes W1 (Crawling) in isolation: `@crawl-execution-orchestrator` drives `main.py --mode crawl` and applies all W1 quality gates (L0, L1, L1.5, L2, SG1, Evidence Chain).

Useful when:
- Only fresh crawl data is needed (W2/W3 will run later)
- Debugging W1-specific issues
- Re-running W1 after fixing crawler configuration
- Incremental pipeline recovery

## Execution Protocol

### Step 0: Preflight

```bash
python3 scripts/execution/p1/validate_p1_scripts.py --check manifest
.venv/bin/python scripts/preflight_check.py --project-dir . --mode full --json
```

### Step 1: Invoke Meta-Orchestrator with W1-only scope

```
@meta-orchestrator, execute ONLY W1 Crawling for today's run. Do NOT advance to W2 or W3.
```

The Meta-Orchestrator:
1. Allocates run_id or reuses existing
2. Transitions null → crawling
3. Delegates to `@crawl-execution-orchestrator`
4. Waits for W1 completion
5. Runs SG1 Semantic Gate (`sg1_crawl_quality.py --check all`)
6. Runs Evidence Chain generation check (`evidence_chain.py --check generate`)
7. **STOPS** at `current_workflow = crawling, status = completed` — does NOT advance to W2

### Step 2: Report

Show W1 results:
- Articles crawled per site/group
- Success rate (%)
- Mandatory field coverage
- HTML contamination rate
- Evidence ID coverage
- SG1 decision (PASS/FAIL)
- W1 pACS score
- Output path (`data/raw/YYYY-MM-DD/all_articles.jsonl`)

## Post-Run Next Steps

To continue the pipeline later:
- `/run-analyze-only` — run W2 using existing W1 data
- `/run-chain` — restart full chain (will detect W1 already complete and skip to W2)

## Mapping User Intent

| User says | Action |
|---|---|
| "크롤링만 에이전트로 실행" | `/run-crawl-only` |
| "W1만", "crawling only" | `/run-crawl-only` |
| "fresh crawl with validation" | `/run-crawl-only` |

## Failure Modes

- **SG1 FAIL**: W1 data fails quality gate. Report specific check failures. Do NOT advance to W2 until remediated.
- **Evidence Chain FAIL**: Some articles missing evidence_id. Usually indicates pre-Phase-0.4 legacy crawl or a crawler bug.
- **Empty run**: Zero articles after all L1-L5 retries. Empty Run Policy escalates to user.
