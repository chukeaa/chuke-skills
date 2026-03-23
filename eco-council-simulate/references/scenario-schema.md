# Scenario Schema

## Purpose

A scenario file is a small deterministic control object for simulated raw-data generation.

It should be cheap to author, diff, and replay.

## Top-Level Fields

```json
{
  "scenario_kind": "eco-council-simulation-scenario",
  "schema_version": "1.0.0",
  "scenario_id": "flood-support",
  "description": "High-confidence public flood discussion plus hydrology support.",
  "claim_type": "flood",
  "mode": "support",
  "seed": 101,
  "public_topic": "river flooding and inundation",
  "place_label": "Brisbane River, Queensland, Australia",
  "source_modes": {
    "open-meteo-flood-fetch": "support"
  },
  "source_overrides": {
    "gdelt-doc-search": {
      "record_count": 4
    },
    "open-meteo-flood-fetch": {
      "metric_values": {
        "river_discharge": [140, 165, 210]
      }
    }
  },
  "fault_profile": {
    "empty_sources": [],
    "degrade_sources": [],
    "time_shift_hours": 0,
    "coordinate_offset_degrees": 0.0
  }
}
```

## Required Fields

- `scenario_kind`
- `schema_version`
- `scenario_id`
- `claim_type`
- `mode`

## Mode Semantics

- `support`
  - environment signals should generally cross support thresholds
- `contradict`
  - environment signals should generally fall on the contradictory side
- `mixed`
  - generate a mix of support and contradiction cues
- `sparse`
  - generate valid but thin evidence, useful for insufficiency tests

Public-text sources still aim to yield claim-like material. The mode mainly affects signal density, engagement, and physical corroboration.

## Supported Faults

- `empty_sources`
  - write a valid empty artifact for named sources
- `degrade_sources`
  - reduce density or completeness for named sources
- `time_shift_hours`
  - shift generated timestamps forward or backward
- `coordinate_offset_degrees`
  - shift generated coordinates for geometry-mismatch tests

## Supported Overrides

Per-source overrides currently support:

- `record_count`
- `metric_values`
- `text_snippets`
- `fire_count`
- `mode`

For raw GDELT table sources, `record_count` controls the number of simulated table rows distributed across one or more ZIP files.

Keep overrides minimal. Prefer presets plus a few focused overrides over huge hand-authored fake payloads.
