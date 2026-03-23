# Source Shapes

These are the minimal artifact shapes the first simulator version writes because they already satisfy the current deterministic normalizers.

## Public Sources

### `gdelt-doc-search`

- JSON object
- important field:
  - `articles[]`

### `gdelt-events-fetch`, `gdelt-mentions-fetch`, `gdelt-gkg-fetch`

- JSON manifest at the canonical `artifact_path`
- important fields:
  - `downloads[]`
  - `downloads[].output_path`
  - `downloads[].preview_member`
  - `downloads[].validation`
- each `downloads[].output_path` points at a simulated ZIP file containing one tab-delimited member file with the expected GDELT column count

### `bluesky-cascade-fetch`

- JSON object
- important fields:
  - `seed_posts[]`
  - optional `threads[].nodes[]`

### `youtube-video-search`

- JSONL
- one record per line
- important fields:
  - `video_id`
  - `query`
  - `video.id`
  - `video.title`
  - `video.description`
  - `video.channel_title`
  - `video.published_at`

### `youtube-comments-fetch`

- JSONL
- important fields:
  - `comment_id`
  - `video_id`
  - `text_original`
  - `published_at`
  - `author_display_name`

### `regulationsgov-comments-fetch`

- JSONL
- important fields:
  - `id`
  - `attributes.comment`
  - `attributes.title`
  - `attributes.postedDate`
  - `attributes.agencyId`

### `regulationsgov-comment-detail-fetch`

- JSONL
- important fields:
  - `detail.data.attributes.*`

## Environment Sources

### `open-meteo-historical-fetch`

- JSON object
- important fields:
  - `records[].latitude`
  - `records[].longitude`
  - `records[].hourly.time[]`
  - `records[].hourly.<metric>[]`
  - `records[].hourly_units.<metric>`
  - optional `records[].daily.*`

### `open-meteo-air-quality-fetch`

- same structure as other Open-Meteo artifacts
- prefer hourly air-quality metrics

### `open-meteo-flood-fetch`

- same structure as other Open-Meteo artifacts
- prefer daily hydrology metrics such as `river_discharge`

### `nasa-firms-fire-fetch`

- JSON object
- important fields:
  - `records[]`
  - `records[]._acquired_at_utc`
  - `records[]._latitude`
  - `records[]._longitude`

### `openaq-data-fetch`

- JSON object
- important fields:
  - `result.records[]`
  - `parameter.name`
  - `parameter.units`
  - `value`
  - `date.utc`
  - `coordinates.latitude`
  - `coordinates.longitude`
