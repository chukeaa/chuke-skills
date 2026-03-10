#!/usr/bin/env python3
"""Route OpenAQ fetch tasks to API or S3 based on mode and workload profile."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import openaq_api_client as api_client
import openaq_s3_fetch as s3_fetch


def parse_api_query_args(items: list[str]) -> dict[str, str]:
    return api_client.parse_key_value_args(items)


def choose_source_mode(args: argparse.Namespace) -> str:
    if args.source_mode in {"api", "s3"}:
        return args.source_mode

    explicit_s3_intent = any(
        [
            bool(args.s3_key),
            bool(args.s3_prefix),
            args.location_id is not None,
            args.year is not None,
            args.s3_action != "ls",
        ]
    )
    if args.volume_profile == "full-backfill" or explicit_s3_intent:
        return "s3"
    return "api"


def run_api(args: argparse.Namespace) -> dict[str, Any]:
    if not args.api_path:
        raise ValueError("--api-path is required when using API mode.")

    query = parse_api_query_args(args.api_query)
    api_key = api_client.env_required(api_client.ENV_API_KEY)

    if args.api_all_pages:
        data = api_client.fetch_all_pages(
            base_url=args.api_base_url,
            path=args.api_path,
            query=query,
            api_key=api_key,
            timeout=args.api_timeout,
            max_pages=args.api_max_pages,
        )
        return {
            "mode": "api",
            "request_mode": "all-pages",
            "path": args.api_path,
            "data": data,
        }

    data, headers, request_url = api_client.fetch_one_page(
        base_url=args.api_base_url,
        path=args.api_path,
        query=query,
        api_key=api_key,
        timeout=args.api_timeout,
    )
    return {
        "mode": "api",
        "request_mode": "single-page",
        "path": args.api_path,
        "request_url": request_url,
        "rate_limit_headers": {
            "x-ratelimit-used": headers.get("x-ratelimit-used"),
            "x-ratelimit-reset": headers.get("x-ratelimit-reset"),
            "x-ratelimit-limit": headers.get("x-ratelimit-limit"),
            "x-ratelimit-remaining": headers.get("x-ratelimit-remaining"),
        },
        "data": data,
    }


def run_s3(args: argparse.Namespace) -> dict[str, Any]:
    bucket = s3_fetch.resolve_bucket(args.s3_bucket)
    region = s3_fetch.resolve_region()

    if args.s3_action == "build-prefix":
        if args.location_id is None or args.year is None:
            raise ValueError("--location-id and --year are required for --s3-action build-prefix.")
        prefix = s3_fetch.build_record_prefix(
            location_id=args.location_id,
            year=args.year,
            month=args.month,
            day=args.day,
            hour=args.hour,
        )
        return {
            "mode": "s3",
            "action": "build-prefix",
            "bucket": bucket,
            "region": region,
            "prefix": prefix,
        }

    if args.s3_action == "download":
        if not args.s3_key:
            raise ValueError("--s3-key is required for --s3-action download.")
        if not args.s3_output:
            raise ValueError("--s3-output is required for --s3-action download.")
        data = s3_fetch.download_key(
            bucket=bucket,
            region=region,
            key=args.s3_key,
            output=args.s3_output,
            timeout=args.s3_timeout,
        )
        return {
            "mode": "s3",
            "action": "download",
            "data": data,
        }

    data = s3_fetch.list_objects(
        bucket=bucket,
        region=region,
        prefix=args.s3_prefix,
        delimiter=args.s3_delimiter,
        max_keys=args.s3_max_keys,
        continuation_token=args.s3_continuation_token,
        timeout=args.s3_timeout,
    )
    return {
        "mode": "s3",
        "action": "ls",
        "data": data,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Route OpenAQ requests to API or S3.")
    sub = parser.add_subparsers(dest="command", required=True)

    fetch = sub.add_parser("fetch", help="Fetch OpenAQ data with auto/API/S3 mode.")
    fetch.add_argument(
        "--source-mode",
        choices=["auto", "api", "s3"],
        default="auto",
        help="Select source mode. auto chooses based on workload profile and provided args.",
    )
    fetch.add_argument(
        "--volume-profile",
        choices=["interactive", "batch", "full-backfill"],
        default="interactive",
        help="Workload profile used by auto mode.",
    )
    fetch.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")

    fetch.add_argument("--api-path", default="", help="OpenAQ API path, e.g. /v3/locations.")
    fetch.add_argument(
        "--api-base-url",
        default=api_client.DEFAULT_BASE_URL,
        help=f"API base URL (default: {api_client.DEFAULT_BASE_URL}).",
    )
    fetch.add_argument(
        "--api-query",
        action="append",
        default=[],
        help="API query pair key=value. Repeat for multiple pairs.",
    )
    fetch.add_argument("--api-timeout", type=int, default=60, help="API timeout in seconds.")
    fetch.add_argument("--api-all-pages", action="store_true", help="Merge API paged results.")
    fetch.add_argument(
        "--api-max-pages",
        type=int,
        default=100,
        help="Max pages to fetch when --api-all-pages is set.",
    )

    fetch.add_argument("--s3-bucket", default="", help="S3 bucket override.")
    fetch.add_argument(
        "--s3-action",
        choices=["ls", "download", "build-prefix"],
        default="ls",
        help="S3 action to run when S3 mode is selected.",
    )
    fetch.add_argument("--s3-prefix", default="", help="Prefix for S3 list action.")
    fetch.add_argument("--s3-delimiter", default="", help="Delimiter for S3 list action.")
    fetch.add_argument("--s3-max-keys", type=int, default=1000, help="Max keys for S3 list.")
    fetch.add_argument(
        "--s3-continuation-token",
        default="",
        help="Continuation token for S3 list action.",
    )
    fetch.add_argument("--s3-key", default="", help="S3 key for download action.")
    fetch.add_argument("--s3-output", default="", help="Output file path for download action.")
    fetch.add_argument("--s3-timeout", type=int, default=120, help="S3 timeout in seconds.")

    fetch.add_argument("--location-id", type=int, default=None, help="Location ID for prefix build.")
    fetch.add_argument("--year", type=int, default=None, help="Year for prefix build.")
    fetch.add_argument("--month", type=int, default=None, help="Month for prefix build.")
    fetch.add_argument("--day", type=int, default=None, help="Day for prefix build.")
    fetch.add_argument("--hour", type=int, default=None, help="Hour for prefix build.")

    return parser


def print_json(payload: dict[str, Any], pretty: bool) -> None:
    print(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2 if pretty else None,
            separators=None if pretty else (",", ":"),
        )
    )


def command_fetch(args: argparse.Namespace) -> int:
    try:
        selected_mode = choose_source_mode(args)
        if selected_mode == "api":
            result = run_api(args)
        else:
            result = run_s3(args)
    except (ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    payload = {
        "source_mode_requested": args.source_mode,
        "source_mode_selected": selected_mode,
        "volume_profile": args.volume_profile,
        "result": result,
    }
    print_json(payload, pretty=args.pretty)
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "fetch":
        return command_fetch(args)
    print(f"Unsupported command: {args.command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
