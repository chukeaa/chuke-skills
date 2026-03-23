---
name: eco-council-simulate
description: Materialize deterministic simulated raw artifacts and fetch execution records for one prepared eco-council round without calling live APIs or using an LLM. Use when you want low-cost experiment runs, repeatable replay cases, source-selection-sensitive smoke tests, or fault-injection scenarios after `prepare-round` has already produced `fetch_plan.json`.
---

# Eco Council Simulate

## Core Goal

- Keep simulation outside the eco-council control plane.
- Keep this skill operator-facing and test-harness-facing, not a default production agent skill.
- Read one already prepared `fetch_plan.json`.
- Write only the `raw/*` artifacts and the canonical `fetch_execution.json` that downstream normalization already expects.
- For manifest-backed sources such as raw GDELT tables, also write the sidecar ZIP files referenced by the manifest.
- Stay deterministic and low-cost:
  - no live API calls
  - no LLM generation
  - no direct writes to `claims.json`, `observations.json`, `evidence_cards.json`, or reports

## Workflow

1. Prepare the round normally through `$eco-council-orchestrate` or `$eco-council-supervisor`.

2. Inspect built-in presets when you want a quick starting point.

```bash
python3 scripts/eco_council_simulate.py list-presets --pretty
```

3. Optionally copy one preset and edit it.

```bash
python3 scripts/eco_council_simulate.py write-preset \
  --preset flood-support \
  --output /tmp/flood-support.json \
  --pretty
```

4. Materialize one round of simulated raw artifacts from the prepared fetch plan.

```bash
python3 scripts/eco_council_simulate.py simulate-round \
  --run-dir ./runs/20260323-brisbane-flood \
  --round-id round-001 \
  --scenario-input /tmp/flood-support.json \
  --pretty
```

5. If the run is under `$eco-council-supervisor`, import the completed fetch execution so the stage can advance without running live fetch commands.

```bash
python3 ../eco-council-supervisor/scripts/eco_council_supervisor.py import-fetch-execution \
  --run-dir ./runs/20260323-brisbane-flood \
  --pretty
```

6. Continue with the normal deterministic data plane.

```bash
python3 ../eco-council-orchestrate/scripts/eco_council_orchestrate.py run-data-plane \
  --run-dir ./runs/20260323-brisbane-flood \
  --round-id round-001 \
  --pretty
```

## Low-Token Design

- Use deterministic templates and numeric profiles instead of LLM-written fake content.
- Keep scenario control in small JSON files.
- Simulate only sources already selected into `fetch_plan.json`.
- Reuse the same downstream normalization, evidence-linking, and reporting stack as live mode.

## Scope Decisions

- Use this skill for experimental or replay raw-data generation only.
- Invoke this skill explicitly with `$eco-council-simulate` or direct script usage.
- Do not expose this skill implicitly to moderator, sociologist, or environmentalist production agents.
- Keep the moderator and expert source-selection audit flow unchanged.
- Do not let this skill auto-select new sources.
- Simulate only the sources already present in `fetch_plan.json`.
- Do not use this skill as canonical real-world evidence for production decisions.
- Do not mutate moderator tasks, source-selection files, or promoted contract objects.

## References

- `references/integration-boundary.md`
- `references/scenario-schema.md`
- `references/source-shapes.md`

## Assets

- `assets/scenarios/*.json`

## Script

- `scripts/eco_council_simulate.py`
