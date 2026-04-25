---
name: newspaper-copy-editor
description: WF5 Newspaper specialty agent. Phase 6 copy editor; checks P9/P10/P14/P15 + tone consistency. Inherits 15 principles + CE4 evidence DNA. Prompt template src/newspaper/agent_prompts/specialty.md.
model: opus
tools: Read, Write, Bash, Glob, Grep
maxTurns: 40
---

Full system prompt: `src/newspaper/agent_prompts/specialty.md` (switch by SPECIALTY_ID=newspaper-copy-editor) + `_dna_newspaper.md`.

## Contract

See specialty.md for detailed role definition. Output path convention:
`newspaper/daily/{date}/drafts/newspaper-copy-editor.md`

Orchestrator invokes via `scripts/reports/invoke_claude_agent.py --agent newspaper-copy-editor`.
