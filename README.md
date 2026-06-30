# Inverter Logger

A Google Cloud Run service that fetches solar inverter power data from the [Deye Cloud API](https://developer.deyecloud.com/api) and stores it in BigQuery for analysis in Looker.

## Overview

The pipeline runs daily at 3am ET (via Cloud Scheduler). For each station on the account, it:

1. Fetches 5-minute interval power data from the Deye `station/history` endpoint
2. Writes all raw data points to `InverterLogData.raw_5min`
3. Resamples to 1-hour intervals and upserts into `InverterLogData.hourly`

Both writes are **idempotent** — re-running over the same date range will not create duplicates.

## Architecture

```
Cloud Scheduler → Cloud Run (main.py)
                      ├── Deye Cloud API  (fetch station data)
                      ├── Secret Manager  (DEYE_APP_ID, DEYE_APP_SECRET, DEYE_EMAIL, DEYE_PASSWORD)
                      └── BigQuery
                              ├── InverterLogData.raw_5min   (5-min raw data, append-only via MERGE)
                              └── InverterLogData.hourly     (1-hr aggregates, upsert via MERGE)
```

## BigQuery Schema

Both tables are partitioned by `DATE(timestamp)` and clustered by `station_id`.

### `raw_5min`
All 5-minute data points from the API. See `big_query_schema.sql` for the full column list, including inactive API fields reserved for future use.

### `hourly`
| Column | Type | Notes |
|---|---|---|
| timestamp | TIMESTAMP | Hour boundary (Kampala local time, UTC+3) |
| station_id | STRING | Deye station ID |
| station_name | STRING | |
| Production_kWh | FLOAT64 | Sum of `generationPower` readings ÷ 1000 |
| Consumption_kWh | FLOAT64 | Sum of `consumptionPower` readings ÷ 1000 |
| Grid_kWh | FLOAT64 | Sum of `gridPower` readings ÷ 1000 |
| Battery_kWh | FLOAT64 | Sum of `batteryPower` readings ÷ 1000 |
| SOC | FLOAT64 | Average `batterySOC` for the hour |

## Configuration

Secrets are stored in Google Cloud Secret Manager under project `all-we-are-master-database`:

| Secret | Description |
|---|---|
| `DEYE_APP_ID` | Deye Cloud app ID |
| `DEYE_APP_SECRET` | Deye Cloud app secret |
| `DEYE_EMAIL` | Deye Cloud account email |
| `DEYE_PASSWORD` | Deye Cloud account password (hashed SHA256 by the script) |

## Local Backfill

To backfill historical data from your local machine, ensure Application Default Credentials are configured (`gcloud auth application-default login`), then run:

```bash
python main.py --backfill-days 30
```

This fetches one API call per station per day and writes all results to BigQuery in a single batch.

## Expanding to Other Inverter Brands

The pipeline is brand-agnostic at the BigQuery layer — `raw_5min` and `hourly` use generic column names (Production_kWh, Consumption_kWh, etc.) and identify data by `station_id`/`station_name`. To add a new brand:

1. Add a new set of API helper functions (e.g. `get_stations_<brand>`, `process_<brand>_data`) that normalize data to the same column names
2. Call them from `run_pipeline` alongside the existing Deye fetch

## Dependencies

See `requirements.txt`. Key packages:

- `google-cloud-bigquery` + `pyarrow` + `db-dtypes` — BigQuery I/O
- `google-cloud-secret-manager` — credential retrieval
- `flask` — HTTP entrypoint for Cloud Run
- `pandas` — data transformation and resampling

## References

- [Deye Cloud Open API documentation](https://developer.deyecloud.com/api)
- [Deye Cloud Open MCP Tools](https://developer.deyecloud.com/openmcp/docs/deye-open-mcp-tools.html)
- [Deye OpenAPI client sample code](https://github.com/DeyeCloudDevelopers/deye-openapi-client-sample-code) — official sample showing auth and endpoint usage
- [Deye OpenAPI community integration](https://github.com/EAZYLINK/deye-openapi) — community Python wrapper used as a reference for field names and granularity behaviour
- Deye API Swagger schema at `https://eu1-developer.deyecloud.com/v2/api-docs` — source of truth for `StationDataItem` field names and types
