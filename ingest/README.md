# ingest

Go service that receives probe batches over gRPC and stores them in ClickHouse, plus the
anomaly job and a small read-only query API.

- **Exactly-once batches**: a batch is identified by `(probe_id, batch_id)`. Ingest writes a
  pending record to a local fsync'd journal before inserting, verifies the raw row count and
  the materialized-view sample count, then marks the batch committed and acknowledges it.
  A retried batch with the same content is acknowledged as a duplicate; different content
  under the same key is rejected. After an uncertain write the writer freezes; the pending
  batch is replayed after a ClickHouse restart, which the HTTP session check
  (`session_check=1`) detects, so a write can never land in a different ClickHouse epoch.
- **Schema** ([schema/001_init.sql](schema/001_init.sql)): raw `probe_results` (MergeTree,
  180-day TTL), 5-minute p50/p95/p99 aggregates in `path_quantiles_5m`, and `path_events`.
- **Anomaly job** (`-anomaly-enable`): every 5 minutes, a one-sided CUSUM on each path's
  5-minute p95 (warm-up 20 windows, K 0.5, H 12, sigma floor 1 ms; H is chosen in
  [the evidence index](../docs/evidence/README.md#cusum-threshold-simulation-one-machine)).
  Alarms are correlated with traceroute snapshots before and after the window to report
  whether the path changed and the first differing TTL, written to `path_events` and posted
  to Alertmanager. After an alarm the path's detector latches: no new alarm until the
  CUSUM sum decays back to 0 (at least H/K = 24 windows, 2 hours, at baseline), so a
  lasting shift alarms once.
- **Modes**: `-mode embedded` (default, one process), or `-mode core` (owns the store) with
  stateless `-mode frontend` replicas that talk to core over mutual TLS. `cmd/adminbridge`
  exposes metrics over TLS for Prometheus.

```sh
source scripts/dev-env.sh
go build -o .cache/pathwatch-ingest ./ingest/cmd/ingest
PATHWATCH_CLICKHOUSE_PASSWORD=... .cache/pathwatch-ingest \
  -clickhouse-url http://127.0.0.1:8123 -database pathwatch -clickhouse-user pathwatch_ingest \
  -journal .cache/ingest-journal -listen 127.0.0.1:9443 \
  -tls-cert server.pem -tls-key server-key.pem -metrics 127.0.0.1:9091
```

ClickHouse must be a dedicated database reachable on a loopback address. Tests: `make test-go`
runs the unit tests; `PATHWATCH_TEST_DOCKER=1 make test-go` adds the ClickHouse
testcontainer suite (needs Docker and the pinned `clickhouse/clickhouse-server:26.3.32.14`
image).

Detailed notes on recovery, limits and the query API (Chinese and English):
[docs/dev/notes/ingest.md](../docs/dev/notes/ingest.md).
