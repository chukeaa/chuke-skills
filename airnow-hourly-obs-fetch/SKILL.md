---
name: airnow-hourly-obs-fetch
description: Fetch AirNow Hourly AQ Obs file products from official AirNow file endpoints and return structured site-hour-parameter records filtered by UTC time window, bounding box, and pollutant parameter list. Use when tasks need deterministic AirNow monitoring-site observations at hourly cadence, especially for broad geography/time extraction where AirNow file products are preferred over per-request web-service loops.
---

# AirNow Hourly Obs Fetch

## Core Goal
- Fetch AirNow Hourly AQ Obs data files for one UTC window.
- Filter rows by mission bounding box and requested pollutant parameters.
- Return one normalized JSON record per site-hour-parameter for downstream processing.
- Keep execution deterministic with retries, throttling, and local safety caps.

## Repository Policy
- This is the canonical AirNow station-observation skill in this repository.
- When eco-council or OpenClaw assigns a raw artifact path, write this skill's full JSON payload to that exact path with `--output`.
- Do not treat dry-run output as collected evidence.

## Required Environment
- Configure runtime by environment variables (see `references/env.md`).
- Start from `assets/config.example.env`.
- Load env values before running commands:

```bash
set -a
source assets/config.example.env
set +a
```

## Workflow
1. Validate effective configuration.

```bash
python3 scripts/airnow_hourly_obs_fetch.py check-config --pretty
```

2. Dry-run to inspect target hour files and filters.

```bash
python3 scripts/airnow_hourly_obs_fetch.py fetch \
  --bbox=-123.5,37.0,-121.5,38.8 \
  --start-datetime 2026-03-22T00:00:00Z \
  --end-datetime 2026-03-22T02:00:00Z \
  --parameter PM25 \
  --parameter OZONE \
  --dry-run \
  --pretty
```

3. Run the fetch and write output payload.

```bash
python3 scripts/airnow_hourly_obs_fetch.py fetch \
  --bbox=-123.5,37.0,-121.5,38.8 \
  --start-datetime 2026-03-22T00:00:00Z \
  --end-datetime 2026-03-22T02:00:00Z \
  --parameter PM25 \
  --parameter OZONE \
  --parameter NO2 \
  --output ./data/airnow-hourly-obs.json \
  --pretty
```

## Output Record Shape
Each output item in `records` is one site-hour-parameter observation with fields:
- `aqsid`, `site_name`, `status`, `epa_region`
- `latitude`, `longitude`, `country_code`, `state_name`
- `observed_at_utc`, `data_source`, `reporting_areas`
- `parameter_name`, `aqi_value`, `aqi_kind`
- `raw_concentration`, `unit`, `measured`
- `source_file_url`

The full raw payload also keeps request metadata, transport stats, validation output, and file-level failure details for downstream auditing.

## Scope Boundaries
- This skill consumes AirNow hourly file products only.
- This skill does not geocode place names.
- This skill does not do AQI health interpretation or policy judgment.
- This skill treats file data as preliminary and for analysis support, not regulatory decisions.
- This skill is the canonical AirNow fetch interface for this repository.

## References
- `references/env.md`
- `references/airnow-hourly-obs-api-notes.md`
- `references/airnow-hourly-obs-limitations.md`
- `references/openclaw-chaining-templates.md`

## Script
- `scripts/airnow_hourly_obs_fetch.py`
