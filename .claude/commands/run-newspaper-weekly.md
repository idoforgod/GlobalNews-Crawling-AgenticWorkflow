Execute WF5 Personal Newspaper — weekly edition (ADR-083). Aggregates 7 daily editions + W4 Longitudinal WoW → deep-dive weekly (~205,000 words).

## Precondition

Requires ≥ 4 daily editions in the target ISO week (ADR-083 §13).

## Usage

```bash
python3 scripts/reports/generate_newspaper_weekly.py \
  --week 2026-W16 --project-dir .

# Skeleton
python3 scripts/reports/generate_newspaper_weekly.py \
  --week 2026-W16 --skeleton-only --project-dir .
```

## Output

```
newspaper/weekly/{YYYY-W##}/
├── index.html
├── week_themes.html
├── forward_agenda.html
├── weekly_synthesis.html
├── weekly_synthesis.md
├── newspaper_metadata.json
└── assets/style.css
```

## Validate

```bash
python3 .claude/hooks/scripts/validate_newspaper.py \
  --kind weekly --week 2026-W16 --project-dir .
```

## Cron

Sunday 04:00 slot (inside `run_daily.sh` — dow=7 check triggers Step 7b).
