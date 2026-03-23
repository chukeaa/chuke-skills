#!/usr/bin/env python3
"""Deterministic raw-data simulator for eco-council rounds."""

from __future__ import annotations

import argparse
import csv
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import io
import json
import os
import random
import re
import tempfile
from pathlib import Path
from typing import Any
import zipfile

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
ASSETS_DIR = SKILL_DIR / "assets"
PRESET_DIR = ASSETS_DIR / "scenarios"

SCHEMA_VERSION = "1.0.0"
SCENARIO_KIND = "eco-council-simulation-scenario"
MODE_VALUES = {"support", "contradict", "mixed", "sparse"}
JSONL_SOURCES = {
    "youtube-video-search",
    "youtube-comments-fetch",
    "regulationsgov-comments-fetch",
    "regulationsgov-comment-detail-fetch",
}
RAW_GDELT_SOURCES = {
    "gdelt-events-fetch",
    "gdelt-mentions-fetch",
    "gdelt-gkg-fetch",
}
GDELT_EXPECTED_COLUMNS = {
    "gdelt-events-fetch": 61,
    "gdelt-mentions-fetch": 16,
    "gdelt-gkg-fetch": 27,
}
GDELT_ZIP_SUFFIX = {
    "gdelt-events-fetch": ".export.CSV.zip",
    "gdelt-mentions-fetch": ".mentions.CSV.zip",
    "gdelt-gkg-fetch": ".gkg.csv.zip",
}
GDELT_MEMBER_SUFFIX = {
    "gdelt-events-fetch": ".export.CSV",
    "gdelt-mentions-fetch": ".mentions.CSV",
    "gdelt-gkg-fetch": ".gkg.csv",
}
DEFAULT_GDELT_BASE_URL = "http://data.gdeltproject.org/gdeltv2"
DEFAULT_GDELT_PREVIEW_LINES = 2
SUPPORTED_SOURCES = {
    "gdelt-doc-search",
    "gdelt-events-fetch",
    "gdelt-mentions-fetch",
    "gdelt-gkg-fetch",
    "bluesky-cascade-fetch",
    "youtube-video-search",
    "youtube-comments-fetch",
    "regulationsgov-comments-fetch",
    "regulationsgov-comment-detail-fetch",
    "airnow-hourly-obs-fetch",
    "open-meteo-historical-fetch",
    "open-meteo-air-quality-fetch",
    "open-meteo-flood-fetch",
    "nasa-firms-fire-fetch",
    "openaq-data-fetch",
}
SUPPORTED_CLAIM_TYPES = {
    "air-pollution",
    "drought",
    "flood",
    "heat",
    "other",
    "policy-reaction",
    "smoke",
    "water-pollution",
    "wildfire",
}
CLAIM_KEYWORDS = {
    "wildfire": ("wildfire", "fire", "bushfire", "burning", "forest fire"),
    "smoke": ("smoke", "haze", "smog", "ash"),
    "flood": ("flood", "flooding", "inundation", "overflow"),
    "heat": ("heat", "heatwave", "extreme heat", "hot weather"),
    "drought": ("drought", "dry spell", "water shortage", "dryness"),
    "air-pollution": ("air quality", "pm2.5", "pm10", "pollution", "dirty air", "aqi"),
    "water-pollution": ("water pollution", "contaminated water", "sewage", "spill"),
    "policy-reaction": ("policy", "regulation", "rulemaking", "public comment", "epa", "agency"),
}
CLAIM_METRIC_RULES = {
    "smoke": {
        "support": {"pm2_5": 35.0, "pm10": 50.0, "us_aqi": 100.0, "fire_detection_count": 1.0},
        "contradict": {"pm2_5": 12.0, "pm10": 20.0, "us_aqi": 50.0},
    },
    "air-pollution": {
        "support": {"pm2_5": 35.0, "pm10": 50.0, "us_aqi": 100.0, "nitrogen_dioxide": 40.0, "ozone": 100.0},
        "contradict": {"pm2_5": 12.0, "pm10": 20.0, "us_aqi": 50.0},
    },
    "wildfire": {
        "support": {"fire_detection_count": 1.0, "temperature_2m": 30.0, "wind_speed_10m": 5.0},
        "contradict": {"fire_detection_count": 0.0, "precipitation_sum": 20.0, "relative_humidity_2m": 70.0},
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
        "support": {"temperature_2m": 32.0},
        "contradict": {"temperature_2m": 22.0},
    },
    "drought": {
        "support": {"precipitation_sum": 2.0, "soil_moisture_0_to_7cm": 0.12},
        "contradict": {"precipitation_sum": 10.0, "soil_moisture_0_to_7cm": 0.25},
    },
}
SOURCE_METRIC_CATALOG = {
    "airnow-hourly-obs-fetch": ("pm2_5", "pm10", "ozone", "nitrogen_dioxide"),
    "open-meteo-air-quality-fetch": ("pm2_5", "pm10", "us_aqi", "nitrogen_dioxide", "ozone"),
    "open-meteo-historical-fetch": (
        "temperature_2m",
        "wind_speed_10m",
        "relative_humidity_2m",
        "precipitation_sum",
        "soil_moisture_0_to_7cm",
    ),
    "open-meteo-flood-fetch": ("river_discharge", "river_discharge_mean", "river_discharge_max", "river_discharge_p75", "precipitation"),
    "openaq-data-fetch": ("pm2_5", "pm10"),
}
METRIC_UNITS = {
    "fire_detection_count": "count",
    "nitrogen_dioxide": "ug/m3",
    "ozone": "ug/m3",
    "pm10": "ug/m3",
    "pm2_5": "ug/m3",
    "precipitation": "mm",
    "precipitation_sum": "mm",
    "relative_humidity_2m": "%",
    "river_discharge": "m3/s",
    "river_discharge_max": "m3/s",
    "river_discharge_mean": "m3/s",
    "river_discharge_p75": "m3/s",
    "soil_moisture_0_to_7cm": "m3/m3",
    "temperature_2m": "C",
    "us_aqi": "index",
    "wind_speed_10m": "m/s",
}
DAILY_METRICS = {
    "precipitation_sum",
    "river_discharge",
    "river_discharge_max",
    "river_discharge_mean",
    "river_discharge_p75",
}
BASE_RECORD_COUNTS = {
    "gdelt-doc-search": 4,
    "gdelt-events-fetch": 4,
    "gdelt-mentions-fetch": 4,
    "gdelt-gkg-fetch": 4,
    "bluesky-cascade-fetch": 5,
    "youtube-video-search": 3,
    "youtube-comments-fetch": 8,
    "regulationsgov-comments-fetch": 4,
    "regulationsgov-comment-detail-fetch": 3,
    "airnow-hourly-obs-fetch": 4,
    "open-meteo-air-quality-fetch": 3,
    "open-meteo-historical-fetch": 3,
    "open-meteo-flood-fetch": 3,
    "nasa-firms-fire-fetch": 3,
    "openaq-data-fetch": 4,
}

AIRNOW_PARAMETER_NAMES = {
    "nitrogen_dioxide": "NO2",
    "ozone": "OZONE",
    "pm10": "PM10",
    "pm2_5": "PM25",
}

AIRNOW_AQI_KIND = {
    "nitrogen_dioxide": "hourly-aqi",
    "ozone": "nowcast-aqi",
    "pm10": "nowcast-aqi",
    "pm2_5": "nowcast-aqi",
}

AIRNOW_UNITS = {
    "nitrogen_dioxide": "PPB",
    "ozone": "PPB",
    "pm10": "UG/M3",
    "pm2_5": "UG/M3",
}
PUBLIC_DOMAINS = {
    "air-pollution": "airwatch.example.invalid",
    "drought": "drylands.example.invalid",
    "flood": "riverdesk.example.invalid",
    "heat": "heatbeat.example.invalid",
    "other": "envdesk.example.invalid",
    "policy-reaction": "policywatch.example.invalid",
    "smoke": "haze-monitor.example.invalid",
    "water-pollution": "waterscope.example.invalid",
    "wildfire": "firewatch.example.invalid",
}


def normalize_space(value: Any) -> str:
    return " ".join(str(value).split())


def maybe_text(value: Any) -> str:
    if value is None:
        return ""
    return normalize_space(value)


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


def pretty_json(payload: Any, *, pretty: bool) -> str:
    if pretty:
        return json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True)
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


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


def atomic_write_bytes_file(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=f".{path.name}.tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
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


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    lines = [json.dumps(record, ensure_ascii=True, sort_keys=True) for record in records]
    atomic_write_text_file(path, "\n".join(lines) + ("\n" if lines else ""))


def write_bytes(path: Path, payload: bytes) -> None:
    atomic_write_bytes_file(path, payload)


def write_text(path: Path, content: str) -> None:
    atomic_write_text_file(path, content)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(65536)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def stable_int(*parts: Any) -> int:
    joined = "||".join(maybe_text(part) for part in parts)
    return int(hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16], 16)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = maybe_text(value)
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
            return datetime.strptime(text, pattern).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def to_rfc3339_z(value: datetime | None) -> str:
    if value is None:
        return utc_now_iso()
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def round_directory_name(round_id: str) -> str:
    return round_id.replace("-", "_")


def round_dir(run_dir: Path, round_id: str) -> Path:
    return run_dir / round_directory_name(round_id)


def discover_round_ids(run_dir: Path) -> list[str]:
    output: list[str] = []
    if not run_dir.exists():
        return output
    for child in run_dir.iterdir():
        if not child.is_dir():
            continue
        name = child.name
        if not name.startswith("round_"):
            continue
        round_id = name.replace("_", "-")
        output.append(round_id)
    output.sort()
    return output


def resolve_round_id(run_dir: Path, round_id: str) -> str:
    if round_id:
        return round_id
    round_ids = discover_round_ids(run_dir)
    if not round_ids:
        raise ValueError(f"No round_* directories found in {run_dir}.")
    return round_ids[-1]


def moderator_derived_dir(run_dir: Path, round_id: str) -> Path:
    return round_dir(run_dir, round_id) / "moderator" / "derived"


def role_raw_dir(run_dir: Path, round_id: str, role: str) -> Path:
    return round_dir(run_dir, round_id) / role / "raw"


def role_meta_dir(run_dir: Path, round_id: str, role: str) -> Path:
    return role_raw_dir(run_dir, round_id, role) / "_meta"


def mission_path(run_dir: Path) -> Path:
    return run_dir / "mission.json"


def tasks_path(run_dir: Path, round_id: str) -> Path:
    return round_dir(run_dir, round_id) / "moderator" / "tasks.json"


def fetch_plan_path(run_dir: Path, round_id: str) -> Path:
    return moderator_derived_dir(run_dir, round_id) / "fetch_plan.json"


def fetch_execution_path(run_dir: Path, round_id: str) -> Path:
    return moderator_derived_dir(run_dir, round_id) / "fetch_execution.json"


def fetch_lock_path(run_dir: Path, round_id: str) -> Path:
    return moderator_derived_dir(run_dir, round_id) / "fetch.lock"


def source_selection_path(run_dir: Path, round_id: str, role: str) -> Path:
    return round_dir(run_dir, round_id) / role / "source_selection.json"


def file_snapshot(path: Path) -> dict[str, Any]:
    exists = path.exists()
    return {"path": str(path), "exists": exists, "sha256": file_sha256(path) if exists else ""}


@contextmanager
def exclusive_file_lock(path: Path) -> Any:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield handle
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def ensure_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object.")
    return value


def ensure_fetch_plan_inputs_match(*, run_dir: Path, round_id: str, plan: dict[str, Any]) -> None:
    snapshot = ensure_object(plan.get("input_snapshot"), "fetch_plan.input_snapshot")
    task_snapshot = ensure_object(snapshot.get("tasks"), "fetch_plan.input_snapshot.tasks")
    current_task_snapshot = file_snapshot(tasks_path(run_dir, round_id))
    issues: list[str] = []
    if maybe_text(task_snapshot.get("sha256")) != maybe_text(current_task_snapshot.get("sha256")):
        issues.append(f"tasks.json changed ({tasks_path(run_dir, round_id)})")

    source_snapshots = ensure_object(snapshot.get("source_selections"), "fetch_plan.input_snapshot.source_selections")
    for role in ("sociologist", "environmentalist"):
        expected = ensure_object(source_snapshots.get(role), f"fetch_plan.input_snapshot.source_selections.{role}")
        path = source_selection_path(run_dir, round_id, role)
        current = file_snapshot(path)
        current_payload = read_json(path) if path.exists() else {}
        current_status = maybe_text((current_payload or {}).get("status"))
        if maybe_text(expected.get("sha256")) != maybe_text(current.get("sha256")):
            issues.append(f"{role} source_selection changed ({path})")
        if maybe_text(expected.get("status")) != current_status:
            issues.append(
                f"{role} source_selection status changed "
                f"(expected {maybe_text(expected.get('status')) or '<empty>'}, found {current_status or '<empty>'})"
            )
    if issues:
        raise RuntimeError("Fetch plan inputs changed since prepare-round. Rerun prepare-round. " + "; ".join(issues))


def mission_window(mission: dict[str, Any]) -> tuple[datetime, datetime]:
    window = ensure_object(mission.get("window"), "mission.window")
    start = parse_datetime(window.get("start_utc"))
    end = parse_datetime(window.get("end_utc"))
    if start is None or end is None:
        raise ValueError("mission.window must include valid start_utc and end_utc.")
    return (start, end)


def mission_region(mission: dict[str, Any]) -> dict[str, Any]:
    return ensure_object(mission.get("region"), "mission.region")


def region_label(mission: dict[str, Any]) -> str:
    return maybe_text(mission_region(mission).get("label")) or "Mission region"


def geometry_center(geometry: dict[str, Any]) -> tuple[float, float]:
    geometry_type = maybe_text(geometry.get("type"))
    if geometry_type == "Point":
        latitude = maybe_number(geometry.get("latitude"))
        longitude = maybe_number(geometry.get("longitude"))
        if latitude is None or longitude is None:
            return (0.0, 0.0)
        return (float(latitude), float(longitude))
    if geometry_type == "BBox":
        min_lat = maybe_number(geometry.get("south"))
        max_lat = maybe_number(geometry.get("north"))
        min_lon = maybe_number(geometry.get("west"))
        max_lon = maybe_number(geometry.get("east"))
        if None in {min_lat, max_lat, min_lon, max_lon}:
            min_lat = maybe_number(geometry.get("min_latitude"))
            max_lat = maybe_number(geometry.get("max_latitude"))
            min_lon = maybe_number(geometry.get("min_longitude"))
            max_lon = maybe_number(geometry.get("max_longitude"))
        if None in {min_lat, max_lat, min_lon, max_lon}:
            return (0.0, 0.0)
        return ((float(min_lat) + float(max_lat)) / 2.0, (float(min_lon) + float(max_lon)) / 2.0)
    return (0.0, 0.0)


def infer_claim_type(text: str, selected_sources: list[str]) -> str:
    lowered = text.lower()
    for claim_type, keywords in CLAIM_KEYWORDS.items():
        if any(keyword in lowered for keyword in keywords):
            return claim_type
    if "regulationsgov-comments-fetch" in selected_sources or "regulationsgov-comment-detail-fetch" in selected_sources:
        return "policy-reaction"
    if "open-meteo-flood-fetch" in selected_sources:
        return "flood"
    if "openaq-data-fetch" in selected_sources or "open-meteo-air-quality-fetch" in selected_sources:
        return "air-pollution"
    if "nasa-firms-fire-fetch" in selected_sources:
        return "wildfire"
    if "open-meteo-historical-fetch" in selected_sources:
        return "heat"
    return "other"


def claim_phrase(claim_type: str, place_label: str, topic: str) -> tuple[str, str]:
    if claim_type == "flood":
        return (
            f"Public flood reports intensify around {place_label}",
            f"Residents and local coverage describe flooding and inundation around {place_label} during the mission window.",
        )
    if claim_type == "heat":
        return (
            f"Extreme heat concern rises around {place_label}",
            f"Public discussion describes unusually intense heat and heat stress around {place_label}.",
        )
    if claim_type == "smoke":
        return (
            f"Smoke concern grows around {place_label}",
            f"People describe smoke, haze, and sharp air-quality concern around {place_label}.",
        )
    if claim_type == "air-pollution":
        return (
            f"Air-quality concern rises around {place_label}",
            f"Public discussion describes pollution and degraded air quality around {place_label}.",
        )
    if claim_type == "wildfire":
        return (
            f"Wildfire concern grows around {place_label}",
            f"People report wildfire activity, burning, or fire spread around {place_label}.",
        )
    if claim_type == "drought":
        return (
            f"Drought concern builds around {place_label}",
            f"Public discussion describes drought, dryness, and water stress around {place_label}.",
        )
    if claim_type == "water-pollution":
        return (
            f"Water-pollution concern spreads around {place_label}",
            f"Public discussion describes contaminated water or water pollution affecting {place_label}.",
        )
    if claim_type == "policy-reaction":
        return (
            f"Public comment activity grows around an environmental rule affecting {place_label}",
            f"Commenters discuss policy, regulation, and public comment activity relevant to {place_label}.",
        )
    topic_text = topic or "environmental concern"
    return (
        f"Environmental concern grows around {place_label}",
        f"Public discussion mentions {topic_text} around {place_label} during the mission window.",
    )


def base_statement_text(scenario: dict[str, Any], mission: dict[str, Any]) -> tuple[str, str]:
    claim_type = maybe_text(scenario.get("claim_type")) or "other"
    return claim_phrase(claim_type, maybe_text(scenario.get("place_label")) or region_label(mission), maybe_text(scenario.get("public_topic")))


def preset_paths() -> list[Path]:
    if not PRESET_DIR.exists():
        return []
    return sorted(path for path in PRESET_DIR.iterdir() if path.is_file() and path.suffix.lower() == ".json")


def find_preset_path(name: str) -> Path:
    normalized = maybe_text(name)
    for path in preset_paths():
        payload = read_json(path)
        scenario_id = maybe_text(payload.get("scenario_id"))
        if path.stem == normalized or scenario_id == normalized:
            return path
    raise ValueError(f"Unknown preset: {name}")


def load_scenario(
    *,
    args: argparse.Namespace,
    mission: dict[str, Any],
    tasks: list[dict[str, Any]],
    steps: list[dict[str, Any]],
) -> tuple[dict[str, Any], str]:
    source = "auto"
    if args.scenario_input:
        scenario = ensure_object(read_json(Path(args.scenario_input).expanduser().resolve()), "scenario")
        source = str(Path(args.scenario_input).expanduser().resolve())
    elif args.preset:
        preset_path = find_preset_path(args.preset)
        scenario = ensure_object(read_json(preset_path), "preset")
        source = str(preset_path)
    else:
        selected_sources = [maybe_text(step.get("source_skill")) for step in steps if maybe_text(step.get("source_skill"))]
        combined_text = " ".join(
            [
                maybe_text(mission.get("topic")),
                maybe_text(mission.get("objective")),
                " ".join(maybe_text(item) for item in mission.get("hypotheses", []) if maybe_text(item))
                if isinstance(mission.get("hypotheses"), list)
                else "",
                " ".join(maybe_text(task.get("objective")) for task in tasks if isinstance(task, dict)),
            ]
        )
        claim_type = maybe_text(args.claim_type) or infer_claim_type(combined_text, selected_sources)
        scenario = {
            "scenario_kind": SCENARIO_KIND,
            "schema_version": SCHEMA_VERSION,
            "scenario_id": f"auto-{claim_type}-{args.mode}",
            "description": f"Auto-generated {args.mode} scenario for {claim_type}.",
            "claim_type": claim_type,
            "mode": args.mode,
            "seed": args.seed if args.seed is not None else 7,
            "public_topic": maybe_text(mission.get("topic")) or claim_type,
            "place_label": region_label(mission),
            "source_modes": {},
            "source_overrides": {},
            "fault_profile": {
                "empty_sources": [],
                "degrade_sources": [],
                "time_shift_hours": 0,
                "coordinate_offset_degrees": 0.0,
            },
        }
    scenario.setdefault("scenario_kind", SCENARIO_KIND)
    scenario.setdefault("schema_version", SCHEMA_VERSION)
    scenario.setdefault("scenario_id", "unnamed-scenario")
    scenario.setdefault("description", "")
    scenario.setdefault("claim_type", infer_claim_type(maybe_text(mission.get("objective")), []))
    scenario.setdefault("mode", args.mode)
    scenario.setdefault("seed", args.seed if args.seed is not None else 7)
    scenario.setdefault("public_topic", maybe_text(mission.get("topic")))
    scenario.setdefault("place_label", region_label(mission))
    scenario.setdefault("source_modes", {})
    scenario.setdefault("source_overrides", {})
    scenario.setdefault("fault_profile", {})
    if args.seed is not None:
        scenario["seed"] = args.seed
    if maybe_text(scenario.get("scenario_kind")) != SCENARIO_KIND:
        raise ValueError(f"scenario_kind must be {SCENARIO_KIND!r}")
    claim_type = maybe_text(scenario.get("claim_type"))
    if claim_type not in SUPPORTED_CLAIM_TYPES:
        raise ValueError(f"Unsupported claim_type: {claim_type}")
    mode = maybe_text(scenario.get("mode"))
    if mode not in MODE_VALUES:
        raise ValueError(f"Unsupported mode: {mode}")
    return scenario, source


def scenario_mode_for_source(scenario: dict[str, Any], source_skill: str) -> str:
    overrides = scenario.get("source_overrides")
    if isinstance(overrides, dict):
        source_override = overrides.get(source_skill)
        if isinstance(source_override, dict):
            override_mode = maybe_text(source_override.get("mode"))
            if override_mode:
                return override_mode
    source_modes = scenario.get("source_modes")
    if isinstance(source_modes, dict):
        mode = maybe_text(source_modes.get(source_skill))
        if mode:
            return mode
    return maybe_text(scenario.get("mode")) or "support"


def source_override(scenario: dict[str, Any], source_skill: str) -> dict[str, Any]:
    overrides = scenario.get("source_overrides")
    if not isinstance(overrides, dict):
        return {}
    value = overrides.get(source_skill)
    if not isinstance(value, dict):
        return {}
    return value


def fault_list(scenario: dict[str, Any], key: str) -> set[str]:
    profile = scenario.get("fault_profile")
    if not isinstance(profile, dict):
        return set()
    value = profile.get(key)
    if not isinstance(value, list):
        return set()
    return {maybe_text(item) for item in value if maybe_text(item)}


def fault_number(scenario: dict[str, Any], key: str, default: float) -> float:
    profile = scenario.get("fault_profile")
    if not isinstance(profile, dict):
        return default
    value = maybe_number(profile.get(key))
    return float(value) if value is not None else default


def shifted_datetime(value: datetime, scenario: dict[str, Any]) -> datetime:
    hours = fault_number(scenario, "time_shift_hours", 0.0)
    return value + timedelta(hours=hours)


def shifted_coordinates(latitude: float, longitude: float, scenario: dict[str, Any]) -> tuple[float, float]:
    offset = fault_number(scenario, "coordinate_offset_degrees", 0.0)
    return (latitude + offset, longitude + offset)


def seed_for_source(scenario: dict[str, Any], source_skill: str) -> int:
    return stable_int(scenario.get("scenario_id"), scenario.get("seed"), source_skill)


def record_count_for_source(source_skill: str, mode: str, scenario: dict[str, Any]) -> int:
    override = source_override(scenario, source_skill)
    override_count = maybe_number(override.get("record_count"))
    if override_count is not None:
        return max(0, int(override_count))
    count = BASE_RECORD_COUNTS.get(source_skill, 3)
    if source_skill in fault_list(scenario, "degrade_sources"):
        count = max(1, count // 2)
    if mode == "sparse":
        count = min(count, 2)
    return max(0, count)


def text_snippets_for_source(scenario: dict[str, Any], source_skill: str) -> list[str]:
    override = source_override(scenario, source_skill)
    value = override.get("text_snippets")
    if not isinstance(value, list):
        return []
    return [maybe_text(item) for item in value if maybe_text(item)]


def slugify(value: Any, *, default: str) -> str:
    tokens = re.findall(r"[a-z0-9]+", maybe_text(value).casefold())
    return "-".join(tokens[:8]) or default


def split_counts(total: int, bucket_count: int) -> list[int]:
    if total <= 0 or bucket_count <= 0:
        return []
    base, remainder = divmod(total, bucket_count)
    return [base + (1 if index < remainder else 0) for index in range(bucket_count)]


def date_labels(start: datetime, end: datetime, count: int, scenario: dict[str, Any]) -> list[str]:
    if count <= 0:
        return []
    shifted_start = shifted_datetime(start, scenario)
    shifted_end = shifted_datetime(end, scenario)
    if count == 1:
        return [shifted_start.date().isoformat()]
    span = max((shifted_end.date() - shifted_start.date()).days, 1)
    output: list[str] = []
    for index in range(count):
        day_offset = round(index * span / max(count - 1, 1))
        output.append((shifted_start.date() + timedelta(days=day_offset)).isoformat())
    return output


def datetime_labels(start: datetime, end: datetime, count: int, scenario: dict[str, Any]) -> list[str]:
    if count <= 0:
        return []
    shifted_start = shifted_datetime(start, scenario)
    shifted_end = shifted_datetime(end, scenario)
    if count == 1:
        return [to_rfc3339_z(shifted_start)]
    total_seconds = max((shifted_end - shifted_start).total_seconds(), 1.0)
    output: list[str] = []
    for index in range(count):
        ratio = index / max(count - 1, 1)
        point = shifted_start + timedelta(seconds=total_seconds * ratio)
        output.append(to_rfc3339_z(point))
    return output


def source_count(payload: Any, source_skill: str) -> int:
    if source_skill == "gdelt-doc-search" and isinstance(payload, dict):
        articles = payload.get("articles")
        return len(articles) if isinstance(articles, list) else 0
    if source_skill in RAW_GDELT_SOURCES and isinstance(payload, dict):
        downloaded_count = maybe_number(payload.get("downloaded_count"))
        if downloaded_count is not None:
            return int(downloaded_count)
        downloads = payload.get("downloads")
        return len(downloads) if isinstance(downloads, list) else 0
    if source_skill == "bluesky-cascade-fetch" and isinstance(payload, dict):
        seeds = payload.get("seed_posts")
        return len(seeds) if isinstance(seeds, list) else 0
    if source_skill in JSONL_SOURCES and isinstance(payload, list):
        return len(payload)
    if source_skill == "airnow-hourly-obs-fetch" and isinstance(payload, dict):
        records = payload.get("records")
        return len(records) if isinstance(records, list) else 0
    if source_skill.startswith("open-meteo") and isinstance(payload, dict):
        records = payload.get("records")
        return len(records) if isinstance(records, list) else 0
    if source_skill == "nasa-firms-fire-fetch" and isinstance(payload, dict):
        records = payload.get("records")
        return len(records) if isinstance(records, list) else 0
    if source_skill == "openaq-data-fetch" and isinstance(payload, dict):
        result = payload.get("result")
        if isinstance(result, dict) and isinstance(result.get("records"), list):
            return len(result["records"])
        if isinstance(payload.get("records"), list):
            return len(payload["records"])
    return 0


def empty_payload_for_source(source_skill: str, start: datetime, end: datetime) -> Any:
    if source_skill == "gdelt-doc-search":
        return {"articles": []}
    if source_skill in RAW_GDELT_SOURCES:
        return {
            "ok": True,
            "mode": "range",
            "selected_count": 0,
            "downloaded_count": 0,
            "skipped_count": 0,
            "downloads": [],
            "skipped": [],
        }
    if source_skill == "bluesky-cascade-fetch":
        return {"seed_posts": [], "threads": []}
    if source_skill in JSONL_SOURCES:
        return []
    if source_skill == "airnow-hourly-obs-fetch":
        return {
            "schema_version": SCHEMA_VERSION,
            "source_skill": source_skill,
            "generated_at_utc": utc_now_iso(),
            "request": {
                "start_datetime_utc": to_rfc3339_z(start),
                "end_datetime_utc": to_rfc3339_z(end),
                "parameter_names": [],
                "hour_count": 0,
                "hourly_file_urls": [],
            },
            "dry_run": False,
            "transport": {"attempted_files": 0, "successful_files": 0, "failed_files": 0},
            "validation_summary": {"ok": True, "total_issue_count": 0, "issues": [], "records_emitted": 0},
            "record_count": 0,
            "records": [],
            "artifacts": {},
        }
    if source_skill.startswith("open-meteo"):
        return {"records": []}
    if source_skill == "nasa-firms-fire-fetch":
        return {"records": [], "request": {"start_date": start.date().isoformat(), "end_date": end.date().isoformat()}}
    if source_skill == "openaq-data-fetch":
        return {"result": {"records": []}}
    return {}


def public_line_text(scenario: dict[str, Any], mission: dict[str, Any], source_skill: str, index: int) -> tuple[str, str]:
    snippets = text_snippets_for_source(scenario, source_skill)
    if snippets:
        text = snippets[index % len(snippets)]
        return (text, text)
    title, body = base_statement_text(scenario, mission)
    title_out = title
    if source_skill == "youtube-video-search":
        return (f"Video: {title_out}", body)
    return (title_out, body)


def gdelt_topic_phrase(mission: dict[str, Any], scenario: dict[str, Any]) -> str:
    parts = [
        maybe_text(scenario.get("public_topic")),
        maybe_text(mission.get("topic")),
        maybe_text(mission.get("objective")),
    ]
    return normalize_space(" ".join(part for part in parts if part))


def gdelt_place_label(mission: dict[str, Any], scenario: dict[str, Any]) -> str:
    return maybe_text(scenario.get("place_label")) or region_label(mission)


def gdelt_claim_tone(mode: str) -> float:
    if mode == "contradict":
        return 1.6
    if mode == "mixed":
        return -0.8
    if mode == "sparse":
        return -1.4
    return -3.8


def gdelt_goldstein_score(mode: str) -> float:
    if mode == "contradict":
        return 2.0
    if mode == "mixed":
        return -1.5
    if mode == "sparse":
        return -2.0
    return -5.0


def gdelt_event_base_code(claim_type: str) -> str:
    return {
        "air-pollution": "112",
        "drought": "023",
        "flood": "0233",
        "heat": "073",
        "policy-reaction": "010",
        "smoke": "112",
        "water-pollution": "043",
        "wildfire": "204",
    }.get(claim_type, "010")


def gdelt_datetime_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S")


def gdelt_archive_filename(source_skill: str, timestamp: datetime) -> str:
    return gdelt_datetime_text(timestamp) + GDELT_ZIP_SUFFIX[source_skill]


def gdelt_member_name(source_skill: str, timestamp: datetime) -> str:
    return gdelt_datetime_text(timestamp) + GDELT_MEMBER_SUFFIX[source_skill]


def build_gdelt_zip_payload(
    *,
    member_name: str,
    rows: list[list[str]],
) -> tuple[bytes, list[str]]:
    text_buffer = io.StringIO()
    writer = csv.writer(text_buffer, delimiter="\t", lineterminator="\n")
    for row in rows:
        writer.writerow(row)
    member_text = text_buffer.getvalue()
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(member_name, member_text.encode("utf-8"))
    preview_lines = member_text.rstrip("\n").splitlines()[:DEFAULT_GDELT_PREVIEW_LINES]
    return (zip_buffer.getvalue(), preview_lines)


def gdelt_validation_summary(*, member_name: str, row_count: int, expected_columns: int) -> dict[str, Any]:
    return {
        "passed": True,
        "checked_member": member_name,
        "expected_columns": expected_columns,
        "scanned_lines": row_count,
        "scan_complete": True,
        "max_lines": row_count or 1,
        "empty_line_count": 0,
        "issue_count": 0,
        "decode_error_count": 0,
        "column_mismatch_count": 0,
        "issues": [],
        "quarantine_path": None,
    }


def gdelt_country_hint(place_label: str) -> str:
    parts = [part.strip() for part in maybe_text(place_label).split(",") if part.strip()]
    return parts[-1] if parts else place_label


def build_gdelt_events_rows(
    *,
    mission: dict[str, Any],
    scenario: dict[str, Any],
    mode: str,
    source_skill: str,
    count: int,
    row_times: list[datetime],
    start_index: int,
) -> list[list[str]]:
    claim_type = maybe_text(scenario.get("claim_type")) or "other"
    place_label = gdelt_place_label(mission, scenario)
    topic_phrase = gdelt_topic_phrase(mission, scenario) or claim_type
    topic_slug = slugify(topic_phrase, default="topic")
    place_slug = slugify(place_label, default="place")
    domain = PUBLIC_DOMAINS.get(claim_type, PUBLIC_DOMAINS["other"])
    title_root = place_label.replace(",", "")
    latitude, longitude = shifted_coordinates(*geometry_center(mission_region(mission).get("geometry", {})), scenario)
    tone = gdelt_claim_tone(mode)
    event_base_code = gdelt_event_base_code(claim_type)
    rows: list[list[str]] = []
    for local_index in range(count):
        global_index = start_index + local_index
        timestamp = row_times[local_index]
        event_id = str((seed_for_source(scenario, source_skill) % 9_000_000) + global_index + 1)
        title, _body = public_line_text(scenario, mission, source_skill, global_index)
        source_url = f"https://{domain}/gdelt-events/{place_slug}/{topic_slug}/{global_index + 1}"
        row = [""] * GDELT_EXPECTED_COLUMNS[source_skill]
        row[0] = event_id
        row[1] = timestamp.strftime("%Y%m%d")
        row[6] = f"{title_root} monitors"
        row[16] = f"{topic_phrase} response"
        row[26] = event_base_code
        row[27] = event_base_code
        row[28] = event_base_code[:2]
        row[30] = f"{gdelt_goldstein_score(mode):.1f}"
        row[31] = str(5 + global_index)
        row[32] = str(2 + (global_index % 3))
        row[33] = str(2 + (global_index % 2))
        row[34] = f"{tone - (local_index * 0.2):.2f}"
        row[52] = place_label
        row[53] = gdelt_country_hint(place_label)
        row[56] = f"{latitude:.4f}"
        row[57] = f"{longitude:.4f}"
        row[59] = gdelt_datetime_text(timestamp)
        row[60] = source_url
        rows.append(row)
    return rows


def build_gdelt_mentions_rows(
    *,
    mission: dict[str, Any],
    scenario: dict[str, Any],
    mode: str,
    source_skill: str,
    count: int,
    row_times: list[datetime],
    start_index: int,
) -> list[list[str]]:
    claim_type = maybe_text(scenario.get("claim_type")) or "other"
    place_label = gdelt_place_label(mission, scenario)
    topic_phrase = gdelt_topic_phrase(mission, scenario) or claim_type
    topic_slug = slugify(topic_phrase, default="topic")
    place_slug = slugify(place_label, default="place")
    domain = PUBLIC_DOMAINS.get(claim_type, PUBLIC_DOMAINS["other"])
    source_name = f"{place_label} {claim_type} bulletin".strip()
    tone = gdelt_claim_tone(mode)
    rows: list[list[str]] = []
    for local_index in range(count):
        global_index = start_index + local_index
        timestamp = row_times[local_index]
        event_id = str((seed_for_source(scenario, source_skill) % 9_000_000) + global_index + 1)
        identifier = f"https://{domain}/gdelt-mentions/{place_slug}/{topic_slug}/{global_index + 1}"
        row = [""] * GDELT_EXPECTED_COLUMNS[source_skill]
        row[0] = event_id
        row[1] = gdelt_datetime_text(timestamp - timedelta(minutes=30))
        row[2] = gdelt_datetime_text(timestamp)
        row[3] = "1"
        row[4] = source_name
        row[5] = identifier
        row[11] = str(70 + (local_index * 4))
        row[12] = str(900 + (local_index * 120))
        row[13] = f"{tone - (local_index * 0.15):.2f}"
        rows.append(row)
    return rows


def build_gdelt_gkg_rows(
    *,
    mission: dict[str, Any],
    scenario: dict[str, Any],
    mode: str,
    source_skill: str,
    count: int,
    row_times: list[datetime],
    start_index: int,
) -> list[list[str]]:
    claim_type = maybe_text(scenario.get("claim_type")) or "other"
    place_label = gdelt_place_label(mission, scenario)
    topic_phrase = gdelt_topic_phrase(mission, scenario) or claim_type
    topic_slug = slugify(topic_phrase, default="topic")
    place_slug = slugify(place_label, default="place")
    domain = PUBLIC_DOMAINS.get(claim_type, PUBLIC_DOMAINS["other"])
    source_common_name = f"{place_label.replace(',', '')} desk".strip()
    latitude, longitude = shifted_coordinates(*geometry_center(mission_region(mission).get("geometry", {})), scenario)
    tone = gdelt_claim_tone(mode)
    topic_theme = slugify(topic_phrase, default=claim_type).replace("-", "_").upper()
    claim_theme = slugify(claim_type, default="environment").replace("-", "_").upper()
    location_text = f"1#{place_label}###{place_slug}#{latitude:.4f}#{longitude:.4f}"
    rows: list[list[str]] = []
    for local_index in range(count):
        global_index = start_index + local_index
        timestamp = row_times[local_index]
        document_identifier = f"https://{domain}/gdelt-gkg/{place_slug}/{topic_slug}/{global_index + 1}"
        row = [""] * GDELT_EXPECTED_COLUMNS[source_skill]
        row[0] = f"{gdelt_datetime_text(timestamp)}-{global_index + 1}"
        row[1] = gdelt_datetime_text(timestamp)
        row[3] = source_common_name
        row[4] = document_identifier
        row[8] = ";".join(
            item
            for item in (
                f"ENV_{claim_theme}",
                topic_theme,
                "ENVIRONMENT",
            )
            if item
        )
        row[10] = location_text
        row[12] = f"{place_label.replace(',', '')} analyst"
        row[14] = f"{place_label.replace(',', '')} council;{claim_type.replace('-', ' ')} desk"
        row[15] = f"{tone - (local_index * 0.1):.2f},0,0,0,0,0,0"
        row[23] = ";".join(
            item
            for item in (
                place_label,
                maybe_text(mission.get("topic")),
                maybe_text(mission.get("objective")),
            )
            if item
        )
        rows.append(row)
    return rows


def build_gdelt_rows(
    *,
    mission: dict[str, Any],
    scenario: dict[str, Any],
    mode: str,
    source_skill: str,
    count: int,
    row_times: list[datetime],
    start_index: int,
) -> list[list[str]]:
    if source_skill == "gdelt-events-fetch":
        return build_gdelt_events_rows(
            mission=mission,
            scenario=scenario,
            mode=mode,
            source_skill=source_skill,
            count=count,
            row_times=row_times,
            start_index=start_index,
        )
    if source_skill == "gdelt-mentions-fetch":
        return build_gdelt_mentions_rows(
            mission=mission,
            scenario=scenario,
            mode=mode,
            source_skill=source_skill,
            count=count,
            row_times=row_times,
            start_index=start_index,
        )
    return build_gdelt_gkg_rows(
        mission=mission,
        scenario=scenario,
        mode=mode,
        source_skill=source_skill,
        count=count,
        row_times=row_times,
        start_index=start_index,
    )


def raw_gdelt_download_dir(step: dict[str, Any]) -> Path:
    configured = maybe_text(step.get("download_dir"))
    if configured:
        return Path(configured).expanduser().resolve()
    artifact_path = Path(maybe_text(step.get("artifact_path"))).expanduser().resolve()
    return artifact_path.parent / artifact_path.stem


def generate_raw_gdelt_manifest(
    *,
    mission: dict[str, Any],
    scenario: dict[str, Any],
    mode: str,
    source_skill: str,
    step: dict[str, Any],
) -> dict[str, Any]:
    start, end = mission_window(mission)
    row_count = record_count_for_source(source_skill, mode, scenario)
    if row_count <= 0:
        return {
            "ok": True,
            "mode": "range",
            "selected_count": 0,
            "downloaded_count": 0,
            "skipped_count": 0,
            "downloads": [],
            "skipped": [],
        }

    download_dir = raw_gdelt_download_dir(step)
    file_count = 1 if row_count <= 2 or mode == "sparse" else 2
    file_count = min(file_count, row_count)
    file_timestamps = [
        parse_datetime(item) or shifted_datetime(start, scenario)
        for item in datetime_labels(start, end, file_count, scenario)
    ]
    rows_per_file = split_counts(row_count, file_count)
    downloads: list[dict[str, Any]] = []
    row_offset = 0

    for file_index, file_timestamp in enumerate(file_timestamps):
        current_count = rows_per_file[file_index]
        row_times = [
            file_timestamp + timedelta(minutes=local_index * 5)
            for local_index in range(current_count)
        ]
        rows = build_gdelt_rows(
            mission=mission,
            scenario=scenario,
            mode=mode,
            source_skill=source_skill,
            count=current_count,
            row_times=row_times,
            start_index=row_offset,
        )
        filename = gdelt_archive_filename(source_skill, file_timestamp)
        member_name = gdelt_member_name(source_skill, file_timestamp)
        payload_bytes, preview_lines = build_gdelt_zip_payload(member_name=member_name, rows=rows)
        output_path = download_dir / filename
        write_bytes(output_path, payload_bytes)
        request_url = f"{DEFAULT_GDELT_BASE_URL}/{filename}"
        downloads.append(
            {
                "entry": {
                    "timestamp_utc": file_timestamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "timestamp_raw": gdelt_datetime_text(file_timestamp),
                    "url": request_url,
                    "md5": hashlib.md5(payload_bytes).hexdigest(),
                    "size_bytes": len(payload_bytes),
                },
                "request_url": request_url,
                "output_path": str(output_path),
                "bytes_written": len(payload_bytes),
                "content_type": "application/zip",
                "preview_member": member_name,
                "preview_lines": preview_lines,
                "validation": gdelt_validation_summary(
                    member_name=member_name,
                    row_count=len(rows),
                    expected_columns=GDELT_EXPECTED_COLUMNS[source_skill],
                ),
            }
        )
        row_offset += current_count

    return {
        "ok": True,
        "mode": "range",
        "selected_count": len(downloads),
        "downloaded_count": len(downloads),
        "skipped_count": 0,
        "downloads": downloads,
        "skipped": [],
    }


def generate_gdelt_payload(*, mission: dict[str, Any], scenario: dict[str, Any], mode: str, source_skill: str) -> dict[str, Any]:
    start, end = mission_window(mission)
    count = record_count_for_source(source_skill, mode, scenario)
    times = [parse_datetime(item) or shifted_datetime(start, scenario) for item in datetime_labels(start, end, count, scenario)]
    claim_type = maybe_text(scenario.get("claim_type")) or "other"
    domain = PUBLIC_DOMAINS.get(claim_type, PUBLIC_DOMAINS["other"])
    articles = []
    for index, seen_at in enumerate(times):
        title, body = public_line_text(scenario, mission, source_skill, index)
        articles.append(
            {
                "title": title,
                "url": f"https://{domain}/article/{maybe_text(scenario.get('scenario_id'))}/{index + 1}",
                "domain": domain,
                "language": "English",
                "sourcelang": "English",
                "seendate": gdelt_datetime_text(seen_at),
                "description": body,
            }
        )
    return {"articles": articles, "simulation": {"scenario_id": maybe_text(scenario.get("scenario_id")), "mode": mode}}


def generate_bluesky_payload(*, mission: dict[str, Any], scenario: dict[str, Any], mode: str, source_skill: str) -> dict[str, Any]:
    start, end = mission_window(mission)
    count = record_count_for_source(source_skill, mode, scenario)
    timestamps = datetime_labels(start, end, count, scenario)
    seed_posts: list[dict[str, Any]] = []
    for index, timestamp in enumerate(timestamps):
        title, body = public_line_text(scenario, mission, source_skill, index)
        uri = f"at://did:plc:sim{seed_for_source(scenario, source_skill) % 100000}/app.bsky.feed.post/{index + 1}"
        parent_uri = seed_posts[0]["uri"] if index >= 3 and seed_posts else ""
        seed_posts.append(
            {
                "uri": uri,
                "cid": f"sim-cid-{index + 1}",
                "text": f"{title}. {body}",
                "author_handle": f"user{index + 1}.example",
                "author_did": f"did:plc:sim{index + 1}",
                "created_at": timestamp,
                "timestamp_utc": timestamp,
                "reply_count": 1 if parent_uri else 0,
                "repost_count": 2 + index,
                "like_count": 8 + index * 3,
                "quote_count": index % 2,
                "reply_parent_uri": parent_uri,
                "reply_root_uri": seed_posts[0]["uri"] if parent_uri else "",
                "langs": ["en"],
            }
        )
    return {"seed_posts": seed_posts, "threads": [], "simulation": {"scenario_id": maybe_text(scenario.get("scenario_id")), "mode": mode}}


def youtube_video_ids_from_artifact(path: Path) -> list[str]:
    if not path.exists():
        return []
    payload = read_jsonl(path) if path.suffix.lower() == ".jsonl" else read_json(path)
    records = payload if isinstance(payload, list) else []
    output: list[str] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        video_id = maybe_text(record.get("video_id"))
        if video_id:
            output.append(video_id)
    return output


def generate_youtube_video_records(*, mission: dict[str, Any], scenario: dict[str, Any], mode: str, source_skill: str) -> list[dict[str, Any]]:
    start, end = mission_window(mission)
    count = record_count_for_source(source_skill, mode, scenario)
    timestamps = datetime_labels(start, end, count, scenario)
    records: list[dict[str, Any]] = []
    for index, timestamp in enumerate(timestamps):
        title, body = public_line_text(scenario, mission, source_skill, index)
        video_id = f"sim-video-{seed_for_source(scenario, source_skill) % 10000}-{index + 1}"
        records.append(
            {
                "video_id": video_id,
                "query": maybe_text(scenario.get("public_topic")) or maybe_text(mission.get("topic")),
                "video": {
                    "id": video_id,
                    "title": title,
                    "description": body,
                    "channel_title": f"Sim Channel {index + 1}",
                    "published_at": timestamp,
                    "statistics": {
                        "view_count": 1200 + index * 250,
                        "like_count": 55 + index * 10,
                        "comment_count": 14 + index * 3,
                    },
                    "default_language": "en",
                },
            }
        )
    return records


def generate_youtube_comment_records(
    *,
    mission: dict[str, Any],
    scenario: dict[str, Any],
    mode: str,
    source_skill: str,
    dependency_artifacts: dict[str, Path],
) -> list[dict[str, Any]]:
    start, end = mission_window(mission)
    count = record_count_for_source(source_skill, mode, scenario)
    timestamps = datetime_labels(start, end, count, scenario)
    video_ids = youtube_video_ids_from_artifact(dependency_artifacts.get("youtube-video-search", Path("/__missing__")))
    if not video_ids:
        video_ids = [f"sim-video-{seed_for_source(scenario, source_skill) % 10000}-1"]
    records: list[dict[str, Any]] = []
    for index, timestamp in enumerate(timestamps):
        title, body = public_line_text(scenario, mission, source_skill, index)
        video_id = video_ids[index % len(video_ids)]
        comment_id = f"sim-comment-{index + 1}"
        parent_comment_id = records[0]["comment_id"] if index >= 4 and records else ""
        records.append(
            {
                "comment_id": comment_id,
                "video_id": video_id,
                "text_original": f"{title}. {body}",
                "text_display": f"{title}. {body}",
                "author_display_name": f"Commenter {index + 1}",
                "published_at": timestamp,
                "like_count": 2 + index,
                "thread_id": f"thread-{video_id}",
                "parent_comment_id": parent_comment_id,
                "comment_type": "reply" if parent_comment_id else "comment",
                "source": {"search_terms": maybe_text(scenario.get("public_topic")) or maybe_text(mission.get("topic"))},
            }
        )
    return records


def reggov_comment_ids_from_artifact(path: Path) -> list[str]:
    if not path.exists():
        return []
    payload = read_jsonl(path) if path.suffix.lower() == ".jsonl" else read_json(path)
    records = payload if isinstance(payload, list) else []
    output: list[str] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        comment_id = maybe_text(record.get("id") or record.get("comment_id"))
        if comment_id:
            output.append(comment_id)
    return output


def reggov_resource(*, mission: dict[str, Any], scenario: dict[str, Any], index: int, comment_id: str) -> dict[str, Any]:
    start, end = mission_window(mission)
    timestamp = datetime_labels(start, end, max(record_count_for_source("regulationsgov-comments-fetch", "support", scenario), 1), scenario)[
        index % max(record_count_for_source("regulationsgov-comments-fetch", "support", scenario), 1)
    ]
    title, body = public_line_text(scenario, mission, "regulationsgov-comments-fetch", index)
    return {
        "id": comment_id,
        "attributes": {
            "comment": f"{body} This public comment references policy, regulation, and agency review.",
            "title": title,
            "postedDate": timestamp,
            "lastModifiedDate": timestamp,
            "docketId": f"EPA-SIM-{seed_for_source(scenario, 'regulationsgov-comments-fetch') % 1000:03d}",
            "documentType": "Public Comment",
            "agencyId": "EPA",
            "organization": f"Simulated Organization {index + 1}",
            "subject": title,
        },
        "links": {
            "self": f"https://example.invalid/regulations/{comment_id}",
        },
    }


def generate_reggov_records(
    *,
    mission: dict[str, Any],
    scenario: dict[str, Any],
    mode: str,
    source_skill: str,
    dependency_artifacts: dict[str, Path],
) -> list[dict[str, Any]]:
    count = record_count_for_source(source_skill, mode, scenario)
    dependency_ids = reggov_comment_ids_from_artifact(dependency_artifacts.get("regulationsgov-comments-fetch", Path("/__missing__")))
    if not dependency_ids:
        dependency_ids = [f"SIM-CMT-{index + 1:03d}" for index in range(max(count, 1))]
    records: list[dict[str, Any]] = []
    for index in range(count):
        comment_id = dependency_ids[index % len(dependency_ids)]
        resource = reggov_resource(mission=mission, scenario=scenario, index=index, comment_id=comment_id)
        if source_skill == "regulationsgov-comment-detail-fetch":
            records.append(
                {
                    "comment_id": comment_id,
                    "detail": {"data": resource},
                    "response_url": resource["links"]["self"],
                    "validation": {"ok": True},
                }
            )
        else:
            records.append(resource)
    return records


def available_metrics_for_source(source_skill: str, claim_type: str, mode: str, scenario: dict[str, Any]) -> list[str]:
    override = source_override(scenario, source_skill)
    metric_values = override.get("metric_values")
    if isinstance(metric_values, dict):
        metrics = [maybe_text(key) for key in metric_values.keys() if maybe_text(key)]
        if metrics:
            return metrics
    available = list(SOURCE_METRIC_CATALOG.get(source_skill, ()))
    if not available:
        return []
    rules = CLAIM_METRIC_RULES.get(claim_type)
    if rules is None:
        return available[:1]
    support_metrics = [metric for metric in available if metric in rules.get("support", {})]
    contradict_metrics = [metric for metric in available if metric in rules.get("contradict", {})]
    if mode == "support":
        return support_metrics[:2] or available[:1]
    if mode == "contradict":
        return contradict_metrics[:2] or support_metrics[:1] or available[:1]
    if mode == "mixed":
        combined: list[str] = []
        for metric in support_metrics[:1] + contradict_metrics[:1]:
            if metric and metric not in combined:
                combined.append(metric)
        return combined or support_metrics[:1] or contradict_metrics[:1] or available[:1]
    if mode == "sparse":
        return support_metrics[:1] or available[:1]
    return available[:1]


def metric_value_for_mode(claim_type: str, metric: str, mode: str) -> float:
    rules = CLAIM_METRIC_RULES.get(claim_type, {"support": {}, "contradict": {}})
    support_value = maybe_number(rules.get("support", {}).get(metric))
    contradict_value = maybe_number(rules.get("contradict", {}).get(metric))
    if mode == "support":
        if metric == "fire_detection_count":
            return 3.0
        if claim_type == "drought" and metric in {"precipitation_sum", "soil_moisture_0_to_7cm"} and support_value is not None:
            return round(support_value * 0.5, 3)
        if support_value is not None:
            return round(support_value * 1.35, 3)
        return 10.0
    if mode == "contradict":
        if metric == "fire_detection_count":
            return 0.0
        if claim_type in {"wildfire", "drought"} and metric in {"precipitation_sum", "relative_humidity_2m", "soil_moisture_0_to_7cm"}:
            base = contradict_value if contradict_value is not None else support_value
            if base is not None:
                return round(base * 1.3, 3)
        if contradict_value is not None:
            return round(contradict_value * 0.7 if contradict_value > 0 else contradict_value, 3)
        if support_value is not None:
            return round(support_value * 0.5, 3)
        return 2.0
    if mode == "mixed":
        if support_value is not None and contradict_value is not None and support_value != contradict_value:
            return round((support_value + contradict_value) / 2.0, 3)
        return metric_value_for_mode(claim_type, metric, "support")
    if mode == "sparse":
        if support_value is not None and contradict_value is not None:
            return round((support_value + contradict_value) / 2.0, 3)
        if support_value is not None:
            return round(support_value * 0.95, 3)
        return 5.0
    return 1.0


def coerce_series(values: Any, *, count: int, fallback: float, rng: random.Random) -> list[float]:
    if isinstance(values, list):
        output = [float(item) for item in values if maybe_number(item) is not None]
        if output:
            return output
    scalar = maybe_number(values)
    base = float(scalar) if scalar is not None else fallback
    if count <= 1:
        return [round(base, 3)]
    spread = abs(base) * 0.12
    if spread < 0.5:
        spread = 0.5
    output: list[float] = []
    for index in range(count):
        position = (index - (count - 1) / 2.0) / max(count - 1, 1)
        jitter = rng.uniform(-spread * 0.05, spread * 0.05)
        value = base + spread * position + jitter
        if base >= 0:
            value = max(0.0, value)
        output.append(round(value, 3))
    return output


def metric_series(
    *,
    source_skill: str,
    claim_type: str,
    metric: str,
    mode: str,
    count: int,
    scenario: dict[str, Any],
    rng: random.Random,
) -> list[float]:
    override = source_override(scenario, source_skill)
    metric_values = override.get("metric_values")
    if isinstance(metric_values, dict) and metric in metric_values:
        return coerce_series(metric_values[metric], count=count, fallback=metric_value_for_mode(claim_type, metric, mode), rng=rng)
    if mode == "mixed":
        rules = CLAIM_METRIC_RULES.get(claim_type, {"support": {}, "contradict": {}})
        support_value = maybe_number(rules.get("support", {}).get(metric))
        contradict_value = maybe_number(rules.get("contradict", {}).get(metric))
        if support_value is not None and contradict_value is not None and count >= 2:
            raw = [metric_value_for_mode(claim_type, metric, "contradict"), metric_value_for_mode(claim_type, metric, "support")]
            if count > 2:
                raw.insert(1, round((raw[0] + raw[1]) / 2.0, 3))
            return coerce_series(raw[:count], count=count, fallback=raw[-1], rng=rng)
    return coerce_series(None, count=count, fallback=metric_value_for_mode(claim_type, metric, mode), rng=rng)


def generate_open_meteo_payload(*, mission: dict[str, Any], scenario: dict[str, Any], mode: str, source_skill: str) -> dict[str, Any]:
    start, end = mission_window(mission)
    geometry = ensure_object(mission_region(mission).get("geometry"), "mission.region.geometry")
    latitude, longitude = geometry_center(geometry)
    latitude, longitude = shifted_coordinates(latitude, longitude, scenario)
    rng = random.Random(seed_for_source(scenario, source_skill))
    claim_type = maybe_text(scenario.get("claim_type")) or "other"
    metrics = available_metrics_for_source(source_skill, claim_type, mode, scenario)
    if source_skill in fault_list(scenario, "degrade_sources") and len(metrics) > 1:
        metrics = metrics[:1]
    point_count = max(1, record_count_for_source(source_skill, mode, scenario))
    record: dict[str, Any] = {
        "latitude": round(latitude, 6),
        "longitude": round(longitude, 6),
        "timezone": "GMT",
        "elevation": 10.0,
    }
    hourly_metrics = [metric for metric in metrics if metric not in DAILY_METRICS]
    daily_metrics = [metric for metric in metrics if metric in DAILY_METRICS]
    if source_skill == "open-meteo-flood-fetch" and not daily_metrics:
        daily_metrics = metrics
        hourly_metrics = []
    if hourly_metrics:
        hourly_time = datetime_labels(start, end, point_count, scenario)
        hourly = {"time": hourly_time}
        hourly_units: dict[str, str] = {}
        for metric in hourly_metrics:
            hourly[metric] = metric_series(
                source_skill=source_skill,
                claim_type=claim_type,
                metric=metric,
                mode=mode,
                count=len(hourly_time),
                scenario=scenario,
                rng=rng,
            )
            hourly_units[metric] = METRIC_UNITS.get(metric, "unknown")
        record["hourly"] = hourly
        record["hourly_units"] = hourly_units
    if daily_metrics:
        daily_time = date_labels(start, end, point_count, scenario)
        daily = {"time": daily_time}
        daily_units: dict[str, str] = {}
        for metric in daily_metrics:
            daily[metric] = metric_series(
                source_skill=source_skill,
                claim_type=claim_type,
                metric=metric,
                mode=mode,
                count=len(daily_time),
                scenario=scenario,
                rng=rng,
            )
            daily_units[metric] = METRIC_UNITS.get(metric, "unknown")
        record["daily"] = daily
        record["daily_units"] = daily_units
    return {"records": [record], "simulation": {"scenario_id": maybe_text(scenario.get("scenario_id")), "mode": mode}}


def generate_nasa_firms_payload(*, mission: dict[str, Any], scenario: dict[str, Any], mode: str, source_skill: str) -> dict[str, Any]:
    start, end = mission_window(mission)
    geometry = ensure_object(mission_region(mission).get("geometry"), "mission.region.geometry")
    latitude, longitude = geometry_center(geometry)
    latitude, longitude = shifted_coordinates(latitude, longitude, scenario)
    override = source_override(scenario, source_skill)
    override_count = maybe_number(override.get("fire_count"))
    if override_count is not None:
        count = max(0, int(override_count))
    elif mode == "support":
        count = 3
    elif mode == "mixed":
        count = 1
    elif mode == "sparse":
        count = 1
    else:
        count = 0
    if source_skill in fault_list(scenario, "degrade_sources"):
        count = min(count, 1)
    timestamps = datetime_labels(start, end, max(count, 1), scenario)
    records: list[dict[str, Any]] = []
    for index in range(count):
        records.append(
            {
                "_acquired_at_utc": timestamps[index % len(timestamps)],
                "_chunk_start_date": start.date().isoformat(),
                "_chunk_end_date": end.date().isoformat(),
                "_latitude": round(latitude + index * 0.01, 6),
                "_longitude": round(longitude + index * 0.01, 6),
                "confidence": "n",
                "satellite": "NOAA-20",
                "instrument": "VIIRS",
                "frp": round(25.0 + index * 8.0, 2),
            }
        )
    return {"records": records, "request": {"start_date": start.date().isoformat(), "end_date": end.date().isoformat()}}


def generate_openaq_payload(*, mission: dict[str, Any], scenario: dict[str, Any], mode: str, source_skill: str) -> dict[str, Any]:
    start, end = mission_window(mission)
    geometry = ensure_object(mission_region(mission).get("geometry"), "mission.region.geometry")
    latitude, longitude = geometry_center(geometry)
    latitude, longitude = shifted_coordinates(latitude, longitude, scenario)
    claim_type = maybe_text(scenario.get("claim_type")) or "other"
    rng = random.Random(seed_for_source(scenario, source_skill))
    metrics = available_metrics_for_source(source_skill, claim_type, mode, scenario)
    if mode == "mixed" and "pm10" not in metrics and "pm2_5" in metrics:
        metrics.append("pm10")
    if source_skill in fault_list(scenario, "degrade_sources") and len(metrics) > 1:
        metrics = metrics[:1]
    sample_count = max(1, record_count_for_source(source_skill, mode, scenario))
    times = datetime_labels(start, end, sample_count, scenario)
    rows: list[dict[str, Any]] = []
    for metric_index, metric in enumerate(metrics or ["pm2_5"]):
        values = metric_series(
            source_skill=source_skill,
            claim_type=claim_type,
            metric=metric,
            mode=mode if mode != "mixed" or metric_index == 0 else "contradict",
            count=len(times),
            scenario=scenario,
            rng=rng,
        )
        for index, timestamp in enumerate(times):
            rows.append(
                {
                    "parameter": {"name": metric, "units": METRIC_UNITS.get(metric, "unknown")},
                    "value": values[index],
                    "date": {"utc": timestamp},
                    "coordinates": {"latitude": round(latitude, 6), "longitude": round(longitude, 6)},
                    "location": {"id": 1000 + metric_index, "name": f"Sim Station {metric_index + 1}"},
                    "sensor": {"id": 2000 + metric_index},
                    "provider": {"name": "Simulated OpenAQ"},
                }
            )
    return {"result": {"records": rows}, "simulation": {"scenario_id": maybe_text(scenario.get("scenario_id")), "mode": mode}}


def estimated_airnow_aqi(metric: str, value: float) -> int:
    factor = {
        "nitrogen_dioxide": 1.5,
        "ozone": 1.0,
        "pm10": 1.6,
        "pm2_5": 2.8,
    }.get(metric, 1.0)
    return max(0, min(500, int(round(value * factor))))


def generate_airnow_payload(*, mission: dict[str, Any], scenario: dict[str, Any], mode: str, source_skill: str) -> dict[str, Any]:
    start, end = mission_window(mission)
    geometry = ensure_object(mission_region(mission).get("geometry"), "mission.region.geometry")
    latitude, longitude = geometry_center(geometry)
    latitude, longitude = shifted_coordinates(latitude, longitude, scenario)
    claim_type = maybe_text(scenario.get("claim_type")) or "other"
    rng = random.Random(seed_for_source(scenario, source_skill))
    metrics = available_metrics_for_source(source_skill, claim_type, mode, scenario)
    if source_skill in fault_list(scenario, "degrade_sources") and len(metrics) > 1:
        metrics = metrics[:1]
    sample_count = max(1, record_count_for_source(source_skill, mode, scenario))
    timestamps = datetime_labels(start, end, sample_count, scenario)
    records: list[dict[str, Any]] = []
    for metric_index, metric in enumerate(metrics or ["pm2_5"]):
        parameter_name = AIRNOW_PARAMETER_NAMES.get(metric, "PM25")
        values = metric_series(
            source_skill=source_skill,
            claim_type=claim_type,
            metric=metric,
            mode=mode if mode != "mixed" or metric_index == 0 else "contradict",
            count=len(timestamps),
            scenario=scenario,
            rng=rng,
        )
        for index, timestamp in enumerate(timestamps):
            site_lat = round(latitude + metric_index * 0.01, 6)
            site_lon = round(longitude + metric_index * 0.01, 6)
            concentration = round(values[index], 3)
            records.append(
                {
                    "aqsid": f"sim-aqsid-{metric_index + 1:03d}",
                    "site_name": f"Sim AirNow Site {metric_index + 1}",
                    "status": "Active",
                    "epa_region": f"{(metric_index % 9) + 1:02d}",
                    "latitude": site_lat,
                    "longitude": site_lon,
                    "country_code": "US",
                    "state_name": "SimState",
                    "observed_at_utc": timestamp,
                    "data_source": "AIRNOW",
                    "reporting_areas": [maybe_text(mission_region(mission).get("label")) or "Sim Reporting Area"],
                    "parameter_name": parameter_name,
                    "aqi_value": estimated_airnow_aqi(metric, concentration),
                    "aqi_kind": AIRNOW_AQI_KIND.get(metric, "nowcast-aqi"),
                    "raw_concentration": concentration,
                    "unit": AIRNOW_UNITS.get(metric, METRIC_UNITS.get(metric, "unknown")),
                    "measured": True,
                    "source_file_url": f"https://files.airnowtech.org/airnow/sim/{maybe_text(scenario.get('scenario_id'))}/{parameter_name}/{index + 1}.dat",
                }
            )
    return {
        "schema_version": SCHEMA_VERSION,
        "source_skill": source_skill,
        "generated_at_utc": utc_now_iso(),
        "request": {
            "bbox": {
                "min_lon": round(longitude - 0.2, 6),
                "min_lat": round(latitude - 0.2, 6),
                "max_lon": round(longitude + 0.2, 6),
                "max_lat": round(latitude + 0.2, 6),
            },
            "start_datetime_utc": to_rfc3339_z(start),
            "end_datetime_utc": to_rfc3339_z(end),
            "parameter_names": [AIRNOW_PARAMETER_NAMES.get(metric, "PM25") for metric in metrics or ["pm2_5"]],
            "hour_count": len(timestamps),
            "hourly_file_urls": [item["source_file_url"] for item in records[: len(timestamps)]],
        },
        "dry_run": False,
        "transport": {
            "attempted_files": len(timestamps),
            "successful_files": len(timestamps),
            "failed_files": 0,
            "total_bytes": len(records) * 128,
            "total_retries": 0,
            "status_code_counts": {"200": len(timestamps)},
            "failures": [],
        },
        "validation_summary": {
            "ok": True,
            "total_issue_count": 0,
            "issues": [],
            "input_rows": len(records),
            "rows_bad_coordinates": 0,
            "rows_outside_bbox": 0,
            "rows_outside_window": 0,
            "parameter_candidates": len(records),
            "records_emitted": len(records),
        },
        "record_count": len(records),
        "records": records,
        "artifacts": {},
    }


def dependency_artifact_paths(statuses: list[dict[str, Any]]) -> dict[str, Path]:
    output: dict[str, Path] = {}
    for status in statuses:
        if not isinstance(status, dict):
            continue
        state = maybe_text(status.get("status"))
        reason = maybe_text(status.get("reason"))
        if state not in {"completed", "skipped"}:
            continue
        if state == "skipped" and reason != "artifact_exists":
            continue
        source_skill = maybe_text(status.get("source_skill"))
        artifact_path = maybe_text(status.get("artifact_path"))
        if not source_skill or not artifact_path:
            continue
        output[source_skill] = Path(artifact_path).expanduser().resolve()
    return output


def generate_payload_for_source(
    *,
    mission: dict[str, Any],
    scenario: dict[str, Any],
    source_skill: str,
    mode: str,
    step: dict[str, Any],
    dependency_statuses: list[dict[str, Any]],
) -> Any:
    start, end = mission_window(mission)
    if source_skill in fault_list(scenario, "empty_sources"):
        return empty_payload_for_source(source_skill, start, end)
    dependency_artifacts = dependency_artifact_paths(dependency_statuses)
    if source_skill == "gdelt-doc-search":
        return generate_gdelt_payload(mission=mission, scenario=scenario, mode=mode, source_skill=source_skill)
    if source_skill in RAW_GDELT_SOURCES:
        return generate_raw_gdelt_manifest(
            mission=mission,
            scenario=scenario,
            mode=mode,
            source_skill=source_skill,
            step=step,
        )
    if source_skill == "bluesky-cascade-fetch":
        return generate_bluesky_payload(mission=mission, scenario=scenario, mode=mode, source_skill=source_skill)
    if source_skill == "youtube-video-search":
        return generate_youtube_video_records(mission=mission, scenario=scenario, mode=mode, source_skill=source_skill)
    if source_skill == "youtube-comments-fetch":
        return generate_youtube_comment_records(
            mission=mission,
            scenario=scenario,
            mode=mode,
            source_skill=source_skill,
            dependency_artifacts=dependency_artifacts,
        )
    if source_skill in {"regulationsgov-comments-fetch", "regulationsgov-comment-detail-fetch"}:
        return generate_reggov_records(
            mission=mission,
            scenario=scenario,
            mode=mode,
            source_skill=source_skill,
            dependency_artifacts=dependency_artifacts,
        )
    if source_skill == "airnow-hourly-obs-fetch":
        return generate_airnow_payload(mission=mission, scenario=scenario, mode=mode, source_skill=source_skill)
    if source_skill in {"open-meteo-historical-fetch", "open-meteo-air-quality-fetch", "open-meteo-flood-fetch"}:
        return generate_open_meteo_payload(mission=mission, scenario=scenario, mode=mode, source_skill=source_skill)
    if source_skill == "nasa-firms-fire-fetch":
        return generate_nasa_firms_payload(mission=mission, scenario=scenario, mode=mode, source_skill=source_skill)
    if source_skill == "openaq-data-fetch":
        return generate_openaq_payload(mission=mission, scenario=scenario, mode=mode, source_skill=source_skill)
    raise ValueError(f"Unsupported source skill in simulator: {source_skill}")


def write_artifact(path: Path, payload: Any, *, pretty: bool) -> None:
    if path.suffix.lower() == ".jsonl":
        if not isinstance(payload, list):
            raise ValueError(f"Expected list payload for JSONL artifact: {path}")
        write_jsonl(path, [item for item in payload if isinstance(item, dict)])
        return
    write_json(path, payload, pretty=pretty)


def load_plan(run_dir: Path, round_id: str) -> dict[str, Any]:
    return ensure_object(read_json(fetch_plan_path(run_dir, round_id)), "fetch_plan")


def simulate_round(
    *,
    run_dir: Path,
    round_id: str,
    scenario: dict[str, Any],
    scenario_source: str,
    skip_input_check: bool,
    continue_on_error: bool,
    overwrite: bool,
    skip_existing: bool,
) -> dict[str, Any]:
    current_round_id = resolve_round_id(run_dir, round_id)
    plan = load_plan(run_dir, current_round_id)
    if not skip_input_check:
        ensure_fetch_plan_inputs_match(run_dir=run_dir, round_id=current_round_id, plan=plan)
    mission = ensure_object(read_json(mission_path(run_dir)), "mission")
    tasks_payload = read_json(tasks_path(run_dir, current_round_id))
    tasks = [item for item in tasks_payload if isinstance(item, dict)] if isinstance(tasks_payload, list) else []
    steps = [item for item in plan.get("steps", []) if isinstance(item, dict)]

    statuses: list[dict[str, Any]] = []
    succeeded: set[str] = set()
    with exclusive_file_lock(fetch_lock_path(run_dir, current_round_id)):
        for step in steps:
            step_id = maybe_text(step.get("step_id"))
            role = maybe_text(step.get("role"))
            source_skill = maybe_text(step.get("source_skill"))
            if source_skill not in SUPPORTED_SOURCES:
                failure_status = {
                    "step_id": step_id,
                    "role": role,
                    "source_skill": source_skill,
                    "status": "failed",
                    "reason": "unsupported_source_skill",
                }
                statuses.append(failure_status)
                if not continue_on_error:
                    break
                continue

            depends_on = [maybe_text(item) for item in step.get("depends_on", []) if maybe_text(item)]
            if any(item not in succeeded for item in depends_on):
                statuses.append(
                    {
                        "step_id": step_id,
                        "role": role,
                        "source_skill": source_skill,
                        "status": "skipped",
                        "reason": f"Unmet dependencies: {depends_on}",
                    }
                )
                if not continue_on_error:
                    break
                continue

            artifact_path = Path(maybe_text(step.get("artifact_path"))).expanduser().resolve()
            stdout_path = Path(maybe_text(step.get("stdout_path"))).expanduser().resolve()
            stderr_path = Path(maybe_text(step.get("stderr_path"))).expanduser().resolve()
            artifact_capture = maybe_text(step.get("artifact_capture"))

            if artifact_path.exists():
                if skip_existing:
                    statuses.append(
                        {
                            "step_id": step_id,
                            "role": role,
                            "source_skill": source_skill,
                            "status": "skipped",
                            "reason": "artifact_exists",
                            "artifact_path": str(artifact_path),
                            "stdout_path": str(stdout_path),
                            "stderr_path": str(stderr_path),
                        }
                    )
                    succeeded.add(step_id)
                    continue
                if not overwrite:
                    statuses.append(
                        {
                            "step_id": step_id,
                            "role": role,
                            "source_skill": source_skill,
                            "status": "failed",
                            "reason": "artifact_exists",
                            "artifact_path": str(artifact_path),
                        }
                    )
                    if not continue_on_error:
                        break
                    continue

            try:
                mode = scenario_mode_for_source(scenario, source_skill)
                if mode not in MODE_VALUES:
                    raise ValueError(f"Unsupported mode for {source_skill}: {mode}")
                payload = generate_payload_for_source(
                    mission=mission,
                    scenario=scenario,
                    source_skill=source_skill,
                    mode=mode,
                    step=step,
                    dependency_statuses=statuses,
                )
                artifact_path.parent.mkdir(parents=True, exist_ok=True)
                stdout_path.parent.mkdir(parents=True, exist_ok=True)
                stderr_path.parent.mkdir(parents=True, exist_ok=True)
                write_artifact(artifact_path, payload, pretty=True)
                record_count = source_count(payload, source_skill)
                if artifact_capture == "stdout-json":
                    write_artifact(stdout_path, payload, pretty=True)
                else:
                    write_json(
                        stdout_path,
                        {
                            "step_id": step_id,
                            "source_skill": source_skill,
                            "scenario_id": maybe_text(scenario.get("scenario_id")),
                            "scenario_source": scenario_source,
                            "mode": mode,
                            "record_count": record_count,
                            "artifact_path": str(artifact_path),
                            "generated_at_utc": utc_now_iso(),
                        },
                        pretty=True,
                    )
                write_text(
                    stderr_path,
                    f"[simulated] step_id={step_id} source_skill={source_skill} "
                    f"scenario_id={maybe_text(scenario.get('scenario_id'))} mode={mode} records={record_count}\n",
                )
                statuses.append(
                    {
                        "step_id": step_id,
                        "role": role,
                        "source_skill": source_skill,
                        "status": "completed",
                        "artifact_path": str(artifact_path),
                        "stdout_path": str(stdout_path),
                        "stderr_path": str(stderr_path),
                        "simulation_mode": mode,
                    }
                )
                succeeded.add(step_id)
            except Exception as exc:
                stderr_path.parent.mkdir(parents=True, exist_ok=True)
                write_text(stderr_path, f"[simulated-error] {exc}\n")
                failure_status = {
                    "step_id": step_id,
                    "role": role,
                    "source_skill": source_skill,
                    "status": "failed",
                    "reason": str(exc),
                    "artifact_path": str(artifact_path),
                    "stderr_path": str(stderr_path),
                }
                statuses.append(failure_status)
                if not continue_on_error:
                    break

        result = {
            "run_dir": str(run_dir),
            "round_id": current_round_id,
            "plan_path": str(fetch_plan_path(run_dir, current_round_id)),
            "plan_sha256": file_sha256(fetch_plan_path(run_dir, current_round_id)),
            "execution_mode": "simulated",
            "scenario": {
                "scenario_id": maybe_text(scenario.get("scenario_id")),
                "scenario_source": scenario_source,
                "claim_type": maybe_text(scenario.get("claim_type")),
                "mode": maybe_text(scenario.get("mode")),
                "seed": scenario.get("seed"),
            },
            "step_count": len(steps),
            "completed_count": sum(1 for item in statuses if maybe_text(item.get("status")) == "completed"),
            "failed_count": sum(1 for item in statuses if maybe_text(item.get("status")) == "failed"),
            "statuses": statuses,
        }
        execution_path = fetch_execution_path(run_dir, current_round_id)
        write_json(execution_path, result, pretty=True)
        result["execution_path"] = str(execution_path)
        return result


def command_list_presets(args: argparse.Namespace) -> dict[str, Any]:
    presets: list[dict[str, Any]] = []
    for path in preset_paths():
        payload = ensure_object(read_json(path), str(path))
        presets.append(
            {
                "preset": path.stem,
                "path": str(path),
                "scenario_id": maybe_text(payload.get("scenario_id")),
                "description": maybe_text(payload.get("description")),
                "claim_type": maybe_text(payload.get("claim_type")),
                "mode": maybe_text(payload.get("mode")),
            }
        )
    return {"preset_count": len(presets), "presets": presets}


def command_write_preset(args: argparse.Namespace) -> dict[str, Any]:
    preset_path = find_preset_path(args.preset)
    payload = ensure_object(read_json(preset_path), str(preset_path))
    output_path = Path(args.output).expanduser().resolve()
    write_json(output_path, payload, pretty=True)
    return {
        "preset": args.preset,
        "source_path": str(preset_path),
        "output_path": str(output_path),
        "scenario_id": maybe_text(payload.get("scenario_id")),
    }


def command_simulate_round(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = Path(args.run_dir).expanduser().resolve()
    current_round_id = resolve_round_id(run_dir, args.round_id)
    plan = load_plan(run_dir, current_round_id)
    mission = ensure_object(read_json(mission_path(run_dir)), "mission")
    tasks_payload = read_json(tasks_path(run_dir, current_round_id))
    tasks = [item for item in tasks_payload if isinstance(item, dict)] if isinstance(tasks_payload, list) else []
    steps = [item for item in plan.get("steps", []) if isinstance(item, dict)]
    scenario, scenario_source = load_scenario(args=args, mission=mission, tasks=tasks, steps=steps)
    return simulate_round(
        run_dir=run_dir,
        round_id=current_round_id,
        scenario=scenario,
        scenario_source=scenario_source,
        skip_input_check=args.skip_input_check,
        continue_on_error=args.continue_on_error,
        overwrite=args.overwrite,
        skip_existing=args.skip_existing,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Deterministically simulate eco-council raw fetch artifacts.")
    sub = parser.add_subparsers(dest="command", required=True)

    list_presets = sub.add_parser("list-presets", help="List built-in simulation presets.")
    list_presets.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")

    write_preset = sub.add_parser("write-preset", help="Copy one built-in preset to a writable JSON file.")
    write_preset.add_argument("--preset", required=True, help="Preset name or scenario_id.")
    write_preset.add_argument("--output", required=True, help="Output JSON path.")
    write_preset.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")

    simulate = sub.add_parser("simulate-round", help="Write simulated raw artifacts and canonical fetch_execution.json for one round.")
    simulate.add_argument("--run-dir", required=True, help="Eco-council run directory.")
    simulate.add_argument("--round-id", default="", help="Round identifier. Defaults to latest round.")
    scenario_group = simulate.add_mutually_exclusive_group()
    scenario_group.add_argument("--scenario-input", default="", help="Custom scenario JSON path.")
    scenario_group.add_argument("--preset", default="", help="Built-in preset name or scenario_id.")
    simulate.add_argument("--claim-type", default="", help="Optional claim type when using auto scenario mode.")
    simulate.add_argument("--mode", default="support", choices=sorted(MODE_VALUES), help="Auto-scenario mode.")
    simulate.add_argument("--seed", type=int, default=None, help="Optional deterministic seed override.")
    simulate.add_argument("--continue-on-error", action="store_true", help="Continue simulating later steps after a failure.")
    overwrite_group = simulate.add_mutually_exclusive_group()
    overwrite_group.add_argument("--overwrite", action="store_true", help="Overwrite any existing artifact paths.")
    overwrite_group.add_argument("--skip-existing", action="store_true", help="Mark existing artifacts as skipped/artifact_exists.")
    simulate.add_argument("--skip-input-check", action="store_true", help="Skip fetch_plan input snapshot validation.")
    simulate.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handlers = {
        "list-presets": command_list_presets,
        "write-preset": command_write_preset,
        "simulate-round": command_simulate_round,
    }
    try:
        payload = handlers[args.command](args)
    except Exception as exc:
        result = {"command": args.command, "ok": False, "error": str(exc)}
        print(pretty_json(result, pretty=getattr(args, "pretty", False)))
        return 1
    result = {"command": args.command, "ok": True, "payload": payload}
    print(pretty_json(result, pretty=getattr(args, "pretty", False)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
