-- ============================================================
-- raw_5min: every 5-min data point from the Deye API
-- ============================================================
-- To re-enable a commented-out field: uncomment it here AND
-- add it to COLUMNS_STATION_HISTORY in main.py.
--
-- Available but inactive API fields:
--   chargePower, gridRatio, chargeRatio, consumptionValue,
--   consumptionRatio, purchaseRatio, consumptionDischargeRatio,
--   gridValue, purchaseValue, chargeValue, dischargeValue,
--   fullPowerHours, irradiate, theoreticalGeneration, pr, cpr,
--   purchasePower, irradiateIntensity, year, month, day
-- ============================================================
CREATE TABLE IF NOT EXISTS `all-we-are-master-database.InverterLogData.raw_5min` (
  timestamp        TIMESTAMP NOT NULL,
  station_id       STRING    NOT NULL,
  station_name     STRING,
  dischargePower   FLOAT64,
  generationPower  FLOAT64,
  consumptionPower FLOAT64,
  batteryPower     FLOAT64,
  batterySOC       FLOAT64,
  wirePower        FLOAT64,
  generationValue  FLOAT64,
  generationRatio  FLOAT64,
  gridPower        FLOAT64
)
PARTITION BY DATE(timestamp)
CLUSTER BY station_id;


-- ============================================================
-- hourly: 1-hour resampled data
--   Power fields (Production, Consumption, Grid, Battery) → summed
--   SOC → averaged
-- ============================================================
CREATE TABLE IF NOT EXISTS `all-we-are-master-database.InverterLogData.hourly` (
  timestamp        TIMESTAMP NOT NULL,
  station_id       STRING    NOT NULL,
  station_name     STRING,
  Production_kWh   FLOAT64,
  Consumption_kWh  FLOAT64,
  Grid_kWh         FLOAT64,
  Battery_kWh      FLOAT64,
  SOC              FLOAT64
)
PARTITION BY DATE(timestamp)
CLUSTER BY station_id;
