# Source Mapping

## Public-Signal Mapping

### `gdelt-doc-search`

- Preferred use: topic recon and article-list retrieval.
- Output mapping:
  - article-like items -> `public_signal.signal_kind=article`
  - timeline bins -> `public_signal.signal_kind=timeline-bin`
- Claim generation:
  - article-like items can become `claim` candidates
  - timeline bins are stored for trend context but usually do not produce standalone claims

### `youtube-video-search`

- Treated as discovery/supporting context.
- Each kept video becomes one `public_signal`.
- Videos can contribute to claims when title/description contains mission-relevant assertions.

### `youtube-comments-fetch`

- Each kept comment/reply becomes one `public_signal`.
- Respect `reply_window_completeness`; do not treat reply counts as fully exhaustive historical coverage.

### `bluesky-cascade-fetch`

- Seed posts and thread nodes can both become `public_signal`.
- The normalizer stores post text, author, URI, timestamp, and lightweight engagement counts.

### `federal-register-doc-fetch`

- Treated as official policy-document discovery from FederalRegister.gov.
- Each result becomes one `public_signal` with:
  - `signal_kind=policy-document`
  - `external_id=document_number`
  - `title`, `abstract`/`excerpts`, publication date, and agency metadata
- Best for official rulemaking or notice context, not public-comment sentiment.
- Canonical raw payloads use the top-level `records` array emitted by `$federal-register-doc-fetch`.

### `regulationsgov-comments-fetch`

- List fetch is good for trend discovery and ID capture.
- Comment text may be partial depending on API payload shape.

### `regulationsgov-comment-detail-fetch`

- Detail fetch is preferred when exact comment text matters.
- Attachment metadata is preserved in `metadata_json`.

### `gdelt-events-fetch`, `gdelt-mentions-fetch`, `gdelt-gkg-fetch`

- The canonical raw artifact is a JSON manifest whose `downloads[].output_path` entries point at ZIP sidecars.
- The normalizer reads the ZIP member rows directly instead of stopping at manifest provenance.
- Current deterministic outputs are:
  - one `table-coverage` signal per source artifact
  - a few mission-matched sample row signals:
    - `event-record`
    - `mention-record`
    - `gkg-record`
- These raw tables are still supplementary bulk layers around `gdelt-doc-search`, not a replacement for article-level recon.

## Environment Mapping

### `airnow-hourly-obs-fetch`

- Treated as monitoring-site hourly observations from AirNow file products.
- The normalizer canonicalizes AirNow pollutant names into the same metric space used by modeled sources:
  - `PM25` -> `pm2_5`
  - `PM10` -> `pm10`
  - `OZONE` -> `ozone`
  - `NO2` -> `nitrogen_dioxide`
  - `CO` -> `carbon_monoxide`
  - `SO2` -> `sulphur_dioxide`
- The normalizer also writes AQI companion metrics when `aqi_value` is present:
  - `pm2_5_aqi`
  - `pm10_aqi`
  - `ozone_aqi`
  - `nitrogen_dioxide_aqi`
- Quality flags include `station-observation` or `station-aqi`, plus `preliminary` and `airnow-file-product`.

### `usgs-water-iv-fetch`

- Treated as station-based hydrology observations from USGS Water Services Instantaneous Values.
- Current first-pass metric mapping is:
  - `00060` -> `river_discharge`
  - `00065` -> `gage_height`
- One signal is written per site-parameter-timestamp observation.
- Quality flags include `station-observation` and `usgs-water-services-iv`.
- When the raw record has a provisional qualifier, the normalizer also adds `provisional`.

### `open-meteo-historical-fetch`

- Produces point-based weather or soil series.
- Normalizer writes one staging row per timestamped metric value.
- Canonical output is one or more `observation` summaries per metric and location.

### `open-meteo-air-quality-fetch`

- Treated as modeled background air-quality context.
- Quality flags include `modeled-background`.

### `open-meteo-flood-fetch`

- Treated as modeled hydrology or flood-background context.
- Quality flags include `hydrology-model`.
- River-discharge metrics can support or contradict `flood` claims directly.

### `nasa-firms-fire-fetch`

- Writes one staging row per fire detection.
- Canonical output includes an `event-count` observation such as `fire_detection_count`.

### `openaq-data-fetch`

- API JSON results and CSV/CSV.GZ artifacts are both supported.
- OpenAQ station metrics are canonicalized into the same metric space as AirNow and Open-Meteo where possible, for example `pm25`/`pm2.5` -> `pm2_5` and `o3` -> `ozone`.
- Quality flags include `station-observation`.
- Prefer API for near-real-time windows and S3 for backfill windows.
