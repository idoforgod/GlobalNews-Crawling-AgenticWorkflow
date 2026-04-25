---
name: future-outlook-writer
description: WF5 Newspaper specialty agent. Writes future_outlook.html (12,000 words) from Public L3 + W3 + DCI. Inherits 15 principles + CE4 evidence DNA. Prompt template src/newspaper/agent_prompts/specialty.md.
model: opus
tools: Read, Write, Bash, Glob, Grep
maxTurns: 40
---

Full system prompt: `src/newspaper/agent_prompts/specialty.md` (switch by SPECIALTY_ID=future-outlook-writer) + `_dna_newspaper.md`.

## Contract

See specialty.md for detailed role definition. Output path convention:
`newspaper/daily/{date}/drafts/future-outlook-writer.md`

Orchestrator invokes via `scripts/reports/invoke_claude_agent.py --agent future-outlook-writer`.
