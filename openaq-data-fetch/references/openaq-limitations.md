# OpenAQ Access Constraints

## API Authentication

- OpenAQ API requires API key authentication.
- Send key in request header:
  - `X-API-Key: <OPENAQ_API_KEY>`
- Missing or invalid key returns `401 Unauthorized`.

## API Rate Limits

From OpenAQ rate-limit documentation (general tier):

- `60` requests per minute
- `2,000` requests per hour
- Scope: OpenAQ API requests authenticated by API key.
- This limit set does not apply to public S3 archive file fetches.

When exceeding limits:

- API may return `429 Too Many Requests`.
- Response headers include:
  - `x-ratelimit-used`
  - `x-ratelimit-reset`
  - `x-ratelimit-limit`
  - `x-ratelimit-remaining`

## Pagination Limits

- Default `limit` is `100`.
- Maximum `limit` is `1000`.
- Use `page` with constrained time/filter ranges for large datasets.

## S3 Archive Constraints

- S3 archive is public but not real-time.
- Documentation states files are written about `72` hours after end-of-day.
- For current or near-real-time reads, prefer API.
