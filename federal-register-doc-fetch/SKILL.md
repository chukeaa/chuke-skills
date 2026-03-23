---
name: federal-register-doc-fetch
description: Fetch Federal Register document search results with explicit term, publication-window, agency, document-type, topic, docket, and RIN filters, then return structured official-policy records with retries, throttling, pagination, and validation. Use when tasks need authoritative U.S. Federal Register notices, proposed rules, final rules, or presidential documents for policy verification and regulatory context.
---

# Federal Register Doc Fetch

## Core Goal

- Search the Federal Register `documents` API with one bounded request plan.
- Retrieve official notices, proposed rules, final rules, or presidential documents relevant to one mission topic.
- Return machine-readable JSON records with publication dates, agencies, URLs, excerpts, docket IDs, and RIN metadata.
- Keep execution deterministic with retries, throttling, pagination caps, and payload validation.

## Repository Policy

- This is the only Federal Register source skill in this repository.
- Do not create or invoke parallel Federal Register wrappers for the same `documents` endpoint.
- When eco-council or OpenClaw assigns a raw artifact path, write this skill's full JSON payload to that exact path with `--output`.

## Required Environment

- Configure runtime by environment variables in `references/env.md`.
- Start from `assets/config.example.env`.
- Load env values before running commands:

```bash
set -a
source assets/config.example.env
set +a
```

## Workflow

1. Validate effective configuration.

```bash
python3 scripts/federal_register_doc_fetch.py check-config --pretty
```

2. Dry-run the search plan before making remote calls.

```bash
python3 scripts/federal_register_doc_fetch.py fetch \
  --term "wildfire smoke EPA" \
  --start-date 2023-06-01 \
  --end-date 2023-06-10 \
  --agency environmental-protection-agency \
  --document-type NOTICE \
  --max-pages 2 \
  --max-records 20 \
  --dry-run \
  --pretty
```

3. Fetch one bounded search window and write the payload.

```bash
python3 scripts/federal_register_doc_fetch.py fetch \
  --term "wildfire smoke EPA" \
  --start-date 2023-06-01 \
  --end-date 2023-06-10 \
  --agency environmental-protection-agency \
  --document-type NOTICE \
  --output ./data/federal-register-docs.json \
  --pretty
```

4. Use task-specific structured filters when the mission already knows them.

```bash
python3 scripts/federal_register_doc_fetch.py fetch \
  --term "greenhouse gas" \
  --start-date 2024-03-01 \
  --end-date 2024-03-31 \
  --regulation-id-number 3235-AM87 \
  --document-type RULE \
  --document-type PRORULE \
  --max-records 50 \
  --pretty
```

## Output Record Shape

Each item in `records` is one Federal Register document record, typically including:

- `document_number`, `title`, `type`
- `publication_date`, `effective_on`
- `agencies`, `topics`
- `abstract`, `excerpts`
- `html_url`, `pdf_url`, `raw_text_url`, `comment_url`
- `docket_ids`, `regulation_id_numbers`
- `source_query_url`, `source_page_number`

The full raw payload also keeps request metadata, page summaries, and validation output for downstream auditing.

## Scope Boundaries

- This skill targets the Federal Register `documents` search endpoint only.
- This skill does not crawl linked HTML, PDF, or raw-text bodies.
- This skill does not infer legal meaning or summarize regulations.
- This skill does not need an API key.
- This skill is the canonical Federal Register fetch interface for this repository.

## References

- `references/env.md`
- `references/federal-register-api-notes.md`
- `references/federal-register-limitations.md`
- `references/openclaw-chaining-templates.md`

## Script

- `scripts/federal_register_doc_fetch.py`
