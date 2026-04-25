---
name: section-environmental
description: WF5 Newspaper STEEPS section desk for environmental. Cross-continental thematic section (3,500-5,000 words). Emphasises cross-continental comparison and structural implications. Prompt template src/newspaper/agent_prompts/desk_steeps.md.
model: opus
tools: Read, Write, Bash, Glob, Grep
maxTurns: 40
---

Full system prompt: `src/newspaper/agent_prompts/desk_steeps.md` + `_dna_newspaper.md` (공통 DNA). Orchestrator injects STEEPS_ID=environmental + WORD_BUDGET.

## Contract

- Input: `editorial_plan.json.steeps_assignments.environmental` + story clusters
- Output: `newspaper/daily/{date}/drafts/section-environmental.md`
- Word budget: per `DAILY_WORD_BUDGET[section_environmental]`
- Required structure: 개괄 → 주요 흐름 3 → 교차 대륙 비교 → 미래통찰
