#!/usr/bin/env python3
"""Deterministic normalization pipeline for eco-council runs."""

from __future__ import annotations

import argparse
import csv
import os
import gzip
import hashlib
import importlib.util
import json
import math
import re
import sqlite3
import statistics
import sys
import tempfile
import zipfile
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
ASSETS_DIR = SKILL_DIR / "assets"
PUBLIC_DDL_PATH = ASSETS_DIR / "sqlite" / "public_signals.sql"
ENVIRONMENT_DDL_PATH = ASSETS_DIR / "sqlite" / "environment_signals.sql"
CONTRACT_SCRIPT_PATH = SKILL_DIR.parent / "eco-council-data-contract" / "scripts" / "eco_council_contract.py"

SCHEMA_VERSION = "1.0.0"
POINT_MATCH_EPSILON_DEGREES = 0.05
NORMALIZE_CACHE_VERSION = "v2"
MAX_CONTEXT_TASKS = 4
MAX_CONTEXT_CLAIMS = 4
MAX_CONTEXT_OBSERVATIONS = 8
MAX_CONTEXT_EVIDENCE = 4
PHYSICAL_CLAIM_TYPES = {
    "wildfire",
    "smoke",
    "flood",
    "heat",
    "drought",
    "air-pollution",
    "water-pollution",
}
STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "but",
    "by",
    "for",
    "from",
    "has",
    "have",
    "in",
    "into",
    "is",
    "it",
    "its",
    "of",
    "on",
    "or",
    "that",
    "the",
    "their",
    "this",
    "to",
    "was",
    "were",
    "with",
}
CLAIM_KEYWORDS = {
    "wildfire": ("wildfire", "fire", "burning", "burn", "forest fire", "bushfire"),
    "smoke": ("smoke", "haze", "smog", "ash"),
    "flood": ("flood", "flooding", "overflow", "inundation"),
    "heat": ("heat", "heatwave", "hot weather", "extreme heat"),
    "drought": ("drought", "dry spell", "water shortage", "dryness"),
    "air-pollution": ("air quality", "pm2.5", "pm10", "pollution", "dirty air", "aqi"),
    "water-pollution": ("water pollution", "contaminated water", "sewage", "toxic spill"),
    "policy-reaction": ("policy", "regulation", "rulemaking", "public comment", "epa", "agency"),
}
CLAIM_METRIC_RULES = {
    "smoke": {
        "support": {
            "pm2_5": 35.0,
            "pm10": 50.0,
            "us_aqi": 100.0,
            "fire_detection_count": 1.0,
        },
        "contradict": {
            "pm2_5": 12.0,
            "pm10": 20.0,
            "us_aqi": 50.0,
        },
    },
    "air-pollution": {
        "support": {
            "pm2_5": 35.0,
            "pm10": 50.0,
            "us_aqi": 100.0,
            "nitrogen_dioxide": 40.0,
            "ozone": 100.0,
        },
        "contradict": {
            "pm2_5": 12.0,
            "pm10": 20.0,
            "us_aqi": 50.0,
        },
    },
    "wildfire": {
        "support": {
            "fire_detection_count": 1.0,
            "temperature_2m": 30.0,
            "wind_speed_10m": 5.0,
        },
        "contradict": {
            "fire_detection_count": 0.0,
            "precipitation_sum": 20.0,
            "relative_humidity_2m": 70.0,
        },
    },
    "flood": {
        "support": {
            "precipitation_sum": 20.0,
            "precipitation": 10.0,
            "river_discharge": 100.0,
            "river_discharge_mean": 100.0,
            "river_discharge_max": 120.0,
            "river_discharge_p75": 100.0,
        },
        "contradict": {
            "precipitation_sum": 1.0,
            "river_discharge": 20.0,
            "river_discharge_mean": 20.0,
            "river_discharge_max": 25.0,
            "river_discharge_p75": 20.0,
        },
    },
    "heat": {
        "support": {
            "temperature_2m": 32.0,
        },
        "contradict": {
            "temperature_2m": 22.0,
        },
    },
    "drought": {
        "support": {
            "precipitation_sum": 2.0,
            "soil_moisture_0_to_7cm": 0.12,
        },
        "contradict": {
            "precipitation_sum": 10.0,
            "soil_moisture_0_to_7cm": 0.25,
        },
    },
}
OPENAQ_TIME_KEYS = (
    "datetime",
    "date",
    "observed_at",
    "observedAt",
    "timestamp",
    "utc",
)
OPENAQ_VALUE_KEYS = ("value", "measurement", "concentration")
OPENAQ_LAT_KEYS = ("latitude", "lat")
OPENAQ_LON_KEYS = ("longitude", "lon", "lng")
ENVIRONMENT_METRIC_ALIASES = {
    "pm25": "pm2_5",
    "pm2.5": "pm2_5",
    "pm2_5": "pm2_5",
    "pm10": "pm10",
    "o3": "ozone",
    "ozone": "ozone",
    "no2": "nitrogen_dioxide",
    "nitrogen_dioxide": "nitrogen_dioxide",
    "so2": "sulphur_dioxide",
    "sulphur_dioxide": "sulphur_dioxide",
    "co": "carbon_monoxide",
    "carbon_monoxide": "carbon_monoxide",
    "us_aqi": "us_aqi",
    "gage_height": "gage_height",
}
AIRNOW_PARAMETER_METRIC_MAP = {
    "PM25": "pm2_5",
    "PM10": "pm10",
    "OZONE": "ozone",
    "NO2": "nitrogen_dioxide",
    "CO": "carbon_monoxide",
    "SO2": "sulphur_dioxide",
}
USGS_PARAMETER_METRIC_MAP = {
    "00060": "river_discharge",
    "00065": "gage_height",
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def pretty_json(data: Any, *, pretty: bool) -> str:
    if pretty:
        return json.dumps(data, ensure_ascii=True, indent=2, sort_keys=True)
    return json.dumps(data, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def read_jsonl(path: Path) -> list[Any]:
    records: list[Any] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            text = line.strip()
            if not text:
                continue
            records.append(json.loads(text))
    return records


def write_json(path: Path, payload: Any, *, pretty: bool) -> None:
    atomic_write_text_file(path, pretty_json(payload, pretty=pretty) + "\n")


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


def load_json_if_exists(path: Path) -> Any | None:
    if not path.exists():
        return None
    return read_json(path)


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(record, ensure_ascii=True, sort_keys=True) for record in records]
    atomic_write_text_file(path, "\n".join(lines) + ("\n" if lines else ""))


def normalize_space(value: str) -> str:
    return " ".join(str(value).split())


def truncate_text(value: str, limit: int) -> str:
    text = normalize_space(value)
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3].rstrip() + "..."


def maybe_text(value: Any) -> str:
    if value is None:
        return ""
    text = normalize_space(str(value))
    return text


def maybe_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def canonical_environment_metric(value: Any) -> str:
    text = maybe_text(value)
    if not text:
        return ""
    lowered = text.casefold()
    if lowered.endswith("_aqi"):
        base_metric = ENVIRONMENT_METRIC_ALIASES.get(lowered[:-4], lowered[:-4])
        return f"{base_metric}_aqi"
    return ENVIRONMENT_METRIC_ALIASES.get(lowered, text)


def parse_loose_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None

    text = normalize_space(str(value))
    if not text:
        return None

    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        pass

    for pattern in ("%Y%m%d%H%M%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text, pattern)
            parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        except ValueError:
            continue
    return None


def to_rfc3339_z(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_path_payload(path: Path) -> Any:
    suffix = path.suffix.lower()
    if suffix == ".json":
        return read_json(path)
    if suffix == ".jsonl":
        return read_jsonl(path)
    raise ValueError(f"Unsupported JSON payload path: {path}")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(65536)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def stable_hash(*parts: Any) -> str:
    joined = "||".join(maybe_text(part) for part in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True)


def parse_round_components(round_id: str) -> tuple[str, int, int] | None:
    match = re.match(r"^(.*?)(\d+)$", maybe_text(round_id))
    if match is None:
        return None
    prefix, digits = match.groups()
    return prefix, int(digits), len(digits)


def round_sort_key(round_id: str) -> tuple[str, int, str]:
    components = parse_round_components(round_id)
    if components is None:
        return (round_id, 10**9, round_id)
    prefix, number, _width = components
    return (prefix, number, round_id)


def round_directory_name(round_id: str) -> str:
    return round_id.replace("-", "_")


def round_dir(run_dir: Path, round_id: str) -> Path:
    return run_dir / round_directory_name(round_id)


def discover_round_ids(run_dir: Path) -> list[str]:
    round_ids: list[str] = []
    for child in run_dir.iterdir():
        if not child.is_dir():
            continue
        if not child.name.startswith("round_"):
            continue
        round_ids.append(child.name.replace("_", "-"))
    return sorted(dict.fromkeys(round_ids), key=round_sort_key)


def previous_round_id(run_dir: Path, round_id: str) -> str | None:
    components = parse_round_components(round_id)
    if components is None:
        candidates = [item for item in discover_round_ids(run_dir) if item < round_id]
        return candidates[-1] if candidates else None
    prefix, number, _width = components
    candidates: list[str] = []
    for item in discover_round_ids(run_dir):
        item_components = parse_round_components(item)
        if item_components is None:
            continue
        item_prefix, item_number, _item_width = item_components
        if item_prefix == prefix and item_number < number:
            candidates.append(item)
    return candidates[-1] if candidates else None


def mission_path(run_dir: Path) -> Path:
    return run_dir / "mission.json"


def load_mission(run_dir: Path) -> dict[str, Any]:
    payload = read_json(mission_path(run_dir))
    if not isinstance(payload, dict):
        raise ValueError("mission.json must be an object.")
    return payload


def mission_run_id(mission: dict[str, Any]) -> str:
    run_id = mission.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("mission.json missing run_id.")
    return run_id


def mission_window(mission: dict[str, Any]) -> dict[str, str]:
    window = mission.get("window")
    if not isinstance(window, dict):
        raise ValueError("mission.json missing window.")
    start_utc = maybe_text(window.get("start_utc"))
    end_utc = maybe_text(window.get("end_utc"))
    if not start_utc or not end_utc:
        raise ValueError("mission.json window is incomplete.")
    return {"start_utc": start_utc, "end_utc": end_utc}


def mission_place_scope(mission: dict[str, Any]) -> dict[str, Any]:
    region = mission.get("region")
    if not isinstance(region, dict):
        raise ValueError("mission.json missing region.")
    label = maybe_text(region.get("label")) or "Mission region"
    geometry = region.get("geometry")
    if not isinstance(geometry, dict):
        raise ValueError("mission.json region.geometry must be an object.")
    return {"label": label, "geometry": geometry}


def geometry_to_bbox(geometry: dict[str, Any]) -> tuple[float, float, float, float] | None:
    kind = maybe_text(geometry.get("type"))
    if kind == "Point":
        lat = maybe_number(geometry.get("latitude"))
        lon = maybe_number(geometry.get("longitude"))
        if lat is None or lon is None:
            return None
        return (lon, lat, lon, lat)
    if kind == "BBox":
        west = maybe_number(geometry.get("west"))
        south = maybe_number(geometry.get("south"))
        east = maybe_number(geometry.get("east"))
        north = maybe_number(geometry.get("north"))
        if None in {west, south, east, north}:
            return None
        return (float(west), float(south), float(east), float(north))
    return None


def geometry_overlap(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_type = maybe_text(left.get("type"))
    right_type = maybe_text(right.get("type"))
    if left_type == "Point" and right_type == "Point":
        left_lat = maybe_number(left.get("latitude"))
        left_lon = maybe_number(left.get("longitude"))
        right_lat = maybe_number(right.get("latitude"))
        right_lon = maybe_number(right.get("longitude"))
        if None in {left_lat, left_lon, right_lat, right_lon}:
            return False
        assert left_lat is not None
        assert left_lon is not None
        assert right_lat is not None
        assert right_lon is not None
        return (
            abs(left_lat - right_lat) <= POINT_MATCH_EPSILON_DEGREES
            and abs(left_lon - right_lon) <= POINT_MATCH_EPSILON_DEGREES
        )
    if left_type == "Point" and right_type == "BBox":
        left_lat = maybe_number(left.get("latitude"))
        left_lon = maybe_number(left.get("longitude"))
        bbox = geometry_to_bbox(right)
        if None in {left_lat, left_lon} or bbox is None:
            return False
        assert left_lat is not None
        assert left_lon is not None
        west, south, east, north = bbox
        return west <= left_lon <= east and south <= left_lat <= north
    if left_type == "BBox" and right_type == "Point":
        return geometry_overlap(right, left)
    left_bbox = geometry_to_bbox(left)
    right_bbox = geometry_to_bbox(right)
    if left_bbox is None or right_bbox is None:
        return False
    left_west, left_south, left_east, left_north = left_bbox
    right_west, right_south, right_east, right_north = right_bbox
    return not (
        left_east < right_west
        or right_east < left_west
        or left_north < right_south
        or right_north < left_south
    )


def time_windows_overlap(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_start = parse_loose_datetime(left.get("start_utc"))
    left_end = parse_loose_datetime(left.get("end_utc"))
    right_start = parse_loose_datetime(right.get("start_utc"))
    right_end = parse_loose_datetime(right.get("end_utc"))
    if None in {left_start, left_end, right_start, right_end}:
        return False
    assert left_start is not None
    assert left_end is not None
    assert right_start is not None
    assert right_end is not None
    return max(left_start, right_start) <= min(left_end, right_end)


def default_public_db_path(run_dir: Path) -> Path:
    return run_dir / "analytics" / "public_signals.sqlite"


def default_environment_db_path(run_dir: Path) -> Path:
    return run_dir / "analytics" / "environment_signals.sqlite"


def default_context_dir(run_dir: Path, round_id: str, role: str) -> Path:
    return round_dir(run_dir, round_id) / role / "derived"


def shared_claims_path(run_dir: Path, round_id: str) -> Path:
    return round_dir(run_dir, round_id) / "shared" / "claims.json"


def shared_observations_path(run_dir: Path, round_id: str) -> Path:
    return round_dir(run_dir, round_id) / "shared" / "observations.json"


def shared_evidence_path(run_dir: Path, round_id: str) -> Path:
    return round_dir(run_dir, round_id) / "shared" / "evidence_cards.json"


def role_normalized_dir(run_dir: Path, round_id: str, role: str) -> Path:
    return round_dir(run_dir, round_id) / role / "normalized"


def run_manifest_path(run_dir: Path) -> Path:
    return run_dir / "run_manifest.json"


def load_or_build_manifest(run_dir: Path, mission: dict[str, Any]) -> dict[str, Any]:
    manifest_file = run_manifest_path(run_dir)
    if manifest_file.exists():
        payload = read_json(manifest_file)
        if isinstance(payload, dict):
            return payload
    return {
        "run_id": mission_run_id(mission),
        "run_dir": str(run_dir),
        "analytics_backend": "sqlite",
        "databases": {
            "public_signals": str(default_public_db_path(run_dir)),
            "environment_signals": str(default_environment_db_path(run_dir)),
        },
    }


def normalize_cache_dir(run_dir: Path) -> Path:
    return run_dir / "analytics" / "normalize_cache"


def normalize_cache_path(
    run_dir: Path,
    *,
    domain: str,
    source_skill: str,
    run_id: str,
    round_id: str,
    artifact_sha256: str,
) -> Path:
    key = stable_hash(NORMALIZE_CACHE_VERSION, domain, source_skill, run_id, round_id, artifact_sha256)
    safe_domain = re.sub(r"[^a-z0-9_-]+", "-", domain.lower())
    safe_source = re.sub(r"[^a-z0-9_-]+", "-", source_skill.lower())
    return normalize_cache_dir(run_dir) / safe_domain / f"{safe_source}_{key[:16]}.json"


def read_cache_payload(path: Path) -> dict[str, Any] | None:
    payload = load_json_if_exists(path)
    if not isinstance(payload, dict):
        return None
    return payload


def write_cache_payload(path: Path, payload: dict[str, Any]) -> None:
    write_json(path, payload, pretty=False)


def load_ddl(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def init_sqlite_db(path: Path, ddl_path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ddl = load_ddl(ddl_path)
    with sqlite3.connect(path) as conn:
        conn.executescript(ddl)
        conn.commit()


def emit_row_id(prefix: str, index: int) -> str:
    return f"{prefix}-{index:03d}"


def percentile95(values: list[float]) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    position = 0.95 * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    lower_value = ordered[lower]
    upper_value = ordered[upper]
    weight = position - lower
    return lower_value + (upper_value - lower_value) * weight


def artifact_ref(signal: dict[str, Any]) -> dict[str, Any]:
    ref = {
        "source_skill": signal["source_skill"],
        "artifact_path": signal["artifact_path"],
        "record_locator": signal["record_locator"],
    }
    if signal.get("external_id"):
        ref["external_id"] = signal["external_id"]
    if signal.get("sha256"):
        ref["sha256"] = signal["sha256"]
    return ref


def load_contract_module() -> Any | None:
    if not CONTRACT_SCRIPT_PATH.exists():
        return None
    module_name = "eco_council_contract"
    spec = importlib.util.spec_from_file_location(module_name, CONTRACT_SCRIPT_PATH)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


CONTRACT_MODULE = load_contract_module()
if CONTRACT_MODULE is not None and hasattr(CONTRACT_MODULE, "SCHEMA_VERSION"):
    SCHEMA_VERSION = CONTRACT_MODULE.SCHEMA_VERSION


def validate_payload(kind: str, payload: Any) -> None:
    if CONTRACT_MODULE is None:
        return
    result = CONTRACT_MODULE.validate_payload(kind, payload)
    validation = result.get("validation", {})
    if validation.get("ok"):
        return
    issue_messages = []
    for issue in validation.get("issues", [])[:5]:
        issue_messages.append(f"{issue.get('path')}: {issue.get('message')}")
    raise ValueError(f"Generated invalid {kind}: {'; '.join(issue_messages)}")


def insert_many(conn: sqlite3.Connection, sql: str, rows: Iterable[tuple[Any, ...]]) -> None:
    data = list(rows)
    if not data:
        return
    conn.executemany(sql, data)
    conn.commit()


def parse_input_specs(values: list[str]) -> list[tuple[str, Path]]:
    parsed: list[tuple[str, Path]] = []
    for raw in values:
        if "=" not in raw:
            raise ValueError(f"Invalid --input value {raw!r}. Use source-skill=/path/to/artifact.")
        source_skill, path_text = raw.split("=", 1)
        source_skill = source_skill.strip()
        path_text = path_text.strip()
        if not source_skill or not path_text:
            raise ValueError(f"Invalid --input value {raw!r}.")
        path = Path(path_text).expanduser().resolve()
        if not path.exists():
            raise ValueError(f"Input artifact does not exist: {path}")
        parsed.append((source_skill, path))
    return parsed


def semantic_fingerprint(text: str) -> str:
    cleaned = []
    token = []
    for char in text.lower():
        if char.isalnum():
            token.append(char)
            continue
        if token:
            cleaned.append("".join(token))
            token = []
    if token:
        cleaned.append("".join(token))
    filtered = [item for item in cleaned if item and item not in STOPWORDS]
    return "-".join(filtered[:12])


def claim_type_from_text(text: str) -> str:
    lowered = text.lower()
    for claim_type, keywords in CLAIM_KEYWORDS.items():
        if any(keyword in lowered for keyword in keywords):
            return claim_type
    return "other"


def candidate_statement(title: str, text: str) -> str:
    if text:
        return truncate_text(text, 420)
    return truncate_text(title, 420)


def extract_value_for_metric(observation: dict[str, Any]) -> float | None:
    statistics_obj = observation.get("statistics")
    if isinstance(statistics_obj, dict):
        for key in ("mean", "max", "p95", "min"):
            value = maybe_number(statistics_obj.get(key))
            if value is not None:
                return value
    return maybe_number(observation.get("value"))


def make_public_signal(
    *,
    run_id: str,
    round_id: str,
    source_skill: str,
    signal_kind: str,
    external_id: str,
    title: str,
    text: str,
    url: str,
    author_name: str,
    channel_name: str,
    language: str,
    query_text: str,
    published_at_utc: str | None,
    engagement: dict[str, Any],
    metadata: dict[str, Any],
    artifact_path: Path,
    record_locator: str,
    sha256_value: str,
    raw_obj: Any,
) -> dict[str, Any]:
    identity = external_id or url or f"{signal_kind}:{record_locator}"
    signal_hash = stable_hash(source_skill, identity, maybe_text(title), maybe_text(text))
    return {
        "signal_id": f"pubsig-{signal_hash[:12]}",
        "run_id": run_id,
        "round_id": round_id,
        "source_skill": source_skill,
        "signal_kind": signal_kind,
        "external_id": external_id,
        "title": title,
        "text": text,
        "url": url,
        "author_name": author_name,
        "channel_name": channel_name,
        "language": language,
        "query_text": query_text,
        "published_at_utc": published_at_utc,
        "captured_at_utc": utc_now_iso(),
        "engagement": engagement,
        "metadata": metadata,
        "artifact_path": str(artifact_path),
        "record_locator": record_locator,
        "sha256": sha256_value,
        "raw_json": raw_obj,
    }


def collect_records(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("records", "items", "data", "results"):
            candidate = payload.get(key)
            if isinstance(candidate, list):
                return [item for item in candidate if isinstance(item, dict)]
    return []


def strip_simple_html(value: str) -> str:
    return normalize_space(re.sub(r"<[^>]+>", " ", value))


def normalize_public_from_youtube_videos(
    path: Path,
    payload: Any,
    *,
    run_id: str,
    round_id: str,
    sha256_value: str,
) -> list[dict[str, Any]]:
    signals: list[dict[str, Any]] = []
    for index, record in enumerate(collect_records(payload)):
        video = record.get("video")
        if not isinstance(video, dict):
            continue
        video_id = maybe_text(record.get("video_id")) or maybe_text(video.get("id"))
        title = maybe_text(video.get("title"))
        description = maybe_text(video.get("description"))
        url = f"https://www.youtube.com/watch?v={video_id}" if video_id else ""
        signals.append(
            make_public_signal(
                run_id=run_id,
                round_id=round_id,
                source_skill="youtube-video-search",
                signal_kind="video",
                external_id=video_id,
                title=title,
                text=description,
                url=url,
                author_name=maybe_text(video.get("channel_title")),
                channel_name=maybe_text(video.get("channel_title")),
                language=maybe_text(video.get("default_language") or video.get("default_audio_language")),
                query_text=maybe_text(record.get("query")),
                published_at_utc=to_rfc3339_z(parse_loose_datetime(video.get("published_at"))),
                engagement=video.get("statistics") if isinstance(video.get("statistics"), dict) else {},
                metadata={
                    "search_match": record.get("search_match"),
                    "content_details": video.get("content_details"),
                    "status": video.get("status"),
                },
                artifact_path=path,
                record_locator=f"$[{index}]",
                sha256_value=sha256_value,
                raw_obj=record,
            )
        )
    return signals


def normalize_public_from_youtube_comments(
    path: Path,
    payload: Any,
    *,
    run_id: str,
    round_id: str,
    sha256_value: str,
) -> list[dict[str, Any]]:
    signals: list[dict[str, Any]] = []
    for index, record in enumerate(collect_records(payload)):
        comment_id = maybe_text(record.get("comment_id"))
        video_id = maybe_text(record.get("video_id"))
        text = maybe_text(record.get("text_original") or record.get("text_display"))
        url = ""
        if video_id and comment_id:
            url = f"https://www.youtube.com/watch?v={video_id}&lc={comment_id}"
        signals.append(
            make_public_signal(
                run_id=run_id,
                round_id=round_id,
                source_skill="youtube-comments-fetch",
                signal_kind=maybe_text(record.get("comment_type")) or "comment",
                external_id=comment_id,
                title=truncate_text(text, 120),
                text=text,
                url=url,
                author_name=maybe_text(record.get("author_display_name")),
                channel_name=maybe_text(record.get("channel_id")),
                language="",
                query_text=maybe_text((record.get("source") or {}).get("search_terms")),
                published_at_utc=to_rfc3339_z(parse_loose_datetime(record.get("published_at"))),
                engagement={"like_count": maybe_number(record.get("like_count"))},
                metadata={
                    "video_id": video_id,
                    "thread_id": maybe_text(record.get("thread_id")),
                    "parent_comment_id": maybe_text(record.get("parent_comment_id")),
                    "source": record.get("source"),
                },
                artifact_path=path,
                record_locator=f"$[{index}]",
                sha256_value=sha256_value,
                raw_obj=record,
            )
        )
    return signals


def bluesky_uri_to_url(uri: str, author_handle: str) -> str:
    if not uri or not author_handle:
        return ""
    parts = uri.split("/")
    post_id = parts[-1] if parts else ""
    if not post_id:
        return ""
    return f"https://bsky.app/profile/{author_handle}/post/{post_id}"


def normalize_public_from_bluesky(
    path: Path,
    payload: Any,
    *,
    run_id: str,
    round_id: str,
    sha256_value: str,
) -> list[dict[str, Any]]:
    seeds: list[dict[str, Any]] = []
    if isinstance(payload, dict):
        if isinstance(payload.get("seed_posts"), list):
            seeds.extend(item for item in payload["seed_posts"] if isinstance(item, dict))
        if isinstance(payload.get("threads"), list):
            for thread in payload["threads"]:
                if not isinstance(thread, dict):
                    continue
                nodes = thread.get("nodes")
                if isinstance(nodes, list):
                    seeds.extend(node for node in nodes if isinstance(node, dict))
    elif isinstance(payload, list):
        seeds.extend(item for item in payload if isinstance(item, dict))

    signals: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, record in enumerate(seeds):
        uri = maybe_text(record.get("uri"))
        if uri and uri in seen_ids:
            continue
        if uri:
            seen_ids.add(uri)
        author_handle = maybe_text(record.get("author_handle"))
        text = maybe_text(record.get("text"))
        signals.append(
            make_public_signal(
                run_id=run_id,
                round_id=round_id,
                source_skill="bluesky-cascade-fetch",
                signal_kind="reply" if maybe_text(record.get("reply_parent_uri")) else "post",
                external_id=uri or maybe_text(record.get("cid")),
                title=truncate_text(text, 120),
                text=text,
                url=bluesky_uri_to_url(uri, author_handle),
                author_name=author_handle,
                channel_name=maybe_text(record.get("author_did")),
                language=",".join(record.get("langs", [])) if isinstance(record.get("langs"), list) else "",
                query_text="",
                published_at_utc=maybe_text(record.get("timestamp_utc")) or to_rfc3339_z(parse_loose_datetime(record.get("created_at"))),
                engagement={
                    "reply_count": maybe_number(record.get("reply_count")),
                    "repost_count": maybe_number(record.get("repost_count")),
                    "like_count": maybe_number(record.get("like_count")),
                    "quote_count": maybe_number(record.get("quote_count")),
                },
                metadata={
                    "author_did": maybe_text(record.get("author_did")),
                    "cid": maybe_text(record.get("cid")),
                    "reply_root_uri": maybe_text(record.get("reply_root_uri")),
                    "timestamp_source": maybe_text(record.get("timestamp_source")),
                },
                artifact_path=path,
                record_locator=f"$[{index}]",
                sha256_value=sha256_value,
                raw_obj=record,
            )
        )
    return signals


def extract_reggov_resource(record: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    if "detail" in record:
        detail = record.get("detail")
        if isinstance(detail, dict):
            resource = detail.get("data") if isinstance(detail.get("data"), dict) else detail.get("data")
            if isinstance(resource, dict):
                return resource, {"response_url": record.get("response_url"), "validation": record.get("validation")}
    return record if "attributes" in record else None, {}


def normalize_reggov_resource(
    path: Path,
    record: dict[str, Any],
    *,
    index: int,
    run_id: str,
    round_id: str,
    source_skill: str,
    sha256_value: str,
) -> dict[str, Any] | None:
    resource, metadata = extract_reggov_resource(record)
    if not isinstance(resource, dict):
        return None
    attrs = resource.get("attributes") if isinstance(resource.get("attributes"), dict) else {}
    links = resource.get("links") if isinstance(resource.get("links"), dict) else {}
    text = maybe_text(
        attrs.get("comment")
        or attrs.get("commentText")
        or attrs.get("commentOn")
        or attrs.get("title")
        or attrs.get("organization")
    )
    title = maybe_text(attrs.get("title") or attrs.get("subject") or attrs.get("organization")) or truncate_text(text, 120)
    metadata.update(
        {
            "docket_id": maybe_text(attrs.get("docketId")),
            "document_type": maybe_text(attrs.get("documentType")),
            "posted_date": maybe_text(attrs.get("postedDate")),
            "last_modified_date": maybe_text(attrs.get("lastModifiedDate")),
        }
    )
    return make_public_signal(
        run_id=run_id,
        round_id=round_id,
        source_skill=source_skill,
        signal_kind="policy-comment",
        external_id=maybe_text(resource.get("id") or record.get("comment_id")),
        title=title,
        text=text,
        url=maybe_text(links.get("self") or metadata.get("response_url")),
        author_name=maybe_text(attrs.get("organization") or attrs.get("firstName")),
        channel_name=maybe_text(attrs.get("agencyId")),
        language="",
        query_text="",
        published_at_utc=to_rfc3339_z(
            parse_loose_datetime(attrs.get("postedDate") or attrs.get("lastModifiedDate"))
        ),
        engagement={},
        metadata=metadata,
        artifact_path=path,
        record_locator=f"$[{index}]",
        sha256_value=sha256_value,
        raw_obj=record,
    )


def normalize_public_from_reggov(
    path: Path,
    payload: Any,
    *,
    run_id: str,
    round_id: str,
    source_skill: str,
    sha256_value: str,
) -> list[dict[str, Any]]:
    signals: list[dict[str, Any]] = []
    for index, record in enumerate(collect_records(payload)):
        normalized = normalize_reggov_resource(
            path,
            record,
            index=index,
            run_id=run_id,
            round_id=round_id,
            source_skill=source_skill,
            sha256_value=sha256_value,
        )
        if normalized is not None:
            signals.append(normalized)
    return signals


def normalize_public_from_federal_register(
    path: Path,
    payload: Any,
    *,
    run_id: str,
    round_id: str,
    source_skill: str,
    sha256_value: str,
) -> list[dict[str, Any]]:
    signals: list[dict[str, Any]] = []
    records = payload.get("records") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        raise ValueError(
            "Federal Register raw artifact must use the canonical federal-register-doc-fetch "
            "payload with a top-level records array."
        )
    query_text = ""
    if isinstance(payload, dict):
        request_obj = payload.get("request")
        if isinstance(request_obj, dict):
            query_text = maybe_text(request_obj.get("term"))
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            continue
        agencies = record.get("agencies") if isinstance(record.get("agencies"), list) else []
        agency_names = [
            maybe_text(item.get("name") or item.get("raw_name") or item.get("slug"))
            for item in agencies
            if isinstance(item, dict) and maybe_text(item.get("name") or item.get("raw_name") or item.get("slug"))
        ]
        agency_slugs = [
            maybe_text(item.get("slug"))
            for item in agencies
            if isinstance(item, dict) and maybe_text(item.get("slug"))
        ]
        title = maybe_text(record.get("title"))
        abstract = maybe_text(record.get("abstract"))
        excerpts = strip_simple_html(maybe_text(record.get("excerpts")))
        text = abstract or excerpts or title
        signals.append(
            make_public_signal(
                run_id=run_id,
                round_id=round_id,
                source_skill=source_skill,
                signal_kind="policy-document",
                external_id=maybe_text(record.get("document_number") or record.get("html_url") or index),
                title=title or maybe_text(record.get("document_number")) or "Federal Register document",
                text=text,
                url=maybe_text(record.get("html_url") or record.get("pdf_url")),
                author_name="",
                channel_name=", ".join(agency_names),
                language="",
                query_text=query_text,
                published_at_utc=to_rfc3339_z(parse_loose_datetime(record.get("publication_date"))),
                engagement={},
                metadata={
                    "type": maybe_text(record.get("type")),
                    "document_number": maybe_text(record.get("document_number")),
                    "pdf_url": maybe_text(record.get("pdf_url")),
                    "public_inspection_pdf_url": maybe_text(record.get("public_inspection_pdf_url")),
                    "agencies": agency_names,
                    "agency_slugs": agency_slugs,
                    "topics": record.get("topics") if isinstance(record.get("topics"), list) else [],
                    "docket_ids": record.get("docket_ids") if isinstance(record.get("docket_ids"), list) else [],
                    "regulation_id_numbers": record.get("regulation_id_numbers") if isinstance(record.get("regulation_id_numbers"), list) else [],
                    "significant": record.get("significant"),
                    "comment_url": maybe_text(record.get("comment_url")),
                    "raw_text_url": maybe_text(record.get("raw_text_url")),
                    "source_page_number": record.get("source_page_number"),
                },
                artifact_path=path,
                record_locator=f"$.records[{index}]",
                sha256_value=sha256_value,
                raw_obj=record,
            )
        )
    return signals


def normalize_public_from_gdelt_doc(
    path: Path,
    payload: Any,
    *,
    run_id: str,
    round_id: str,
    sha256_value: str,
) -> list[dict[str, Any]]:
    signals: list[dict[str, Any]] = []
    if not isinstance(payload, dict):
        return signals

    if isinstance(payload.get("articles"), list):
        records = payload["articles"]
        for index, item in enumerate(records):
            if not isinstance(item, dict):
                continue
            title = maybe_text(item.get("title"))
            description = maybe_text(item.get("seendate") or item.get("domain"))
            signals.append(
                make_public_signal(
                    run_id=run_id,
                    round_id=round_id,
                    source_skill="gdelt-doc-search",
                    signal_kind="article",
                    external_id=maybe_text(item.get("url") or item.get("title")),
                    title=title,
                    text=title or description,
                    url=maybe_text(item.get("url")),
                    author_name="",
                    channel_name=maybe_text(item.get("domain")),
                    language=maybe_text(item.get("language") or item.get("sourcelang")),
                    query_text="",
                    published_at_utc=to_rfc3339_z(
                        parse_loose_datetime(item.get("seendate") or item.get("date"))
                    ),
                    engagement={},
                    metadata=item,
                    artifact_path=path,
                    record_locator=f"$.articles[{index}]",
                    sha256_value=sha256_value,
                    raw_obj=item,
                )
            )
        return signals

    for key in ("timeline", "data", "records"):
        candidate = payload.get(key)
        if not isinstance(candidate, list):
            continue
        for index, item in enumerate(candidate):
            if not isinstance(item, dict):
                continue
            title = maybe_text(item.get("title")) or "GDELT timeline bin"
            text = title
            if maybe_text(item.get("value")):
                text = f"{title} value={item.get('value')}"
            signals.append(
                make_public_signal(
                    run_id=run_id,
                    round_id=round_id,
                    source_skill="gdelt-doc-search",
                    signal_kind="timeline-bin",
                    external_id=maybe_text(item.get("date") or item.get("datetime") or index),
                    title=title,
                    text=text,
                    url=maybe_text(item.get("url")),
                    author_name="",
                    channel_name="",
                    language="",
                    query_text="",
                    published_at_utc=to_rfc3339_z(
                        parse_loose_datetime(item.get("date") or item.get("datetime"))
                    ),
                    engagement={},
                    metadata=item,
                    artifact_path=path,
                    record_locator=f"$.{key}[{index}]",
                    sha256_value=sha256_value,
                    raw_obj=item,
                )
            )
        if signals:
            return signals
    return signals


def normalize_public_from_gdelt_manifest(
    path: Path,
    payload: Any,
    *,
    run_id: str,
    round_id: str,
    source_skill: str,
    sha256_value: str,
) -> list[dict[str, Any]]:
    signals: list[dict[str, Any]] = []
    if not isinstance(payload, dict):
        return signals
    downloads = payload.get("downloads")
    if not isinstance(downloads, list):
        return signals
    for index, item in enumerate(downloads):
        if not isinstance(item, dict):
            continue
        output_path = maybe_text(item.get("output_path"))
        title = maybe_text(item.get("filename") or Path(output_path).name or "GDELT artifact")
        signals.append(
            make_public_signal(
                run_id=run_id,
                round_id=round_id,
                source_skill=source_skill,
                signal_kind="artifact-manifest",
                external_id=title,
                title=title,
                text="",
                url=maybe_text(item.get("url")),
                author_name="",
                channel_name="",
                language="",
                query_text="",
                published_at_utc=to_rfc3339_z(parse_loose_datetime(item.get("timestamp"))),
                engagement={},
                metadata=item,
                artifact_path=path,
                record_locator=f"$.downloads[{index}]",
                sha256_value=sha256_value,
                raw_obj=item,
            )
        )
    return signals


def normalize_public_source(
    source_skill: str,
    path: Path,
    *,
    run_id: str,
    round_id: str,
) -> list[dict[str, Any]]:
    sha256_value = file_sha256(path)
    payload = parse_path_payload(path)
    if source_skill == "youtube-video-search":
        return normalize_public_from_youtube_videos(path, payload, run_id=run_id, round_id=round_id, sha256_value=sha256_value)
    if source_skill == "youtube-comments-fetch":
        return normalize_public_from_youtube_comments(path, payload, run_id=run_id, round_id=round_id, sha256_value=sha256_value)
    if source_skill == "bluesky-cascade-fetch":
        return normalize_public_from_bluesky(path, payload, run_id=run_id, round_id=round_id, sha256_value=sha256_value)
    if source_skill == "federal-register-doc-fetch":
        return normalize_public_from_federal_register(
            path,
            payload,
            run_id=run_id,
            round_id=round_id,
            source_skill=source_skill,
            sha256_value=sha256_value,
        )
    if source_skill in {"regulationsgov-comments-fetch", "regulationsgov-comment-detail-fetch"}:
        return normalize_public_from_reggov(
            path,
            payload,
            run_id=run_id,
            round_id=round_id,
            source_skill=source_skill,
            sha256_value=sha256_value,
        )
    if source_skill == "gdelt-doc-search":
        return normalize_public_from_gdelt_doc(path, payload, run_id=run_id, round_id=round_id, sha256_value=sha256_value)
    if source_skill in {"gdelt-events-fetch", "gdelt-mentions-fetch", "gdelt-gkg-fetch"}:
        return normalize_public_from_gdelt_manifest(
            path,
            payload,
            run_id=run_id,
            round_id=round_id,
            source_skill=source_skill,
            sha256_value=sha256_value,
        )
    raise ValueError(f"Unsupported public source skill: {source_skill}")


def normalize_public_source_cached(
    *,
    run_dir: Path,
    source_skill: str,
    path: Path,
    run_id: str,
    round_id: str,
) -> tuple[list[dict[str, Any]], str]:
    artifact_sha256 = file_sha256(path)
    cache_path = normalize_cache_path(
        run_dir,
        domain="public",
        source_skill=source_skill,
        run_id=run_id,
        round_id=round_id,
        artifact_sha256=artifact_sha256,
    )
    cached = read_cache_payload(cache_path)
    if isinstance(cached, dict):
        signals = cached.get("signals")
        if (
            cached.get("cache_version") == NORMALIZE_CACHE_VERSION
            and cached.get("artifact_sha256") == artifact_sha256
            and isinstance(signals, list)
        ):
            return [item for item in signals if isinstance(item, dict)], "hit"

    signals = normalize_public_source(source_skill, path, run_id=run_id, round_id=round_id)
    write_cache_payload(
        cache_path,
        {
            "cache_version": NORMALIZE_CACHE_VERSION,
            "domain": "public",
            "source_skill": source_skill,
            "run_id": run_id,
            "round_id": round_id,
            "artifact_path": str(path),
            "artifact_sha256": artifact_sha256,
            "signals": signals,
        },
    )
    return signals, "miss"


def public_signals_to_claims(
    *,
    mission: dict[str, Any],
    round_id: str,
    signals: list[dict[str, Any]],
    max_claims: int,
) -> list[dict[str, Any]]:
    run_id = mission_run_id(mission)
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for signal in signals:
        source_text = normalize_space(
            " ".join(
                part
                for part in (
                    maybe_text(signal.get("title")),
                    maybe_text(signal.get("text")),
                )
                if part
            )
        )
        if not source_text:
            continue
        claim_type = claim_type_from_text(source_text)
        if claim_type == "other":
            continue
        fingerprint = semantic_fingerprint(source_text)
        if not fingerprint:
            fingerprint = signal["signal_id"]
        groups[f"{claim_type}|{fingerprint}"].append(signal)

    ranked = sorted(
        groups.values(),
        key=lambda items: (
            -len(items),
            -(parse_loose_datetime(items[0].get("published_at_utc")) or datetime(1970, 1, 1, tzinfo=timezone.utc)).timestamp(),
            items[0]["signal_id"],
        ),
    )

    claims: list[dict[str, Any]] = []
    place_scope = mission_place_scope(mission)
    time_window = mission_window(mission)
    for index, items in enumerate(ranked[:max_claims], start=1):
        lead = items[0]
        combined_text = maybe_text(lead.get("text")) or maybe_text(lead.get("title"))
        summary = truncate_text(maybe_text(lead.get("title")) or combined_text, 180)
        claim_type = claim_type_from_text(summary + " " + combined_text)
        claim = {
            "schema_version": SCHEMA_VERSION,
            "claim_id": emit_row_id("claim", index),
            "run_id": run_id,
            "round_id": round_id,
            "agent_role": "sociologist",
            "claim_type": claim_type,
            "status": "candidate",
            "summary": summary or f"Candidate claim from {lead['source_skill']}",
            "statement": candidate_statement(summary, combined_text or summary),
            "priority": min(index, 5),
            "needs_physical_validation": claim_type in PHYSICAL_CLAIM_TYPES,
            "time_window": time_window,
            "place_scope": place_scope,
            "public_refs": [artifact_ref(item) for item in items[:8]],
        }
        validate_payload("claim", claim)
        claims.append(claim)
    return claims


def save_public_db(db_path: Path, signals: list[dict[str, Any]], claims: list[dict[str, Any]]) -> None:
    init_sqlite_db(db_path, PUBLIC_DDL_PATH)
    with sqlite3.connect(db_path) as conn:
        insert_many(
            conn,
            """
            INSERT OR REPLACE INTO public_signals (
                signal_id, run_id, round_id, source_skill, signal_kind, external_id, title, text,
                url, author_name, channel_name, language, query_text, published_at_utc,
                captured_at_utc, engagement_json, metadata_json, artifact_path, record_locator,
                sha256, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    signal["signal_id"],
                    signal["run_id"],
                    signal["round_id"],
                    signal["source_skill"],
                    signal["signal_kind"],
                    signal["external_id"],
                    signal["title"],
                    signal["text"],
                    signal["url"],
                    signal["author_name"],
                    signal["channel_name"],
                    signal["language"],
                    signal["query_text"],
                    signal["published_at_utc"],
                    signal["captured_at_utc"],
                    json.dumps(signal.get("engagement", {}), ensure_ascii=True, sort_keys=True),
                    json.dumps(signal.get("metadata", {}), ensure_ascii=True, sort_keys=True),
                    signal["artifact_path"],
                    signal["record_locator"],
                    signal["sha256"],
                    json.dumps(signal.get("raw_json"), ensure_ascii=True, sort_keys=True),
                )
                for signal in signals
            ),
        )
        insert_many(
            conn,
            """
            INSERT OR REPLACE INTO claim_candidates (
                claim_id, run_id, round_id, claim_type, priority, summary, statement,
                source_signal_ids_json, claim_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    claim["claim_id"],
                    claim["run_id"],
                    claim["round_id"],
                    claim["claim_type"],
                    claim["priority"],
                    claim["summary"],
                    claim["statement"],
                    json.dumps(
                        [ref.get("external_id") or ref.get("record_locator") for ref in claim.get("public_refs", [])],
                        ensure_ascii=True,
                        sort_keys=True,
                    ),
                    json.dumps(claim, ensure_ascii=True, sort_keys=True),
                )
                for claim in claims
            ),
        )


def first_datetime_and_last(values: list[dict[str, Any]]) -> tuple[str, str] | None:
    datetimes: list[datetime] = []
    for item in values:
        observed = parse_loose_datetime(item.get("observed_at_utc") or item.get("window_start_utc"))
        if observed is not None:
            datetimes.append(observed)
    if not datetimes:
        return None
    datetimes.sort()
    return to_rfc3339_z(datetimes[0]) or "", to_rfc3339_z(datetimes[-1]) or ""


def aggregate_stats(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"min": None, "max": None, "mean": None, "p95": None}
    return {
        "min": min(values),
        "max": max(values),
        "mean": statistics.fmean(values),
        "p95": percentile95(values),
    }


def make_environment_signal(
    *,
    run_id: str,
    round_id: str,
    source_skill: str,
    signal_kind: str,
    metric: str,
    value: float | None,
    unit: str,
    observed_at_utc: str | None,
    window_start_utc: str | None,
    window_end_utc: str | None,
    latitude: float | None,
    longitude: float | None,
    bbox: dict[str, Any] | None,
    quality_flags: list[str],
    metadata: dict[str, Any],
    artifact_path: Path,
    record_locator: str,
    sha256_value: str,
    raw_obj: Any,
) -> dict[str, Any]:
    canonical_metric = canonical_environment_metric(metric)
    signal_hash = stable_hash(
        source_skill,
        canonical_metric,
        observed_at_utc or window_start_utc or record_locator,
        value,
        latitude,
        longitude,
    )
    return {
        "signal_id": f"envsig-{signal_hash[:12]}",
        "run_id": run_id,
        "round_id": round_id,
        "source_skill": source_skill,
        "signal_kind": signal_kind,
        "metric": canonical_metric,
        "value": value,
        "unit": unit or "unknown",
        "observed_at_utc": observed_at_utc,
        "window_start_utc": window_start_utc,
        "window_end_utc": window_end_utc,
        "latitude": latitude,
        "longitude": longitude,
        "bbox": bbox,
        "quality_flags": quality_flags,
        "metadata": metadata,
        "artifact_path": str(artifact_path),
        "record_locator": record_locator,
        "sha256": sha256_value,
        "raw_json": raw_obj,
    }


def open_meteo_point_scope(record: dict[str, Any], default_scope: dict[str, Any]) -> dict[str, Any]:
    lat = maybe_number(record.get("latitude"))
    lon = maybe_number(record.get("longitude"))
    if lat is None or lon is None:
        return default_scope
    return {
        "label": maybe_text(record.get("timezone")) or default_scope["label"],
        "geometry": {"type": "Point", "latitude": lat, "longitude": lon},
    }


def iter_open_meteo_signals(
    path: Path,
    payload: Any,
    *,
    run_id: str,
    round_id: str,
    source_skill: str,
    sha256_value: str,
) -> list[dict[str, Any]]:
    signals: list[dict[str, Any]] = []
    records = payload.get("records") if isinstance(payload, dict) else payload
    if not isinstance(records, list):
        return signals
    for record_index, record in enumerate(records):
        if not isinstance(record, dict):
            continue
        latitude = maybe_number(record.get("latitude"))
        longitude = maybe_number(record.get("longitude"))
        for section_name, units_name in (("hourly", "hourly_units"), ("daily", "daily_units")):
            section = record.get(section_name)
            if not isinstance(section, dict):
                continue
            units = record.get(units_name) if isinstance(record.get(units_name), dict) else {}
            times = section.get("time") if isinstance(section.get("time"), list) else []
            for metric, series in section.items():
                if metric == "time" or not isinstance(series, list):
                    continue
                unit = maybe_text(units.get(metric)) or "unknown"
                for value_index, raw_value in enumerate(series):
                    numeric_value = maybe_number(raw_value)
                    if numeric_value is None:
                        continue
                    observed_at = parse_loose_datetime(times[value_index]) if value_index < len(times) else None
                    signals.append(
                        make_environment_signal(
                            run_id=run_id,
                            round_id=round_id,
                            source_skill=source_skill,
                            signal_kind=section_name,
                            metric=metric,
                            value=numeric_value,
                            unit=unit,
                            observed_at_utc=to_rfc3339_z(observed_at),
                            window_start_utc=None,
                            window_end_utc=None,
                            latitude=latitude,
                            longitude=longitude,
                            bbox=None,
                            quality_flags=(
                                ["modeled-background"]
                                if source_skill == "open-meteo-air-quality-fetch"
                                else ["hydrology-model"]
                                if source_skill == "open-meteo-flood-fetch"
                                else ["reanalysis-or-model"]
                            ),
                            metadata={
                                "section": section_name,
                                "timezone": maybe_text(record.get("timezone")),
                                "elevation": maybe_number(record.get("elevation")),
                                "record_index": record_index,
                            },
                            artifact_path=path,
                            record_locator=f"$.records[{record_index}].{section_name}.{metric}[{value_index}]",
                            sha256_value=sha256_value,
                            raw_obj=raw_value,
                        )
                    )
    return signals


def iter_nasa_firms_signals(
    path: Path,
    payload: Any,
    *,
    run_id: str,
    round_id: str,
    sha256_value: str,
) -> list[dict[str, Any]]:
    signals: list[dict[str, Any]] = []
    if not isinstance(payload, dict):
        return signals
    rows = payload.get("records")
    if not isinstance(rows, list):
        return signals
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        bbox = None
        signals.append(
            make_environment_signal(
                run_id=run_id,
                round_id=round_id,
                source_skill="nasa-firms-fire-fetch",
                signal_kind="fire-detection",
                metric="fire_detection",
                value=1.0,
                unit="count",
                observed_at_utc=maybe_text(row.get("_acquired_at_utc")),
                window_start_utc=maybe_text(row.get("_chunk_start_date")),
                window_end_utc=maybe_text(row.get("_chunk_end_date")),
                latitude=maybe_number(row.get("_latitude")),
                longitude=maybe_number(row.get("_longitude")),
                bbox=bbox,
                quality_flags=["satellite-detection"],
                metadata={
                    "confidence": maybe_text(row.get("confidence")),
                    "satellite": maybe_text(row.get("satellite")),
                    "instrument": maybe_text(row.get("instrument")),
                    "frp": maybe_number(row.get("frp")),
                },
                artifact_path=path,
                record_locator=f"$.records[{index}]",
                sha256_value=sha256_value,
                raw_obj=row,
            )
        )
    return signals


def unwrap_openaq_payload(payload: Any) -> Any:
    if isinstance(payload, dict) and "result" in payload:
        return unwrap_openaq_payload(payload["result"])
    return payload


def extract_nested_value(row: dict[str, Any], *paths: str) -> Any:
    for path in paths:
        current: Any = row
        ok = True
        for part in path.split("."):
            if not isinstance(current, dict):
                ok = False
                break
            current = current.get(part)
        if ok and current is not None:
            return current
    return None


def openaq_row_to_signal(
    row: dict[str, Any],
    *,
    path: Path,
    run_id: str,
    round_id: str,
    index: int,
    sha256_value: str,
) -> dict[str, Any] | None:
    metric = maybe_text(
        extract_nested_value(row, "parameter.name", "parameter", "parameterName", "metric", "name")
    )
    unit = maybe_text(extract_nested_value(row, "parameter.units", "unit", "units")) or "unknown"
    value = None
    for key in OPENAQ_VALUE_KEYS:
        value = maybe_number(extract_nested_value(row, key))
        if value is not None:
            break
    if value is None or not metric:
        return None
    timestamp_text = ""
    timestamp_candidate = extract_nested_value(row, "date.utc", "date.local")
    if timestamp_candidate is None:
        for key in OPENAQ_TIME_KEYS:
            timestamp_candidate = extract_nested_value(row, key)
            if timestamp_candidate is not None:
                break
    if timestamp_candidate is not None:
        timestamp_text = maybe_text(timestamp_candidate)
    coordinates = row.get("coordinates") if isinstance(row.get("coordinates"), dict) else {}
    latitude = maybe_number(coordinates.get("latitude"))
    longitude = maybe_number(coordinates.get("longitude"))
    if latitude is None:
        for key in OPENAQ_LAT_KEYS:
            latitude = maybe_number(extract_nested_value(row, key))
            if latitude is not None:
                break
    if longitude is None:
        for key in OPENAQ_LON_KEYS:
            longitude = maybe_number(extract_nested_value(row, key))
            if longitude is not None:
                break
    metadata = {
        "location_id": extract_nested_value(row, "location.id", "locationId", "locationsId"),
        "location_name": maybe_text(extract_nested_value(row, "location.name", "location")),
        "sensor_id": extract_nested_value(row, "sensor.id", "sensorId", "sensorsId"),
        "provider": maybe_text(extract_nested_value(row, "provider.name", "provider")),
    }
    return make_environment_signal(
        run_id=run_id,
        round_id=round_id,
        source_skill="openaq-data-fetch",
        signal_kind="station-measurement",
        metric=metric,
        value=value,
        unit=unit,
        observed_at_utc=to_rfc3339_z(parse_loose_datetime(timestamp_text)),
        window_start_utc=None,
        window_end_utc=None,
        latitude=latitude,
        longitude=longitude,
        bbox=None,
        quality_flags=["station-observation"],
        metadata=metadata,
        artifact_path=path,
        record_locator=f"$[{index}]",
        sha256_value=sha256_value,
        raw_obj=row,
    )


def iter_csv_rows(path: Path) -> list[dict[str, str]]:
    open_func = gzip.open if path.suffix.lower() == ".gz" else open
    with open_func(path, "rt", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        return [dict(row) for row in reader]


def iter_openaq_signals(
    path: Path,
    *,
    run_id: str,
    round_id: str,
) -> list[dict[str, Any]]:
    sha256_value = file_sha256(path)
    suffix = path.suffix.lower()
    rows: list[dict[str, Any]] = []
    if suffix in {".json", ".jsonl"}:
        payload = unwrap_openaq_payload(parse_path_payload(path))
        rows = collect_records(payload)
        if not rows and isinstance(payload, dict):
            output_path = maybe_text(payload.get("output_path"))
            if output_path:
                nested_path = Path(output_path).expanduser().resolve()
                if nested_path.exists():
                    return iter_openaq_signals(nested_path, run_id=run_id, round_id=round_id)
    elif suffix in {".csv", ".gz"}:
        rows = iter_csv_rows(path)
    else:
        raise ValueError(f"Unsupported OpenAQ artifact path: {path}")

    signals: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        normalized = openaq_row_to_signal(
            row,
            path=path,
            run_id=run_id,
            round_id=round_id,
            index=index,
            sha256_value=sha256_value,
        )
        if normalized is not None:
            signals.append(normalized)
    return signals


def iter_airnow_signals(
    path: Path,
    *,
    run_id: str,
    round_id: str,
) -> list[dict[str, Any]]:
    payload = parse_path_payload(path)
    rows = payload.get("records") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return []

    sha256_value = file_sha256(path)
    signals: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        parameter_name = maybe_text(row.get("parameter_name")).upper()
        metric_base = AIRNOW_PARAMETER_METRIC_MAP.get(parameter_name)
        if not metric_base:
            continue
        latitude = maybe_number(row.get("latitude"))
        longitude = maybe_number(row.get("longitude"))
        observed_at_utc = to_rfc3339_z(parse_loose_datetime(row.get("observed_at_utc")))
        metadata = {
            "aqsid": maybe_text(row.get("aqsid")),
            "site_name": maybe_text(row.get("site_name")),
            "status": maybe_text(row.get("status")),
            "epa_region": maybe_text(row.get("epa_region")),
            "country_code": maybe_text(row.get("country_code")),
            "state_name": maybe_text(row.get("state_name")),
            "data_source": maybe_text(row.get("data_source")),
            "reporting_areas": row.get("reporting_areas") if isinstance(row.get("reporting_areas"), list) else [],
            "aqi_kind": maybe_text(row.get("aqi_kind")),
            "measured": row.get("measured"),
            "source_file_url": maybe_text(row.get("source_file_url")),
        }
        raw_concentration = maybe_number(row.get("raw_concentration"))
        if raw_concentration is not None:
            signals.append(
                make_environment_signal(
                    run_id=run_id,
                    round_id=round_id,
                    source_skill="airnow-hourly-obs-fetch",
                    signal_kind="station-measurement",
                    metric=metric_base,
                    value=raw_concentration,
                    unit=maybe_text(row.get("unit")) or "unknown",
                    observed_at_utc=observed_at_utc,
                    window_start_utc=None,
                    window_end_utc=None,
                    latitude=latitude,
                    longitude=longitude,
                    bbox=None,
                    quality_flags=["station-observation", "preliminary", "airnow-file-product"],
                    metadata=metadata,
                    artifact_path=path,
                    record_locator=f"$.records[{index}].raw_concentration",
                    sha256_value=sha256_value,
                    raw_obj=row,
                )
            )
        aqi_value = maybe_number(row.get("aqi_value"))
        if aqi_value is not None:
            signals.append(
                make_environment_signal(
                    run_id=run_id,
                    round_id=round_id,
                    source_skill="airnow-hourly-obs-fetch",
                    signal_kind="station-aqi",
                    metric=f"{metric_base}_aqi",
                    value=aqi_value,
                    unit="AQI",
                    observed_at_utc=observed_at_utc,
                    window_start_utc=None,
                    window_end_utc=None,
                    latitude=latitude,
                    longitude=longitude,
                    bbox=None,
                    quality_flags=["station-aqi", "preliminary", "airnow-file-product"],
                    metadata=metadata,
                    artifact_path=path,
                    record_locator=f"$.records[{index}].aqi_value",
                    sha256_value=sha256_value,
                    raw_obj=row,
                )
            )
    return signals


def iter_usgs_water_iv_signals(
    path: Path,
    *,
    run_id: str,
    round_id: str,
) -> list[dict[str, Any]]:
    payload = parse_path_payload(path)
    rows = payload.get("records") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return []

    sha256_value = file_sha256(path)
    signals: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        parameter_code = maybe_text(row.get("parameter_code"))
        metric = USGS_PARAMETER_METRIC_MAP.get(parameter_code)
        if not metric:
            continue
        value = maybe_number(row.get("value"))
        if value is None:
            continue
        latitude = maybe_number(row.get("latitude"))
        longitude = maybe_number(row.get("longitude"))
        observed_at_utc = to_rfc3339_z(parse_loose_datetime(row.get("observed_at_utc")))
        quality_flags = ["station-observation", "usgs-water-services-iv"]
        if bool(row.get("provisional")):
            quality_flags.append("provisional")
        metadata = {
            "site_number": maybe_text(row.get("site_number")),
            "site_name": maybe_text(row.get("site_name")),
            "agency_code": maybe_text(row.get("agency_code")),
            "site_type": maybe_text(row.get("site_type")),
            "state_code": maybe_text(row.get("state_code")),
            "county_code": maybe_text(row.get("county_code")),
            "huc_code": maybe_text(row.get("huc_code")),
            "parameter_code": parameter_code,
            "variable_name": maybe_text(row.get("variable_name")),
            "variable_description": maybe_text(row.get("variable_description")),
            "statistic_code": maybe_text(row.get("statistic_code")),
            "qualifiers": row.get("qualifiers") if isinstance(row.get("qualifiers"), list) else [],
            "source_query_url": maybe_text(row.get("source_query_url")),
        }
        signals.append(
            make_environment_signal(
                run_id=run_id,
                round_id=round_id,
                source_skill="usgs-water-iv-fetch",
                signal_kind="station-measurement",
                metric=metric,
                value=value,
                unit=maybe_text(row.get("unit")) or "unknown",
                observed_at_utc=observed_at_utc,
                window_start_utc=None,
                window_end_utc=None,
                latitude=latitude,
                longitude=longitude,
                bbox=None,
                quality_flags=quality_flags,
                metadata=metadata,
                artifact_path=path,
                record_locator=f"$.records[{index}].value",
                sha256_value=sha256_value,
                raw_obj=row,
            )
        )
    return signals


def normalize_environment_source(
    source_skill: str,
    path: Path,
    *,
    run_id: str,
    round_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    signals: list[dict[str, Any]] = []
    extra_observations: list[dict[str, Any]] = []
    if source_skill in {"open-meteo-historical-fetch", "open-meteo-air-quality-fetch", "open-meteo-flood-fetch"}:
        sha256_value = file_sha256(path)
        payload = parse_path_payload(path)
        signals = iter_open_meteo_signals(
            path,
            payload,
            run_id=run_id,
            round_id=round_id,
            source_skill=source_skill,
            sha256_value=sha256_value,
        )
    elif source_skill == "nasa-firms-fire-fetch":
        sha256_value = file_sha256(path)
        payload = parse_path_payload(path)
        signals = iter_nasa_firms_signals(path, payload, run_id=run_id, round_id=round_id, sha256_value=sha256_value)
        if isinstance(payload, dict) and isinstance(payload.get("records"), list) and not payload.get("records"):
            run_id_value = run_id
            extra_observations.append(
                {
                    "schema_version": SCHEMA_VERSION,
                    "observation_id": "obs-placeholder",
                    "run_id": run_id_value,
                    "round_id": round_id,
                    "agent_role": "environmentalist",
                    "source_skill": "nasa-firms-fire-fetch",
                    "metric": "fire_detection_count",
                    "aggregation": "event-count",
                    "value": 0.0,
                    "unit": "count",
                    "statistics": {"min": 0.0, "max": 0.0, "mean": 0.0, "p95": 0.0},
                    "time_window": {
                        "start_utc": maybe_text((payload.get("request") or {}).get("start_date")) or utc_now_iso(),
                        "end_utc": maybe_text((payload.get("request") or {}).get("end_date")) or utc_now_iso(),
                    },
                    "place_scope": {"label": "Mission region", "geometry": {"type": "Point", "latitude": 0.0, "longitude": 0.0}},
                    "quality_flags": ["satellite-detection", "zero-detections"],
                    "provenance": {
                        "source_skill": "nasa-firms-fire-fetch",
                        "artifact_path": str(path),
                        "sha256": sha256_value,
                    },
                }
            )
    elif source_skill == "openaq-data-fetch":
        signals = iter_openaq_signals(path, run_id=run_id, round_id=round_id)
    elif source_skill == "airnow-hourly-obs-fetch":
        signals = iter_airnow_signals(path, run_id=run_id, round_id=round_id)
    elif source_skill == "usgs-water-iv-fetch":
        signals = iter_usgs_water_iv_signals(path, run_id=run_id, round_id=round_id)
    else:
        raise ValueError(f"Unsupported environment source skill: {source_skill}")
    return signals, extra_observations


def normalize_environment_source_cached(
    *,
    run_dir: Path,
    source_skill: str,
    path: Path,
    run_id: str,
    round_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    artifact_sha256 = file_sha256(path)
    cache_path = normalize_cache_path(
        run_dir,
        domain="environment",
        source_skill=source_skill,
        run_id=run_id,
        round_id=round_id,
        artifact_sha256=artifact_sha256,
    )
    cached = read_cache_payload(cache_path)
    if isinstance(cached, dict):
        signals = cached.get("signals")
        extra_observations = cached.get("extra_observations")
        if (
            cached.get("cache_version") == NORMALIZE_CACHE_VERSION
            and cached.get("artifact_sha256") == artifact_sha256
            and isinstance(signals, list)
            and isinstance(extra_observations, list)
        ):
            return (
                [item for item in signals if isinstance(item, dict)],
                [item for item in extra_observations if isinstance(item, dict)],
                "hit",
            )

    signals, extra_observations = normalize_environment_source(source_skill, path, run_id=run_id, round_id=round_id)
    write_cache_payload(
        cache_path,
        {
            "cache_version": NORMALIZE_CACHE_VERSION,
            "domain": "environment",
            "source_skill": source_skill,
            "run_id": run_id,
            "round_id": round_id,
            "artifact_path": str(path),
            "artifact_sha256": artifact_sha256,
            "signals": signals,
            "extra_observations": extra_observations,
        },
    )
    return signals, extra_observations, "miss"


def observation_group_key(signal: dict[str, Any], mission_scope: dict[str, Any]) -> tuple[str, str, str]:
    metric = canonical_environment_metric(signal.get("metric"))
    source_skill = maybe_text(signal.get("source_skill"))
    lat = maybe_number(signal.get("latitude"))
    lon = maybe_number(signal.get("longitude"))
    if lat is None or lon is None:
        return (source_skill, metric, stable_hash(json.dumps(mission_scope, sort_keys=True))[:8])
    return (source_skill, metric, f"{lat:.3f},{lon:.3f}")


def derive_place_scope(signals: list[dict[str, Any]], mission_scope: dict[str, Any]) -> dict[str, Any]:
    if not signals:
        return mission_scope
    latitudes = [maybe_number(item.get("latitude")) for item in signals]
    longitudes = [maybe_number(item.get("longitude")) for item in signals]
    if any(value is None for value in latitudes + longitudes):
        return mission_scope
    unique_points = {(round(float(lat), 3), round(float(lon), 3)) for lat, lon in zip(latitudes, longitudes)}
    if len(unique_points) != 1:
        return mission_scope
    latitude = statistics.fmean(float(value) for value in latitudes if value is not None)
    longitude = statistics.fmean(float(value) for value in longitudes if value is not None)
    return {
        "label": mission_scope["label"],
        "geometry": {"type": "Point", "latitude": latitude, "longitude": longitude},
    }


def environment_signals_to_observations(
    *,
    mission: dict[str, Any],
    round_id: str,
    signals: list[dict[str, Any]],
    extra_observations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    run_id = mission_run_id(mission)
    mission_scope = mission_place_scope(mission)
    mission_time_window = mission_window(mission)
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for signal in signals:
        grouped[observation_group_key(signal, mission_scope)].append(signal)

    observations: list[dict[str, Any]] = []
    counter = 1
    for (_, metric, _), group in sorted(grouped.items()):
        values = [float(signal["value"]) for signal in group if maybe_number(signal.get("value")) is not None]
        if not values:
            continue
        source_skill = group[0]["source_skill"]
        output_metric = metric
        aggregation = "window-summary" if len(values) > 1 else "point"
        value = statistics.fmean(values) if len(values) > 1 else values[0]
        if source_skill == "nasa-firms-fire-fetch" and metric == "fire_detection":
            output_metric = "fire_detection_count"
            aggregation = "event-count"
            value = float(len(group))
        window = first_datetime_and_last(group)
        if window is None:
            time_window = mission_time_window
        else:
            start_utc, end_utc = window
            time_window = {"start_utc": start_utc or mission_time_window["start_utc"], "end_utc": end_utc or mission_time_window["end_utc"]}
        quality_flags = sorted({flag for signal in group for flag in signal.get("quality_flags", [])})
        observation = {
            "schema_version": SCHEMA_VERSION,
            "observation_id": emit_row_id("obs", counter),
            "run_id": run_id,
            "round_id": round_id,
            "agent_role": "environmentalist",
            "source_skill": source_skill,
            "metric": output_metric,
            "aggregation": aggregation,
            "value": value,
            "unit": "count" if output_metric == "fire_detection_count" else group[0]["unit"],
            "statistics": aggregate_stats(values),
            "time_window": time_window,
            "place_scope": derive_place_scope(group, mission_scope),
            "quality_flags": quality_flags,
            "provenance": artifact_ref(group[0]),
        }
        validate_payload("observation", observation)
        observations.append(observation)
        counter += 1

    for item in extra_observations:
        item["observation_id"] = emit_row_id("obs", counter)
        item["run_id"] = run_id
        item["round_id"] = round_id
        item["place_scope"] = mission_scope
        item["time_window"] = mission_time_window
        validate_payload("observation", item)
        observations.append(item)
        counter += 1
    return observations


def save_environment_db(db_path: Path, signals: list[dict[str, Any]], observations: list[dict[str, Any]]) -> None:
    init_sqlite_db(db_path, ENVIRONMENT_DDL_PATH)
    with sqlite3.connect(db_path) as conn:
        insert_many(
            conn,
            """
            INSERT OR REPLACE INTO environment_signals (
                signal_id, run_id, round_id, source_skill, signal_kind, metric, value, unit,
                observed_at_utc, window_start_utc, window_end_utc, latitude, longitude,
                bbox_json, quality_flags_json, metadata_json, artifact_path, record_locator,
                sha256, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    signal["signal_id"],
                    signal["run_id"],
                    signal["round_id"],
                    signal["source_skill"],
                    signal["signal_kind"],
                    signal["metric"],
                    signal["value"],
                    signal["unit"],
                    signal["observed_at_utc"],
                    signal["window_start_utc"],
                    signal["window_end_utc"],
                    signal["latitude"],
                    signal["longitude"],
                    json.dumps(signal.get("bbox"), ensure_ascii=True, sort_keys=True) if signal.get("bbox") is not None else None,
                    json.dumps(signal.get("quality_flags", []), ensure_ascii=True, sort_keys=True),
                    json.dumps(signal.get("metadata", {}), ensure_ascii=True, sort_keys=True),
                    signal["artifact_path"],
                    signal["record_locator"],
                    signal["sha256"],
                    json.dumps(signal.get("raw_json"), ensure_ascii=True, sort_keys=True),
                )
                for signal in signals
            ),
        )
        insert_many(
            conn,
            """
            INSERT OR REPLACE INTO observation_summaries (
                observation_id, run_id, round_id, metric, source_skill, observation_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    observation["observation_id"],
                    observation["run_id"],
                    observation["round_id"],
                    observation["metric"],
                    observation["source_skill"],
                    json.dumps(observation, ensure_ascii=True, sort_keys=True),
                )
                for observation in observations
            ),
        )


def load_canonical_list(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    payload = read_json(path)
    if not isinstance(payload, list):
        raise ValueError(f"Expected list in {path}")
    return [item for item in payload if isinstance(item, dict)]


def observation_signature_payload(observation: dict[str, Any]) -> dict[str, Any]:
    provenance = observation.get("provenance")
    if not isinstance(provenance, dict):
        provenance = {}
    return {
        "source_skill": maybe_text(observation.get("source_skill")),
        "metric": maybe_text(observation.get("metric")),
        "aggregation": maybe_text(observation.get("aggregation")),
        "value": observation.get("value"),
        "unit": maybe_text(observation.get("unit")),
        "statistics": observation.get("statistics"),
        "time_window": observation.get("time_window"),
        "place_scope": observation.get("place_scope"),
        "quality_flags": sorted(
            maybe_text(item) for item in observation.get("quality_flags", []) if maybe_text(item)
        ),
        "provenance": {
            "source_skill": maybe_text(provenance.get("source_skill")),
            "record_locator": maybe_text(provenance.get("record_locator")),
            "external_id": maybe_text(provenance.get("external_id")),
            "sha256": maybe_text(provenance.get("sha256")),
        },
    }


def shared_observation_id(observation: dict[str, Any]) -> str:
    signature = stable_hash(stable_json(observation_signature_payload(observation)))
    return f"obs-{signature[:12]}"


def materialize_shared_observation(observation: dict[str, Any]) -> dict[str, Any]:
    item = dict(observation)
    item["observation_id"] = shared_observation_id(observation)
    return item


def merge_effective_observations(*observation_groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged_by_signature: dict[str, dict[str, Any]] = {}
    ordered_signatures: list[str] = []
    for group in observation_groups:
        for observation in group:
            signature_payload = observation_signature_payload(observation)
            signature = stable_hash(stable_json(signature_payload))
            if signature not in merged_by_signature:
                ordered_signatures.append(signature)
            merged_by_signature[signature] = materialize_shared_observation(observation)
    return [merged_by_signature[signature] for signature in ordered_signatures]


def effective_shared_observations(
    run_dir: Path,
    round_id: str,
    *,
    current_round_observations: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    inherited: list[dict[str, Any]] = []
    prior_round_id = previous_round_id(run_dir, round_id)
    if prior_round_id is not None:
        inherited = effective_shared_observations(run_dir, prior_round_id)
    current = (
        current_round_observations
        if current_round_observations is not None
        else load_canonical_list(shared_observations_path(run_dir, round_id))
    )
    return merge_effective_observations(inherited, current)


def metric_relevant(claim_type: str, metric: str) -> bool:
    metric = canonical_environment_metric(metric)
    if claim_type not in CLAIM_METRIC_RULES:
        return True
    support_metrics = set(CLAIM_METRIC_RULES[claim_type]["support"].keys())
    contradict_metrics = set(CLAIM_METRIC_RULES[claim_type]["contradict"].keys())
    return metric in support_metrics or metric in contradict_metrics


def assess_observation_against_claim(claim_type: str, observation: dict[str, Any]) -> tuple[int, int, str]:
    metric = canonical_environment_metric(observation.get("metric"))
    metric_value = extract_value_for_metric(observation)
    if metric_value is None:
        return 0, 0, ""
    rules = CLAIM_METRIC_RULES.get(claim_type)
    if rules is None:
        return 0, 0, ""
    support_threshold = rules["support"].get(metric)
    contradict_threshold = rules["contradict"].get(metric)
    if support_threshold is not None:
        if metric == "fire_detection_count":
            if metric_value >= support_threshold:
                return 2, 0, f"{metric}={metric_value:g}"
        elif claim_type == "drought" and metric in {"precipitation_sum", "soil_moisture_0_to_7cm"}:
            if metric_value <= support_threshold:
                return 2, 0, f"{metric}={metric_value:g}"
        else:
            if metric_value >= support_threshold:
                return 2, 0, f"{metric}={metric_value:g}"
    if contradict_threshold is not None:
        if metric == "fire_detection_count":
            if metric_value <= contradict_threshold:
                return 0, 1, f"{metric}={metric_value:g}"
        elif claim_type == "wildfire" and metric in {"precipitation_sum", "relative_humidity_2m"}:
            if metric_value >= contradict_threshold:
                return 0, 1, f"{metric}={metric_value:g}"
        elif claim_type == "drought" and metric in {"precipitation_sum", "soil_moisture_0_to_7cm"}:
            if metric_value >= contradict_threshold:
                return 0, 1, f"{metric}={metric_value:g}"
        else:
            if metric_value <= contradict_threshold:
                return 0, 1, f"{metric}={metric_value:g}"
    return 0, 0, ""


def build_evidence_summary(claim: dict[str, Any], observation_notes: list[str], verdict: str, gaps: list[str]) -> str:
    lead = claim.get("summary") or claim.get("statement") or "Claim"
    base = truncate_text(maybe_text(lead), 140)
    if observation_notes:
        return f"{base}. Matched metrics: {', '.join(observation_notes[:4])}."
    if gaps:
        return f"{base}. Evidence remains limited: {'; '.join(gaps[:2])}."
    return f"{base}. Current evidence verdict: {verdict}."


def compact_task(task: dict[str, Any]) -> dict[str, Any]:
    inputs = task.get("inputs") if isinstance(task.get("inputs"), dict) else {}
    preferred_sources = inputs.get("preferred_sources") if isinstance(inputs.get("preferred_sources"), list) else []
    return {
        "task_id": maybe_text(task.get("task_id")),
        "assigned_role": maybe_text(task.get("assigned_role")),
        "objective": truncate_text(maybe_text(task.get("objective")), 180),
        "status": maybe_text(task.get("status")),
        "preferred_sources": [maybe_text(item) for item in preferred_sources if maybe_text(item)][:3],
    }


def claim_source_skills(claim: dict[str, Any]) -> list[str]:
    refs = claim.get("public_refs")
    if not isinstance(refs, list):
        return []
    return sorted(
        {
            maybe_text(ref.get("source_skill"))
            for ref in refs
            if isinstance(ref, dict) and maybe_text(ref.get("source_skill"))
        }
    )


def compact_claim(claim: dict[str, Any]) -> dict[str, Any]:
    return {
        "claim_id": maybe_text(claim.get("claim_id")),
        "claim_type": maybe_text(claim.get("claim_type")),
        "summary": truncate_text(maybe_text(claim.get("summary")), 180),
        "priority": claim.get("priority"),
        "needs_physical_validation": bool(claim.get("needs_physical_validation")),
        "public_source_skills": claim_source_skills(claim),
    }


def compact_observation(observation: dict[str, Any]) -> dict[str, Any]:
    return {
        "observation_id": maybe_text(observation.get("observation_id")),
        "source_skill": maybe_text(observation.get("source_skill")),
        "metric": maybe_text(observation.get("metric")),
        "aggregation": maybe_text(observation.get("aggregation")),
        "value": observation.get("value"),
        "unit": maybe_text(observation.get("unit")),
        "time_window": observation.get("time_window"),
        "quality_flags": [maybe_text(item) for item in observation.get("quality_flags", []) if maybe_text(item)][:4],
    }


def compact_evidence_card(card: dict[str, Any]) -> dict[str, Any]:
    return {
        "evidence_id": maybe_text(card.get("evidence_id")),
        "claim_id": maybe_text(card.get("claim_id")),
        "verdict": maybe_text(card.get("verdict")),
        "confidence": maybe_text(card.get("confidence")),
        "summary": truncate_text(maybe_text(card.get("summary")), 220),
        "observation_ids": [maybe_text(item) for item in card.get("observation_ids", []) if maybe_text(item)][:6],
        "gaps": [truncate_text(maybe_text(item), 120) for item in card.get("gaps", []) if maybe_text(item)][:3],
    }


def ordered_context_observations(observations: list[dict[str, Any]], evidence_cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id = {maybe_text(item.get("observation_id")): item for item in observations}
    ordered: list[dict[str, Any]] = []
    seen: set[str] = set()
    for card in evidence_cards:
        ids = card.get("observation_ids")
        if not isinstance(ids, list):
            continue
        for observation_id in ids:
            key = maybe_text(observation_id)
            if not key or key in seen or key not in by_id:
                continue
            ordered.append(by_id[key])
            seen.add(key)
    for observation in observations:
        key = maybe_text(observation.get("observation_id"))
        if not key or key in seen:
            continue
        ordered.append(observation)
        seen.add(key)
    return ordered


def build_public_signal_summary(signals: list[dict[str, Any]], claims: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "generated_at_utc": utc_now_iso(),
        "signal_count": len(signals),
        "claim_count": len(claims),
        "source_skill_counts": dict(Counter(maybe_text(item.get("source_skill")) for item in signals)),
        "signal_kind_counts": dict(Counter(maybe_text(item.get("signal_kind")) for item in signals)),
        "top_signals": [
            {
                "signal_id": maybe_text(item.get("signal_id")),
                "source_skill": maybe_text(item.get("source_skill")),
                "title": truncate_text(maybe_text(item.get("title")), 120),
                "published_at_utc": maybe_text(item.get("published_at_utc")),
            }
            for item in signals[:5]
        ],
        "claims": [compact_claim(item) for item in claims[:MAX_CONTEXT_CLAIMS]],
    }


def build_environment_signal_summary(signals: list[dict[str, Any]], observations: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "generated_at_utc": utc_now_iso(),
        "signal_count": len(signals),
        "observation_count": len(observations),
        "source_skill_counts": dict(Counter(maybe_text(item.get("source_skill")) for item in signals)),
        "metric_counts": dict(Counter(maybe_text(item.get("metric")) for item in signals)),
        "top_observations": [compact_observation(item) for item in observations[:MAX_CONTEXT_OBSERVATIONS]],
    }


def link_claims_to_evidence(
    *,
    claims: list[dict[str, Any]],
    observations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    evidence_cards: list[dict[str, Any]] = []
    for index, claim in enumerate(claims, start=1):
        matching = [
            observation
            for observation in observations
            if metric_relevant(maybe_text(claim.get("claim_type")), maybe_text(observation.get("metric")))
            and time_windows_overlap(claim.get("time_window", {}), observation.get("time_window", {}))
            and geometry_overlap(
                claim.get("place_scope", {}).get("geometry", {}),
                observation.get("place_scope", {}).get("geometry", {}),
            )
        ]
        support_score = 0
        contradict_score = 0
        notes: list[str] = []
        gaps: list[str] = []
        for observation in matching:
            support, contradict, note = assess_observation_against_claim(
                maybe_text(claim.get("claim_type")),
                observation,
            )
            support_score += support
            contradict_score += contradict
            if note:
                notes.append(note)

        if not matching:
            verdict = "insufficient"
            confidence = "low"
            gaps.append("No mission-aligned observations matched the claim window and geometry.")
        elif support_score > 0 and contradict_score == 0:
            verdict = "supports"
            confidence = "high" if support_score >= 4 and len(matching) >= 2 else "medium"
        elif support_score == 0 and contradict_score > 0:
            verdict = "contradicts"
            confidence = "medium"
        elif support_score > 0 and contradict_score > 0:
            verdict = "mixed"
            confidence = "medium"
        else:
            verdict = "insufficient"
            confidence = "low"
            gaps.append("Matched observations were mostly contextual and did not cross rule thresholds.")

        if maybe_text(claim.get("claim_type")) in {"smoke", "air-pollution"}:
            if not any(item.get("source_skill") == "openaq-data-fetch" for item in matching):
                gaps.append("Station-grade corroboration is missing.")
            if any("modeled-background" in item.get("quality_flags", []) for item in matching):
                gaps.append("Modeled background fields should be cross-checked with station or local observations.")

        evidence = {
            "schema_version": SCHEMA_VERSION,
            "evidence_id": emit_row_id("evidence", index),
            "run_id": claim["run_id"],
            "round_id": claim["round_id"],
            "claim_id": claim["claim_id"],
            "verdict": verdict,
            "confidence": confidence,
            "summary": build_evidence_summary(claim, notes, verdict, gaps),
            "public_refs": claim.get("public_refs", []),
            "observation_ids": [item["observation_id"] for item in matching],
            "gaps": sorted(dict.fromkeys(gaps)),
        }
        validate_payload("evidence-card", evidence)
        evidence_cards.append(evidence)
    return evidence_cards


def build_round_snapshot(
    *,
    run_dir: Path,
    mission: dict[str, Any],
    round_id: str,
    tasks: list[dict[str, Any]],
    claims: list[dict[str, Any]],
    observations: list[dict[str, Any]],
    evidence_cards: list[dict[str, Any]],
    role: str,
) -> dict[str, Any]:
    run = {
        "run_id": mission_run_id(mission),
        "round_id": round_id,
        "topic": maybe_text(mission.get("topic")),
        "objective": maybe_text(mission.get("objective")),
        "region": mission_place_scope(mission),
        "window": mission_window(mission),
        "role": role,
    }
    role_tasks = [task for task in tasks if role == "moderator" or task.get("assigned_role") == role]
    verdict_counter = Counter(maybe_text(item.get("verdict")) for item in evidence_cards)
    focus_claims = claims
    if role == "environmentalist":
        focus_claims = [claim for claim in claims if claim.get("needs_physical_validation")]

    dataset = {
        "generated_at_utc": utc_now_iso(),
        "task_count": len(role_tasks),
        "claim_count": len(claims),
        "observation_count": len(observations),
        "evidence_count": len(evidence_cards),
    }
    focus = {
        "task_ids": [maybe_text(task.get("task_id")) for task in role_tasks],
        "claims_needing_more_evidence": [
            card["claim_id"] for card in evidence_cards if card.get("verdict") in {"mixed", "insufficient"}
        ],
    }
    if role == "sociologist":
        focus["candidate_claim_ids"] = [claim["claim_id"] for claim in focus_claims]
    if role == "environmentalist":
        focus["metrics_requested"] = sorted({observation["metric"] for observation in observations})

    compact_claims_list = [compact_claim(item) for item in focus_claims[:MAX_CONTEXT_CLAIMS]]
    compact_evidence = [compact_evidence_card(item) for item in evidence_cards[:MAX_CONTEXT_EVIDENCE]]
    compact_observations = [
        compact_observation(item)
        for item in ordered_context_observations(observations, evidence_cards)[:MAX_CONTEXT_OBSERVATIONS]
    ]

    return {
        "context_layer": "compact-v1",
        "run": run,
        "dataset": dataset,
        "aggregates": {
            "claim_type_counts": dict(Counter(maybe_text(item.get("claim_type")) for item in claims)),
            "observation_metric_counts": dict(Counter(maybe_text(item.get("metric")) for item in observations)),
            "evidence_verdict_counts": dict(verdict_counter),
        },
        "canonical_paths": {
            "tasks": str(round_dir(run_dir, round_id) / "moderator" / "tasks.json"),
            "claims": str(shared_claims_path(run_dir, round_id)),
            "observations": str(shared_observations_path(run_dir, round_id)),
            "evidence_cards": str(shared_evidence_path(run_dir, round_id)),
        },
        "tasks": [compact_task(item) for item in role_tasks[:MAX_CONTEXT_TASKS]],
        "focus": focus,
        "claims": compact_claims_list,
        "observations": compact_observations,
        "evidence_cards": compact_evidence,
    }


def command_init_run(args: argparse.Namespace) -> dict[str, Any]:
    run_dir_path = Path(args.run_dir).expanduser().resolve()
    mission = load_mission(run_dir_path)
    public_db = Path(args.public_db).expanduser().resolve() if args.public_db else default_public_db_path(run_dir_path)
    environment_db = (
        Path(args.environment_db).expanduser().resolve()
        if args.environment_db
        else default_environment_db_path(run_dir_path)
    )
    init_sqlite_db(public_db, PUBLIC_DDL_PATH)
    init_sqlite_db(environment_db, ENVIRONMENT_DDL_PATH)

    for role in ("moderator", "sociologist", "environmentalist"):
        default_context_dir(run_dir_path, args.round_id, role).mkdir(parents=True, exist_ok=True)
    (round_dir(run_dir_path, args.round_id) / "shared" / "contexts").mkdir(parents=True, exist_ok=True)

    manifest = load_or_build_manifest(run_dir_path, mission)
    manifest["round_id_initialized"] = args.round_id
    manifest["databases"] = {
        "public_signals": str(public_db),
        "environment_signals": str(environment_db),
    }
    manifest["normalization_cache"] = {
        "version": NORMALIZE_CACHE_VERSION,
        "directory": str(normalize_cache_dir(run_dir_path)),
    }
    manifest["initialized_at_utc"] = utc_now_iso()
    write_json(run_manifest_path(run_dir_path), manifest, pretty=args.pretty)

    return {
        "run_dir": str(run_dir_path),
        "public_db": str(public_db),
        "environment_db": str(environment_db),
        "manifest_path": str(run_manifest_path(run_dir_path)),
    }


def command_normalize_public(args: argparse.Namespace) -> dict[str, Any]:
    run_dir_path = Path(args.run_dir).expanduser().resolve()
    mission = load_mission(run_dir_path)
    run_id = mission_run_id(mission)
    public_db = Path(args.public_db).expanduser().resolve() if args.public_db else default_public_db_path(run_dir_path)
    inputs = parse_input_specs(args.input)
    all_signals: list[dict[str, Any]] = []
    cache_hits = 0
    cache_misses = 0
    for source_skill, path in inputs:
        signals, cache_status = normalize_public_source_cached(
            run_dir=run_dir_path,
            source_skill=source_skill,
            path=path,
            run_id=run_id,
            round_id=args.round_id,
        )
        all_signals.extend(signals)
        if cache_status == "hit":
            cache_hits += 1
        else:
            cache_misses += 1

    deduped_by_id: dict[str, dict[str, Any]] = {signal["signal_id"]: signal for signal in all_signals}
    signals = sorted(
        deduped_by_id.values(),
        key=lambda item: (
            item.get("published_at_utc") or "",
            item["signal_id"],
        ),
        reverse=False,
    )
    claims = public_signals_to_claims(
        mission=mission,
        round_id=args.round_id,
        signals=signals,
        max_claims=args.max_claims,
    )

    save_public_db(public_db, signals, claims)
    normalized_dir = role_normalized_dir(run_dir_path, args.round_id, "sociologist")
    public_signals_file = normalized_dir / "public_signals.jsonl"
    claims_file = normalized_dir / "claim_candidates.json"
    summary_file = normalized_dir / "public_signal_summary.json"
    write_jsonl(public_signals_file, signals)
    write_json(claims_file, claims, pretty=args.pretty)
    write_json(summary_file, build_public_signal_summary(signals, claims), pretty=args.pretty)
    write_json(shared_claims_path(run_dir_path, args.round_id), claims, pretty=args.pretty)

    return {
        "public_db": str(public_db),
        "cache_hits": cache_hits,
        "cache_misses": cache_misses,
        "signal_count": len(signals),
        "claim_count": len(claims),
        "signals_path": str(public_signals_file),
        "signal_summary_path": str(summary_file),
        "claims_path": str(claims_file),
        "shared_claims_path": str(shared_claims_path(run_dir_path, args.round_id)),
    }


def command_normalize_environment(args: argparse.Namespace) -> dict[str, Any]:
    run_dir_path = Path(args.run_dir).expanduser().resolve()
    mission = load_mission(run_dir_path)
    run_id = mission_run_id(mission)
    environment_db = (
        Path(args.environment_db).expanduser().resolve()
        if args.environment_db
        else default_environment_db_path(run_dir_path)
    )
    inputs = parse_input_specs(args.input)
    all_signals: list[dict[str, Any]] = []
    extra_observations: list[dict[str, Any]] = []
    cache_hits = 0
    cache_misses = 0
    for source_skill, path in inputs:
        source_signals, source_observations, cache_status = normalize_environment_source_cached(
            run_dir=run_dir_path,
            source_skill=source_skill,
            path=path,
            run_id=run_id,
            round_id=args.round_id,
        )
        all_signals.extend(source_signals)
        extra_observations.extend(source_observations)
        if cache_status == "hit":
            cache_hits += 1
        else:
            cache_misses += 1

    deduped_by_id: dict[str, dict[str, Any]] = {signal["signal_id"]: signal for signal in all_signals}
    signals = sorted(deduped_by_id.values(), key=lambda item: (item.get("metric") or "", item["signal_id"]))
    observations = environment_signals_to_observations(
        mission=mission,
        round_id=args.round_id,
        signals=signals,
        extra_observations=extra_observations,
    )

    save_environment_db(environment_db, signals, observations)
    normalized_dir = role_normalized_dir(run_dir_path, args.round_id, "environmentalist")
    signals_file = normalized_dir / "environment_signals.jsonl"
    observations_file = normalized_dir / "observations.json"
    summary_file = normalized_dir / "environment_signal_summary.json"
    shared_observations = effective_shared_observations(
        run_dir_path,
        args.round_id,
        current_round_observations=observations,
    )
    write_jsonl(signals_file, signals)
    write_json(observations_file, observations, pretty=args.pretty)
    write_json(summary_file, build_environment_signal_summary(signals, observations), pretty=args.pretty)
    write_json(shared_observations_path(run_dir_path, args.round_id), shared_observations, pretty=args.pretty)

    return {
        "environment_db": str(environment_db),
        "cache_hits": cache_hits,
        "cache_misses": cache_misses,
        "signal_count": len(signals),
        "observation_count": len(observations),
        "shared_observation_count": len(shared_observations),
        "signals_path": str(signals_file),
        "signal_summary_path": str(summary_file),
        "observations_path": str(observations_file),
        "shared_observations_path": str(shared_observations_path(run_dir_path, args.round_id)),
    }


def command_link_evidence(args: argparse.Namespace) -> dict[str, Any]:
    run_dir_path = Path(args.run_dir).expanduser().resolve()
    claims = load_canonical_list(shared_claims_path(run_dir_path, args.round_id))
    observations = effective_shared_observations(run_dir_path, args.round_id)
    evidence_cards = link_claims_to_evidence(claims=claims, observations=observations)

    normalized_dir = role_normalized_dir(run_dir_path, args.round_id, "environmentalist")
    evidence_path = normalized_dir / "evidence_cards.json"
    write_json(evidence_path, evidence_cards, pretty=args.pretty)
    write_json(shared_evidence_path(run_dir_path, args.round_id), evidence_cards, pretty=args.pretty)

    return {
        "evidence_count": len(evidence_cards),
        "evidence_path": str(evidence_path),
        "shared_evidence_path": str(shared_evidence_path(run_dir_path, args.round_id)),
    }


def command_build_round_context(args: argparse.Namespace) -> dict[str, Any]:
    run_dir_path = Path(args.run_dir).expanduser().resolve()
    mission = load_mission(run_dir_path)
    tasks_path = round_dir(run_dir_path, args.round_id) / "moderator" / "tasks.json"
    tasks = load_canonical_list(tasks_path)
    claims = load_canonical_list(shared_claims_path(run_dir_path, args.round_id))
    observations = effective_shared_observations(run_dir_path, args.round_id)
    evidence_cards = load_canonical_list(shared_evidence_path(run_dir_path, args.round_id))

    outputs: dict[str, str] = {}
    for role in ("moderator", "sociologist", "environmentalist"):
        payload = build_round_snapshot(
            run_dir=run_dir_path,
            mission=mission,
            round_id=args.round_id,
            tasks=tasks,
            claims=claims,
            observations=observations,
            evidence_cards=evidence_cards,
            role=role,
        )
        context_path = default_context_dir(run_dir_path, args.round_id, role) / f"context_{role}.json"
        write_json(context_path, payload, pretty=args.pretty)
        outputs[role] = str(context_path)

    snapshot = build_round_snapshot(
        run_dir=run_dir_path,
        mission=mission,
        round_id=args.round_id,
        tasks=tasks,
        claims=claims,
        observations=observations,
        evidence_cards=evidence_cards,
        role="moderator",
    )
    shared_snapshot_path = round_dir(run_dir_path, args.round_id) / "shared" / "contexts" / "round_snapshot.json"
    write_json(shared_snapshot_path, snapshot, pretty=args.pretty)
    outputs["shared_snapshot"] = str(shared_snapshot_path)

    return {
        "claim_count": len(claims),
        "observation_count": len(observations),
        "evidence_count": len(evidence_cards),
        "outputs": outputs,
    }


def add_pretty_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Deterministic normalization pipeline for eco-council runs.")
    sub = parser.add_subparsers(dest="command", required=True)

    init_run = sub.add_parser("init-run", help="Initialize normalization databases and derived directories.")
    init_run.add_argument("--run-dir", required=True, help="Eco-council run directory.")
    init_run.add_argument("--round-id", default="round-001", help="Round identifier.")
    init_run.add_argument("--public-db", default="", help="Override public-signals SQLite path.")
    init_run.add_argument("--environment-db", default="", help="Override environment-signals SQLite path.")
    add_pretty_flag(init_run)

    normalize_public = sub.add_parser("normalize-public", help="Normalize sociologist-side raw artifacts.")
    normalize_public.add_argument("--run-dir", required=True, help="Eco-council run directory.")
    normalize_public.add_argument("--round-id", required=True, help="Round identifier.")
    normalize_public.add_argument(
        "--input",
        action="append",
        default=[],
        help="Input artifact in source-skill=/path form. Repeat for multiple artifacts.",
    )
    normalize_public.add_argument("--public-db", default="", help="Override public-signals SQLite path.")
    normalize_public.add_argument("--max-claims", type=int, default=8, help="Maximum canonical claims to emit.")
    add_pretty_flag(normalize_public)

    normalize_environment = sub.add_parser("normalize-environment", help="Normalize environment raw artifacts.")
    normalize_environment.add_argument("--run-dir", required=True, help="Eco-council run directory.")
    normalize_environment.add_argument("--round-id", required=True, help="Round identifier.")
    normalize_environment.add_argument(
        "--input",
        action="append",
        default=[],
        help="Input artifact in source-skill=/path form. Repeat for multiple artifacts.",
    )
    normalize_environment.add_argument("--environment-db", default="", help="Override environment-signals SQLite path.")
    add_pretty_flag(normalize_environment)

    link_evidence = sub.add_parser("link-evidence", help="Link shared claims and observations into evidence cards.")
    link_evidence.add_argument("--run-dir", required=True, help="Eco-council run directory.")
    link_evidence.add_argument("--round-id", required=True, help="Round identifier.")
    add_pretty_flag(link_evidence)

    build_context = sub.add_parser("build-round-context", help="Build role-specific round context payloads.")
    build_context.add_argument("--run-dir", required=True, help="Eco-council run directory.")
    build_context.add_argument("--round-id", required=True, help="Round identifier.")
    add_pretty_flag(build_context)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handlers = {
        "init-run": command_init_run,
        "normalize-public": command_normalize_public,
        "normalize-environment": command_normalize_environment,
        "link-evidence": command_link_evidence,
        "build-round-context": command_build_round_context,
    }
    try:
        payload = handlers[args.command](args)
    except Exception as exc:  # noqa: BLE001
        result = {"command": args.command, "ok": False, "error": str(exc)}
        print(pretty_json(result, pretty=getattr(args, "pretty", False)))
        return 1

    result = {"command": args.command, "ok": True, "payload": payload}
    print(pretty_json(result, pretty=getattr(args, "pretty", False)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
