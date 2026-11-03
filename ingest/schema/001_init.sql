CREATE TABLE IF NOT EXISTS probe_results
(
 batch_key FixedString(64), result_index UInt32,
 probe_id String, batch_id String, target_id String, kind LowCardinality(String),
 family LowCardinality(String), addr String,
 path_key String, started_at DateTime64(9, 'UTC'),
 phases_nanoseconds Map(String, Int64), err String, protocol LowCardinality(String),
 hop_ttls Array(UInt32), hop_addrs Array(String), hop_rtt_nanoseconds Array(Int64),
 tls_version UInt32, tls_resumed Bool,
 phase_ms Float64, phase_present UInt8
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(started_at)
ORDER BY (path_key, started_at)
TTL toDateTime(started_at) + INTERVAL 180 DAY
SETTINGS fsync_after_insert=1, fsync_part_directory=1, min_rows_to_fsync_after_merge=1;

CREATE TABLE IF NOT EXISTS path_quantiles_5m
(
 path_key String, window_start DateTime('UTC'), batch_key FixedString(64),
 phase_ms AggregateFunction(quantiles(0.5, 0.95, 0.99), Float64),
 samples AggregateFunction(count)
)
ENGINE = AggregatingMergeTree
PARTITION BY toYYYYMM(window_start)
ORDER BY (path_key, window_start, batch_key)
TTL window_start + INTERVAL 180 DAY
SETTINGS fsync_after_insert=1, fsync_part_directory=1, min_rows_to_fsync_after_merge=1;

CREATE MATERIALIZED VIEW IF NOT EXISTS probe_quantiles_mv TO path_quantiles_5m AS
SELECT path_key, toStartOfFiveMinutes(started_at) AS window_start, batch_key,
 quantilesState(0.5, 0.95, 0.99)(phase_ms) AS phase_ms, countState() AS samples
FROM probe_results WHERE phase_present=1
GROUP BY path_key, window_start, batch_key;

CREATE TABLE IF NOT EXISTS path_events
(
 event_id FixedString(64), path_key String, window_start DateTime('UTC'),
 p95_ms Float64, baseline_ms Float64, sigma_ms Float64,
 cusum_k Float64, cusum_h Float64, path_changed Bool,
 first_divergent_ttl UInt16, correlation_reason LowCardinality(String)
)
ENGINE = ReplacingMergeTree
PARTITION BY toYYYYMM(window_start)
ORDER BY event_id
TTL window_start + INTERVAL 180 DAY
SETTINGS fsync_after_insert=1, fsync_part_directory=1, min_rows_to_fsync_after_merge=1;
