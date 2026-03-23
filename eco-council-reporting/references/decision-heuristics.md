# Decision Heuristics

This skill uses deterministic heuristics to seed moderator decisions.

## Missing Evidence Typing

Current gap-to-type mappings:

- station corroboration gaps -> `station-air-quality`
- wildfire gaps without fire detections -> `fire-detection`
- wildfire gaps without weather context -> `meteorology-background`
- flood gaps -> `precipitation-hydrology`
- heat gaps -> `temperature-extremes`
- drought gaps -> `precipitation-soil-moisture`
- policy-reaction gaps -> `policy-comment-coverage`
- thin public coverage across channels -> `public-discussion-coverage`

## Task Seeding

Each missing evidence type maps to one default next-round task shape:

- `station-air-quality` -> `environmentalist` using `openaq-data-fetch`
- `fire-detection` -> `environmentalist` using `nasa-firms-fire-fetch`
- `meteorology-background` -> `environmentalist` using `open-meteo-historical-fetch`
- `precipitation-hydrology` -> `environmentalist` using `open-meteo-historical-fetch` or `open-meteo-flood-fetch`
- `temperature-extremes` -> `environmentalist` using `open-meteo-historical-fetch`
- `precipitation-soil-moisture` -> `environmentalist` using `open-meteo-historical-fetch`
- `policy-comment-coverage` -> `sociologist` using `regulationsgov-comments-fetch` and `regulationsgov-comment-detail-fetch`
- `public-discussion-coverage` -> `sociologist` using GDELT, Bluesky, or YouTube fetch skills

The script intersects these source hints with `mission.source_policy` when available.
When counting public-source diversity, `gdelt-doc-search` and the three raw GDELT table skills are treated as one GDELT family rather than four independent channels.
The generated drafts only populate `task.inputs.preferred_sources`.
They do not auto-emit `task.inputs.required_sources`; that field is reserved for moderator-authored overrides or rare system-level hard constraints outside reporting.

## Completion Logic

High-level decision rules:

- unresolved evidence plus remaining round budget -> `moderator_status=continue`
- fully resolved evidence and complete reports -> `moderator_status=complete`
- unresolved evidence after `max_rounds` is exhausted, or no usable round artifacts -> `moderator_status=blocked`

The generated `completion_score` is heuristic only. Moderator review may still revise it.
