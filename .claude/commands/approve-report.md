Approve a candidate Master Integration report and promote it to final.

## Purpose

Manual promotion gate for Master Integration reports when autopilot is OFF. Moves a candidate report from `reports/candidate/` to `reports/final/`.

## When This Command Is Needed

If the Meta-Orchestrator finishes W4 Master Integration with autopilot OFF, the report will be left in `reports/candidate/` awaiting human approval. This command performs that approval step.

If autopilot is ON, reports are promoted automatically and this command is a no-op.

## Execution Protocol

### Step 1: Verify Candidate Exists

```bash
CANDIDATE=$(ls reports/candidate/integrated-report-*.md 2>/dev/null | head -1)
if [ -z "$CANDIDATE" ]; then
  echo "No candidate report found. Run /integrate-results first."
  exit 1
fi
echo "Candidate: $CANDIDATE"
```

### Step 2: Re-verify P1 Gates (Safety Check)

Before promoting, re-run the key validators:

```bash
python3 scripts/execution/p1/master_assembly.py --check structure \
  --report "$CANDIDATE"
```

```bash
python3 scripts/execution/p1/evidence_chain.py --check master_chain \
  --report "$CANDIDATE" \
  --jsonl data/raw/$(date +%Y-%m-%d)/all_articles.jsonl
```

Both must exit 0.

### Step 3: Display the Candidate Report

Show the user the content of the candidate so they can review before approving:

```bash
cat "$CANDIDATE"
```

### Step 4: Confirm with User

Unless the user already explicitly requested approval in the current turn, ask:

> Promote this candidate to final? The report will be moved to `reports/final/` and recorded in `execution.history`.

### Step 5: Promote

```bash
DATE=$(basename "$CANDIDATE" | sed 's/integrated-report-//;s/\.md$//')
FINAL="reports/final/integrated-report-${DATE}.md"
mkdir -p reports/final
mv "$CANDIDATE" "$FINAL"
```

Also promote the Korean translation if it exists:
```bash
CANDIDATE_KO="reports/candidate/integrated-report-${DATE}.ko.md"
if [ -f "$CANDIDATE_KO" ]; then
  mv "$CANDIDATE_KO" "reports/final/integrated-report-${DATE}.ko.md"
fi
```

### Step 6: Record in SOT

Record the promotion in `execution.runs.{run_id}.meta_decisions`:

```bash
python3 scripts/sot_manager.py --atomic-write --actor meta \
  --path execution.runs.$(python3 scripts/sot_manager.py --read --project-dir . | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["workflow"].get("_placeholder",""))').meta_decisions \
  --append-list \
  --value '{"ts":"'$(date -u +%Y-%m-%dT%H:%M:%SZ)'","decision":"promote_master_report","gate_result":"PASS","from":"candidate","to":"final"}' \
  --project-dir .
```

(Note: the actual implementation by the Meta-Orchestrator should look up the current run_id and write properly. The above is a representative example.)

### Step 7: Report

Show the user:
- Final report location
- Report size (bytes)
- Any meta warnings from the run
- Invitation to view: `open reports/final/integrated-report-${DATE}.md`

## Safety Rules

- NEVER promote a candidate that failed SG3 or the evidence chain re-verification
- NEVER promote if the run is marked `completed_with_meta_warnings` without explicit user confirmation of the warnings
- NEVER delete the candidate without moving it (the `mv` command ensures this)

## Mapping User Intent

| User says | Action |
|---|---|
| "보고서 승인" | `/approve-report` |
| "candidate를 final로 승급" | `/approve-report` |
| "approve the master report" | `/approve-report` |
