#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Generate a hotspot summary report from journals data.

Workflow:
1) Fetch title + DOI candidates in a rolling time window.
2) Use LLM to select topic-relevant DOIs with JSON schema output.
3) Fetch abstracts for selected DOIs.
4) In `end2end` mode, use LLM to synthesize cross-paper hotspots (EN first, ZH second).
5) In `selected-abstract-only` mode, export selected rows as JSON for agent-side summarization.
"""

import argparse
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

DB_REQUIRED_ENV_KEYS = {
    "host": "KB_DB_HOST",
    "port": "KB_DB_PORT",
    "database": "KB_DB_NAME",
    "user": "KB_DB_USER",
    "password": "KB_DB_PASSWORD",
}

DEFAULT_OPENAI_MODEL = "gpt-5-mini"
DEFAULT_TOPICS = ["carbon emissions", "lca", "environmental impact"]
SELECTION_BATCH_SIZE = 50
SELECTION_API_MAX_RETRIES = 3
SELECTION_API_RETRY_DELAY_SECONDS = 1.0


def _load_dotenv_if_exists() -> None:
    env_candidates = [Path.cwd() / ".env", Path(__file__).resolve().parent.parent / ".env"]
    env_path = next((p for p in env_candidates if p.exists()), None)
    if not env_path:
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue

        if len(value) >= 2 and ((value[0] == '"' and value[-1] == '"') or (value[0] == "'" and value[-1] == "'")):
            value = value[1:-1]

        if value == "":
            continue

        if key not in os.environ:
            os.environ[key] = value


def _normalize_doi(doi: str) -> str:
    return doi.strip().lower().rstrip(".,;")


def _get_db_config() -> Dict[str, object]:
    missing = [env_key for env_key in DB_REQUIRED_ENV_KEYS.values() if not os.environ.get(env_key, "").strip()]
    if missing:
        raise RuntimeError(
            "Missing database env variables: " + ", ".join(missing) + ". You can set them in .env."
        )

    port_raw = os.environ[DB_REQUIRED_ENV_KEYS["port"]].strip()
    try:
        port = int(port_raw)
    except ValueError as exc:
        raise RuntimeError(f"KB_DB_PORT must be an integer, got: {port_raw!r}") from exc

    return {
        "host": os.environ[DB_REQUIRED_ENV_KEYS["host"]].strip(),
        "port": port,
        "database": os.environ[DB_REQUIRED_ENV_KEYS["database"]].strip(),
        "user": os.environ[DB_REQUIRED_ENV_KEYS["user"]].strip(),
        "password": os.environ[DB_REQUIRED_ENV_KEYS["password"]].strip(),
    }


def _load_title_doi_rows(start_time: datetime) -> List[Dict]:
    try:
        import psycopg2
    except Exception as exc:
        raise RuntimeError("Missing psycopg2 SDK. Install with `pip install psycopg2-binary`.") from exc

    db_cfg = _get_db_config()
    with psycopg2.connect(**db_cfg) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, journal, title, doi, created_at
            FROM journals
            WHERE created_at >= %s
            ORDER BY created_at DESC
            """,
            (start_time,),
        )
        rows = cur.fetchall()

    cols = ["id", "journal", "title", "doi", "created_at"]
    norm_rows = []
    for r in rows:
        d = dict(zip(cols, r))
        if d.get("created_at") is not None:
            d["created_at"] = d["created_at"].isoformat()
        norm_rows.append(d)
    return norm_rows


def _load_rows_by_dois(start_time: datetime, dois: Sequence[str]) -> List[Dict]:
    if not dois:
        return []

    try:
        import psycopg2
    except Exception as exc:
        raise RuntimeError("Missing psycopg2 SDK. Install with `pip install psycopg2-binary`.") from exc

    db_cfg = _get_db_config()
    placeholders = ", ".join(["%s"] * len(dois))
    params: List[object] = [start_time, *dois]

    with psycopg2.connect(**db_cfg) as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, journal, title, authors, doi, date, created_at, abstract
            FROM journals
            WHERE created_at >= %s AND doi IN ({placeholders})
            ORDER BY created_at DESC
            """,
            tuple(params),
        )
        rows = cur.fetchall()

    cols = ["id", "journal", "title", "authors", "doi", "date", "created_at", "abstract"]
    norm_rows = []
    for r in rows:
        d = dict(zip(cols, r))
        if d.get("created_at") is not None:
            d["created_at"] = d["created_at"].isoformat()
        norm_rows.append(d)
    return norm_rows


def _extract_response_text(response) -> str:
    if hasattr(response, "output_text") and response.output_text:
        return response.output_text
    output = getattr(response, "output", None)
    if not output:
        return str(response)

    texts = []
    for item in output:
        if isinstance(item, dict):
            if item.get("type") == "output_text" and item.get("text"):
                texts.append(item["text"])
            for content in item.get("content", []) or []:
                if content.get("type") == "output_text" and content.get("text"):
                    texts.append(content["text"])
    return "\n".join(texts).strip()


def _build_openai_client():
    try:
        from openai import OpenAI
    except Exception as exc:
        raise RuntimeError("Missing openai SDK. Install with `pip install openai`.") from exc

    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY is required for LLM calls. You can set it in .env."
        )

    base_url = os.environ.get("OPENAI_BASE_URL", "").strip()
    client_kwargs = {"api_key": api_key}
    if base_url:
        client_kwargs["base_url"] = base_url
    return OpenAI(**client_kwargs)


def _openai_generate_text(client, model: str, prompt: str) -> str:
    try:
        response = client.responses.create(
            model=model,
            input=[{"role": "user", "content": prompt}],
        )
        return _extract_response_text(response)
    except Exception as exc:
        if "404" not in str(exc) and "Not Found" not in str(exc):
            raise

    completion = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
    )
    return (completion.choices[0].message.content or "").strip()


def _generate_text(model: str, prompt: str) -> str:
    client = _build_openai_client()
    return _openai_generate_text(client, model, prompt)


def _resolve_model() -> str:
    return DEFAULT_OPENAI_MODEL


def _prepare_doi_candidates(rows: List[Dict]) -> List[Dict]:
    candidates = []
    for row in rows:
        doi = (row.get("doi") or "").strip()
        if not doi:
            continue
        candidates.append(
            {
                "doi": doi,
                "title": (row.get("title") or "").strip(),
            }
        )
    return candidates


def _build_relevance_prompt(candidates: List[Dict], topics: List[str]) -> str:
    entries = []
    for i, item in enumerate(candidates, start=1):
        title = item["title"].replace("\n", " ").strip()
        doi = item["doi"]
        entries.append(f"{i}. DOI: {doi}\n   Title: {title}")

    return (
        "You are a paper relevance classifier.\n"
        f"User topics: {', '.join(topics)}\n"
        "Task: Select candidate papers that are truly relevant to the user topics using DOI + title information.\n"
        "Output rules:\n"
        "- Return strict JSON only.\n"
        "- JSON must match the provided schema exactly.\n"
        "- DOI must be copied exactly from the candidates.\n\n"
        "Candidates:\n"
        + "\n".join(entries)
    )


SELECTION_JSON_SCHEMA = {
    "name": "doi_selection",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "selected_dois": {
                "type": "array",
                "items": {"type": "string"},
            }
        },
        "required": ["selected_dois"],
        "additionalProperties": False,
    },
}


def _extract_json_payload(raw_text: str) -> Dict:
    text = raw_text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)

    try:
        payload = json.loads(text)
        if isinstance(payload, dict):
            return payload
    except Exception:
        pass

    match = re.search(r"\{.*\}", raw_text, flags=re.DOTALL)
    if not match:
        raise RuntimeError("LLM did not return a valid JSON object for DOI selection.")
    try:
        payload = json.loads(match.group(0))
    except Exception as exc:
        raise RuntimeError("Failed to parse DOI selection JSON from LLM output.") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("DOI selection JSON must be an object.")
    return payload


def _generate_selection_payload_openai(model: str, prompt: str) -> Dict:
    client = _build_openai_client()
    try:
        completion = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            response_format={
                "type": "json_schema",
                "json_schema": SELECTION_JSON_SCHEMA,
            },
        )
        content = completion.choices[0].message.content or "{}"
        return _extract_json_payload(content)
    except Exception:
        fallback_prompt = (
            f"{prompt}\n\n"
            "Return JSON only with this schema:\n"
            '{"selected_dois": ["10.xxxx/xxxx", "..."]}\n'
            "Do not output markdown or explanations."
        )
        text = _openai_generate_text(client, model, fallback_prompt)
        return _extract_json_payload(text)


def _generate_selection_payload(model: str, prompt: str) -> Dict:
    return _generate_selection_payload_openai(model, prompt)


def _parse_selected_dois(payload: Dict, candidate_dois: Sequence[str]) -> List[str]:
    if set(payload.keys()) != {"selected_dois"}:
        raise RuntimeError("DOI selection JSON must only contain: selected_dois")

    raw_selected = payload.get("selected_dois")
    if not isinstance(raw_selected, list):
        raise RuntimeError("DOI selection JSON field `selected_dois` must be an array.")

    doi_map = {_normalize_doi(doi): doi for doi in candidate_dois}
    selected = []
    seen = set()
    for item in raw_selected:
        if not isinstance(item, str):
            raise RuntimeError("Every `selected_dois` item must be a string DOI.")
        norm = _normalize_doi(item)
        if norm in doi_map and norm not in seen:
            seen.add(norm)
            selected.append(doi_map[norm])
    return selected


def _select_relevant_dois(
    title_doi_rows: List[Dict],
    topics: List[str],
    model: str,
) -> Tuple[List[str], int]:
    candidates = _prepare_doi_candidates(title_doi_rows)
    candidate_dois = [item["doi"] for item in candidates]

    if not candidate_dois:
        return [], 0

    selected: List[str] = []
    seen = set()
    for i in range(0, len(candidates), SELECTION_BATCH_SIZE):
        batch = candidates[i : i + SELECTION_BATCH_SIZE]
        batch_dois = [item["doi"] for item in batch]
        prompt = _build_relevance_prompt(batch, topics)
        batch_selected: List[str] = []
        for attempt in range(1, SELECTION_API_MAX_RETRIES + 1):
            try:
                payload = _generate_selection_payload(model, prompt)
                batch_selected = _parse_selected_dois(payload, batch_dois)
                break
            except Exception as exc:
                if attempt == SELECTION_API_MAX_RETRIES:
                    batch_start = i + 1
                    batch_end = i + len(batch)
                    raise RuntimeError(
                        f"Batch DOI selection failed after {SELECTION_API_MAX_RETRIES} attempts "
                        f"for candidates {batch_start}-{batch_end}."
                    ) from exc
                time.sleep(SELECTION_API_RETRY_DELAY_SECONDS * attempt)
        for doi in batch_selected:
            norm = _normalize_doi(doi)
            if norm in seen:
                continue
            seen.add(norm)
            selected.append(doi)

    return selected, len(candidate_dois)


def _build_summary_prompt(rows: List[Dict], topics: List[str]) -> str:
    entries = []
    for i, row in enumerate(rows, start=1):
        title = (row.get("title") or "").replace("\n", " ").strip()
        journal = (row.get("journal") or "").replace("\n", " ").strip()
        doi = (row.get("doi") or "").strip()
        abstract = (row.get("abstract") or "").strip() or "[No abstract provided]"
        entries.append(
            f"[{i}] DOI: {doi}\n"
            f"Title: {title}\n"
            f"Journal: {journal}\n"
            f"Abstract: {abstract}\n"
        )

    topic_focus = (
        f"Focus on trends related to: {', '.join(topics)}. Keep broader context only when needed."
        if topics
        else "Infer overall shared hotspots and trends from the selected papers."
    )

    return (
        "You are a research trend analyst.\n"
        "Based on the paper records below, synthesize shared hotspots and trends across papers.\n"
        "Requirements:\n"
        "- Output 3-6 markdown bullets.\n"
        "- Each bullet must be exactly one line in this format:\n"
        "  - EN: <concise English insight> | ZH: <concise Chinese insight>\n"
        "- English must come first, Chinese second.\n"
        "- Do not write per-paper summaries.\n"
        "- Do not quote abstracts verbatim; paraphrase only.\n"
        "- Keep wording concrete and cross-paper.\n"
        f"- {topic_focus}\n\n"
        "Papers:\n"
        + "\n".join(entries)
    )


def _theme_summary_llm(rows: List[Dict], topics: List[str], model: str) -> List[str]:
    prompt = _build_summary_prompt(rows, topics)
    text = _generate_text(model, prompt)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    bullets = [line for line in lines if line.startswith("-")]
    if bullets:
        return bullets
    if lines:
        return [f"- {line}" if not line.startswith("-") else line for line in lines[:6]]
    return ["- EN: Trend synthesis failed. | ZH: 趋势总结生成失败。"]


def _representative_items(rows: List[Dict], max_items: int = 10) -> List[Tuple[str, str]]:
    return [((r.get("title") or "").strip(), (r.get("journal") or "").strip()) for r in rows[:max_items]]


def _format_report(
    rows_for_summary: List[Dict],
    window_start: datetime,
    window_end: datetime,
    topics: List[str],
    days: int,
    total_window_rows: int,
    candidate_doi_count: int,
    selected_doi_count: int,
    theme_lines: List[str],
) -> str:
    journal_count = len({r.get("journal") for r in rows_for_summary if r.get("journal")})
    rep_items = _representative_items(rows_for_summary)
    rep_list = (
        "\n".join([f"- EN: {title} ({journal}) | ZH: {title}（{journal}）" for title, journal in rep_items])
        if rep_items
        else "- EN: No representative items extracted. | ZH: 本期未提取代表性条目。"
    )

    header = [
        "# Journal Hotspot Summary Report / 期刊热点摘要报告",
        f"**Time Window (UTC)**: {window_start.strftime('%Y-%m-%d %H:%M UTC')} to {window_end.strftime('%Y-%m-%d %H:%M UTC')} (last {days} days)",
        f"**统计时间段（UTC）**：{window_start.strftime('%Y-%m-%d %H:%M UTC')} 至 {window_end.strftime('%Y-%m-%d %H:%M UTC')}（最近 {days} 天）",
        f"**Topic Filter**: {', '.join(topics) if topics else 'Not set (all articles)'}",
        f"**主题过滤**：{', '.join(topics) if topics else '未设置（全部文章）'}",
        f"**Articles In Window**: {total_window_rows} | **DOI Candidates**: {candidate_doi_count} | **Selected DOIs**: {selected_doi_count}",
        f"**窗口内文章数**：{total_window_rows} 篇 | **可筛选 DOI**：{candidate_doi_count} | **命中 DOI**：{selected_doi_count}",
        f"**Summarized Articles**: {len(rows_for_summary)} | **Journals**: {journal_count}",
        f"**用于总结的文章数**：{len(rows_for_summary)} 篇 | 涉及期刊：{journal_count} 种",
    ]

    if topics and selected_doi_count == 0:
        header.append("**Note**: LLM found no DOI relevant to the provided topics in this time window.")
        header.append("**提示**：LLM 在该时间窗口内未筛选到与主题相关的 DOI。")

    body = [
        "---",
        "",
        "## Shared Hotspots and Trends",
        "## 本期共性与趋势",
        *(theme_lines if theme_lines else ["- EN: Trend synthesis failed. | ZH: 趋势总结生成失败。"]),
        "",
        "## Representative Items (Optional)",
        "## 代表性条目（可选）",
        rep_list,
    ]
    return "\n".join(header + body)


def _to_json_safe(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_to_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _to_json_safe(v) for k, v in value.items()}
    return str(value)


def _build_selected_abstract_payload(
    rows_for_summary: List[Dict],
) -> List[Dict]:
    items = []
    for row in rows_for_summary:
        items.append(
            {
                "doi": (row.get("doi") or "").strip(),
                "title": (row.get("title") or "").strip(),
                "journal": (row.get("journal") or "").strip(),
                "date": _to_json_safe(row.get("date")),
                "abstract": _to_json_safe(row.get("abstract")),
            }
        )

    return items


def main() -> int:
    _load_dotenv_if_exists()

    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument(
        "--topics",
        type=str,
        default="",
        help='Comma-separated list of topics, e.g. "AI, machine learning"',
    )
    parser.add_argument(
        "--openai-base-url",
        type=str,
        default="",
        help="Optional OpenAI-compatible base URL. Overrides OPENAI_BASE_URL for this run.",
    )
    parser.add_argument(
        "--mode",
        type=str,
        choices=["end2end", "selected-abstract-only"],
        default="end2end",
        help="Run full report generation (`end2end`) or export selected abstracts for agent-side summarization (`selected-abstract-only`).",
    )
    parser.add_argument("--output", type=str, default="report_last_7d.md")
    parser.add_argument(
        "--selected-output",
        type=str,
        default="selected-abstract.json",
        help="Output path for `--mode selected-abstract-only`.",
    )
    args = parser.parse_args()

    if args.openai_base_url.strip():
        os.environ["OPENAI_BASE_URL"] = args.openai_base_url.strip()

    topics = [t.strip() for t in args.topics.split(",") if t.strip()]
    if not topics:
        topics = list(DEFAULT_TOPICS)

    window_end = datetime.now(timezone.utc)
    window_start = window_end - timedelta(days=args.days)

    title_doi_rows = _load_title_doi_rows(window_start)
    if not title_doi_rows:
        if args.mode == "selected-abstract-only":
            payload = _build_selected_abstract_payload(rows_for_summary=[])
            with open(args.selected_output, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        else:
            report = "\n".join(
                [
                    "# Journal Hotspot Summary Report / 期刊热点摘要报告",
                    f"**Time Window (UTC)**: {window_start.strftime('%Y-%m-%d %H:%M UTC')} to {window_end.strftime('%Y-%m-%d %H:%M UTC')} (last {args.days} days)",
                    f"**统计时间段（UTC）**：{window_start.strftime('%Y-%m-%d %H:%M UTC')} 至 {window_end.strftime('%Y-%m-%d %H:%M UTC')}（最近 {args.days} 天）",
                    f"**Topic Filter**: {', '.join(topics)}",
                    f"**主题过滤**：{', '.join(topics)}",
                    "",
                    "No rows found in this time window. Try increasing DAYS.",
                    "该时间窗口内未检索到文章，请尝试增大 DAYS。",
                ]
            )
            with open(args.output, "w", encoding="utf-8") as f:
                f.write(report)
        return 0

    model = _resolve_model()
    selected_dois, candidate_doi_count = _select_relevant_dois(
        title_doi_rows=title_doi_rows,
        topics=topics,
        model=model,
    )

    rows_for_summary = _load_rows_by_dois(window_start, selected_dois)
    if args.mode == "selected-abstract-only":
        payload = _build_selected_abstract_payload(rows_for_summary=rows_for_summary)
        with open(args.selected_output, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return 0

    if rows_for_summary:
        theme_lines = _theme_summary_llm(rows_for_summary, topics, model)
    else:
        theme_lines = [
            "- EN: No topic-relevant papers were selected in this time window. | ZH: 当前时间窗口内未筛选到与主题相关的论文。"
        ]

    report = _format_report(
        rows_for_summary=rows_for_summary,
        window_start=window_start,
        window_end=window_end,
        topics=topics,
        days=args.days,
        total_window_rows=len(title_doi_rows),
        candidate_doi_count=candidate_doi_count,
        selected_doi_count=len(selected_dois),
        theme_lines=theme_lines,
    )

    with open(args.output, "w", encoding="utf-8") as f:
        f.write(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
