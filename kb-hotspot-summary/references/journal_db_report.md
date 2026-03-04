# Journal DB Hotspot Summary Reference

Use this reference as the canonical specification for the workflow.

## Variables

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `DAYS` | Integer | `7` | Query articles uploaded in the last N days |
| `TOPICS` | String | `"carbon emissions, lca, environmental impact"` | Comma-separated topics. Prefer user input; if empty, use default topics |

## Database Connection

Use the same DB env contract as `kb-meta-fetch`:

- `KB_DB_HOST`
- `KB_DB_PORT`
- `KB_DB_NAME`
- `KB_DB_USER`
- `KB_DB_PASSWORD`

LLM env requirement:

- `OPENAI_API_KEY` (required)
- `OPENAI_BASE_URL` (optional, only for OpenAI-compatible gateways)

Never print or log credentials.

## Table Schema: `journals`

- `id`: UUID
- `journal`: Journal name
- `title`: Paper title
- `authors`: JSON array, e.g. `["张三", "李四"]`
- `doi`: DOI identifier
- `date`: Publication year-month (e.g. `"2023-08"`)
- `created_at`: timestamptz (used for time filtering; always use this field)
- `abstract`: Original abstract text (nullable)

## Query Stage 1: Candidate Fetch (title + DOI)

```python
from datetime import datetime, timedelta, timezone
import psycopg2

days = int(DAYS) if DAYS else 7
window_end = datetime.now(timezone.utc)
window_start = window_end - timedelta(days=days)

conn = psycopg2.connect(
    host=KB_DB_HOST,
    port=int(KB_DB_PORT),
    database=KB_DB_NAME,
    user=KB_DB_USER,
    password=KB_DB_PASSWORD,
)
cursor = conn.cursor()
cursor.execute(
    """
    SELECT id, journal, title, doi, created_at
    FROM journals
    WHERE created_at >= %s
    ORDER BY created_at DESC
    """,
    (window_start,),
)
rows = cursor.fetchall()
```

## Topic Filtering (LLM-based DOI Selection)

- Always apply topic filtering.
- If user does not provide topics, use default topics: `carbon emissions, lca, environmental impact`.
- Use OpenAI (`gpt-5-mini`) to select relevant DOI values based on candidate `title` + `doi` only.
- Send candidates to LLM in batches of 50 records per call, with up to 3 retries per batch, then merge all selected DOI values.
- Keep only DOI values selected by LLM.
- If selected DOI count is zero, report no topic-relevant papers in this window.

### Required LLM Output Schema

```json
{
  "selected_dois": ["10.xxxx/xxxx", "10.xxxx/yyyy"]
}
```

- The response must be strict JSON object.
- The only allowed key is `selected_dois`.
- `selected_dois` must be an array of DOI strings copied from candidates.

## Query Stage 2: Fetch Abstract by Selected DOI

```python
selected_dois = [...]  # returned by LLM in stage 1
if selected_dois:
    placeholders = ", ".join(["%s"] * len(selected_dois))
    cursor.execute(
        f"""
        SELECT id, journal, title, authors, doi, date, created_at, abstract
        FROM journals
        WHERE created_at >= %s AND doi IN ({placeholders})
        ORDER BY created_at DESC
        """,
        (window_start, *selected_dois),
    )
    rows = cursor.fetchall()
```

## Execution Modes

- `end2end` (default): Continue with OpenAI trend synthesis and output Markdown report.
- `selected-abstract-only`: Do not run trend synthesis. Output selected rows as `selected-abstract.json` for agent-side summarization.

`selected-abstract.json` should be a JSON array:

```json
[
  {
    "doi": "...",
    "title": "...",
    "journal": "...",
    "date": "...",
    "abstract": "..."
  }
]
```

## Hotspot Summary Rules (for `end2end`)

- Summarize shared themes, methods, and trends across all selected papers.
- Focus on 3-6 major trends and notable shifts.
- Do not produce per-paper summaries.
- Use `title`, `journal`, and `abstract` as evidence; paraphrase only.
- Output must be bilingual with English first and Chinese second.
- Trend bullets format: `- EN: ... | ZH: ...`.

## Output Report Format

```markdown
# Journal Hotspot Summary Report / 期刊热点摘要报告
**Time Window (UTC)**: [window_start] to [window_end] (last {{DAYS}} days)
**统计时间段（UTC）**：[window_start] 至 [window_end]（最近 {{DAYS}} 天）
**Topic Filter**: [user topics or default topics]
**主题过滤**：[用户主题或默认主题]
**Selection Pipeline**: LLM filter by title+DOI, then fetch abstracts by selected DOI
**窗口内文章数**：X 篇 | **可筛选 DOI**：X | **命中 DOI**：X
**Summarized Articles**: X | **Journals**: X
**用于总结的文章数**：X 篇 | 涉及期刊：X 种

## Shared Hotspots and Trends
## 本期共性与趋势
- EN: ... | ZH: ...

## Representative Items (Optional)
## 代表性条目（可选）
- EN: [title] ([journal]) | ZH: [title]（[journal]）
```

## Supplementary Rules

1. Do not return raw `abstract` content.
2. If zero rows in the time range, suggest increasing `DAYS`.
3. Report time window must be `now_utc - DAYS` to `now_utc`.
4. Never output database passwords in any form.
5. Always write report files with UTF-8 encoding.
6. Do not use keyword/fuzzy fallback to all rows for topic filtering.
7. In `selected-abstract-only`, keep selected rows in JSON output even when count is zero.
