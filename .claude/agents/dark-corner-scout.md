---
name: dark-corner-scout
description: WF5 Newspaper specialty agent. Writes dark_corners.html — bottom-20% visibility countries (6,000 words). Inherits 15 principles + CE4 evidence DNA. Prompt template src/newspaper/agent_prompts/specialty.md.
model: opus
tools: Read, Write, Bash, Glob, Grep
maxTurns: 40
---

Full system prompt: `src/newspaper/agent_prompts/specialty.md` (switch by SPECIALTY_ID=dark-corner-scout) + `_dna_newspaper.md`.

## Contract

See specialty.md for detailed role definition. Output path convention:
`newspaper/daily/{date}/drafts/dark-corner-scout.md`

Orchestrator invokes via `scripts/reports/invoke_claude_agent.py --agent dark-corner-scout`.
