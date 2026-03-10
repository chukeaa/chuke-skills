# Environment Variables

This skill uses environment variables for all connection and auth settings.

- `OPENAQ_API_KEY` (required for API calls):
  - Used as `X-API-Key` request header for `https://api.openaq.org`.
- `OPENAQ_REGION` (optional for S3 access):
  - Default: `us-east-1` when the variable is unset or empty.
  - Used to build the S3 endpoint URL.
- `OPENAQ_S3_BUCKET` (optional):
  - Default: `openaq-data-archive`.

Example:

```bash
export OPENAQ_API_KEY="your-openaq-api-key"
export OPENAQ_REGION="us-east-1"
export OPENAQ_S3_BUCKET="openaq-data-archive"
```
