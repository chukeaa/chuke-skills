# OpenClaw Chaining Templates

For eco-council runs, always use the exact role-owned `raw/` artifact path from the fetch plan as `--output`.

## Recon

```text
Use $federal-register-doc-fetch.
Run:
python3 scripts/federal_register_doc_fetch.py check-config --pretty
Return only the JSON result.
```

## Dry Run

```text
Use $federal-register-doc-fetch.
Run:
python3 scripts/federal_register_doc_fetch.py fetch \
  --term "[QUERY_TEXT]" \
  --start-date [YYYY-MM-DD] \
  --end-date [YYYY-MM-DD] \
  --max-pages [N] \
  --max-records [M] \
  --dry-run \
  --pretty
Return only the JSON result.
```

## Fetch

```text
Use $federal-register-doc-fetch.
Run:
python3 scripts/federal_register_doc_fetch.py fetch \
  --term "[QUERY_TEXT]" \
  --start-date [YYYY-MM-DD] \
  --end-date [YYYY-MM-DD] \
  --agency [OPTIONAL_AGENCY_SLUG] \
  --document-type [OPTIONAL_TYPE] \
  --output [OUTPUT_FILE] \
  --pretty
Return the JSON result and confirm `[OUTPUT_FILE]`.
```

## Validate

```text
Use $federal-register-doc-fetch.
Run:
python3 scripts/federal_register_doc_fetch.py fetch \
  --term "[QUERY_TEXT]" \
  --start-date [YYYY-MM-DD] \
  --end-date [YYYY-MM-DD] \
  --max-pages 1 \
  --max-records 10 \
  --output [OUTPUT_FILE] \
  --pretty
Check returned_count and validation_summary.total_issue_count.
Return JSON plus one-line pass/fail verdict and confirm `[OUTPUT_FILE]`.
```
