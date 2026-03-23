#!/usr/bin/env python3
"""Build report packets and moderator decision drafts for eco-council rounds."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
CONTRACT_SCRIPT_PATH = SKILL_DIR.parent / "eco-council-data-contract" / "scripts" / "eco_council_contract.py"

SCHEMA_VERSION = "1.0.0"
REPORT_ROLES = ("sociologist", "environmentalist")
PROMOTABLE_REPORT_ROLES = ("sociologist", "environmentalist", "historian")
VERDICT_SCORES = {"supports": 1.0, "contradicts": 1.0, "mixed": 0.6, "insufficient": 0.25}
METEOROLOGY_METRICS = {"temperature_2m", "wind_speed_10m", "relative_humidity_2m", "precipitation_sum", "precipitation"}
PRECIPITATION_METRICS = {
    "precipitation",
    "precipitation_sum",
    "soil_moisture_0_to_7cm",
}
HYDROLOGY_METRICS = {
    "river_discharge",
    "river_discharge_mean",
    "river_discharge_max",
    "river_discharge_min",
    "river_discharge_p25",
    "river_discharge_p75",
    "gage_height",
}
MAX_SOURCES_PER_NEXT_TASK = 2
QUESTION_RULES = (
    ("station-grade corroboration is missing", "Can station-grade air-quality measurements be added for the same mission window?"),
    ("modeled background fields should be cross-checked", "Can modeled air-quality fields be cross-checked with station or local observations?"),
    ("no mission-aligned observations matched", "Should the next round expand physical coverage or narrow claim scope so observations can be matched?"),
)
NEXT_ACTION_LIBRARY: dict[str, dict[str, Any]] = {
    "normalized-public-claims": {
        "assigned_role": "sociologist",
        "objective": "Collect and normalize concrete mission-window public claims from approved news and discussion sources.",
        "reason": "The round did not produce enough normalized public claims to assess public concern, event severity, or attribution narratives.",
        "preferred_sources": ["gdelt-doc-search", "youtube-video-search", "youtube-comments-fetch", "bluesky-cascade-fetch"],
    },
    "evidence-cards-linking-public-claims-to-physical-observations": {
        "assigned_role": "sociologist",
        "objective": "Recover more attributable public claims that can be linked directly against the available physical observations.",
        "reason": "Public-side evidence needs more concrete and attributable claim phrasing before physical evidence cards can be linked reliably.",
        "preferred_sources": ["gdelt-doc-search", "youtube-video-search", "youtube-comments-fetch"],
    },
    "station-air-quality": {
        "assigned_role": "environmentalist",
        "objective": "Fetch station-based air-quality corroboration for the same mission window and geometry.",
        "reason": "Station-grade corroboration remains incomplete or modeled fields still need cross-checking.",
        "preferred_sources": ["openaq-data-fetch", "airnow-hourly-obs-fetch"],
    },
    "fire-detection": {
        "assigned_role": "environmentalist",
        "objective": "Fetch fire-detection evidence aligned with the mission window and geometry.",
        "reason": "Wildfire-related claims still lack direct fire-detection corroboration.",
        "preferred_sources": ["nasa-firms-fire-fetch"],
    },
    "meteorology-background": {
        "assigned_role": "environmentalist",
        "objective": "Add meteorology background such as wind, humidity, and precipitation for the same mission window.",
        "reason": "Physical interpretation still needs weather context.",
        "preferred_sources": ["open-meteo-historical-fetch"],
    },
    "precipitation-hydrology": {
        "assigned_role": "environmentalist",
        "objective": "Add precipitation or flood-related evidence for the same mission window and geometry.",
        "reason": "Flood or water-related claims still lack direct hydrometeorological corroboration.",
        "preferred_sources": ["usgs-water-iv-fetch", "open-meteo-flood-fetch", "open-meteo-historical-fetch"],
    },
    "temperature-extremes": {
        "assigned_role": "environmentalist",
        "objective": "Add temperature-extreme evidence for the same mission window and geometry.",
        "reason": "Heat-related claims still lack direct thermal corroboration.",
        "preferred_sources": ["open-meteo-historical-fetch"],
    },
    "precipitation-soil-moisture": {
        "assigned_role": "environmentalist",
        "objective": "Add precipitation and soil-moisture evidence for the same mission window and geometry.",
        "reason": "Drought-related claims still lack direct precipitation or soil-moisture corroboration.",
        "preferred_sources": ["open-meteo-historical-fetch"],
    },
    "policy-comment-coverage": {
        "assigned_role": "sociologist",
        "objective": "Collect more policy-comment or docket evidence for the same environmental issue.",
        "reason": "Policy-reaction claims still need stronger docket or public-comment coverage.",
        "preferred_sources": ["regulationsgov-comments-fetch", "regulationsgov-comment-detail-fetch"],
    },
    "public-discussion-coverage": {
        "assigned_role": "sociologist",
        "objective": "Collect more independent public-discussion evidence for the same mission window.",
        "reason": "Current public-claim coverage is too thin or concentrated in too few channels.",
        "preferred_sources": ["gdelt-doc-search", "bluesky-cascade-fetch", "youtube-video-search"],
    },
}
SOURCE_DEPENDENCIES: dict[str, list[str]] = {
    "youtube-comments-fetch": ["youtube-video-search"],
    "regulationsgov-comment-detail-fetch": ["regulationsgov-comments-fetch"],
}
SOURCE_KEYWORDS = (
    ("youtube comments", ["youtube-comments-fetch"]),
    ("youtube comment", ["youtube-comments-fetch"]),
    ("youtube videos", ["youtube-video-search"]),
    ("youtube video", ["youtube-video-search"]),
    ("public discussion", ["gdelt-doc-search", "bluesky-cascade-fetch", "youtube-video-search"]),
    ("news and social", ["gdelt-doc-search", "youtube-video-search", "bluesky-cascade-fetch"]),
    ("news and discussion", ["gdelt-doc-search", "youtube-video-search", "bluesky-cascade-fetch"]),
    ("public claims", ["gdelt-doc-search", "youtube-video-search"]),
    ("public claim", ["gdelt-doc-search", "youtube-video-search"]),
    ("air quality", ["openaq-data-fetch", "open-meteo-air-quality-fetch"]),
    ("airnow", ["airnow-hourly-obs-fetch"]),
    ("openaq", ["openaq-data-fetch"]),
    ("station", ["openaq-data-fetch"]),
    ("firms", ["nasa-firms-fire-fetch"]),
    ("fire", ["nasa-firms-fire-fetch"]),
    ("wind", ["open-meteo-historical-fetch"]),
    ("humidity", ["open-meteo-historical-fetch"]),
    ("precipitation", ["open-meteo-historical-fetch"]),
    ("hydrology", ["usgs-water-iv-fetch", "open-meteo-flood-fetch"]),
    ("streamflow", ["usgs-water-iv-fetch"]),
    ("discharge", ["usgs-water-iv-fetch", "open-meteo-flood-fetch"]),
    ("gage", ["usgs-water-iv-fetch"]),
    ("river", ["usgs-water-iv-fetch", "open-meteo-flood-fetch"]),
    ("usgs", ["usgs-water-iv-fetch"]),
    ("weather", ["open-meteo-historical-fetch"]),
    ("meteorology", ["open-meteo-historical-fetch"]),
    ("flood", ["usgs-water-iv-fetch", "open-meteo-flood-fetch", "open-meteo-historical-fetch"]),
    ("temperature", ["open-meteo-historical-fetch"]),
    ("heat", ["open-meteo-historical-fetch"]),
    ("federal register", ["federal-register-doc-fetch"]),
    ("rulemaking", ["federal-register-doc-fetch", "regulationsgov-comments-fetch"]),
    ("proposed rule", ["federal-register-doc-fetch"]),
    ("final rule", ["federal-register-doc-fetch"]),
    ("regulations", ["regulationsgov-comments-fetch", "regulationsgov-comment-detail-fetch"]),
    ("docket", ["regulationsgov-comments-fetch", "regulationsgov-comment-detail-fetch"]),
    ("comment", ["regulationsgov-comments-fetch", "regulationsgov-comment-detail-fetch"]),
    ("youtube", ["youtube-video-search", "youtube-comments-fetch"]),
    ("bluesky", ["bluesky-cascade-fetch"]),
    ("gdelt", ["gdelt-doc-search"]),
    ("news", ["gdelt-doc-search"]),
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def pretty_json(data: Any, *, pretty: bool) -> str:
    if pretty:
        return json.dumps(data, ensure_ascii=True, indent=2, sort_keys=True)
    return json.dumps(data, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def normalize_space(value: str) -> str:
    return " ".join(str(value).split())


def maybe_text(value: Any) -> str:
    if value is None:
        return ""
    return normalize_space(str(value))


def truncate_text(value: str, limit: int) -> str:
    text = normalize_space(value)
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3].rstrip() + "..."


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, payload: Any, *, pretty: bool) -> None:
    atomic_write_text_file(path, pretty_json(payload, pretty=pretty) + "\n")


def write_text(path: Path, text: str) -> None:
    atomic_write_text_file(path, text)


def atomic_write_text_file(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=f".{path.name}.tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
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


def load_canonical_list(path: Path) -> list[dict[str, Any]]:
    payload = load_json_if_exists(path)
    if payload is None:
        return []
    if not isinstance(payload, list):
        raise ValueError(f"Expected list in {path}")
    return [item for item in payload if isinstance(item, dict)]


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True)


def stable_hash(*parts: Any) -> str:
    joined = "||".join(maybe_text(part) for part in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def unique_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = maybe_text(value)
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def expand_source_dependencies(sources: list[str]) -> list[str]:
    expanded: list[str] = []
    visiting: set[str] = set()

    def visit(source: str) -> None:
        text = maybe_text(source)
        if not text or text in visiting:
            return
        visiting.add(text)
        for dependency in SOURCE_DEPENDENCIES.get(text, []):
            visit(dependency)
        expanded.append(text)
        visiting.remove(text)

    for source in unique_strings(sources):
        visit(source)
    return unique_strings(expanded)


def counter_dict(values: list[str]) -> dict[str, int]:
    return dict(Counter(item for item in values if item))


def round_directory_name(round_id: str) -> str:
    return round_id.replace("-", "_")


def round_dir(run_dir: Path, round_id: str) -> Path:
    return run_dir / round_directory_name(round_id)


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


def mission_constraints(mission: dict[str, Any]) -> dict[str, int]:
    constraints = mission.get("constraints")
    if not isinstance(constraints, dict):
        return {}
    result: dict[str, int] = {}
    for key in ("max_rounds", "max_claims_per_round", "max_tasks_per_round"):
        value = constraints.get(key)
        if isinstance(value, int) and value > 0:
            result[key] = value
    return result


def mission_source_policy(mission: dict[str, Any], role: str) -> list[str]:
    policy = mission.get("source_policy")
    if not isinstance(policy, dict):
        return []
    values = policy.get(role)
    if not isinstance(values, list):
        return []
    return [item for item in values if isinstance(item, str) and item.strip()]


def shared_claims_path(run_dir: Path, round_id: str) -> Path:
    return round_dir(run_dir, round_id) / "shared" / "claims.json"


def shared_observations_path(run_dir: Path, round_id: str) -> Path:
    return round_dir(run_dir, round_id) / "shared" / "observations.json"


def shared_evidence_path(run_dir: Path, round_id: str) -> Path:
    return round_dir(run_dir, round_id) / "shared" / "evidence_cards.json"


def role_context_path(run_dir: Path, round_id: str, role: str) -> Path:
    return round_dir(run_dir, round_id) / role / "derived" / f"context_{role}.json"


def report_target_path(run_dir: Path, round_id: str, role: str) -> Path:
    return round_dir(run_dir, round_id) / role / f"{role}_report.json"


def report_draft_path(run_dir: Path, round_id: str, role: str) -> Path:
    return round_dir(run_dir, round_id) / role / "derived" / f"{role}_report_draft.json"


def report_packet_path(run_dir: Path, round_id: str, role: str) -> Path:
    return round_dir(run_dir, round_id) / role / "derived" / "report_packet.json"


def report_prompt_path(run_dir: Path, round_id: str, role: str) -> Path:
    return round_dir(run_dir, round_id) / role / "derived" / "openclaw_report_prompt.txt"


def decision_target_path(run_dir: Path, round_id: str) -> Path:
    return round_dir(run_dir, round_id) / "moderator" / "council_decision.json"


def decision_draft_path(run_dir: Path, round_id: str) -> Path:
    return round_dir(run_dir, round_id) / "moderator" / "derived" / "council_decision_draft.json"


def decision_packet_path(run_dir: Path, round_id: str) -> Path:
    return round_dir(run_dir, round_id) / "moderator" / "derived" / "decision_packet.json"


def decision_prompt_path(run_dir: Path, round_id: str) -> Path:
    return round_dir(run_dir, round_id) / "moderator" / "derived" / "openclaw_decision_prompt.txt"


def tasks_path(run_dir: Path, round_id: str) -> Path:
    return round_dir(run_dir, round_id) / "moderator" / "tasks.json"


def fetch_execution_path(run_dir: Path, round_id: str) -> Path:
    return round_dir(run_dir, round_id) / "moderator" / "derived" / "fetch_execution.json"


def load_contract_module() -> Any | None:
    if not CONTRACT_SCRIPT_PATH.exists():
        return None
    module_name = "eco_council_contract_reporting"
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
    issues = []
    for issue in validation.get("issues", [])[:5]:
        issues.append(f"{issue.get('path')}: {issue.get('message')}")
    raise ValueError(f"Generated invalid {kind}: {'; '.join(issues)}")


def validate_bundle(run_dir: Path) -> dict[str, Any] | None:
    if CONTRACT_MODULE is None or not hasattr(CONTRACT_MODULE, "validate_bundle"):
        return None
    return CONTRACT_MODULE.validate_bundle(run_dir)


def parse_round_components(round_id: str) -> tuple[str, int, int] | None:
    match = re.match(r"^(.*?)(\d+)$", round_id)
    if match is None:
        return None
    prefix, digits = match.groups()
    return prefix, int(digits), len(digits)


def next_round_id_for(round_id: str) -> str:
    components = parse_round_components(round_id)
    if components is None:
        return f"{round_id}-next"
    prefix, number, width = components
    return f"{prefix}{number + 1:0{width}d}"


def current_round_number(round_id: str) -> int | None:
    components = parse_round_components(round_id)
    if components is None:
        return None
    return components[1]


def round_sort_key(round_id: str) -> tuple[str, int, str]:
    components = parse_round_components(round_id)
    if components is None:
        return (round_id, 10**9, round_id)
    prefix, number, _width = components
    return (prefix, number, round_id)


def discover_round_ids(run_dir: Path) -> list[str]:
    round_ids: list[str] = []
    for child in run_dir.iterdir():
        if not child.is_dir():
            continue
        if not child.name.startswith("round_"):
            continue
        round_ids.append(child.name.replace("_", "-"))
    return sorted(unique_strings(round_ids), key=round_sort_key)


def round_ids_through(run_dir: Path, round_id: str) -> list[str]:
    current = parse_round_components(round_id)
    if current is None:
        return [item for item in discover_round_ids(run_dir) if item <= round_id]
    prefix, number, _width = current
    selected: list[str] = []
    for item in discover_round_ids(run_dir):
        components = parse_round_components(item)
        if components is None:
            continue
        item_prefix, item_number, _item_width = components
        if item_prefix == prefix and item_number <= number:
            selected.append(item)
    return selected


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


def effective_shared_observations(run_dir: Path, round_id: str) -> list[dict[str, Any]]:
    merged_by_signature: dict[str, dict[str, Any]] = {}
    ordered_signatures: list[str] = []
    for observed_round_id in round_ids_through(run_dir, round_id):
        for observation in load_canonical_list(shared_observations_path(run_dir, observed_round_id)):
            signature_payload = observation_signature_payload(observation)
            signature = stable_hash(stable_json(signature_payload))
            if signature not in merged_by_signature:
                ordered_signatures.append(signature)
            merged_by_signature[signature] = materialize_shared_observation(observation)
    return [merged_by_signature[signature] for signature in ordered_signatures]


def fetch_status_role(status: dict[str, Any]) -> str:
    role = maybe_text(status.get("assigned_role"))
    if role:
        return role
    step_id = maybe_text(status.get("step_id"))
    match = re.match(r"^step-([a-z]+)-", step_id)
    if match is None:
        return ""
    return maybe_text(match.group(1))


def completed_sources_history(run_dir: Path, round_id: str) -> dict[str, set[str]]:
    history: dict[str, set[str]] = defaultdict(set)
    for item in round_ids_through(run_dir, round_id):
        payload = load_json_if_exists(fetch_execution_path(run_dir, item))
        if not isinstance(payload, dict):
            continue
        statuses = payload.get("statuses")
        if not isinstance(statuses, list):
            continue
        for status in statuses:
            if not isinstance(status, dict):
                continue
            if maybe_text(status.get("status")) != "completed":
                continue
            role = fetch_status_role(status)
            source = maybe_text(status.get("source_skill")) or maybe_text(status.get("source"))
            if role and source:
                history[role].add(source)
    return history


def build_fallback_context(
    *,
    mission: dict[str, Any],
    round_id: str,
    tasks: list[dict[str, Any]],
    claims: list[dict[str, Any]],
    observations: list[dict[str, Any]],
    evidence_cards: list[dict[str, Any]],
    role: str,
) -> dict[str, Any]:
    role_tasks = [task for task in tasks if role == "moderator" or task.get("assigned_role") == role]
    return {
        "run": {
            "run_id": mission_run_id(mission),
            "round_id": round_id,
            "topic": maybe_text(mission.get("topic")),
            "objective": maybe_text(mission.get("objective")),
            "region": mission.get("region"),
            "window": mission.get("window"),
            "role": role,
        },
        "dataset": {
            "generated_at_utc": utc_now_iso(),
            "task_count": len(role_tasks),
            "claim_count": len(claims),
            "observation_count": len(observations),
            "evidence_count": len(evidence_cards),
        },
        "aggregates": {
            "claim_type_counts": counter_dict([maybe_text(item.get("claim_type")) for item in claims]),
            "observation_metric_counts": counter_dict([maybe_text(item.get("metric")) for item in observations]),
            "evidence_verdict_counts": counter_dict([maybe_text(item.get("verdict")) for item in evidence_cards]),
        },
        "tasks": role_tasks,
        "focus": {
            "task_ids": [maybe_text(task.get("task_id")) for task in role_tasks],
            "claims_needing_more_evidence": [
                maybe_text(card.get("claim_id"))
                for card in evidence_cards
                if maybe_text(card.get("verdict")) in {"mixed", "insufficient"}
            ],
        },
        "claims": claims,
        "observations": observations,
        "evidence_cards": evidence_cards,
    }


def load_context_or_fallback(
    *,
    run_dir: Path,
    round_id: str,
    role: str,
    mission: dict[str, Any],
    tasks: list[dict[str, Any]],
    claims: list[dict[str, Any]],
    observations: list[dict[str, Any]],
    evidence_cards: list[dict[str, Any]],
) -> dict[str, Any]:
    path = role_context_path(run_dir, round_id, role)
    payload = load_json_if_exists(path)
    if isinstance(payload, dict):
        return payload
    return build_fallback_context(
        mission=mission,
        round_id=round_id,
        tasks=tasks,
        claims=claims,
        observations=observations,
        evidence_cards=evidence_cards,
        role=role,
    )


def report_is_placeholder(report: dict[str, Any] | None) -> bool:
    if not isinstance(report, dict):
        return False
    return maybe_text(report.get("summary")).lower().startswith("pending ")


def report_has_substance(report: dict[str, Any] | None) -> bool:
    if not isinstance(report, dict):
        return False
    if report_is_placeholder(report):
        return False
    if report.get("findings"):
        return True
    return bool(report.get("open_questions") or report.get("recommended_next_actions"))


def load_report_for_decision(run_dir: Path, round_id: str, role: str, *, prefer_drafts: bool) -> tuple[dict[str, Any] | None, str]:
    final_report = load_json_if_exists(report_target_path(run_dir, round_id, role))
    if not isinstance(final_report, dict):
        final_report = None
    draft_report = load_json_if_exists(report_draft_path(run_dir, round_id, role))
    if not isinstance(draft_report, dict):
        draft_report = None
    if prefer_drafts and draft_report is not None:
        return draft_report, "draft"
    if final_report is not None:
        return final_report, "final"
    if draft_report is not None:
        return draft_report, "draft"
    return None, "missing"


def claim_sort_key(claim: dict[str, Any]) -> tuple[int, str]:
    priority = claim.get("priority")
    if not isinstance(priority, int):
        priority = 99
    return (priority, maybe_text(claim.get("claim_id")))


def evidence_rank(card: dict[str, Any]) -> int:
    verdict = maybe_text(card.get("verdict"))
    if verdict in {"supports", "contradicts"}:
        return 0
    if verdict == "mixed":
        return 1
    return 2


def gap_to_question(gap: str) -> str:
    lowered = maybe_text(gap).lower()
    for needle, question in QUESTION_RULES:
        if needle in lowered:
            return question
    if lowered.endswith("?"):
        return gap
    return f"How should the next round address this gap: {maybe_text(gap)}?"


def expected_output_kinds_for_role(role: str) -> list[str]:
    if role == "sociologist":
        return ["source-selection", "claim", "expert-report"]
    if role == "environmentalist":
        return ["source-selection", "observation", "expert-report"]
    if role == "historian":
        return ["expert-report"]
    return ["expert-report"]


def public_source_skills(claims: list[dict[str, Any]]) -> list[str]:
    sources: list[str] = []
    for claim in claims:
        refs = claim.get("public_refs")
        if not isinstance(refs, list):
            continue
        for ref in refs:
            if isinstance(ref, dict):
                sources.append(maybe_text(ref.get("source_skill")))
    return unique_strings(sources)


def infer_missing_evidence_types(*, claims: list[dict[str, Any]], observations: list[dict[str, Any]], evidence_cards: list[dict[str, Any]]) -> list[str]:
    observation_metrics = {maybe_text(item.get("metric")) for item in observations}
    has_station_observation = any(maybe_text(item.get("source_skill")) == "openaq-data-fetch" for item in observations)
    cards_by_claim_id = {maybe_text(item.get("claim_id")): item for item in evidence_cards}
    unresolved_claims: list[dict[str, Any]] = []
    for claim in claims:
        claim_id = maybe_text(claim.get("claim_id"))
        card = cards_by_claim_id.get(claim_id)
        if card is None or maybe_text(card.get("verdict")) in {"mixed", "insufficient"}:
            unresolved_claims.append(claim)

    missing: set[str] = set()
    if not claims:
        missing.add("normalized-public-claims")
    if observations and (not claims or (claims and not evidence_cards)):
        missing.add("evidence-cards-linking-public-claims-to-physical-observations")

    for card in evidence_cards:
        gaps = card.get("gaps")
        if not isinstance(gaps, list):
            continue
        gap_text = " ".join(maybe_text(item) for item in gaps).lower()
        if "station" in gap_text or "modeled background" in gap_text:
            missing.add("station-air-quality")

    for claim in unresolved_claims:
        claim_id = maybe_text(claim.get("claim_id"))
        claim_type = maybe_text(claim.get("claim_type"))
        card = cards_by_claim_id.get(claim_id)
        gap_text = " ".join(card.get("gaps", [])) if isinstance(card, dict) and isinstance(card.get("gaps"), list) else ""
        lowered_gap_text = gap_text.lower()

        if "station" in lowered_gap_text or "modeled background" in lowered_gap_text:
            missing.add("station-air-quality")

        if claim_type in {"smoke", "air-pollution"} and not has_station_observation:
            missing.add("station-air-quality")

        if claim_type in {"smoke", "wildfire"} and "fire_detection_count" not in observation_metrics:
            if "wildfire" in maybe_text(claim.get("summary")).lower() or claim_type == "wildfire":
                missing.add("fire-detection")

        if claim_type == "wildfire" and not (observation_metrics & METEOROLOGY_METRICS):
            missing.add("meteorology-background")

        if claim_type == "flood" and not (observation_metrics & (PRECIPITATION_METRICS | HYDROLOGY_METRICS)):
            missing.add("precipitation-hydrology")

        if claim_type == "heat" and "temperature_2m" not in observation_metrics:
            missing.add("temperature-extremes")

        if claim_type == "drought" and not {"precipitation_sum", "soil_moisture_0_to_7cm"} <= observation_metrics:
            missing.add("precipitation-soil-moisture")

        if claim_type == "policy-reaction":
            refs = claim.get("public_refs")
            has_reggov = False
            if isinstance(refs, list):
                has_reggov = any(
                    isinstance(ref, dict)
                    and maybe_text(ref.get("source_skill")) in {"regulationsgov-comments-fetch", "regulationsgov-comment-detail-fetch"}
                    for ref in refs
                )
            if not has_reggov:
                missing.add("policy-comment-coverage")

    if unresolved_claims and len(public_source_skills(claims)) < 2:
        if any(maybe_text(claim.get("claim_type")) != "policy-reaction" for claim in unresolved_claims):
            missing.add("public-discussion-coverage")

    return sorted(missing)


def filter_sources_for_role(mission: dict[str, Any], role: str, sources: list[str]) -> list[str]:
    allowed = set(mission_source_policy(mission, role))
    cleaned = unique_strings(sources)
    if not allowed:
        return cleaned
    filtered = [item for item in cleaned if item in allowed]
    return filtered if filtered else cleaned


def infer_sources_from_text(text: str, *, mission: dict[str, Any], role: str) -> list[str]:
    lowered = maybe_text(text).lower()
    sources: list[str] = []
    for keyword, values in SOURCE_KEYWORDS:
        pattern = r"(^|[^a-z0-9])" + re.escape(keyword) + r"([^a-z0-9]|$)"
        if re.search(pattern, lowered):
            sources.extend(values)
    if not sources:
        policy_sources = mission_source_policy(mission, role)
        if policy_sources:
            sources.extend(policy_sources[:3])
    return filter_sources_for_role(mission, role, expand_source_dependencies(sources))


def recommendation_template(recommendation: dict[str, Any]) -> dict[str, Any] | None:
    role = maybe_text(recommendation.get("assigned_role"))
    objective = maybe_text(recommendation.get("objective")).casefold()
    if not role or not objective:
        return None
    for template in NEXT_ACTION_LIBRARY.values():
        if maybe_text(template.get("assigned_role")) != role:
            continue
        if maybe_text(template.get("objective")).casefold() == objective:
            return template
    return None


def recommendation_source_list(template: dict[str, Any] | None, key: str) -> list[str]:
    if not isinstance(template, dict):
        return []
    values = template.get(key)
    if not isinstance(values, list):
        return []
    return [maybe_text(item) for item in values if maybe_text(item)]


def novel_sources(sources: list[str], completed_sources: set[str]) -> list[str]:
    return [item for item in unique_strings(sources) if item not in completed_sources]


def select_sources_with_novelty(
    *,
    candidates: list[str],
    completed_sources: set[str],
) -> list[str]:
    ordered = unique_strings(candidates)
    novel = {item for item in ordered if item not in completed_sources}
    if not novel:
        return []

    selected: list[str] = []
    for index, source in enumerate(ordered):
        if len(selected) >= MAX_SOURCES_PER_NEXT_TASK:
            break
        if source in novel:
            selected.append(source)
            continue
        remaining = [item for item in ordered[index + 1 :] if item not in selected]
        remaining_novel = any(item in novel for item in remaining)
        remaining_slots_after_pick = MAX_SOURCES_PER_NEXT_TASK - len(selected) - 1
        if remaining_novel and remaining_slots_after_pick >= 1:
            selected.append(source)

    if not any(item in novel for item in selected):
        return []
    return selected


def recommendation_key(recommendation: dict[str, Any]) -> tuple[str, str]:
    return (maybe_text(recommendation.get("assigned_role")), maybe_text(recommendation.get("objective")).lower())


def base_recommendations_from_missing_types(missing_types: list[str]) -> list[dict[str, Any]]:
    recommendations: list[dict[str, Any]] = []
    for missing_type in missing_types:
        template = NEXT_ACTION_LIBRARY.get(missing_type)
        if template is None:
            continue
        recommendations.append(
            {
                "assigned_role": template["assigned_role"],
                "objective": template["objective"],
                "reason": template["reason"],
            }
        )
    return recommendations


def combine_recommendations(*, reports: list[dict[str, Any]], missing_types: list[str]) -> list[dict[str, Any]]:
    combined: list[dict[str, Any]] = []
    for report in reports:
        actions = report.get("recommended_next_actions")
        if not isinstance(actions, list):
            continue
        for action in actions:
            if not isinstance(action, dict):
                continue
            recommendation = {
                "assigned_role": maybe_text(action.get("assigned_role")),
                "objective": maybe_text(action.get("objective")),
                "reason": maybe_text(action.get("reason")),
            }
            if all(recommendation.values()):
                combined.append(recommendation)
    combined.extend(base_recommendations_from_missing_types(missing_types))

    deduped: dict[tuple[str, str], dict[str, Any]] = {}
    for recommendation in combined:
        key = recommendation_key(recommendation)
        if not key[0] or not key[1]:
            continue
        deduped.setdefault(key, recommendation)
    return list(deduped.values())


def select_task_sources(
    *,
    mission: dict[str, Any],
    recommendation: dict[str, Any],
    role: str,
    objective: str,
    reason: str,
    completed_sources: set[str],
) -> list[str]:
    template = recommendation_template(recommendation)
    preferred_candidates = recommendation_source_list(template, "preferred_sources")
    if not preferred_candidates:
        preferred_candidates = infer_sources_from_text(f"{objective} {reason}", mission=mission, role=role)
    else:
        preferred_candidates = filter_sources_for_role(
            mission,
            role,
            expand_source_dependencies(preferred_candidates),
        )
    if not novel_sources(preferred_candidates, completed_sources):
        policy_candidates = filter_sources_for_role(
            mission,
            role,
            expand_source_dependencies(mission_source_policy(mission, role)),
        )
        preferred_candidates = unique_strings(preferred_candidates + policy_candidates)
    return select_sources_with_novelty(candidates=preferred_candidates, completed_sources=completed_sources)


def build_task_notes(current_round_id: str, reason: str, novelty_sources: list[str]) -> str:
    base = f"Keep the same mission geometry and UTC window. Derived from {current_round_id}."
    if novelty_sources:
        base = f"{base} Novel source hints: {', '.join(novelty_sources)}."
    if maybe_text(reason):
        return f"{base} Reason: {maybe_text(reason)}"
    return base


def build_next_round_tasks(
    *,
    run_dir: Path,
    mission: dict[str, Any],
    current_round_id: str,
    next_round_id: str,
    recommendations: list[dict[str, Any]],
    unresolved_claim_ids: list[str],
) -> list[dict[str, Any]]:
    run_id = mission_run_id(mission)
    counters: dict[str, int] = defaultdict(int)
    tasks: list[dict[str, Any]] = []
    seen_signatures: set[tuple[str, tuple[str, ...]]] = set()
    geometry = mission.get("region", {}).get("geometry") if isinstance(mission.get("region"), dict) else None
    window = mission.get("window")
    max_tasks = mission_constraints(mission).get("max_tasks_per_round", 4)
    source_history = completed_sources_history(run_dir, current_round_id)

    for recommendation in recommendations:
        role = maybe_text(recommendation.get("assigned_role"))
        if not role:
            continue
        objective = maybe_text(recommendation.get("objective"))
        reason = maybe_text(recommendation.get("reason"))
        preferred_sources = select_task_sources(
            mission=mission,
            recommendation=recommendation,
            role=role,
            objective=objective,
            reason=reason,
            completed_sources=source_history.get(role, set()),
        )
        if not preferred_sources:
            continue
        signature = (role, tuple(sorted(preferred_sources)))
        if signature in seen_signatures:
            continue
        seen_signatures.add(signature)
        counters[role] += 1
        task_id = f"task-{role}-{next_round_id}-{counters[role]:02d}"
        task = {
            "schema_version": SCHEMA_VERSION,
            "task_id": task_id,
            "run_id": run_id,
            "round_id": next_round_id,
            "assigned_role": role,
            "objective": objective,
            "status": "planned",
            "depends_on": [],
            "expected_output_kinds": expected_output_kinds_for_role(role),
            "inputs": {
                "mission_geometry": geometry,
                "mission_window": window,
                "focus_claim_ids": unresolved_claim_ids[:5],
                "preferred_sources": preferred_sources,
                "upstream_round_id": current_round_id,
            },
            "notes": build_task_notes(current_round_id, reason, preferred_sources),
        }
        validate_payload("round-task", task)
        tasks.append(task)
        if len(tasks) >= max_tasks:
            break
    return tasks


def observations_by_id_map(observations: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {maybe_text(item.get("observation_id")): item for item in observations}


def claims_by_id_map(claims: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {maybe_text(item.get("claim_id")): item for item in claims}


def evidence_by_claim_map(evidence_cards: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {maybe_text(item.get("claim_id")): item for item in evidence_cards}


def metrics_for_evidence(card: dict[str, Any], observations_by_id: dict[str, dict[str, Any]]) -> list[str]:
    metrics: list[str] = []
    observation_ids = card.get("observation_ids")
    if not isinstance(observation_ids, list):
        return metrics
    for observation_id in observation_ids:
        observation = observations_by_id.get(maybe_text(observation_id))
        if observation is not None:
            metrics.append(maybe_text(observation.get("metric")))
    return unique_strings(metrics)


def report_status_for_role(*, role: str, claims: list[dict[str, Any]], observations: list[dict[str, Any]], evidence_cards: list[dict[str, Any]]) -> str:
    if role == "sociologist":
        if not claims:
            return "blocked"
        if not evidence_cards or any(maybe_text(card.get("verdict")) in {"mixed", "insufficient"} for card in evidence_cards):
            return "needs-more-evidence"
        return "complete"
    if not observations and not evidence_cards:
        return "blocked"
    if not evidence_cards or any(maybe_text(card.get("verdict")) in {"mixed", "insufficient"} for card in evidence_cards):
        return "needs-more-evidence"
    return "complete"


def build_summary_for_role(*, role: str, claims: list[dict[str, Any]], observations: list[dict[str, Any]], evidence_cards: list[dict[str, Any]]) -> str:
    verdict_counts = counter_dict([maybe_text(item.get("verdict")) for item in evidence_cards])
    if role == "sociologist":
        if not claims:
            return "No normalized public claims were available for this round."
        return (
            f"The round produced {len(claims)} candidate public claims. "
            f"Evidence verdicts currently include {verdict_counts.get('supports', 0)} supports, "
            f"{verdict_counts.get('contradicts', 0)} contradicts, "
            f"{verdict_counts.get('mixed', 0)} mixed, and "
            f"{verdict_counts.get('insufficient', 0)} insufficient."
        )
    if not observations and not evidence_cards:
        return "No mission-aligned physical observations were available for this round."
    metric_counts = counter_dict([maybe_text(item.get("metric")) for item in observations])
    metric_text = ", ".join(sorted(metric_counts)) if metric_counts else "no linked metrics"
    return (
        f"The round produced {len(observations)} observations and {len(evidence_cards)} evidence cards. "
        f"Current physical coverage includes {metric_text}."
    )


def build_sociologist_findings(
    *,
    claims: list[dict[str, Any]],
    evidence_by_claim: dict[str, dict[str, Any]],
    observations_by_id: dict[str, dict[str, Any]],
    max_findings: int,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for index, claim in enumerate(sorted(claims, key=claim_sort_key)[:max_findings], start=1):
        claim_id = maybe_text(claim.get("claim_id"))
        card = evidence_by_claim.get(claim_id)
        title = truncate_text(maybe_text(claim.get("summary")) or maybe_text(claim.get("statement")), 72)
        if card is None:
            summary = f"Claim {claim_id} was captured from public signals but has not yet been linked to physical evidence."
            confidence = "low"
            observation_ids: list[str] = []
            evidence_ids: list[str] = []
        else:
            metrics = metrics_for_evidence(card, observations_by_id)
            metric_text = f" Linked metrics: {', '.join(metrics[:4])}." if metrics else ""
            summary = f"Claim {claim_id} is currently {maybe_text(card.get('verdict'))}. {maybe_text(card.get('summary'))}{metric_text}".strip()
            confidence = maybe_text(card.get("confidence")) or "low"
            observation_ids = [maybe_text(item) for item in card.get("observation_ids", []) if maybe_text(item)]
            evidence_ids = [maybe_text(card.get("evidence_id"))] if maybe_text(card.get("evidence_id")) else []
        findings.append(
            {
                "finding_id": f"finding-{index:03d}",
                "title": title or f"Claim {claim_id}",
                "summary": truncate_text(summary, 300),
                "confidence": confidence,
                "claim_ids": [claim_id],
                "observation_ids": observation_ids[:6],
                "evidence_ids": evidence_ids,
            }
        )
    return findings


def build_environmentalist_findings(
    *,
    claims: list[dict[str, Any]],
    observations: list[dict[str, Any]],
    evidence_cards: list[dict[str, Any]],
    observations_by_id: dict[str, dict[str, Any]],
    claims_by_id: dict[str, dict[str, Any]],
    max_findings: int,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    ordered_cards = sorted(
        evidence_cards,
        key=lambda item: (
            evidence_rank(item),
            claim_sort_key(claims_by_id.get(maybe_text(item.get("claim_id")), {})),
            maybe_text(item.get("evidence_id")),
        ),
    )
    for index, card in enumerate(ordered_cards[:max_findings], start=1):
        claim_id = maybe_text(card.get("claim_id"))
        claim = claims_by_id.get(claim_id, {})
        metrics = metrics_for_evidence(card, observations_by_id)
        metric_text = ", ".join(metrics[:4]) if metrics else "linked observations"
        findings.append(
            {
                "finding_id": f"finding-{index:03d}",
                "title": truncate_text(maybe_text(claim.get("summary")) or f"Physical evidence for {claim_id}", 72),
                "summary": truncate_text(f"{maybe_text(card.get('summary'))} Main metrics: {metric_text}.", 300),
                "confidence": maybe_text(card.get("confidence")) or "low",
                "claim_ids": [claim_id] if claim_id else [],
                "observation_ids": [maybe_text(item) for item in card.get("observation_ids", []) if maybe_text(item)][:8],
                "evidence_ids": [maybe_text(card.get("evidence_id"))] if maybe_text(card.get("evidence_id")) else [],
            }
        )

    if findings:
        return findings

    for index, observation in enumerate(observations[:max_findings], start=1):
        findings.append(
            {
                "finding_id": f"finding-{index:03d}",
                "title": truncate_text(f"{maybe_text(observation.get('metric'))} observation", 72),
                "summary": truncate_text(
                    (
                        f"Observation {maybe_text(observation.get('observation_id'))} reports "
                        f"{maybe_text(observation.get('metric'))}={observation.get('value')} "
                        f"{maybe_text(observation.get('unit'))} from {maybe_text(observation.get('source_skill'))}."
                    ),
                    300,
                ),
                "confidence": "medium",
                "claim_ids": [],
                "observation_ids": [maybe_text(observation.get("observation_id"))] if maybe_text(observation.get("observation_id")) else [],
                "evidence_ids": [],
            }
        )
    return findings


def build_open_questions(evidence_cards: list[dict[str, Any]]) -> list[str]:
    questions: list[str] = []
    for card in evidence_cards:
        items = card.get("gaps")
        if not isinstance(items, list):
            continue
        for item in items:
            questions.append(gap_to_question(maybe_text(item)))
    return unique_strings(questions)[:5]


def build_report_draft(
    *,
    mission: dict[str, Any],
    round_id: str,
    role: str,
    claims: list[dict[str, Any]],
    observations: list[dict[str, Any]],
    evidence_cards: list[dict[str, Any]],
    max_findings: int,
) -> dict[str, Any]:
    evidence_by_claim = evidence_by_claim_map(evidence_cards)
    observations_by_id = observations_by_id_map(observations)
    claims_by_id = claims_by_id_map(claims)
    if role == "sociologist":
        findings = build_sociologist_findings(
            claims=claims,
            evidence_by_claim=evidence_by_claim,
            observations_by_id=observations_by_id,
            max_findings=max_findings,
        )
    else:
        findings = build_environmentalist_findings(
            claims=claims,
            observations=observations,
            evidence_cards=evidence_cards,
            observations_by_id=observations_by_id,
            claims_by_id=claims_by_id,
            max_findings=max_findings,
        )
    missing_types = infer_missing_evidence_types(claims=claims, observations=observations, evidence_cards=evidence_cards)
    recommendations = combine_recommendations(reports=[], missing_types=missing_types)[:4]
    open_questions = build_open_questions(evidence_cards)
    status = report_status_for_role(role=role, claims=claims, observations=observations, evidence_cards=evidence_cards)
    if status == "blocked" and not open_questions:
        if role == "sociologist":
            open_questions = ["Should the next round expand public-signal collection before report writing?"]
        else:
            open_questions = ["Should the next round expand physical-source coverage before physical validation resumes?"]
    draft = {
        "schema_version": SCHEMA_VERSION,
        "report_id": f"report-{role}-{round_id}",
        "run_id": mission_run_id(mission),
        "round_id": round_id,
        "agent_role": role,
        "status": status,
        "summary": build_summary_for_role(role=role, claims=claims, observations=observations, evidence_cards=evidence_cards),
        "findings": findings,
        "open_questions": open_questions,
        "recommended_next_actions": recommendations,
    }
    validate_payload("expert-report", draft)
    return draft


def build_report_instructions(role: str) -> list[str]:
    instructions = [
        "Return one JSON object only, shaped like expert-report.",
        "Treat `context` as a compact summary layer first; only rely on `canonical_paths` when the summary is insufficient.",
        "Use only claim_ids, observation_ids, and evidence_ids already present in the packet context.",
        "Do not invent coordinates, timestamps, or raw-source facts outside the packet.",
        "If evidence remains partial or mixed, keep status as needs-more-evidence.",
        "Keep each finding traceable to specific canonical objects.",
        "If you include recommended_next_actions, each item must be an object with assigned_role, objective, and reason.",
    ]
    if role == "sociologist":
        instructions.append("Emphasize claim phrasing, public narrative concentration, and what still needs corroboration.")
    else:
        instructions.append("Emphasize metric interpretation, provenance limits, and what is or is not physically supported.")
    return instructions


def build_report_packet(
    *,
    run_dir: Path,
    round_id: str,
    role: str,
    mission: dict[str, Any],
    tasks: list[dict[str, Any]],
    context: dict[str, Any],
    draft_report: dict[str, Any],
) -> dict[str, Any]:
    relevant_tasks = [task for task in tasks if maybe_text(task.get("assigned_role")) == role]
    existing_report = load_json_if_exists(report_target_path(run_dir, round_id, role))
    if not isinstance(existing_report, dict):
        existing_report = None
    return {
        "packet_kind": "expert-report-packet",
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": utc_now_iso(),
        "run": {
            "run_id": mission_run_id(mission),
            "round_id": round_id,
            "topic": maybe_text(mission.get("topic")),
            "objective": maybe_text(mission.get("objective")),
            "role": role,
        },
        "role": role,
        "task_scope": relevant_tasks,
        "context": context,
        "instructions": build_report_instructions(role),
        "validation": {
            "kind": "expert-report",
            "target_report_path": str(report_target_path(run_dir, round_id, role)),
            "draft_report_path": str(report_draft_path(run_dir, round_id, role)),
            "validate_command": f"python3 {CONTRACT_SCRIPT_PATH} validate --kind expert-report --input {report_draft_path(run_dir, round_id, role)}",
        },
        "existing_report": existing_report,
        "draft_report": draft_report,
    }


def evidence_resolution_score(evidence_cards: list[dict[str, Any]]) -> float:
    if not evidence_cards:
        return 0.0
    total = 0.0
    for card in evidence_cards:
        total += VERDICT_SCORES.get(maybe_text(card.get("verdict")), 0.0)
    return total / len(evidence_cards)


def report_completion_score(reports: list[dict[str, Any]]) -> float:
    if not reports:
        return 0.0
    complete = 0
    for report in reports:
        if report_has_substance(report):
            complete += 1
    return complete / len(reports)


def completion_score_for_round(evidence_cards: list[dict[str, Any]], reports: list[dict[str, Any]]) -> float:
    score = 0.1 + 0.7 * evidence_resolution_score(evidence_cards) + 0.2 * report_completion_score(reports)
    score = max(0.0, min(1.0, score))
    return round(score, 2)


def evidence_sufficiency_for_round(evidence_cards: list[dict[str, Any]], missing_evidence_types: list[str]) -> str:
    if not evidence_cards:
        return "insufficient"
    verdicts = [maybe_text(item.get("verdict")) for item in evidence_cards]
    confidences = [maybe_text(item.get("confidence")) for item in evidence_cards]
    if any(verdict in {"mixed", "insufficient", ""} for verdict in verdicts):
        return "insufficient"
    if missing_evidence_types:
        return "partial"
    if confidences and all(confidence == "low" for confidence in confidences):
        return "partial"
    if len(evidence_cards) > 1 and any(confidence == "low" for confidence in confidences):
        return "partial"
    if set(verdicts) <= {"supports", "contradicts"}:
        return "sufficient"
    return "partial"


def unresolved_claim_entries(claims: list[dict[str, Any]], evidence_cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cards_by_claim = evidence_by_claim_map(evidence_cards)
    entries: list[dict[str, Any]] = []
    for claim in sorted(claims, key=claim_sort_key):
        claim_id = maybe_text(claim.get("claim_id"))
        card = cards_by_claim.get(claim_id)
        if card is None or maybe_text(card.get("verdict")) in {"mixed", "insufficient"}:
            entries.append(
                {
                    "claim_id": claim_id,
                    "claim_type": maybe_text(claim.get("claim_type")),
                    "summary": maybe_text(claim.get("summary")),
                    "verdict": maybe_text(card.get("verdict")) if card is not None else "unlinked",
                    "gaps": card.get("gaps", []) if isinstance(card, dict) else ["No linked evidence-card is available yet."],
                }
            )
    return entries


def build_decision_summary(
    *,
    moderator_status: str,
    sufficiency: str,
    evidence_cards: list[dict[str, Any]],
    missing_evidence_types: list[str],
    report_sources: dict[str, str],
    blocked_reason: str,
) -> str:
    total = len(evidence_cards)
    resolved = sum(1 for item in evidence_cards if maybe_text(item.get("verdict")) in {"supports", "contradicts"})
    if len(missing_evidence_types) <= 4:
        missing_text = ", ".join(missing_evidence_types)
    else:
        missing_text = ", ".join(missing_evidence_types[:3]) + f", and {len(missing_evidence_types) - 3} more"
    if moderator_status == "blocked" and blocked_reason:
        return blocked_reason
    if moderator_status == "complete":
        return f"The round resolved {resolved} of {total} evidence cards and now has {sufficiency} evidence for closure."
    if moderator_status == "continue":
        if not missing_text:
            missing_text = "additional targeted evidence"
        return (
            f"The round resolved {resolved} of {total} evidence cards, but another round is still needed for {missing_text}. "
            f"Report sources used: {', '.join(f'{role}:{source}' for role, source in sorted(report_sources.items()))}."
        )
    return "The round could not produce enough usable reporting or evidence artifacts for a confident moderator decision."


def build_final_brief(*, moderator_status: str, decision_summary: str, reports: dict[str, dict[str, Any] | None]) -> str:
    if moderator_status == "continue":
        return ""
    report_summaries = [maybe_text(report.get("summary")) for report in reports.values() if isinstance(report, dict)]
    return truncate_text(" ".join([decision_summary] + [item for item in report_summaries if item]), 600)


def build_decision_draft(
    *,
    run_dir: Path,
    mission: dict[str, Any],
    round_id: str,
    next_round_id: str,
    claims: list[dict[str, Any]],
    observations: list[dict[str, Any]],
    evidence_cards: list[dict[str, Any]],
    reports: dict[str, dict[str, Any] | None],
    report_sources: dict[str, str],
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    unresolved_entries = unresolved_claim_entries(claims, evidence_cards)
    cards_by_claim = evidence_by_claim_map(evidence_cards)
    follow_up_claim_ids: list[str] = []
    for claim in sorted(claims, key=claim_sort_key):
        claim_id = maybe_text(claim.get("claim_id"))
        card = cards_by_claim.get(claim_id)
        has_gaps = isinstance(card, dict) and isinstance(card.get("gaps"), list) and bool(card.get("gaps"))
        is_unresolved = card is None or maybe_text(card.get("verdict")) in {"mixed", "insufficient"}
        if claim_id and (has_gaps or is_unresolved):
            follow_up_claim_ids.append(claim_id)
    unresolved_claim_ids = unique_strings(follow_up_claim_ids)
    missing_types = infer_missing_evidence_types(claims=claims, observations=observations, evidence_cards=evidence_cards)
    usable_reports = [report for report in reports.values() if isinstance(report, dict)]
    recommendations = combine_recommendations(reports=usable_reports, missing_types=missing_types)
    next_round_tasks = build_next_round_tasks(
        run_dir=run_dir,
        mission=mission,
        current_round_id=round_id,
        next_round_id=next_round_id,
        recommendations=recommendations,
        unresolved_claim_ids=unresolved_claim_ids,
    )

    max_rounds = mission_constraints(mission).get("max_rounds")
    current_number = current_round_number(round_id)
    next_number = current_round_number(next_round_id)
    blocked_reason = ""

    if not claims and not observations and not evidence_cards:
        moderator_status = "blocked"
        next_round_required = False
        blocked_reason = "The round did not produce enough canonical claims, observations, or evidence cards to continue."
    elif max_rounds is not None and current_number is not None and next_number is not None and next_number > max_rounds and unresolved_entries:
        moderator_status = "blocked"
        next_round_required = False
        blocked_reason = f"The round still has unresolved evidence, but the configured max_rounds={max_rounds} would be exceeded by {next_round_id}."
        next_round_tasks = []
    elif unresolved_entries or missing_types:
        if next_round_tasks:
            moderator_status = "continue"
            next_round_required = True
        else:
            moderator_status = "blocked"
            next_round_required = False
            blocked_reason = "The remaining gaps would require repeating already completed sources without adding a new evidence angle."
    else:
        moderator_status = "complete"
        next_round_required = False
        next_round_tasks = []

    sufficiency = evidence_sufficiency_for_round(evidence_cards, missing_types)
    decision_summary = build_decision_summary(
        moderator_status=moderator_status,
        sufficiency=sufficiency,
        evidence_cards=evidence_cards,
        missing_evidence_types=missing_types,
        report_sources=report_sources,
        blocked_reason=blocked_reason,
    )
    final_brief = build_final_brief(moderator_status=moderator_status, decision_summary=decision_summary, reports=reports)

    draft = {
        "schema_version": SCHEMA_VERSION,
        "decision_id": f"decision-{round_id}",
        "run_id": mission_run_id(mission),
        "round_id": round_id,
        "moderator_status": moderator_status,
        "completion_score": completion_score_for_round(evidence_cards, usable_reports),
        "evidence_sufficiency": sufficiency,
        "decision_summary": decision_summary,
        "next_round_required": next_round_required,
        "missing_evidence_types": missing_types,
        "next_round_tasks": next_round_tasks,
        "final_brief": final_brief,
    }
    validate_payload("council-decision", draft)
    return draft, next_round_tasks, missing_types


def build_decision_packet(
    *,
    run_dir: Path,
    round_id: str,
    next_round_id: str,
    mission: dict[str, Any],
    moderator_context: dict[str, Any],
    claims: list[dict[str, Any]],
    evidence_cards: list[dict[str, Any]],
    reports: dict[str, dict[str, Any] | None],
    report_sources: dict[str, str],
    draft_decision: dict[str, Any],
    proposed_next_round_tasks: list[dict[str, Any]],
    missing_evidence_types: list[str],
) -> dict[str, Any]:
    return {
        "packet_kind": "council-decision-packet",
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": utc_now_iso(),
        "run": {
            "run_id": mission_run_id(mission),
            "round_id": round_id,
            "next_round_id": next_round_id,
            "topic": maybe_text(mission.get("topic")),
            "objective": maybe_text(mission.get("objective")),
        },
        "round_context": moderator_context,
        "reports": reports,
        "report_sources": report_sources,
        "unresolved_claims": unresolved_claim_entries(claims, evidence_cards),
        "missing_evidence_types": missing_evidence_types,
        "proposed_next_round_tasks": proposed_next_round_tasks,
        "instructions": [
            "Return one JSON object only, shaped like council-decision.",
            "Base the decision on evidence_cards and expert-report content, not on raw fetch artifacts.",
            "Treat `round_context` as a compact summary layer first and consult `canonical_paths` only if a summary detail is insufficient.",
            "If another round is required, add new round-task objects for next_round_id instead of editing current tasks in place.",
            "Respect mission constraints such as max_rounds and max_tasks_per_round.",
            "Each next_round task must introduce at least one not-yet-used source skill or materially different collection angle; previously useful sources may remain as supporting context, but do not repeat an identical role/source bundle for the same geometry and window.",
            "Keep final_brief empty unless the council is complete or blocked.",
        ],
        "validation": {
            "kind": "council-decision",
            "target_decision_path": str(decision_target_path(run_dir, round_id)),
            "draft_decision_path": str(decision_draft_path(run_dir, round_id)),
            "validate_command": f"python3 {CONTRACT_SCRIPT_PATH} validate --kind council-decision --input {decision_draft_path(run_dir, round_id)}",
        },
        "draft_decision": draft_decision,
    }


def load_required_object(path: Path, label: str) -> dict[str, Any]:
    payload = load_json_if_exists(path)
    if not isinstance(payload, dict):
        raise ValueError(f"{label} is missing or not a JSON object: {path}")
    return payload


def report_prompt_text(*, role: str, packet_path: Path, packet: dict[str, Any]) -> str:
    run = packet.get("run", {}) if isinstance(packet.get("run"), dict) else {}
    validation = packet.get("validation", {}) if isinstance(packet.get("validation"), dict) else {}
    lines = [
        "Use $eco-council-reporting.",
        f"You are the {role} for eco-council run {maybe_text(run.get('run_id'))} round {maybe_text(run.get('round_id'))}.",
        "",
        "Open and read this packet JSON first:",
        str(packet_path),
        "",
        "Then follow these rules:",
        "1. Treat packet `instructions` as binding.",
        "2. Review `task_scope` and `context` before editing.",
        "3. Start from `draft_report` inside the packet.",
        "4. Return only one JSON object shaped like expert-report.",
        "5. Keep `schema_version`, `run_id`, `round_id`, and `agent_role` consistent with the packet.",
        "6. `recommended_next_actions` must be a list of objects with `assigned_role`, `objective`, and `reason`; do not emit strings there.",
        "7. Do not return markdown, prose, code fences, or extra commentary.",
        "",
        "If you persist the result locally, write it to:",
        maybe_text(validation.get("draft_report_path")),
        "",
        "Validation command:",
        maybe_text(validation.get("validate_command")),
        "",
        "Return only JSON.",
        "",
    ]
    return "\n".join(lines)


def decision_prompt_text(*, packet_path: Path, packet: dict[str, Any]) -> str:
    run = packet.get("run", {}) if isinstance(packet.get("run"), dict) else {}
    validation = packet.get("validation", {}) if isinstance(packet.get("validation"), dict) else {}
    lines = [
        "Use $eco-council-reporting.",
        f"You are the moderator for eco-council run {maybe_text(run.get('run_id'))} round {maybe_text(run.get('round_id'))}.",
        "",
        "Open and read this packet JSON first:",
        str(packet_path),
        "",
        "Then follow these rules:",
        "1. Treat packet `instructions` as binding.",
        "2. Review `round_context`, `reports`, `unresolved_claims`, and `proposed_next_round_tasks` before editing.",
        "3. Start from `draft_decision` inside the packet.",
        "4. If another round is needed, make sure each task adds at least one new source skill or materially different collection angle; previously useful sources may still appear as supporting context.",
        "5. Return only one JSON object shaped like council-decision.",
        "6. Keep `schema_version`, `run_id`, and `round_id` consistent with the packet.",
        "7. Do not return markdown, prose, code fences, or extra commentary.",
        "",
        "If you persist the result locally, write it to:",
        maybe_text(validation.get("draft_decision_path")),
        "",
        "Validation command:",
        maybe_text(validation.get("validate_command")),
        "",
        "Return only JSON.",
        "",
    ]
    return "\n".join(lines)


def can_replace_existing_report(existing_payload: dict[str, Any] | None, new_payload: dict[str, Any]) -> bool:
    if existing_payload is None:
        return True
    if existing_payload == new_payload:
        return True
    return report_is_placeholder(existing_payload)


def can_replace_existing_decision(existing_payload: dict[str, Any] | None, new_payload: dict[str, Any]) -> bool:
    if existing_payload is None:
        return True
    return existing_payload == new_payload


def load_report_draft_payload(run_dir: Path, round_id: str, role: str, draft_path_text: str) -> tuple[Path, dict[str, Any]]:
    draft_path = Path(draft_path_text).expanduser().resolve() if draft_path_text else report_draft_path(run_dir, round_id, role)
    payload = load_required_object(draft_path, f"{role} report draft")
    if maybe_text(payload.get("agent_role")) != role:
        raise ValueError(f"Report draft role mismatch: expected {role}, got {payload.get('agent_role')!r}")
    if maybe_text(payload.get("round_id")) != round_id:
        raise ValueError(f"Report draft round mismatch: expected {round_id}, got {payload.get('round_id')!r}")
    validate_payload("expert-report", payload)
    return draft_path, payload


def load_decision_draft_payload(run_dir: Path, round_id: str, draft_path_text: str) -> tuple[Path, dict[str, Any]]:
    draft_path = Path(draft_path_text).expanduser().resolve() if draft_path_text else decision_draft_path(run_dir, round_id)
    payload = load_required_object(draft_path, "moderator decision draft")
    if maybe_text(payload.get("round_id")) != round_id:
        raise ValueError(f"Decision draft round mismatch: expected {round_id}, got {payload.get('round_id')!r}")
    validate_payload("council-decision", payload)
    return draft_path, payload


def promote_report_draft(
    *,
    run_dir: Path,
    round_id: str,
    role: str,
    draft_path_text: str,
    pretty: bool,
    allow_overwrite: bool,
) -> dict[str, Any]:
    draft_path, payload = load_report_draft_payload(run_dir, round_id, role, draft_path_text)
    target_path = report_target_path(run_dir, round_id, role)
    existing_payload = load_json_if_exists(target_path)
    if existing_payload is not None and not isinstance(existing_payload, dict):
        raise ValueError(f"Existing canonical report is not a JSON object: {target_path}")
    if not allow_overwrite and not can_replace_existing_report(existing_payload, payload):
        raise ValueError(f"Refusing to overwrite non-placeholder canonical report without --allow-overwrite: {target_path}")
    write_json(target_path, payload, pretty=pretty)
    return {
        "role": role,
        "draft_path": str(draft_path),
        "target_path": str(target_path),
        "overwrote_existing": existing_payload is not None and existing_payload != payload,
    }


def promote_decision_draft(
    *,
    run_dir: Path,
    round_id: str,
    draft_path_text: str,
    pretty: bool,
    allow_overwrite: bool,
) -> dict[str, Any]:
    draft_path, payload = load_decision_draft_payload(run_dir, round_id, draft_path_text)
    target_path = decision_target_path(run_dir, round_id)
    existing_payload = load_json_if_exists(target_path)
    if existing_payload is not None and not isinstance(existing_payload, dict):
        raise ValueError(f"Existing canonical decision is not a JSON object: {target_path}")
    if not allow_overwrite and not can_replace_existing_decision(existing_payload, payload):
        raise ValueError(f"Refusing to overwrite canonical decision without --allow-overwrite: {target_path}")
    write_json(target_path, payload, pretty=pretty)
    return {
        "draft_path": str(draft_path),
        "target_path": str(target_path),
        "overwrote_existing": existing_payload is not None and existing_payload != payload,
    }


def render_openclaw_prompts(
    *,
    run_dir: Path,
    round_id: str,
) -> dict[str, Any]:
    outputs: dict[str, str] = {}
    for role in REPORT_ROLES:
        packet_path = report_packet_path(run_dir, round_id, role)
        packet = load_required_object(packet_path, f"{role} report packet")
        prompt_path = report_prompt_path(run_dir, round_id, role)
        write_text(prompt_path, report_prompt_text(role=role, packet_path=packet_path, packet=packet))
        outputs[role] = str(prompt_path)

    packet_path = decision_packet_path(run_dir, round_id)
    packet = load_required_object(packet_path, "moderator decision packet")
    moderator_prompt_path = decision_prompt_path(run_dir, round_id)
    write_text(moderator_prompt_path, decision_prompt_text(packet_path=packet_path, packet=packet))
    outputs["moderator"] = str(moderator_prompt_path)
    return outputs


def collect_round_state(run_dir: Path, round_id: str) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    mission = load_mission(run_dir)
    tasks = load_canonical_list(tasks_path(run_dir, round_id))
    claims = load_canonical_list(shared_claims_path(run_dir, round_id))
    observations = effective_shared_observations(run_dir, round_id)
    evidence_cards = load_canonical_list(shared_evidence_path(run_dir, round_id))
    return mission, tasks, claims, observations, evidence_cards


def report_artifacts(*, run_dir: Path, round_id: str, pretty: bool) -> dict[str, Any]:
    mission, tasks, claims, observations, evidence_cards = collect_round_state(run_dir, round_id)
    max_findings = mission_constraints(mission).get("max_claims_per_round", 4)
    outputs: dict[str, dict[str, str]] = {}
    for role in REPORT_ROLES:
        context = load_context_or_fallback(
            run_dir=run_dir,
            round_id=round_id,
            role=role,
            mission=mission,
            tasks=tasks,
            claims=claims,
            observations=observations,
            evidence_cards=evidence_cards,
        )
        draft_report = build_report_draft(
            mission=mission,
            round_id=round_id,
            role=role,
            claims=claims,
            observations=observations,
            evidence_cards=evidence_cards,
            max_findings=max_findings,
        )
        packet = build_report_packet(
            run_dir=run_dir,
            round_id=round_id,
            role=role,
            mission=mission,
            tasks=tasks,
            context=context,
            draft_report=draft_report,
        )
        packet_path = report_packet_path(run_dir, round_id, role)
        draft_path = report_draft_path(run_dir, round_id, role)
        write_json(packet_path, packet, pretty=pretty)
        write_json(draft_path, draft_report, pretty=pretty)
        outputs[role] = {"report_packet_path": str(packet_path), "report_draft_path": str(draft_path)}
    return {
        "run_id": mission_run_id(mission),
        "round_id": round_id,
        "claim_count": len(claims),
        "observation_count": len(observations),
        "evidence_count": len(evidence_cards),
        "outputs": outputs,
    }


def decision_artifacts(
    *,
    run_dir: Path,
    round_id: str,
    next_round_id: str,
    pretty: bool,
    prefer_draft_reports: bool,
) -> dict[str, Any]:
    mission, tasks, claims, observations, evidence_cards = collect_round_state(run_dir, round_id)
    reports: dict[str, dict[str, Any] | None] = {}
    report_sources: dict[str, str] = {}
    for role in REPORT_ROLES:
        report, source = load_report_for_decision(run_dir, round_id, role, prefer_drafts=prefer_draft_reports)
        reports[role] = report
        report_sources[role] = source
    moderator_context = load_context_or_fallback(
        run_dir=run_dir,
        round_id=round_id,
        role="moderator",
        mission=mission,
        tasks=tasks,
        claims=claims,
        observations=observations,
        evidence_cards=evidence_cards,
    )
    draft_decision, next_round_tasks, missing_types = build_decision_draft(
        run_dir=run_dir,
        mission=mission,
        round_id=round_id,
        next_round_id=next_round_id,
        claims=claims,
        observations=observations,
        evidence_cards=evidence_cards,
        reports=reports,
        report_sources=report_sources,
    )
    packet = build_decision_packet(
        run_dir=run_dir,
        round_id=round_id,
        next_round_id=next_round_id,
        mission=mission,
        moderator_context=moderator_context,
        claims=claims,
        evidence_cards=evidence_cards,
        reports=reports,
        report_sources=report_sources,
        draft_decision=draft_decision,
        proposed_next_round_tasks=next_round_tasks,
        missing_evidence_types=missing_types,
    )
    packet_path = decision_packet_path(run_dir, round_id)
    draft_path = decision_draft_path(run_dir, round_id)
    write_json(packet_path, packet, pretty=pretty)
    write_json(draft_path, draft_decision, pretty=pretty)
    return {
        "run_id": mission_run_id(mission),
        "round_id": round_id,
        "next_round_id": next_round_id,
        "decision_packet_path": str(packet_path),
        "decision_draft_path": str(draft_path),
        "report_sources": report_sources,
        "missing_evidence_types": missing_types,
        "next_round_task_count": len(next_round_tasks),
    }


def command_build_report_packets(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = Path(args.run_dir).expanduser().resolve()
    return report_artifacts(run_dir=run_dir, round_id=args.round_id, pretty=args.pretty)


def command_build_decision_packet(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = Path(args.run_dir).expanduser().resolve()
    next_round_id = args.next_round_id or next_round_id_for(args.round_id)
    return decision_artifacts(
        run_dir=run_dir,
        round_id=args.round_id,
        next_round_id=next_round_id,
        pretty=args.pretty,
        prefer_draft_reports=args.prefer_draft_reports,
    )


def command_build_all(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = Path(args.run_dir).expanduser().resolve()
    next_round_id = args.next_round_id or next_round_id_for(args.round_id)
    reports_payload = report_artifacts(run_dir=run_dir, round_id=args.round_id, pretty=args.pretty)
    decision_payload = decision_artifacts(
        run_dir=run_dir,
        round_id=args.round_id,
        next_round_id=next_round_id,
        pretty=args.pretty,
        prefer_draft_reports=args.prefer_draft_reports,
    )
    return {"reports": reports_payload, "decision": decision_payload}


def command_render_openclaw_prompts(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = Path(args.run_dir).expanduser().resolve()
    outputs = render_openclaw_prompts(run_dir=run_dir, round_id=args.round_id)
    return {
        "run_dir": str(run_dir),
        "round_id": args.round_id,
        "outputs": outputs,
    }


def command_promote_report_draft(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = Path(args.run_dir).expanduser().resolve()
    return promote_report_draft(
        run_dir=run_dir,
        round_id=args.round_id,
        role=args.role,
        draft_path_text=args.draft_path,
        pretty=args.pretty,
        allow_overwrite=args.allow_overwrite,
    )


def command_promote_decision_draft(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = Path(args.run_dir).expanduser().resolve()
    return promote_decision_draft(
        run_dir=run_dir,
        round_id=args.round_id,
        draft_path_text=args.draft_path,
        pretty=args.pretty,
        allow_overwrite=args.allow_overwrite,
    )


def command_promote_all(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = Path(args.run_dir).expanduser().resolve()
    report_results = []
    for role in REPORT_ROLES:
        report_results.append(
            promote_report_draft(
                run_dir=run_dir,
                round_id=args.round_id,
                role=role,
                draft_path_text="",
                pretty=args.pretty,
                allow_overwrite=args.allow_overwrite,
            )
        )
    decision_result = promote_decision_draft(
        run_dir=run_dir,
        round_id=args.round_id,
        draft_path_text="",
        pretty=args.pretty,
        allow_overwrite=args.allow_overwrite,
    )
    bundle_result = validate_bundle(run_dir)
    return {
        "run_dir": str(run_dir),
        "round_id": args.round_id,
        "report_results": report_results,
        "decision_result": decision_result,
        "bundle_validation": bundle_result,
    }


def add_pretty_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build eco-council report packets and decision drafts.")
    sub = parser.add_subparsers(dest="command", required=True)

    report_packets = sub.add_parser("build-report-packets", help="Build expert report packets and draft expert reports.")
    report_packets.add_argument("--run-dir", required=True, help="Eco-council run directory.")
    report_packets.add_argument("--round-id", required=True, help="Round identifier.")
    add_pretty_flag(report_packets)

    decision_packet = sub.add_parser("build-decision-packet", help="Build moderator decision packet and decision draft.")
    decision_packet.add_argument("--run-dir", required=True, help="Eco-council run directory.")
    decision_packet.add_argument("--round-id", required=True, help="Round identifier.")
    decision_packet.add_argument("--next-round-id", default="", help="Optional explicit next round identifier.")
    decision_packet.add_argument("--prefer-draft-reports", action="store_true", help="Prefer derived report drafts over canonical expert reports whenever drafts are present.")
    add_pretty_flag(decision_packet)

    build_all = sub.add_parser("build-all", help="Build expert report packets and moderator decision packet together.")
    build_all.add_argument("--run-dir", required=True, help="Eco-council run directory.")
    build_all.add_argument("--round-id", required=True, help="Round identifier.")
    build_all.add_argument("--next-round-id", default="", help="Optional explicit next round identifier.")
    build_all.add_argument("--prefer-draft-reports", action="store_true", help="Prefer derived report drafts over canonical expert reports whenever drafts are present.")
    add_pretty_flag(build_all)

    render_prompts = sub.add_parser("render-openclaw-prompts", help="Render OpenClaw text prompts from existing report and decision packets.")
    render_prompts.add_argument("--run-dir", required=True, help="Eco-council run directory.")
    render_prompts.add_argument("--round-id", required=True, help="Round identifier.")
    add_pretty_flag(render_prompts)

    promote_report = sub.add_parser("promote-report-draft", help="Promote one draft expert-report into the canonical report path.")
    promote_report.add_argument("--run-dir", required=True, help="Eco-council run directory.")
    promote_report.add_argument("--round-id", required=True, help="Round identifier.")
    promote_report.add_argument("--role", required=True, choices=PROMOTABLE_REPORT_ROLES, help="Expert role.")
    promote_report.add_argument("--draft-path", default="", help="Optional explicit draft JSON path.")
    promote_report.add_argument("--allow-overwrite", action="store_true", help="Allow overwrite of an existing non-placeholder canonical report.")
    add_pretty_flag(promote_report)

    promote_decision = sub.add_parser("promote-decision-draft", help="Promote one draft council-decision into the canonical moderator path.")
    promote_decision.add_argument("--run-dir", required=True, help="Eco-council run directory.")
    promote_decision.add_argument("--round-id", required=True, help="Round identifier.")
    promote_decision.add_argument("--draft-path", default="", help="Optional explicit draft JSON path.")
    promote_decision.add_argument("--allow-overwrite", action="store_true", help="Allow overwrite of an existing canonical decision.")
    add_pretty_flag(promote_decision)

    promote_all = sub.add_parser("promote-all", help="Promote derived expert-report drafts plus the moderator decision draft into canonical paths.")
    promote_all.add_argument("--run-dir", required=True, help="Eco-council run directory.")
    promote_all.add_argument("--round-id", required=True, help="Round identifier.")
    promote_all.add_argument("--allow-overwrite", action="store_true", help="Allow overwrite of existing canonical outputs.")
    add_pretty_flag(promote_all)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handlers = {
        "build-report-packets": command_build_report_packets,
        "build-decision-packet": command_build_decision_packet,
        "build-all": command_build_all,
        "render-openclaw-prompts": command_render_openclaw_prompts,
        "promote-report-draft": command_promote_report_draft,
        "promote-decision-draft": command_promote_decision_draft,
        "promote-all": command_promote_all,
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
