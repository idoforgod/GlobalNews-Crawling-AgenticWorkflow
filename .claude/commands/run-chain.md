Run the full Triple Execution Workflow chain: W1 Crawling → W2 Analysis → W3 Insight → W4 Master Integration.

## Purpose

This command invokes the `@meta-orchestrator` sub-agent, which orchestrates all four execution workflows sequentially under P1 Hallucination Prevention. Every transition is backed by `meta_gates.py --check transition` exit codes. No LLM judgment is involved in transition decisions.

**This is distinct from `/run`**: `/run` executes `main.py --mode ...` directly (raw Python pipeline). `/run-chain` wraps the same pipeline in the agent orchestration layer with full P1 validation at each boundary.

## When to Use

- Scheduled weekly deep runs (`/run` is used for daily cron; `/run-chain` is for the weekly quality run)
- Manual invocation when full Master Integration report is required
- Debugging the agent orchestration layer end-to-end

## Execution Protocol

### Step 0: Preflight

Verify the P1 Suite is operational:
```bash
python3 scripts/execution/p1/validate_p1_scripts.py --check all
```
If exit != 0, abort and report the P1 Suite failure.

Verify the domain venv is healthy:
```bash
.venv/bin/python scripts/preflight_check.py --project-dir . --mode full --json
```

### Step 1: Delegate to `@meta-orchestrator`

Invoke the Meta-Orchestrator sub-agent with:

```
@meta-orchestrator, execute the full W1→W2→W3→W4 chain for today's run.
```

The Meta-Orchestrator handles:
1. Run ID allocation (`exec-YYYY-MM-DD`)
2. Execution section initialization (if not already present)
3. Sequential workflow invocation (W1 → W2 → W3 → W4)
4. Transition gate checks at every boundary (`meta_gates.py --check transition`)
5. Semantic Gate invocation at W1/W2/W3 outputs (SG1/SG2/SG3)
6. Evidence Chain validation at every stage
7. Retry with Abductive Diagnosis on gate failures (within retry budget)
8. Master Integration (5-phase)
9. Staging → Candidate → Final promotion
10. `@meta-reviewer` invocation at end of run

### Step 2: Report Status

After the Meta-Orchestrator completes, show the user:
- Run ID
- Final status (`completed`, `completed_with_meta_warnings`, or `failed`)
- pACS score per workflow
- Master report location (`reports/final/integrated-report-{date}.md`)
- Any meta warnings or retry events

### Step 3: Final Report

If the run completed successfully, display the final Master Integration report:
```bash
cat reports/final/integrated-report-$(date +%Y-%m-%d).md
```

And the Korean translation if present:
```bash
cat reports/final/integrated-report-$(date +%Y-%m-%d).ko.md
```

## Mapping User Intent

| User says | Action |
|---|---|
| "전체 파이프라인을 에이전트로 실행" | `/run-chain` |
| "풀 체인 실행", "통합 보고서까지" | `/run-chain` |
| "full chain", "master report" | `/run-chain` |
| "주간 실행" (weekly run) | `/run-chain` |
| "autopilot으로 전부 실행" | `/run-chain` with autopilot enabled |

## Difference from `/run`

| Aspect | `/run` | `/run-chain` |
|---|---|---|
| Layer | Raw Python CLI (`main.py --mode full`) | Agent orchestration + P1 gates |
| Quality gates | None | L0, L1, L1.5, L2 + SG1/SG2/SG3 |
| Evidence Chain | Best effort | Enforced at every boundary |
| Cost | Low (Python only) | High (agent inference + validation) |
| Output | Parquet + SQLite | Parquet + SQLite + Master Integration report (MD + KO) |
| Use case | Daily cron | Weekly deep run / manual audit |

## Failure Modes

- **P1 Suite not operational**: Abort at Step 0, show `validate_p1_scripts.py --check all` output
- **Meta retry budget exhausted**: Meta-Orchestrator escalates to user with `meta_decisions` summary
- **Meta-reviewer verdict = FAIL**: Run marked `completed_with_meta_warnings`; Master report NOT promoted past `candidate`
- **Empty W1 run**: Empty Run Policy escalates to user (preserves D2 Never-Give-Up principle for L1-L5 retries)

## Absolute Standard

Quality over speed. This command may take hours. That is acceptable. What is NOT acceptable is skipping a P1 gate for speed.
