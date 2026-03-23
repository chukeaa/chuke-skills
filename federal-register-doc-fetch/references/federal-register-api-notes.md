# Federal Register API Notes

## Endpoint

- `GET /documents.json`
- Base URL: `https://www.federalregister.gov/api/v1`
- Authentication: none

## Filters Used by This Skill

- `conditions[term]`
- `conditions[publication_date][gte]`
- `conditions[publication_date][lte]`
- Optional:
  - `conditions[agencies][]`
  - `conditions[type][]`
  - `conditions[topics][]`
  - `conditions[sections][]`
  - `conditions[docket_id]`
  - `conditions[regulation_id_number]`
  - `conditions[significant]`

## Pagination

- `per_page`: accepted `1` to `1000`
- `page`: starts at `1`
- Response commonly includes:
  - `count`
  - `total_pages`
  - `next_page_url`
  - `results`

When no documents match, the API may return only `description` and `count`.

## Ordering

- Supported values documented for `order`:
  - `relevance`
  - `newest`
  - `oldest`
  - `executive_order_number`

## Document Types

- `RULE`
- `PRORULE`
- `NOTICE`
- `PRESDOCU`

## Useful Response Fields

- `title`
- `type`
- `abstract`
- `document_number`
- `html_url`
- `pdf_url`
- `publication_date`
- `effective_on`
- `agencies`
- `topics`
- `excerpts`
- `docket_ids`
- `regulation_id_numbers`
- `comment_url`
- `raw_text_url`

The skill requests an explicit field subset to keep payload size predictable.
