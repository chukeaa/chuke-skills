# OpenClaw Chaining Templates

Use this skill atomically for one bounded window. Let OpenClaw chain into other social or physical-source skills as needed.
For eco-council runs, always use the exact role-owned `raw/` artifact path from the fetch plan as `--output`.

## Pattern 1: Hourly PM/Ozone Snapshot

```text
Use $airnow-hourly-obs-fetch.
Run:
python3 scripts/airnow_hourly_obs_fetch.py fetch \
  --bbox=[MIN_LON,MIN_LAT,MAX_LON,MAX_LAT] \
  --start-datetime [YYYY-MM-DDTHH:MM:SSZ] \
  --end-datetime [YYYY-MM-DDTHH:MM:SSZ] \
  --parameter PM25 \
  --parameter OZONE \
  --output [OUTPUT_FILE] \
  --pretty
Return the JSON result and confirm `[OUTPUT_FILE]`.
```

## Pattern 2: Multi-Parameter Monitoring-Site Pull

```text
Use $airnow-hourly-obs-fetch.
Run:
python3 scripts/airnow_hourly_obs_fetch.py fetch \
  --bbox=[MIN_LON,MIN_LAT,MAX_LON,MAX_LAT] \
  --start-datetime [YYYY-MM-DDTHH:MM:SSZ] \
  --end-datetime [YYYY-MM-DDTHH:MM:SSZ] \
  --parameter PM25 \
  --parameter PM10 \
  --parameter OZONE \
  --parameter NO2 \
  --output [OUTPUT_FILE] \
  --pretty
Return the JSON payload and confirm `[OUTPUT_FILE]`.
```

## Pattern 3: Preflight Then Execute

```text
Use $airnow-hourly-obs-fetch.
First run a dry-run fetch for:
- bbox: [MIN_LON,MIN_LAT,MAX_LON,MAX_LAT]
- UTC window: [START_UTC] to [END_UTC]
- parameters: PM25
If dry-run looks correct, run the real fetch with `--output [OUTPUT_FILE]` and return the JSON result plus `[OUTPUT_FILE]`.
```
