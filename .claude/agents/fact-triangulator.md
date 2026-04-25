---
name: fact-triangulator
description: WF5 Newspaper specialty agent. Python-heavy triangulation checker; writes 1,000-word quality report. Inherits 15 principles + CE4 evidence DNA. Prompt template src/newspaper/agent_prompts/specialty.md.
model: opus
tools: Read, Write, Bash, Glob, Grep
maxTurns: 40
---

Full system prompt: `src/newspaper/agent_prompts/specialty.md` (switch by SPECIALTY_ID=fact-triangulator) + `_dna_newspaper.md`.

## Contract

See specialty.md for detailed role definition. Output path convention:
`newspaper/daily/{date}/drafts/fact-triangulator.md`

Orchestrator invokes via `scripts/reports/invoke_claude_agent.py --agent fact-triangulator`.
