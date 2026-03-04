---
name: kb-hotspot-summary
description: Generate a bilingual (English first, Chinese second) hotspot summary report from PostgreSQL `journals` data using a rolling DAYS window and a two-stage OpenAI pipeline (title+DOI relevance selection with JSON schema output, then abstract-based trend synthesis). Use when users need topic-driven cross-paper shared themes from recent uploads; if no topic is provided, use default carbon-emissions/LCA/environmental-impact topics.
---

# KB Hotspot Summary

## Overview

Generate a hotspot trend report from recent records in `journals` with topic-driven filtering. Output a bilingual digest (EN first, ZH second) with counts and synthesized cross-paper trends (no per-paper summaries).

## Inputs

- `DAYS`: Integer, default `7`. Query articles uploaded in the last N days.
- `TOPICS`: Comma-separated string. Prefer user input; if empty, use default topics: `carbon emissions, lca, environmental impact`.

## Workflow

1. Load `references/journal_db_report.md` for the canonical prompt, schema, SQL, and report template.
2. Read DB connection values from the same env contract as `kb-meta-fetch`: `KB_DB_HOST`, `KB_DB_PORT`, `KB_DB_NAME`, `KB_DB_USER`, `KB_DB_PASSWORD`.
3. Query candidate records by `created_at >= now_utc - DAYS` and fetch `title` + `doi` candidates.
4. Use LLM to select topic-relevant DOI values from candidates (`doi` + `title` only), sent in batches of 50 records per call, with up to 3 retries per batch. LLM output must follow JSON schema.
5. Query `abstract` rows by the selected DOI set (same time window).
6. If `--mode end2end` (default): use OpenAI LLM to synthesize shared themes/methods/trends across selected rows, then format bilingual report.
7. If `--mode selected-abstract-only`: skip LLM summary and export selected abstract rows to JSON for agent-side summarization.
8. Display report time window as `now_utc - DAYS` to `now_utc` (not min/max row timestamps).

## Script

Use `scripts/run_report.py` to generate a report. Stage 1 always uses OpenAI LLM.

Prepare env file in skill root (`kb-hotspot-summary/.env`):

```dotenv
KB_DB_HOST=<HOST>
KB_DB_PORT=5432
KB_DB_NAME=<DATABASE>
KB_DB_USER=<USER>
KB_DB_PASSWORD=<PASSWORD>
OPENAI_API_KEY=<OPENAI_API_KEY>
OPENAI_BASE_URL=
```

Example:

```powershell
cd kb-hotspot-summary
python scripts/run_report.py --days 7 --topics "AI, machine learning" --output report_last_7d.md
python scripts/run_report.py --days 7 --output report_last_7d.md
python scripts/run_report.py --mode selected-abstract-only --days 7 --topics "AI" --selected-output selected-abstract.json
# no --topics: use default topics (carbon emissions, lca, environmental impact)
python scripts/run_report.py --mode selected-abstract-only --days 7 --selected-output selected-abstract.json
```

LLM requirements:

- `OPENAI_API_KEY` + `openai` SDK are required.
- Stage 1 DOI selection always uses OpenAI and requires JSON schema output.
- The script uses fixed OpenAI model `gpt-5-mini`.
- Optional: set `OPENAI_BASE_URL` (or `--openai-base-url`) for OpenAI-compatible gateway.

## Output Rules

- Do not output any database password or raw `abstract` content verbatim.
- If no rows are found, notify the user and suggest increasing `DAYS`.
- If no DOI is selected, report no topic-relevant papers in the window (do not fallback to all rows).
- Stage 1 DOI selection response must be JSON schema-shaped (`{"selected_dois": [...]}`).
- In `selected-abstract-only` mode, write `selected-abstract.json` as a JSON array of selected rows, each with exactly: `doi`, `title`, `journal`, `date`, `abstract`.
- Keep trend bullets bilingual with `EN` then `ZH`.
- Keep report time window fixed to `now_utc - DAYS` through `now_utc`.
- Ensure report files are written as UTF-8 to avoid `?` garbling on Windows.

## Resources

- `references/journal_db_report.md`: Detailed prompt, DB schema, two-stage filtering/summarization logic, and report format.
