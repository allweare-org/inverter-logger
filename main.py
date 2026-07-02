import os
import time
import logging
import hashlib
import argparse
import requests
import pandas as pd

from datetime import date, datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, request
from zoneinfo import ZoneInfo

from google.cloud import secretmanager
from google.cloud import bigquery

# =========================
# CONFIG
# =========================
DEYE_BASE_URL = "https://eu1-developer.deyecloud.com/v1.0"

os.environ["GCP_PROJECT"] = "all-we-are-master-database"
BQ_DATASET = "InverterLogData"
BQ_TABLE_RAW = "raw_5min"
BQ_TABLE_HOURLY = "hourly"

MAX_WORKERS = 5
DATA_SOURCE = "deye"

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# Active columns from the Deye station/history API (granularity=1/Frame).
# Commented-out fields are returned by the API but not currently stored.
# To re-enable a field: uncomment it here AND add it to the BQ table schema.
COLUMNS_STATION_HISTORY = [
    "dischargePower",
    "generationPower",
    "consumptionPower",
    "batteryPower",
    "batterySOC",
    "wirePower",
    "generationValue",
    "generationRatio",
    "gridPower",
    # "chargePower",
    # "gridRatio",
    # "chargeRatio",
    # "consumptionValue",
    # "consumptionRatio",
    # "purchaseRatio",
    # "consumptionDischargeRatio",
    # "gridValue",
    # "purchaseValue",
    # "chargeValue",
    # "dischargeValue",
    # "fullPowerHours",
    # "irradiate",
    # "theoreticalGeneration",
    # "pr",
    # "cpr",
    # "purchasePower",
    # "irradiateIntensity",
    # "year",
    # "month",
    # "day",
]

# Column renames applied to both raw and hourly tables
COLUMN_RENAMES = {
    "generationPower": "Production_kW",
    "consumptionPower": "Consumption_kW",
    "gridPower": "Grid_kW",
    "batteryPower": "Battery_kW",
    "batterySOC": "SOC",
}

# Columns written to the raw_5min BQ table.
# Renamed columns come first (after the station identifiers), then remaining API fields.
# Columns not present in the API response are silently skipped at write time.
RAW_COLUMNS = [
    "timestamp",
    "station_id",
    "station_name",
    "data_source",
    # renamed columns
    "Production_kW",
    "Consumption_kW",
    "Grid_kW",
    "Battery_kW",
    "SOC",
    # active API fields (currently collected)
    "dischargePower",
    "wirePower",
    "generationValue",
    "generationRatio",
    # inactive API fields (returned by API but not currently used)
    "chargePower",
    "gridRatio",
    "chargeRatio",
    "consumptionValue",
    "consumptionRatio",
    "purchaseRatio",
    "consumptionDischargeRatio",
    "gridValue",
    "purchaseValue",
    "chargeValue",
    "dischargeValue",
    "fullPowerHours",
    "irradiate",
    "theoreticalGeneration",
    "pr",
    "cpr",
    "purchasePower",
    "irradiateIntensity",
    "year",
    "month",
    "day",
]

# Columns written to the hourly BQ table
HOURLY_COLUMNS = [
    "timestamp",
    "station_id",
    "station_name",
    "data_source",
    "Production_kW",
    "Consumption_kW",
    "Grid_kW",
    "Battery_kW",
    "SOC",
]

bq_client = bigquery.Client()


# =========================
# SECRET MANAGER
# =========================
def get_secret(secret_name):
    client = secretmanager.SecretManagerServiceClient()
    project_id = os.environ["GCP_PROJECT"]
    name = f"projects/{project_id}/secrets/{secret_name}/versions/latest"
    response = client.access_secret_version(name=name)
    return response.payload.data.decode("UTF-8")


# =========================
# RETRY
# =========================
def retry(func, retries=3, delay=2):
    for i in range(retries):
        try:
            return func()
        except Exception as e:
            logging.warning(f"Retry {i+1} failed: {e}")
            time.sleep(delay * (2**i))
    raise Exception("Max retries exceeded")


# =========================
# AUTH
# =========================
def get_access_token():
    app_id = get_secret("DEYE_APP_ID")
    app_secret = get_secret("DEYE_APP_SECRET")
    email = get_secret("DEYE_EMAIL")
    # API requires password hashed with SHA256, lowercase
    password = (
        hashlib.sha256(get_secret("DEYE_PASSWORD").encode("utf-8")).hexdigest().lower()
    )

    try:
        response = requests.post(
            f"{DEYE_BASE_URL}/account/token?appId={app_id}",
            headers={"Content-Type": "application/json"},
            json={"appSecret": app_secret, "email": email, "password": password},
        )
    except requests.exceptions.RequestException as e:
        logging.error(f"Request failed: {e}")

    return response.json()["accessToken"]


# =========================
# STATION API HELPERS
# =========================
def get_stations(token):
    return retry(
        lambda: requests.post(
            f"{DEYE_BASE_URL}/station/list",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={"page": 1, "size": 100},
        ).json()["stationList"]
    )


def get_station_power_history(token, station, days=1):
    """Returns 5-min power data for a station for a single day (granularity=1/Frame)."""
    start = (date.today() - timedelta(days=days)).strftime("%Y-%m-%d")

    return retry(
        lambda: requests.post(
            f"{DEYE_BASE_URL}/station/history",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={
                "stationId": str(station["id"]),
                "granularity": 1,  # Frame: one day only, startAt == endAt
                "startAt": start,
                "endAt": start,
            },
        ).json()["stationDataItems"]
    )


def process_station_data(raw_data, station):
    df = pd.DataFrame(raw_data)
    if df.empty:
        return None, None

    df["timestamp"] = df["timeStamp"].apply(
        lambda x: datetime.strptime(
            datetime.fromtimestamp(x)
            .astimezone(ZoneInfo("Africa/Kampala"))
            .strftime("%Y-%m-%d %H:%M:%S"),
            "%Y-%m-%d %H:%M:%S",
        )
    )
    df["station_name"] = station["name"]
    df["station_id"] = str(station["id"])  # cast to string to match BQ STRING schema
    df["data_source"] = DATA_SOURCE

    raw_df = df.rename(columns=COLUMN_RENAMES).copy()

    df.set_index("timestamp", inplace=True)
    df.rename(columns=COLUMN_RENAMES, inplace=True)

    hourly = df[
        [
            "station_name",
            "station_id",
            "Production_kW",
            "Consumption_kW",
            "Grid_kW",
            "Battery_kW",
            "SOC",
        ]
    ]
    agg = {
        c: "sum"
        for c in hourly.columns
        if c not in ["SOC", "station_id", "station_name"]
    }
    agg["SOC"] = "mean"

    hourly = hourly.resample("h").agg(agg)
    hourly["station_name"] = station["name"]
    hourly["station_id"] = str(station["id"])

    return raw_df.reset_index(drop=True), hourly.reset_index()


def process_station(token, station, lookback_days):
    try:
        raw = get_station_power_history(token, station, lookback_days)
        return process_station_data(raw, station)
    except Exception as e:
        logging.error(f"{station['name']} [{station['id']}] failed: {e}")
        return None, None


# =========================
# BIGQUERY
# =========================
_RAW_SCHEMA = [
    bigquery.SchemaField("timestamp", "TIMESTAMP", mode="REQUIRED"),
    bigquery.SchemaField("station_id", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("station_name", "STRING"),
    bigquery.SchemaField("data_source", "STRING"),
    # renamed columns
    bigquery.SchemaField("Production_kW", "FLOAT64"),
    bigquery.SchemaField("Consumption_kW", "FLOAT64"),
    bigquery.SchemaField("Grid_kW", "FLOAT64"),
    bigquery.SchemaField("Battery_kW", "FLOAT64"),
    bigquery.SchemaField("SOC", "FLOAT64"),
    # active API fields
    bigquery.SchemaField("dischargePower", "FLOAT64"),
    bigquery.SchemaField("wirePower", "FLOAT64"),
    bigquery.SchemaField("generationValue", "FLOAT64"),
    bigquery.SchemaField("generationRatio", "FLOAT64"),
    # inactive API fields (reserved for future use)
    bigquery.SchemaField("chargePower", "FLOAT64"),
    bigquery.SchemaField("gridRatio", "FLOAT64"),
    bigquery.SchemaField("chargeRatio", "FLOAT64"),
    bigquery.SchemaField("consumptionValue", "FLOAT64"),
    bigquery.SchemaField("consumptionRatio", "FLOAT64"),
    bigquery.SchemaField("purchaseRatio", "FLOAT64"),
    bigquery.SchemaField("consumptionDischargeRatio", "FLOAT64"),
    bigquery.SchemaField("gridValue", "FLOAT64"),
    bigquery.SchemaField("purchaseValue", "FLOAT64"),
    bigquery.SchemaField("chargeValue", "FLOAT64"),
    bigquery.SchemaField("dischargeValue", "FLOAT64"),
    bigquery.SchemaField("fullPowerHours", "FLOAT64"),
    bigquery.SchemaField("irradiate", "FLOAT64"),
    bigquery.SchemaField("theoreticalGeneration", "FLOAT64"),
    bigquery.SchemaField("pr", "FLOAT64"),
    bigquery.SchemaField("cpr", "FLOAT64"),
    bigquery.SchemaField("purchasePower", "FLOAT64"),
    bigquery.SchemaField("irradiateIntensity", "FLOAT64"),
    bigquery.SchemaField("year", "INTEGER"),
    bigquery.SchemaField("month", "INTEGER"),
    bigquery.SchemaField("day", "INTEGER"),
]

_HOURLY_SCHEMA = [
    bigquery.SchemaField("timestamp", "TIMESTAMP", mode="REQUIRED"),
    bigquery.SchemaField("station_id", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("station_name", "STRING"),
    bigquery.SchemaField("data_source", "STRING"),
    bigquery.SchemaField("Production_kW", "FLOAT64"),
    bigquery.SchemaField("Consumption_kW", "FLOAT64"),
    bigquery.SchemaField("Grid_kW", "FLOAT64"),
    bigquery.SchemaField("Battery_kW", "FLOAT64"),
    bigquery.SchemaField("SOC", "FLOAT64"),
]


def ensure_tables_exist():
    """Create raw_5min and hourly BQ tables if they don't already exist."""
    project = os.environ["GCP_PROJECT"]

    for table_id, schema in [
        (f"{project}.{BQ_DATASET}.{BQ_TABLE_RAW}", _RAW_SCHEMA),
        (f"{project}.{BQ_DATASET}.{BQ_TABLE_HOURLY}", _HOURLY_SCHEMA),
    ]:
        table = bigquery.Table(table_id, schema=schema)
        table.time_partitioning = bigquery.TimePartitioning(field="timestamp")
        table.clustering_fields = ["station_id"]
        bq_client.create_table(table, exists_ok=True)

    logging.info("BigQuery tables verified.")


def _df_to_schema_order(df, schema):
    """Return a copy of df with columns in schema order, adding NULL for any missing columns."""
    df = df.copy()
    for field in schema:
        if field.name not in df.columns:
            df[field.name] = None  # written as BQ NULL
    return df[[f.name for f in schema]]


def load_raw_to_bq(df):
    """Insert raw 5-min rows that don't already exist (idempotent on re-runs)."""
    project = os.environ["GCP_PROJECT"]
    temp_table = f"{project}.{BQ_DATASET}.temp_raw_{int(time.time())}"
    table_id = f"{project}.{BQ_DATASET}.{BQ_TABLE_RAW}"

    job_config = bigquery.LoadJobConfig(
        schema=_RAW_SCHEMA,
        write_disposition="WRITE_TRUNCATE",
    )
    job = bq_client.load_table_from_dataframe(
        _df_to_schema_order(df, _RAW_SCHEMA), temp_table, job_config=job_config
    )
    job.result()

    query = f"""
    MERGE `{table_id}` T
    USING `{temp_table}` S
    ON T.timestamp = S.timestamp AND T.station_id = S.station_id AND T.data_source = S.data_source
    WHEN NOT MATCHED THEN INSERT ROW
    """
    bq_client.query(query).result()
    bq_client.delete_table(temp_table, not_found_ok=True)
    logging.info(f"Upserted {len(df)} raw rows → {table_id}")


def upsert_hourly_to_bq(df):
    """Write hourly rows via a temp table + MERGE to deduplicate on (timestamp, station_id)."""
    project = os.environ["GCP_PROJECT"]
    temp_table = f"{project}.{BQ_DATASET}.temp_{int(time.time())}"
    table_id = f"{project}.{BQ_DATASET}.{BQ_TABLE_HOURLY}"

    job_config = bigquery.LoadJobConfig(
        schema=_HOURLY_SCHEMA,
        write_disposition="WRITE_TRUNCATE",
    )
    job = bq_client.load_table_from_dataframe(
        _df_to_schema_order(df, _HOURLY_SCHEMA), temp_table, job_config=job_config
    )
    job.result()

    query = f"""
    MERGE `{table_id}` T
    USING `{temp_table}` S
    ON T.timestamp = S.timestamp AND T.station_id = S.station_id AND T.data_source = S.data_source
    WHEN MATCHED THEN UPDATE SET
        Production_kW  = S.Production_kW,
        Consumption_kW = S.Consumption_kW,
        Grid_kW        = S.Grid_kW,
        Battery_kW     = S.Battery_kW,
        SOC            = S.SOC,
        station_name   = S.station_name,
        data_source    = S.data_source
    WHEN NOT MATCHED THEN INSERT ROW
    """
    bq_client.query(query).result()
    bq_client.delete_table(temp_table, not_found_ok=True)
    logging.info(f"Upserted {len(df)} hourly rows → {table_id}")


# =========================
# DERIVE HOURLY
# =========================
def _derive_hourly(raw_df):
    """Build hourly aggregates from deduplicated 5-min raw data.

    Grouping per station then resampling guarantees exactly one row per
    (timestamp, station_id) — a hard requirement for the MERGE upsert.
    """
    kw_cols = ["Production_kW", "Consumption_kW", "Grid_kW", "Battery_kW"]
    agg = {c: "mean" for c in kw_cols}
    agg["SOC"] = "mean"

    results = []
    for (station_id, station_name, data_source), group in raw_df.groupby(
        ["station_id", "station_name", "data_source"]
    ):
        hourly = (
            group.set_index("timestamp")[kw_cols + ["SOC"]]
            .resample("h")
            .agg(agg)
            .reset_index()
        )
        hourly[kw_cols] = (hourly[kw_cols] / 1000).round(1)
        hourly["SOC"] = hourly["SOC"].round(0)
        hourly["station_id"] = station_id
        hourly["station_name"] = station_name
        hourly["data_source"] = data_source
        results.append(hourly)

    return pd.concat(results, ignore_index=True)


# =========================
# MAIN PIPELINE
# =========================
def run_pipeline(backfill_days=1):
    ensure_tables_exist()
    token = get_access_token()
    stations = get_stations(token)

    raw_all = []

    with ThreadPoolExecutor(MAX_WORKERS) as ex:
        futures = []
        for s in stations:
            for d in range(backfill_days):
                day_str = (date.today() - timedelta(days=d)).strftime("%Y-%m-%d")
                logging.info(f"Queuing {s['name']} [{s['id']}] — {day_str}")
                futures.append(ex.submit(process_station, token, s, d))

        for f in as_completed(futures):
            raw_df, _ = f.result()  # hourly re-derived below from deduplicated raw
            if raw_df is not None:
                raw_all.append(raw_df)

    if not raw_all:
        logging.warning("No data collected — nothing to write.")
        return

    # Deduplicate 5-min data — adjacent day API responses can overlap at boundaries,
    # producing the same (timestamp, station_id) in two day chunks.
    raw_df = pd.concat(raw_all, ignore_index=True).drop_duplicates(
        subset=["timestamp", "station_id", "data_source"]
    )
    # Derive hourly from clean deduplicated raw — guarantees no duplicate MERGE keys.
    hourly_df = _derive_hourly(raw_df)

    # Sort both frames: newest first, then station name A→Z
    sort_kwargs = dict(by=["timestamp", "station_name"], ascending=[False, True])
    raw_df = raw_df.sort_values(**sort_kwargs).reset_index(drop=True)
    hourly_df = hourly_df.sort_values(**sort_kwargs).reset_index(drop=True)

    logging.info(
        f"Writing {len(raw_df)} raw rows and {len(hourly_df)} hourly rows to BigQuery..."
    )
    load_raw_to_bq(raw_df)
    upsert_hourly_to_bq(hourly_df)


# =========================
# ENTRYPOINTS
# =========================
@app.route("/", methods=["POST"])
def http_entrypoint():
    data = request.get_json(silent=True) or {}
    run_pipeline(backfill_days=data.get("backfill_days", 1))
    return "OK", 200


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Inverter data pipeline")
    parser.add_argument(
        "--backfill-days",
        type=int,
        default=1,
        help="Number of past days to fetch (default: 1 = yesterday)",
    )
    args = parser.parse_args()
    run_pipeline(backfill_days=args.backfill_days)
