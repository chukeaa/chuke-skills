#!/usr/bin/env python3
"""Fetch Federal Register documents with retries, throttling, and validation."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib import parse, request
from urllib.error import HTTPError, URLError

ENV_BASE_URL = "FEDERAL_REGISTER_API_BASE_URL"
ENV_TIMEOUT_SECONDS = "FEDERAL_REGISTER_TIMEOUT_SECONDS"
ENV_MAX_RETRIES = "FEDERAL_REGISTER_MAX_RETRIES"
ENV_RETRY_BACKOFF_SECONDS = "FEDERAL_REGISTER_RETRY_BACKOFF_SECONDS"
ENV_RETRY_BACKOFF_MULTIPLIER = "FEDERAL_REGISTER_RETRY_BACKOFF_MULTIPLIER"
ENV_MIN_REQUEST_INTERVAL_SECONDS = "FEDERAL_REGISTER_MIN_REQUEST_INTERVAL_SECONDS"
ENV_PAGE_SIZE = "FEDERAL_REGISTER_PAGE_SIZE"
ENV_MAX_PAGES_PER_RUN = "FEDERAL_REGISTER_MAX_PAGES_PER_RUN"
ENV_MAX_RECORDS_PER_RUN = "FEDERAL_REGISTER_MAX_RECORDS_PER_RUN"
ENV_MAX_RESPONSE_BYTES = "FEDERAL_REGISTER_MAX_RESPONSE_BYTES"
ENV_MAX_RETRY_AFTER_SECONDS = "FEDERAL_REGISTER_MAX_RETRY_AFTER_SECONDS"
ENV_USER_AGENT = "FEDERAL_REGISTER_USER_AGENT"

DEFAULT_BASE_URL = "https://www.federalregister.gov/api/v1"
DEFAULT_TIMEOUT_SECONDS = 45
DEFAULT_MAX_RETRIES = 4
DEFAULT_RETRY_BACKOFF_SECONDS = 1.5
DEFAULT_RETRY_BACKOFF_MULTIPLIER = 2.0
DEFAULT_MIN_REQUEST_INTERVAL_SECONDS = 0.8
DEFAULT_PAGE_SIZE = 25
DEFAULT_MAX_PAGES_PER_RUN = 10
DEFAULT_MAX_RECORDS_PER_RUN = 250
DEFAULT_MAX_RESPONSE_BYTES = 10_000_000
DEFAULT_MAX_RETRY_AFTER_SECONDS = 120
DEFAULT_USER_AGENT = "federal-register-doc-fetch/1.0"
DEFAULT_ORDER = "newest"
DEFAULT_FIELDS = (
    "title",
    "type",
    "abstract",
    "document_number",
    "html_url",
    "pdf_url",
    "public_inspection_pdf_url",
    "publication_date",
    "effective_on",
    "agencies",
    "topics",
    "excerpts",
    "docket_ids",
    "regulation_id_numbers",
    "comment_url",
    "body_html_url",
    "raw_text_url",
    "significant",
)

SEARCH_PATH = "documents.json"
RETRIABLE_HTTP_CODES = {429, 500, 502, 503, 504}
ORDER_CHOICES = ("relevance", "newest", "oldest", "executive_order_number")
DOCUMENT_TYPE_CHOICES = ("RULE", "PRORULE", "NOTICE", "PRESDOCU")
MAX_VALIDATION_ISSUES = 40


@dataclass(frozen=True)
class RuntimeConfig:
    base_url: str
    timeout_seconds: int
    max_retries: int
    retry_backoff_seconds: float
    retry_backoff_multiplier: float
    min_request_interval_seconds: float
    page_size: int
    max_pages_per_run: int
    max_records_per_run: int
    max_response_bytes: int
    max_retry_after_seconds: int
    user_agent: str


@dataclass(frozen=True)
class RequestSpec:
    term: str
    publication_start_date: str
    publication_end_date: str
    agencies: list[str]
    document_types: list[str]
    topics: list[str]
    sections: list[str]
    docket_id: str
    regulation_id_number: str
    significant: str
    order: str
    fields: list[str]
    page_size: int
    max_pages: int
    max_records: int


@dataclass(frozen=True)
class HttpJsonResponse:
    url: str
    status_code: int
    headers: dict[str, str]
    payload: dict[str, Any]
    byte_length: int


@dataclass
class IssueCollector:
    max_issues: int
    total_count: int = 0
    issues: list[dict[str, Any]] = field(default_factory=list)

    def add(self, *, level: str, path: str, message: str, value: Any | None = None) -> None:
        self.total_count += 1
        if len(self.issues) >= self.max_issues:
            return
        issue = {"level": level, "path": path, "message": message}
        if value is not None:
            issue["value"] = value
        self.issues.append(issue)


def env_or_default(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


def maybe_text(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split())


def unique_preserve_order(items: list[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = maybe_text(item)
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        output.append(text)
    return output


def parse_positive_int(name: str, raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got: {raw!r}") from exc
    if value <= 0:
        raise ValueError(f"{name} must be > 0, got: {value}")
    return value


def parse_non_negative_int(name: str, raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got: {raw!r}") from exc
    if value < 0:
        raise ValueError(f"{name} must be >= 0, got: {value}")
    return value


def parse_positive_float(name: str, raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got: {raw!r}") from exc
    if value <= 0:
        raise ValueError(f"{name} must be > 0, got: {value}")
    return value


def parse_non_negative_float(name: str, raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got: {raw!r}") from exc
    if value < 0:
        raise ValueError(f"{name} must be >= 0, got: {value}")
    return value


def parse_date_text(raw: str, *, field_name: str) -> str:
    text = maybe_text(raw)
    if not text:
        raise ValueError(f"{field_name} cannot be empty.")
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise ValueError(f"Invalid {field_name} {raw!r}. Expected YYYY-MM-DD.") from exc


def parse_rfc3339_datetime(raw: str, *, field_name: str) -> datetime:
    text = maybe_text(raw)
    if not text:
        raise ValueError(f"{field_name} cannot be empty.")
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"Invalid {field_name} {raw!r}. Expected RFC3339 timestamp.") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def to_date_from_datetime(raw: str, *, field_name: str) -> str:
    return parse_rfc3339_datetime(raw, field_name=field_name).date().isoformat()


def normalize_base_url(value: str) -> str:
    normalized = value.strip().rstrip("/")
    if not normalized:
        raise ValueError("Base URL cannot be empty.")
    if not normalized.startswith("http://") and not normalized.startswith("https://"):
        raise ValueError(f"Base URL must start with http:// or https://, got: {normalized!r}")
    return normalized


def normalize_order(value: str) -> str:
    normalized = maybe_text(value).casefold()
    if normalized not in ORDER_CHOICES:
        raise ValueError(f"order must be one of {', '.join(ORDER_CHOICES)}, got: {value!r}")
    return normalized


def normalize_document_type(value: str) -> str:
    normalized = maybe_text(value).upper()
    if normalized not in DOCUMENT_TYPE_CHOICES:
        raise ValueError(
            f"document type must be one of {', '.join(DOCUMENT_TYPE_CHOICES)}, got: {value!r}"
        )
    return normalized


def normalize_significant(value: str) -> str:
    normalized = maybe_text(value)
    if normalized not in {"0", "1"}:
        raise ValueError(f"significant must be '0' or '1', got: {value!r}")
    return normalized


def pretty_json(data: Any, *, pretty: bool) -> str:
    if pretty:
        return json.dumps(data, ensure_ascii=True, indent=2, sort_keys=True)
    return json.dumps(data, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def atomic_write_text_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=f".{path.name}.tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except Exception:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass
        raise


def write_json(path: Path, payload: Any, *, pretty: bool) -> None:
    atomic_write_text_file(path, pretty_json(payload, pretty=pretty) + "\n")


def to_rfc3339_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def build_runtime_config(args: argparse.Namespace) -> RuntimeConfig:
    base_url = normalize_base_url(
        args.base_url if args.base_url else env_or_default(ENV_BASE_URL, DEFAULT_BASE_URL)
    )
    timeout_seconds = parse_positive_int(
        "--timeout-seconds",
        str(
            args.timeout_seconds
            if args.timeout_seconds is not None
            else env_or_default(ENV_TIMEOUT_SECONDS, str(DEFAULT_TIMEOUT_SECONDS))
        ),
    )
    max_retries = parse_non_negative_int(
        "--max-retries",
        str(
            args.max_retries
            if args.max_retries is not None
            else env_or_default(ENV_MAX_RETRIES, str(DEFAULT_MAX_RETRIES))
        ),
    )
    retry_backoff_seconds = parse_positive_float(
        "--retry-backoff-seconds",
        str(
            args.retry_backoff_seconds
            if args.retry_backoff_seconds is not None
            else env_or_default(ENV_RETRY_BACKOFF_SECONDS, str(DEFAULT_RETRY_BACKOFF_SECONDS))
        ),
    )
    retry_backoff_multiplier = parse_positive_float(
        "--retry-backoff-multiplier",
        str(
            args.retry_backoff_multiplier
            if args.retry_backoff_multiplier is not None
            else env_or_default(
                ENV_RETRY_BACKOFF_MULTIPLIER, str(DEFAULT_RETRY_BACKOFF_MULTIPLIER)
            )
        ),
    )
    min_request_interval_seconds = parse_non_negative_float(
        "--min-request-interval-seconds",
        str(
            args.min_request_interval_seconds
            if args.min_request_interval_seconds is not None
            else env_or_default(
                ENV_MIN_REQUEST_INTERVAL_SECONDS, str(DEFAULT_MIN_REQUEST_INTERVAL_SECONDS)
            )
        ),
    )
    page_size = parse_positive_int(
        "--page-size",
        str(
            args.page_size
            if args.page_size is not None
            else env_or_default(ENV_PAGE_SIZE, str(DEFAULT_PAGE_SIZE))
        ),
    )
    if page_size > 1000:
        raise ValueError(f"page size must be <= 1000, got: {page_size}")
    max_pages_per_run = parse_positive_int(
        "--max-pages-per-run",
        str(
            args.max_pages_per_run
            if args.max_pages_per_run is not None
            else env_or_default(ENV_MAX_PAGES_PER_RUN, str(DEFAULT_MAX_PAGES_PER_RUN))
        ),
    )
    max_records_per_run = parse_positive_int(
        "--max-records-per-run",
        str(
            args.max_records_per_run
            if args.max_records_per_run is not None
            else env_or_default(ENV_MAX_RECORDS_PER_RUN, str(DEFAULT_MAX_RECORDS_PER_RUN))
        ),
    )
    max_response_bytes = parse_positive_int(
        "--max-response-bytes",
        str(
            args.max_response_bytes
            if args.max_response_bytes is not None
            else env_or_default(ENV_MAX_RESPONSE_BYTES, str(DEFAULT_MAX_RESPONSE_BYTES))
        ),
    )
    max_retry_after_seconds = parse_non_negative_int(
        "--max-retry-after-seconds",
        str(
            args.max_retry_after_seconds
            if args.max_retry_after_seconds is not None
            else env_or_default(
                ENV_MAX_RETRY_AFTER_SECONDS, str(DEFAULT_MAX_RETRY_AFTER_SECONDS)
            )
        ),
    )
    user_agent = (
        args.user_agent
        if args.user_agent is not None
        else env_or_default(ENV_USER_AGENT, DEFAULT_USER_AGENT)
    ).strip()
    if not user_agent:
        raise ValueError("User-Agent cannot be empty.")
    return RuntimeConfig(
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
        retry_backoff_seconds=retry_backoff_seconds,
        retry_backoff_multiplier=retry_backoff_multiplier,
        min_request_interval_seconds=min_request_interval_seconds,
        page_size=page_size,
        max_pages_per_run=max_pages_per_run,
        max_records_per_run=max_records_per_run,
        max_response_bytes=max_response_bytes,
        max_retry_after_seconds=max_retry_after_seconds,
        user_agent=user_agent,
    )


def configure_logging(level: str, log_file: str) -> None:
    logger = logging.getLogger("federal_register_doc_fetch")
    logger.handlers.clear()
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)
    if log_file.strip():
        path = Path(log_file).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    logger.propagate = False


def resolve_publication_start_date(args: argparse.Namespace) -> str:
    start_date = maybe_text(args.start_date)
    start_datetime = maybe_text(args.start_datetime)
    if start_date and start_datetime:
        converted = to_date_from_datetime(start_datetime, field_name="start datetime")
        normalized = parse_date_text(start_date, field_name="start date")
        if converted != normalized:
            raise ValueError("start date and start datetime refer to different UTC dates.")
        return normalized
    if start_date:
        return parse_date_text(start_date, field_name="start date")
    if start_datetime:
        return to_date_from_datetime(start_datetime, field_name="start datetime")
    return ""


def resolve_publication_end_date(args: argparse.Namespace) -> str:
    end_date = maybe_text(args.end_date)
    end_datetime = maybe_text(args.end_datetime)
    if end_date and end_datetime:
        converted = to_date_from_datetime(end_datetime, field_name="end datetime")
        normalized = parse_date_text(end_date, field_name="end date")
        if converted != normalized:
            raise ValueError("end date and end datetime refer to different UTC dates.")
        return normalized
    if end_date:
        return parse_date_text(end_date, field_name="end date")
    if end_datetime:
        return to_date_from_datetime(end_datetime, field_name="end datetime")
    return ""


def build_request_spec(args: argparse.Namespace, config: RuntimeConfig) -> RequestSpec:
    publication_start_date = resolve_publication_start_date(args)
    publication_end_date = resolve_publication_end_date(args)
    if publication_start_date and publication_end_date:
        start_dt = date.fromisoformat(publication_start_date)
        end_dt = date.fromisoformat(publication_end_date)
        if start_dt > end_dt:
            raise ValueError("start date must be <= end date.")

    agencies = unique_preserve_order([maybe_text(item) for item in getattr(args, "agency", [])])
    document_types = unique_preserve_order(
        [normalize_document_type(item) for item in getattr(args, "document_type", []) if maybe_text(item)]
    )
    topics = unique_preserve_order([maybe_text(item) for item in getattr(args, "topic", [])])
    sections = unique_preserve_order([maybe_text(item) for item in getattr(args, "section", [])])
    term = maybe_text(args.term)
    docket_id = maybe_text(args.docket_id)
    regulation_id_number = maybe_text(args.regulation_id_number)
    significant = normalize_significant(args.significant) if maybe_text(args.significant) else ""
    order = normalize_order(args.order or DEFAULT_ORDER)
    fields = unique_preserve_order(
        [maybe_text(item) for item in getattr(args, "field", []) if maybe_text(item)]
    ) or list(DEFAULT_FIELDS)

    if not any([term, agencies, document_types, topics, sections, docket_id, regulation_id_number, significant]):
        raise ValueError(
            "Provide at least one narrowing filter: --term, --agency, --document-type, --topic, --section, --docket-id, --regulation-id-number, or --significant."
        )
    if not publication_start_date and not publication_end_date:
        raise ValueError("Provide at least one publication date bound via --start-date/--start-datetime or --end-date/--end-datetime.")

    page_size = args.page_size if args.page_size is not None else config.page_size
    if page_size > 1000:
        raise ValueError("page size must be <= 1000.")
    max_pages = args.max_pages if args.max_pages is not None else config.max_pages_per_run
    max_records = args.max_records if args.max_records is not None else config.max_records_per_run
    if max_pages <= 0:
        raise ValueError("max pages must be > 0.")
    if max_pages > config.max_pages_per_run:
        raise ValueError(
            f"max pages {max_pages} exceeds configured cap {config.max_pages_per_run}."
        )
    if max_records <= 0:
        raise ValueError("max records must be > 0.")
    if max_records > config.max_records_per_run:
        raise ValueError(
            f"max records {max_records} exceeds configured cap {config.max_records_per_run}."
        )
    if order == "relevance" and not term:
        raise ValueError("order=relevance requires --term.")

    return RequestSpec(
        term=term,
        publication_start_date=publication_start_date,
        publication_end_date=publication_end_date,
        agencies=agencies,
        document_types=document_types,
        topics=topics,
        sections=sections,
        docket_id=docket_id,
        regulation_id_number=regulation_id_number,
        significant=significant,
        order=order,
        fields=fields,
        page_size=page_size,
        max_pages=max_pages,
        max_records=max_records,
    )


def build_query_params(spec: RequestSpec, *, page: int) -> list[tuple[str, str]]:
    params: list[tuple[str, str]] = [("per_page", str(spec.page_size)), ("page", str(page)), ("order", spec.order)]
    for field_name in spec.fields:
        params.append(("fields[]", field_name))
    if spec.term:
        params.append(("conditions[term]", spec.term))
    if spec.publication_start_date:
        params.append(("conditions[publication_date][gte]", spec.publication_start_date))
    if spec.publication_end_date:
        params.append(("conditions[publication_date][lte]", spec.publication_end_date))
    for agency in spec.agencies:
        params.append(("conditions[agencies][]", agency))
    for document_type in spec.document_types:
        params.append(("conditions[type][]", document_type))
    for topic in spec.topics:
        params.append(("conditions[topics][]", topic))
    for section in spec.sections:
        params.append(("conditions[sections][]", section))
    if spec.docket_id:
        params.append(("conditions[docket_id]", spec.docket_id))
    if spec.regulation_id_number:
        params.append(("conditions[regulation_id_number]", spec.regulation_id_number))
    if spec.significant:
        params.append(("conditions[significant]", spec.significant))
    return params


def build_fetch_url(base_url: str, params: list[tuple[str, str]]) -> str:
    query_string = parse.urlencode(params, doseq=True)
    return f"{base_url}/{SEARCH_PATH}?{query_string}"


def parse_retry_after_seconds(value: str | None) -> float | None:
    text = maybe_text(value)
    if not text:
        return None
    try:
        parsed = float(text)
    except ValueError:
        return None
    if parsed < 0:
        return None
    return parsed


def read_limited_bytes(handle: Any, *, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = handle.read(65536)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise ValueError(f"Response exceeded max_response_bytes={max_bytes}.")
        chunks.append(chunk)
    return b"".join(chunks)


def fetch_json(url: str, config: RuntimeConfig, logger: logging.Logger) -> HttpJsonResponse:
    headers = {
        "Accept": "application/json",
        "User-Agent": config.user_agent,
    }
    sleep_seconds = config.retry_backoff_seconds
    last_request_monotonic: float | None = None
    for attempt in range(config.max_retries + 1):
        if last_request_monotonic is not None:
            gap = time.monotonic() - last_request_monotonic
            remaining = config.min_request_interval_seconds - gap
            if remaining > 0:
                time.sleep(remaining)
        request_obj = request.Request(url, headers=headers, method="GET")
        try:
            with request.urlopen(request_obj, timeout=config.timeout_seconds) as response:
                last_request_monotonic = time.monotonic()
                status_code = getattr(response, "status", 200)
                body = read_limited_bytes(response, max_bytes=config.max_response_bytes)
                content_type = maybe_text(response.headers.get("Content-Type"))
                if "json" not in content_type.casefold():
                    raise ValueError(f"Unexpected content type {content_type!r} for {url}")
                payload = json.loads(body.decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("Federal Register response must be a JSON object.")
                return HttpJsonResponse(
                    url=url,
                    status_code=status_code,
                    headers={key.lower(): value for key, value in response.headers.items()},
                    payload=payload,
                    byte_length=len(body),
                )
        except HTTPError as exc:
            last_request_monotonic = time.monotonic()
            if exc.code not in RETRIABLE_HTTP_CODES or attempt >= config.max_retries:
                detail = exc.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"Federal Register request failed with HTTP {exc.code}: {detail[:500]}") from exc
            retry_after = parse_retry_after_seconds(exc.headers.get("Retry-After"))
            if retry_after is not None:
                if retry_after > config.max_retry_after_seconds:
                    raise RuntimeError(
                        f"Federal Register Retry-After {retry_after}s exceeds cap {config.max_retry_after_seconds}s."
                    ) from exc
                sleep_for = retry_after
            else:
                sleep_for = sleep_seconds
                sleep_seconds *= config.retry_backoff_multiplier
            logger.warning("Retrying Federal Register fetch after %.2fs (attempt %s/%s).", sleep_for, attempt + 1, config.max_retries)
            time.sleep(sleep_for)
        except (URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            last_request_monotonic = time.monotonic()
            if attempt >= config.max_retries:
                raise RuntimeError(f"Federal Register request failed: {exc}") from exc
            logger.warning("Retrying Federal Register fetch after %.2fs (attempt %s/%s).", sleep_seconds, attempt + 1, config.max_retries)
            time.sleep(sleep_seconds)
            sleep_seconds *= config.retry_backoff_multiplier
    raise RuntimeError("Federal Register request failed after retries.")


def validate_document_record(record: Any, *, index: int, issues: IssueCollector) -> dict[str, Any] | None:
    path = f"$.results[{index}]"
    if not isinstance(record, dict):
        issues.add(level="error", path=path, message="document record must be an object.")
        return None
    if not maybe_text(record.get("document_number")):
        issues.add(level="warning", path=f"{path}.document_number", message="missing document_number.")
    if not maybe_text(record.get("title")):
        issues.add(level="warning", path=f"{path}.title", message="missing title.")
    return record


def extract_page_records(
    payload: dict[str, Any],
    *,
    page_number: int,
    response_url: str,
    issues: IssueCollector,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    description = maybe_text(payload.get("description"))
    count_value = payload.get("count")
    if count_value is None:
        issues.add(level="warning", path="$.count", message="missing count field.")
    elif not isinstance(count_value, int):
        issues.add(level="warning", path="$.count", message="count should be an integer.", value=count_value)

    total_pages_value = payload.get("total_pages")
    if total_pages_value is not None and not isinstance(total_pages_value, int):
        issues.add(
            level="warning",
            path="$.total_pages",
            message="total_pages should be an integer when present.",
            value=total_pages_value,
        )

    raw_results = payload.get("results")
    if raw_results is None:
        raw_results = []
    if not isinstance(raw_results, list):
        issues.add(level="error", path="$.results", message="results must be a list when present.")
        raw_results = []

    records: list[dict[str, Any]] = []
    for index, item in enumerate(raw_results):
        validated = validate_document_record(item, index=index, issues=issues)
        if validated is None:
            continue
        enriched = dict(validated)
        enriched["source_query_url"] = response_url
        enriched["source_page_number"] = page_number
        records.append(enriched)

    next_page_url = maybe_text(payload.get("next_page_url"))
    return records, {
        "description": description,
        "count": count_value if isinstance(count_value, int) else None,
        "total_pages": total_pages_value if isinstance(total_pages_value, int) else None,
        "next_page_url": next_page_url,
    }


def runtime_config_payload(config: RuntimeConfig) -> dict[str, Any]:
    return {
        "api_key_required": False,
        "base_url": config.base_url,
        "timeout_seconds": config.timeout_seconds,
        "max_retries": config.max_retries,
        "retry_backoff_seconds": config.retry_backoff_seconds,
        "retry_backoff_multiplier": config.retry_backoff_multiplier,
        "min_request_interval_seconds": config.min_request_interval_seconds,
        "page_size": config.page_size,
        "max_pages_per_run": config.max_pages_per_run,
        "max_records_per_run": config.max_records_per_run,
        "max_response_bytes": config.max_response_bytes,
        "max_retry_after_seconds": config.max_retry_after_seconds,
        "user_agent": config.user_agent,
    }


def request_payload(spec: RequestSpec, *, first_page_url: str) -> dict[str, Any]:
    return {
        "term": spec.term,
        "publication_start_date": spec.publication_start_date,
        "publication_end_date": spec.publication_end_date,
        "agencies": spec.agencies,
        "document_types": spec.document_types,
        "topics": spec.topics,
        "sections": spec.sections,
        "docket_id": spec.docket_id,
        "regulation_id_number": spec.regulation_id_number,
        "significant": spec.significant,
        "order": spec.order,
        "fields": spec.fields,
        "page_size": spec.page_size,
        "max_pages": spec.max_pages,
        "max_records": spec.max_records,
        "first_page_url": first_page_url,
    }


def check_config(args: argparse.Namespace) -> dict[str, Any]:
    config = build_runtime_config(args)
    return {
        "command": "check-config",
        "ok": True,
        "payload": runtime_config_payload(config),
    }


def fetch_command(args: argparse.Namespace) -> dict[str, Any]:
    config = build_runtime_config(args)
    spec = build_request_spec(args, config)
    configure_logging(args.log_level, args.log_file)
    logger = logging.getLogger("federal_register_doc_fetch")

    first_page_url = build_fetch_url(config.base_url, build_query_params(spec, page=1))
    payload: dict[str, Any] = {
        "source_skill": "federal-register-doc-fetch",
        "generated_at_utc": to_rfc3339_z(datetime.now(timezone.utc)),
        "dry_run": bool(args.dry_run),
        "request": request_payload(spec, first_page_url=first_page_url),
    }

    if args.dry_run:
        payload["runtime_config"] = runtime_config_payload(config)
        if args.output:
            payload["artifacts"] = {"full_payload_json": str(Path(args.output).expanduser().resolve())}
            write_json(Path(args.output).expanduser().resolve(), payload, pretty=args.pretty)
        return {"command": "fetch", "ok": True, "payload": payload}

    issues = IssueCollector(max_issues=args.max_validation_issues)
    current_url = first_page_url
    current_page = 1
    records: list[dict[str, Any]] = []
    page_summaries: list[dict[str, Any]] = []
    total_count: int | None = None
    total_pages: int | None = None
    description = ""
    stop_reason = "completed"

    while current_url and current_page <= spec.max_pages and len(records) < spec.max_records:
        logger.info("Fetching Federal Register documents: %s", current_url)
        response = fetch_json(current_url, config, logger)
        page_records, page_info = extract_page_records(
            response.payload,
            page_number=current_page,
            response_url=response.url,
            issues=issues,
        )
        if total_count is None:
            total_count = page_info["count"]
        if total_pages is None:
            total_pages = page_info["total_pages"]
        if not description:
            description = maybe_text(page_info["description"])
        remaining = spec.max_records - len(records)
        records.extend(page_records[:remaining])
        page_summaries.append(
            {
                "page_number": current_page,
                "response_url": response.url,
                "status_code": response.status_code,
                "byte_length": response.byte_length,
                "record_count": len(page_records),
            }
        )
        next_page_url = maybe_text(page_info["next_page_url"])
        if not next_page_url:
            if current_page == 1 and not page_records:
                stop_reason = "no-results"
            break
        if len(records) >= spec.max_records:
            stop_reason = "max-records"
            break
        if current_page >= spec.max_pages:
            stop_reason = "max-pages"
            break
        current_page += 1
        current_url = next_page_url
    else:
        if len(records) >= spec.max_records:
            stop_reason = "max-records"
        elif current_page >= spec.max_pages:
            stop_reason = "max-pages"

    payload.update(
        {
            "description": description,
            "count": total_count,
            "total_pages": total_pages,
            "returned_count": len(records),
            "records": records,
            "page_summaries": page_summaries,
            "stop_reason": stop_reason,
            "validation_summary": {
                "ok": issues.total_count == 0,
                "total_issue_count": issues.total_count,
                "issues": issues.issues,
            },
        }
    )
    if args.output:
        output_path = Path(args.output).expanduser().resolve()
        payload["artifacts"] = {"full_payload_json": str(output_path)}
        write_json(output_path, payload, pretty=args.pretty)
    return {"command": "fetch", "ok": True, "payload": payload}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fetch Federal Register documents with bounded filters.")
    sub = parser.add_subparsers(dest="command", required=True)

    check_config_parser = sub.add_parser("check-config", help="Show effective runtime configuration.")
    check_config_parser.add_argument("--base-url", default="", help="Override Federal Register API base URL.")
    check_config_parser.add_argument("--timeout-seconds", type=int, default=None, help="HTTP timeout in seconds.")
    check_config_parser.add_argument("--max-retries", type=int, default=None, help="Maximum retry count.")
    check_config_parser.add_argument("--retry-backoff-seconds", type=float, default=None, help="Initial retry backoff in seconds.")
    check_config_parser.add_argument("--retry-backoff-multiplier", type=float, default=None, help="Retry backoff multiplier.")
    check_config_parser.add_argument("--min-request-interval-seconds", type=float, default=None, help="Minimum interval between requests.")
    check_config_parser.add_argument("--page-size", type=int, default=None, help="Default page size override.")
    check_config_parser.add_argument("--max-pages-per-run", type=int, default=None, help="Configured max-pages cap.")
    check_config_parser.add_argument("--max-records-per-run", type=int, default=None, help="Configured max-records cap.")
    check_config_parser.add_argument("--max-response-bytes", type=int, default=None, help="Maximum response size per page.")
    check_config_parser.add_argument("--max-retry-after-seconds", type=int, default=None, help="Maximum accepted Retry-After.")
    check_config_parser.add_argument("--user-agent", default=None, help="Override User-Agent header.")
    check_config_parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")

    fetch_parser = sub.add_parser("fetch", help="Fetch Federal Register documents.")
    fetch_parser.add_argument("--term", default="", help="Full-text search term.")
    fetch_parser.add_argument("--start-date", default="", help="Publication start date YYYY-MM-DD.")
    fetch_parser.add_argument("--end-date", default="", help="Publication end date YYYY-MM-DD.")
    fetch_parser.add_argument("--start-datetime", default="", help="Publication start datetime RFC3339 UTC; converted to date.")
    fetch_parser.add_argument("--end-datetime", default="", help="Publication end datetime RFC3339 UTC; converted to date.")
    fetch_parser.add_argument("--agency", action="append", default=[], help="Agency slug filter; repeatable.")
    fetch_parser.add_argument("--document-type", action="append", default=[], help="Document type filter; repeatable.")
    fetch_parser.add_argument("--topic", action="append", default=[], help="Topic slug filter; repeatable.")
    fetch_parser.add_argument("--section", action="append", default=[], help="Section slug filter; repeatable.")
    fetch_parser.add_argument("--docket-id", default="", help="Agency docket ID filter.")
    fetch_parser.add_argument("--regulation-id-number", default="", help="RIN filter.")
    fetch_parser.add_argument("--significant", default="", help="EO 12866 significant filter: 0 or 1.")
    fetch_parser.add_argument("--field", action="append", default=[], help="Explicit field name; repeatable.")
    fetch_parser.add_argument("--order", default=DEFAULT_ORDER, help="Order: relevance, newest, oldest, executive_order_number.")
    fetch_parser.add_argument("--page-size", type=int, default=None, help="Results per page (1-1000).")
    fetch_parser.add_argument("--max-pages", type=int, default=None, help="Maximum pages to fetch.")
    fetch_parser.add_argument("--max-records", type=int, default=None, help="Maximum records to keep.")
    fetch_parser.add_argument("--dry-run", action="store_true", help="Validate and print the first-page request only.")
    fetch_parser.add_argument("--output", default="", help="Optional path for full JSON payload.")
    fetch_parser.add_argument("--base-url", default="", help="Override Federal Register API base URL.")
    fetch_parser.add_argument("--timeout-seconds", type=int, default=None, help="HTTP timeout in seconds.")
    fetch_parser.add_argument("--max-retries", type=int, default=None, help="Maximum retry count.")
    fetch_parser.add_argument("--retry-backoff-seconds", type=float, default=None, help="Initial retry backoff in seconds.")
    fetch_parser.add_argument("--retry-backoff-multiplier", type=float, default=None, help="Retry backoff multiplier.")
    fetch_parser.add_argument("--min-request-interval-seconds", type=float, default=None, help="Minimum interval between requests.")
    fetch_parser.add_argument("--max-pages-per-run", type=int, default=None, help="Configured max-pages cap.")
    fetch_parser.add_argument("--max-records-per-run", type=int, default=None, help="Configured max-records cap.")
    fetch_parser.add_argument("--max-response-bytes", type=int, default=None, help="Maximum response size per page.")
    fetch_parser.add_argument("--max-retry-after-seconds", type=int, default=None, help="Maximum accepted Retry-After.")
    fetch_parser.add_argument("--user-agent", default=None, help="Override User-Agent header.")
    fetch_parser.add_argument("--log-level", default="INFO", help="Logger level.")
    fetch_parser.add_argument("--log-file", default="", help="Optional log file path.")
    fetch_parser.add_argument("--max-validation-issues", type=int, default=MAX_VALIDATION_ISSUES, help="Maximum validation issues stored in output.")
    fetch_parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.command == "check-config":
            result = check_config(args)
        elif args.command == "fetch":
            result = fetch_command(args)
        else:
            raise ValueError(f"Unsupported command {args.command!r}")
    except Exception as exc:
        error = {"command": args.command, "ok": False, "error": str(exc)}
        sys.stdout.write(pretty_json(error, pretty=getattr(args, "pretty", False)) + "\n")
        return 1

    sys.stdout.write(pretty_json(result, pretty=getattr(args, "pretty", False)) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
