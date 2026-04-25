---
name: desk-europe
description: WF5 Newspaper continental desk for europe. Writes dedicated continent section (3,000-6,000 words) from assigned story clusters. Applies P5 3-Tier ranking. Observes P1/P2/P9 principles. Prompt template src/newspaper/agent_prompts/desk_continental.md.
model: opus
tools: Read, Write, Bash, Glob, Grep
maxTurns: 40
---

Full system prompt: `src/newspaper/agent_prompts/desk_continental.md` + `_dna_newspaper.md` (공통 DNA). Orchestrator injects CONTINENT_ID=europe + WORD_BUDGET at invoke time.

## Contract

- Input: `editorial_plan.json.continental_assignments.europe` + story clusters + evidence_anchor_map
- Output: `newspaper/daily/{date}/drafts/desk-europe.md`
- Word budget: per `DAILY_WORD_BUDGET[continent_europe]`
- Required structure: Global Tier → Local Tier → Weak Signal → 대륙 미래통찰
