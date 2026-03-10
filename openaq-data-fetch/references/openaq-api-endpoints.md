# OpenAQ API Endpoint Map

The script `scripts/openaq_api_client.py` is path-driven and can call any v3 endpoint.

## Metadata and Discovery

- `/v3/countries`
- `/v3/providers`
- `/v3/owners`
- `/v3/licenses`
- `/v3/manufacturers`
- `/v3/instruments`
- `/v3/parameters`
- `/v3/locations`
- `/v3/locations/{locations_id}/sensors`
- `/v3/sensors/{sensors_id}`

## Latest and Flags

- `/v3/locations/{locations_id}/latest`
- `/v3/parameters/{parameters_id}/latest`
- `/v3/locations/{locations_id}/flags`
- `/v3/sensors/{sensor_id}/flags`

## Measurements and Aggregations

- `/v3/sensors/{sensors_id}/measurements`
- `/v3/sensors/{sensors_id}/measurements/hourly`
- `/v3/sensors/{sensors_id}/measurements/daily`
- `/v3/sensors/{sensors_id}/hours`
- `/v3/sensors/{sensors_id}/days`
- `/v3/sensors/{sensors_id}/years`
- Additional hourly and daily aggregate variations are also available under `/v3/sensors/{sensors_id}/...`

## Common Query Controls

- Pagination:
  - `limit` (default `100`, max `1000`)
  - `page` (default `1`)
- Time filters (endpoint dependent):
  - `datetime_from`, `datetime_to`
  - `date_from`, `date_to`
- Spatial filters (endpoint dependent):
  - `countries_id`, `iso`
  - `coordinates` + `radius`
  - `bbox`

Use `python3 scripts/openaq_api_client.py request --path <path> --query key=value`.
