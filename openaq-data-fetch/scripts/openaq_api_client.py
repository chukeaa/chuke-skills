#!/usr/bin/env python3
"""Generic OpenAQ API v3 client with optional pagination merge."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib import parse, request
from urllib.error import HTTPError, URLError

ENV_API_KEY = "OPENAQ_API_KEY"
DEFAULT_BASE_URL = "https://api.openaq.org"


def env_required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value


def parse_key_value_args(items: list[str]) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise ValueError(
                f"Invalid query pair '{item}'. Use key=value format, for example limit=100."
            )
        key, value = item.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            raise ValueError(f"Invalid query pair '{item}'. Key cannot be empty.")
        pairs[key] = value
    return pairs


def merge_query(url: str, query: dict[str, str]) -> str:
    split = parse.urlsplit(url)
    existing = dict(parse.parse_qsl(split.query, keep_blank_values=True))
    existing.update(query)
    encoded = parse.urlencode(existing)
    return parse.urlunsplit((split.scheme, split.netloc, split.path, encoded, split.fragment))


def resolve_url(base_url: str, path: str, query: dict[str, str]) -> str:
    path_clean = path.strip()
    if not path_clean:
        raise ValueError("Path cannot be empty.")

    if path_clean.startswith("http://") or path_clean.startswith("https://"):
        return merge_query(path_clean, query)

    if not path_clean.startswith("/"):
        path_clean = "/" + path_clean
    base = base_url.rstrip("/")
    return merge_query(base + path_clean, query)


def parse_error_payload(error: HTTPError) -> str:
    try:
        text = error.read().decode("utf-8", errors="replace")
    except Exception:
        text = ""

    if not text:
        return f"HTTP {error.code}: {error.reason}"

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return f"HTTP {error.code}: {text}"

    if isinstance(data, dict):
        message = data.get("message")
        if isinstance(message, str) and message.strip():
            return f"HTTP {error.code}: {message.strip()}"
        detail = data.get("detail")
        if isinstance(detail, str) and detail.strip():
            return f"HTTP {error.code}: {detail.strip()}"
    return f"HTTP {error.code}: {text}"


def fetch_json(url: str, api_key: str, timeout: int) -> tuple[dict[str, Any], dict[str, str]]:
    req = request.Request(url, method="GET")
    req.add_header("Accept", "application/json")
    req.add_header("X-API-Key", api_key)

    try:
        with request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            headers = {k.lower(): v for k, v in resp.headers.items()}
    except HTTPError as exc:
        raise RuntimeError(parse_error_payload(exc)) from exc
    except URLError as exc:
        raise RuntimeError(f"Request failed: {exc}") from exc
    except TimeoutError as exc:
        raise RuntimeError("Request timed out.") from exc

    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError("API response is not valid JSON.") from exc

    if not isinstance(data, dict):
        raise RuntimeError("API response must be a JSON object.")
    return data, headers


def fetch_one_page(
    *,
    base_url: str,
    path: str,
    query: dict[str, str],
    api_key: str,
    timeout: int,
) -> tuple[dict[str, Any], dict[str, str], str]:
    url = resolve_url(base_url, path, query)
    payload, headers = fetch_json(url, api_key=api_key, timeout=timeout)
    return payload, headers, url


def to_int(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return int(value)
        except ValueError:
            return None
    return None


def fetch_all_pages(
    *,
    base_url: str,
    path: str,
    query: dict[str, str],
    api_key: str,
    timeout: int,
    max_pages: int,
) -> dict[str, Any]:
    if max_pages < 1:
        raise ValueError("--max-pages must be >= 1.")

    work_query = dict(query)
    if "limit" not in work_query:
        work_query["limit"] = "1000"

    start_page = to_int(work_query.get("page")) or 1
    page = start_page
    merged_results: list[Any] = []
    page_trace: list[dict[str, Any]] = []
    found_total: int | None = None
    first_meta: dict[str, Any] | None = None

    for _ in range(max_pages):
        work_query["page"] = str(page)
        payload, headers, url = fetch_one_page(
            base_url=base_url,
            path=path,
            query=work_query,
            api_key=api_key,
            timeout=timeout,
        )

        results = payload.get("results")
        if not isinstance(results, list):
            raise RuntimeError("Response does not contain 'results' list; cannot merge pages.")

        if first_meta is None:
            meta = payload.get("meta")
            first_meta = meta if isinstance(meta, dict) else {}

        meta = payload.get("meta")
        if isinstance(meta, dict):
            found = to_int(meta.get("found"))
            if found is not None:
                found_total = found

        merged_results.extend(results)
        page_trace.append(
            {
                "page": page,
                "result_count": len(results),
                "request_url": url,
                "x_ratelimit_remaining": headers.get("x-ratelimit-remaining"),
            }
        )

        if not results:
            break
        if found_total is not None and len(merged_results) >= found_total:
            break

        limit_value = to_int(work_query.get("limit"))
        if limit_value is not None and len(results) < limit_value:
            break
        page += 1

    output_meta = dict(first_meta or {})
    output_meta["aggregated"] = True
    output_meta["page_start"] = start_page
    output_meta["pages_fetched"] = len(page_trace)
    output_meta["result_count"] = len(merged_results)
    if found_total is not None:
        output_meta["found"] = found_total

    return {
        "meta": output_meta,
        "results": merged_results,
        "page_trace": page_trace,
    }


def write_json(data: dict[str, Any], output: str, pretty: bool) -> None:
    serialized = json.dumps(
        data,
        ensure_ascii=False,
        indent=2 if pretty else None,
        separators=None if pretty else (",", ":"),
    )
    if output:
        output_path = Path(output).expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(serialized + "\n", encoding="utf-8")
    else:
        print(serialized)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="OpenAQ API v3 client.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check-config", help="Validate required API environment variables.")

    req = sub.add_parser("request", help="Send one API request or merge paged responses.")
    req.add_argument("--path", required=True, help="API path, e.g. /v3/locations.")
    req.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"API base URL (default: {DEFAULT_BASE_URL}).",
    )
    req.add_argument(
        "--query",
        action="append",
        default=[],
        help="Query pair key=value. Repeat for multiple pairs.",
    )
    req.add_argument("--timeout", type=int, default=60, help="Request timeout seconds.")
    req.add_argument("--all-pages", action="store_true", help="Merge paged results via page=N.")
    req.add_argument(
        "--max-pages",
        type=int,
        default=100,
        help="Maximum number of pages to fetch when --all-pages is set.",
    )
    req.add_argument("--output", default="", help="Optional output JSON file path.")
    req.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")
    req.add_argument(
        "--show-request",
        action="store_true",
        help="Include request URL and selected response headers in output.",
    )
    return parser


def command_check_config() -> int:
    try:
        env_required(ENV_API_KEY)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    payload = {
        "ok": True,
        "api_base_url_default": DEFAULT_BASE_URL,
        "openaq_api_key_env": ENV_API_KEY,
        "openaq_api_key_set": True,
    }
    print(json.dumps(payload))
    return 0


def command_request(args: argparse.Namespace) -> int:
    try:
        api_key = env_required(ENV_API_KEY)
        query = parse_key_value_args(args.query)
        if args.all_pages:
            payload = fetch_all_pages(
                base_url=args.base_url,
                path=args.path,
                query=query,
                api_key=api_key,
                timeout=args.timeout,
                max_pages=args.max_pages,
            )
            if args.show_request:
                payload = {"request_mode": "all-pages", "path": args.path, "data": payload}
        else:
            data, headers, request_url = fetch_one_page(
                base_url=args.base_url,
                path=args.path,
                query=query,
                api_key=api_key,
                timeout=args.timeout,
            )
            if args.show_request:
                payload = {
                    "request_mode": "single-page",
                    "request_url": request_url,
                    "headers": {
                        "x-ratelimit-used": headers.get("x-ratelimit-used"),
                        "x-ratelimit-reset": headers.get("x-ratelimit-reset"),
                        "x-ratelimit-limit": headers.get("x-ratelimit-limit"),
                        "x-ratelimit-remaining": headers.get("x-ratelimit-remaining"),
                    },
                    "data": data,
                }
            else:
                payload = data
    except (ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    write_json(payload, output=args.output, pretty=args.pretty)
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "check-config":
        return command_check_config()
    if args.command == "request":
        return command_request(args)
    print(f"Unsupported command: {args.command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
