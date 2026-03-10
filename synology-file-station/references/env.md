# Environment Variables

## Required
- `SYNOLOGY_BASE_URL`: DSM service address, for example `https://nas.example.com:5001`.
- `SYNOLOGY_USERNAME`: DSM username.
- `SYNOLOGY_PASSWORD`: DSM password.

## Optional
- `SYNOLOGY_VERIFY_SSL`: `true`/`false`, default `true`.
- `SYNOLOGY_TIMEOUT`: HTTP timeout in seconds, default `30`.
- `SYNOLOGY_SESSION`: login session name, default `FileStation`.
- `SYNOLOGY_READONLY`: `true`/`false`, default `false`.
  - `true`: block all mutation commands (safe read-only mode).
  - `false`: allow mutation commands.
- `SYNOLOGY_MUTATION_ALLOW_PATHS`: comma-separated mutable root paths, e.g. `/home/ai-work,/data/projects`.
  - Optional.
  - If set, any write target outside these roots is rejected.
  - If empty/unset, mutation paths are unrestricted (backward-compatible behavior).
- Compatibility aliases:
  - `SYNOLOGY_URL` (same as `SYNOLOGY_BASE_URL`)
  - `SYNOLOGY_USER` (same as `SYNOLOGY_USERNAME`)
  - `SYNOLOGY_PASS` (same as `SYNOLOGY_PASSWORD`)

## Quick Setup
```bash
cp assets/config.example.env .env
# edit .env with real values
set -a
source .env
set +a
```

## Sanity Check
```bash
python3 scripts/synology_file_station.py check-config
```

## Remote Probe (optional)
```bash
python3 scripts/synology_file_station.py check-config --probe
```
