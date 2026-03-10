---
name: openaq-data-fetch
description: Retrieve OpenAQ metadata and measurements through authenticated API queries or public S3 archive fetches with automatic source routing. Use when tasks need OpenAQ data discovery across countries/providers/locations/sensors/parameters, filtered current or historical API queries, or large historical backfills from the AWS Open Data archive.
---

# OpenAQ Data Fetch

## Core Goal
- Query OpenAQ API v3 endpoints for metadata and measurements with one generic client.
- Fetch large historical files from the public OpenAQ S3 archive by prefix or direct key.
- Auto-route requests:
  - API for interactive or filtered queries.
  - S3 for full backfills and partition-based bulk download.
- Keep credentials and region configuration in environment variables.

## Required Environment
- `OPENAQ_API_KEY` (required for API requests).
- `OPENAQ_REGION` (optional for S3 endpoint selection, defaults to `us-east-1` when unset).
- Optional:
  - `OPENAQ_S3_BUCKET` (defaults to `openaq-data-archive`).

Reference:
- `references/env.md`
- `assets/config.example.env`

## Workflow
1. Validate environment and defaults.

```bash
python3 scripts/openaq_api_client.py check-config
python3 scripts/openaq_s3_fetch.py check-config
```

2. Choose a source mode:
- `api`: filtered queries, entity lookup, latest data access, paged exploration.
- `s3`: partition-based historical file retrieval for large volumes.
- `auto`: use `volume-profile` (`interactive`, `batch`, `full-backfill`) to select API or S3.

3. Fetch data with one of the commands below.

## API Usage
Rate limits (API only, scoped to API key):
- `60 / minute`
- `2,000 / hour`
- On exceed: `429 Too Many Requests`.
- S3 archive access does not use these OpenAQ API key rate limits.

Query any v3 path with optional query parameters:

```bash
python3 scripts/openaq_api_client.py request \
  --path /v3/locations \
  --query countries_id=9 \
  --query limit=100 \
  --pretty
```

Fetch across pages and merge `results`:

```bash
python3 scripts/openaq_api_client.py request \
  --path /v3/locations \
  --query countries_id=9 \
  --all-pages \
  --max-pages 20 \
  --pretty
```

## S3 Usage
List partition prefixes:

```bash
python3 scripts/openaq_s3_fetch.py ls \
  --prefix records/csv.gz/locationid=2178/ \
  --delimiter / \
  --max-keys 200
```

Build a Hive-style prefix for partition traversal:

```bash
python3 scripts/openaq_s3_fetch.py build-prefix \
  --location-id 2178 \
  --year 2022 \
  --month 5
```

Download one archived file by key:

```bash
python3 scripts/openaq_s3_fetch.py download \
  --key records/csv.gz/locationid=2178/year=2022/month=05/location-2178-20220503.csv.gz \
  --output ./data/location-2178-20220503.csv.gz
```

## Auto Routing
Let the router choose API vs S3:

```bash
python3 scripts/openaq_router.py fetch \
  --source-mode auto \
  --volume-profile interactive \
  --api-path /v3/providers \
  --api-query limit=50 \
  --pretty
```

Force S3 path via full-backfill profile:

```bash
python3 scripts/openaq_router.py fetch \
  --source-mode auto \
  --volume-profile full-backfill \
  --s3-action build-prefix \
  --location-id 2178 \
  --year 2022 \
  --month 5 \
  --pretty
```

## References
- `references/openaq-api-endpoints.md`
- `references/openaq-s3-layout.md`
- `references/openaq-limitations.md`

## Scripts
- `scripts/openaq_api_client.py`
- `scripts/openaq_s3_fetch.py`
- `scripts/openaq_router.py`
