---
name: eco-council-orchestrate
description: Orchestrate eco-council multi-round runs around moderator task review, audited expert source selection, expert raw-data collection handoffs, deterministic normalization/reporting, and next-round scaffolding. Use when an OpenClaw-based eco-council needs one control-plane skill to bootstrap a run from mission JSON, prepare fetch plans only from explicitly selected sources, run the shared data plane after raw artifacts land, or advance from one moderator decision to the next round safely.
---

# Eco Council Orchestrate

## Core Goal

- Keep OpenClaw agents in the control plane:
  - moderator reviews or revises `tasks.json`
  - sociologist and environmentalist first write audited `source_selection.json`
  - sociologist and environmentalist then fetch raw artifacts
  - deterministic scripts normalize, link, aggregate, and seed report drafts
- Bridge these phases with stable files:
  - round task review prompt
  - role-specific fetch prompts
  - fetch plan JSON
  - reporting handoff JSON

## Workflow

1. Bootstrap one run from an authored mission file.

```bash
python3 scripts/eco_council_orchestrate.py bootstrap-run \
  --run-dir ./runs/20260321-chiangmai-smoke \
  --mission-input ./configs/chiangmai-mission.json \
  --pretty
```

2. Let the moderator review `round_001/moderator/tasks.json` through the generated prompt file:
- `round_001/moderator/derived/openclaw_task_review_prompt.txt`

3. Let each expert write one canonical source-selection object before any fetch stage:
- `round_001/sociologist/source_selection.json`
- `round_001/environmentalist/source_selection.json`
- `task.inputs.preferred_sources` are hints only
- `task.inputs.required_sources` are the only task-level force override

4. Prepare one round after source selection. This writes:
- `round_001/moderator/derived/fetch_plan.json`
- `round_001/sociologist/derived/openclaw_fetch_prompt.txt`
- `round_001/environmentalist/derived/openclaw_fetch_prompt.txt`

```bash
python3 scripts/eco_council_orchestrate.py prepare-round \
  --run-dir ./runs/20260321-chiangmai-smoke \
  --round-id round-001 \
  --pretty
```

5. Let the expert agents fetch raw artifacts into the exact `raw/` paths named by the prompt files.
  - Public fetches can be zero-step, or can include `gdelt-doc-search`, `bluesky-cascade-fetch`, `youtube-*`, `federal-register-doc-fetch`, and `regulationsgov-*`, depending on audited source selection and mission source policy.
  - Environment fetches can be zero-step, or can include `airnow-hourly-obs-fetch`, `usgs-water-iv-fetch`, `open-meteo-*`, `nasa-firms-fire-fetch`, and `openaq-data-fetch`, depending on audited source selection and mission source policy.

6. Run the deterministic data plane after raw artifacts exist.

```bash
python3 scripts/eco_council_orchestrate.py run-data-plane \
  --run-dir ./runs/20260321-chiangmai-smoke \
  --round-id round-001 \
  --pretty
```

7. Let OpenClaw experts revise the generated report drafts and let the moderator revise the decision draft through the prompt files produced by `$eco-council-reporting`.

8. Promote approved drafts, then scaffold the next round if the moderator decision says `next_round_required=true`.

```bash
python3 ../eco-council-reporting/scripts/eco_council_reporting.py promote-all \
  --run-dir ./runs/20260321-chiangmai-smoke \
  --round-id round-001 \
  --pretty

python3 scripts/eco_council_orchestrate.py advance-round \
  --run-dir ./runs/20260321-chiangmai-smoke \
  --round-id round-001 \
  --pretty
```

## Scope Decisions

- Use this skill for run lifecycle and handoff generation.
- Use `$eco-council-data-contract` for schema validation and round scaffolding.
- Use `$eco-council-normalize` for deterministic cleaning and linking.
- Use `$eco-council-reporting` for report packets, draft objects, and moderator decision seeding.
- Do not let this skill replace expert judgment inside OpenClaw.
- Do not let this skill auto-run every allowed source. `prepare-round` should emit zero fetch steps when experts selected none.
- Do not let expert agents exchange raw payloads directly; normalize first.

## Special Capability

- `prepare-round` can emit one-step AirNow hourly file-product fetches when the environmentalist selected `airnow-hourly-obs-fetch` and the mission source policy allows it.
- `collect-openaq` wraps the multi-step OpenAQ chain:
  - nearby location discovery
  - sensor discovery
  - measurement fetch
  - aggregation into one normalizer-ready raw artifact

Use it directly when `openaq-data-fetch` needs a station-measurement artifact without pushing OpenAQ API chaining into the expert prompt.

- `prepare-round` can also emit direct `federal-register-doc-fetch` steps for official U.S. rulemaking, notice, and policy-document discovery.
  - Task inputs may override the default plain-text term with `federal_register_term`.
  - Task inputs may also constrain agency, document type, topic, section, docket ID, RIN, significance, page size, and output field set.

- `prepare-round` can also emit direct `airnow-hourly-obs-fetch` steps for mission or task geometry in the United States.
  - Geometry is converted into one fetch bbox automatically.
  - Default pollutant parameters are `PM25`, `PM10`, `OZONE`, and `NO2`.
  - Task inputs may override this with `airnow_parameter_names` and `airnow_point_padding_deg`.

- `prepare-round` can also emit direct `usgs-water-iv-fetch` steps for mission or task geometry in the United States.
  - Geometry is converted into one fetch bbox automatically.
  - Default parameter codes are `00060` and `00065`.
  - Default site filters are `siteType=ST` and `siteStatus=active`.
  - Task inputs may override this with `usgs_parameter_codes`, `usgs_point_padding_deg`, `usgs_site_type`, and `usgs_site_status`.

## References

- `references/orchestration-flow.md`
- `references/fetch-plan-format.md`
- `references/openaq-collection.md`
