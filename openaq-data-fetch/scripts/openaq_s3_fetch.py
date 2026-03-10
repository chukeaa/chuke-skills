#!/usr/bin/env python3
"""List and download OpenAQ AWS archive files from public S3."""

from __future__ import annotations

import argparse
import json
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib import parse, request
from urllib.error import HTTPError, URLError

ENV_REGION = "OPENAQ_REGION"
ENV_BUCKET = "OPENAQ_S3_BUCKET"
DEFAULT_REGION = "us-east-1"
DEFAULT_BUCKET = "openaq-data-archive"
S3_XML_NAMESPACE = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}


def resolve_region() -> str:
    value = os.environ.get(ENV_REGION, "").strip()
    return value or DEFAULT_REGION


def resolve_bucket(bucket_arg: str) -> str:
    if bucket_arg.strip():
        return bucket_arg.strip()
    value = os.environ.get(ENV_BUCKET, "").strip()
    return value or DEFAULT_BUCKET


def s3_base_url(bucket: str, region: str) -> str:
    if region == "us-east-1":
        return f"https://{bucket}.s3.amazonaws.com"
    return f"https://{bucket}.s3.{region}.amazonaws.com"


def http_get_bytes(url: str, timeout: int) -> bytes:
    req = request.Request(url, method="GET")
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        detail = body if body else exc.reason
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Request failed: {exc}") from exc
    except TimeoutError as exc:
        raise RuntimeError("Request timed out.") from exc


def list_objects(
    *,
    bucket: str,
    region: str,
    prefix: str,
    delimiter: str,
    max_keys: int,
    continuation_token: str,
    timeout: int,
) -> dict[str, object]:
    if max_keys < 1:
        raise ValueError("--max-keys must be >= 1.")

    query: dict[str, str] = {
        "list-type": "2",
        "max-keys": str(max_keys),
    }
    if prefix:
        query["prefix"] = prefix
    if delimiter:
        query["delimiter"] = delimiter
    if continuation_token:
        query["continuation-token"] = continuation_token

    base = s3_base_url(bucket, region)
    url = base + "/?" + parse.urlencode(query)
    content = http_get_bytes(url, timeout=timeout)

    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise RuntimeError("Failed to parse S3 XML response.") from exc

    keys: list[dict[str, object]] = []
    for node in root.findall("s3:Contents", S3_XML_NAMESPACE):
        key = node.findtext("s3:Key", default="", namespaces=S3_XML_NAMESPACE)
        size_text = node.findtext("s3:Size", default="", namespaces=S3_XML_NAMESPACE)
        size_value = None
        if size_text:
            try:
                size_value = int(size_text)
            except ValueError:
                size_value = None
        keys.append({"key": key, "size": size_value})

    common_prefixes = [
        node.findtext("s3:Prefix", default="", namespaces=S3_XML_NAMESPACE)
        for node in root.findall("s3:CommonPrefixes", S3_XML_NAMESPACE)
    ]

    is_truncated = root.findtext("s3:IsTruncated", default="false", namespaces=S3_XML_NAMESPACE)
    next_token = root.findtext(
        "s3:NextContinuationToken", default="", namespaces=S3_XML_NAMESPACE
    )

    return {
        "bucket": bucket,
        "region": region,
        "endpoint": base,
        "request_url": url,
        "prefix": prefix,
        "delimiter": delimiter,
        "max_keys": max_keys,
        "is_truncated": is_truncated.lower() == "true",
        "next_continuation_token": next_token or None,
        "common_prefixes": common_prefixes,
        "objects": keys,
    }


def download_key(
    *,
    bucket: str,
    region: str,
    key: str,
    output: str,
    timeout: int,
) -> dict[str, object]:
    key_clean = key.strip()
    if not key_clean:
        raise ValueError("--key cannot be empty.")

    output_path = Path(output).expanduser()
    if output_path.is_dir():
        output_path = output_path / Path(key_clean).name
    output_path = output_path.resolve()

    base = s3_base_url(bucket, region)
    encoded_key = parse.quote(key_clean, safe="/=:-_.,")
    url = f"{base}/{encoded_key}"

    content = http_get_bytes(url, timeout=timeout)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(content)

    return {
        "bucket": bucket,
        "region": region,
        "endpoint": base,
        "key": key_clean,
        "request_url": url,
        "output_path": str(output_path),
        "bytes_written": len(content),
    }


def build_record_prefix(
    *,
    location_id: int,
    year: int,
    month: int | None,
    day: int | None,
    hour: int | None,
) -> str:
    if month is None and (day is not None or hour is not None):
        raise ValueError("Specify --month when using --day or --hour.")
    if day is None and hour is not None:
        raise ValueError("Specify --day when using --hour.")

    if year < 0:
        raise ValueError("--year must be >= 0.")
    if month is not None and not 1 <= month <= 12:
        raise ValueError("--month must be between 1 and 12.")
    if day is not None and not 1 <= day <= 31:
        raise ValueError("--day must be between 1 and 31.")
    if hour is not None and not 0 <= hour <= 23:
        raise ValueError("--hour must be between 0 and 23.")

    prefix = f"records/csv.gz/locationid={location_id}/year={year:04d}/"
    if month is not None:
        prefix += f"month={month:02d}/"
    if day is not None:
        prefix += f"day={day:02d}/"
    if hour is not None:
        prefix += f"hour={hour:02d}/"
    return prefix


def print_json(data: dict[str, object], pretty: bool) -> None:
    print(
        json.dumps(
            data,
            ensure_ascii=False,
            indent=2 if pretty else None,
            separators=None if pretty else (",", ":"),
        )
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="OpenAQ S3 archive helper.")
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check-config", help="Show effective bucket/region configuration.")
    check.add_argument("--bucket", default="", help=f"Override bucket (default: {DEFAULT_BUCKET}).")
    check.add_argument("--pretty", action="store_true", help="Pretty-print JSON.")

    ls = sub.add_parser("ls", help="List S3 objects and common prefixes.")
    ls.add_argument("--bucket", default="", help=f"Override bucket (default: {DEFAULT_BUCKET}).")
    ls.add_argument("--prefix", default="", help="S3 prefix to list.")
    ls.add_argument("--delimiter", default="", help="Optional delimiter, for example '/'.")
    ls.add_argument("--max-keys", type=int, default=1000, help="Max keys per list response.")
    ls.add_argument("--continuation-token", default="", help="Continuation token from previous list.")
    ls.add_argument("--timeout", type=int, default=60, help="Request timeout in seconds.")
    ls.add_argument("--pretty", action="store_true", help="Pretty-print JSON.")

    dl = sub.add_parser("download", help="Download one S3 key to local file.")
    dl.add_argument("--bucket", default="", help=f"Override bucket (default: {DEFAULT_BUCKET}).")
    dl.add_argument("--key", required=True, help="Full S3 key under the bucket.")
    dl.add_argument("--output", required=True, help="Local output file path or existing directory.")
    dl.add_argument("--timeout", type=int, default=120, help="Request timeout in seconds.")
    dl.add_argument("--pretty", action="store_true", help="Pretty-print JSON.")

    pref = sub.add_parser("build-prefix", help="Build partition prefix for OpenAQ records.")
    pref.add_argument("--location-id", type=int, required=True, help="OpenAQ location ID.")
    pref.add_argument("--year", type=int, required=True, help="Four-digit year.")
    pref.add_argument("--month", type=int, default=None, help="Month 1-12.")
    pref.add_argument("--day", type=int, default=None, help="Day 1-31.")
    pref.add_argument("--hour", type=int, default=None, help="Hour 0-23.")
    pref.add_argument("--pretty", action="store_true", help="Pretty-print JSON.")

    return parser


def command_check_config(args: argparse.Namespace) -> int:
    region = resolve_region()
    bucket = resolve_bucket(args.bucket)
    payload = {
        "ok": True,
        "bucket": bucket,
        "region": region,
        "region_env": ENV_REGION,
        "region_default_applied": os.environ.get(ENV_REGION, "").strip() == "",
        "endpoint": s3_base_url(bucket, region),
    }
    print_json(payload, pretty=args.pretty)
    return 0


def command_ls(args: argparse.Namespace) -> int:
    try:
        bucket = resolve_bucket(args.bucket)
        region = resolve_region()
        payload = list_objects(
            bucket=bucket,
            region=region,
            prefix=args.prefix,
            delimiter=args.delimiter,
            max_keys=args.max_keys,
            continuation_token=args.continuation_token,
            timeout=args.timeout,
        )
    except (ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print_json(payload, pretty=args.pretty)
    return 0


def command_download(args: argparse.Namespace) -> int:
    try:
        bucket = resolve_bucket(args.bucket)
        region = resolve_region()
        payload = download_key(
            bucket=bucket,
            region=region,
            key=args.key,
            output=args.output,
            timeout=args.timeout,
        )
    except (ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print_json(payload, pretty=args.pretty)
    return 0


def command_build_prefix(args: argparse.Namespace) -> int:
    try:
        prefix = build_record_prefix(
            location_id=args.location_id,
            year=args.year,
            month=args.month,
            day=args.day,
            hour=args.hour,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print_json({"prefix": prefix}, pretty=args.pretty)
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "check-config":
        return command_check_config(args)
    if args.command == "ls":
        return command_ls(args)
    if args.command == "download":
        return command_download(args)
    if args.command == "build-prefix":
        return command_build_prefix(args)
    print(f"Unsupported command: {args.command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
