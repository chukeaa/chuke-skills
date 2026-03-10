# OpenAQ S3 Archive Layout

OpenAQ publishes historical records in a public S3 bucket.

## Bucket and Endpoint

- Default bucket: `openaq-data-archive`
- Effective region for this skill: `OPENAQ_REGION` (default `us-east-1`)
- Key root:
  - `records/csv.gz/`

## Partition Pattern

The archive follows Hive-style partitioning after `records/csv.gz`:

```text
records/csv.gz/locationid={locationid}/year={year}/month={month}/...
```

Typical key example:

```text
records/csv.gz/locationid=2178/year=2022/month=05/location-2178-20220503.csv.gz
```

## File Characteristics

- Format: gzip-compressed CSV (`csv.gz`)
- Daily files can contain all sensors for a location/day.

## Operational Notes

- Archive files are published with delay relative to API (documented as about 72 hours after end-of-day).
- Public read access works without signed AWS credentials.
- AWS documentation in OpenAQ docs also mentions SNS notifications for object creation events.
