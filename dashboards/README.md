# Dashboard source, not live rendering evidence

Terraform1.17.0 and Grafana provider3.25.9 are pinned in `.terraform.lock.hcl`. Install the official `grafana-clickhouse-datasource` **4.23.2** plugin in the target Grafana before applying; current local acceptance does not launch Grafana due the total project disk budget. Native `terraform validate` verifies provider schema; actual panel rendering remains unverified.

Provider credentials use `GRAFANA_URL`/`GRAFANA_AUTH`; datasource reader password uses sensitive `TF_VAR_clickhouse_password`. All generated state/backups must stay private and ignored. The default URLs assume Grafana is a native home service with access to loopback; a future isolated container requires explicit reviewed network wiring and budget before changing addresses. No public plaintext database or Grafana exposure is supplied.

The ClickHouse reader is `pathwatch_grafana`, created by the owned home lifecycle when `PATHWATCH_GRAFANA_CH_PASSWORD` is supplied. It is read-only with bounded query memory/time. Panels use the actual schema: raw primary-phase histogram for the latency distribution heatmap, `quantilesMerge` and `countMerge` across every batch for5min quantiles/sample counts, raw `phases_nanoseconds / 1e6` for stage milliseconds, and `path_events FINAL` for event identity deduplication. Stages such as TTFB/total overlap; they must not be stacked as additive phases. CH raw/panel views may include an in-flight/unacknowledged row; they are exploratory measurements and never the authoritative freshness SLO input.

`pathwatch.json` includes a multi-select path variable and both SLO error ratios from Prometheus. All time filters are measurement/window time; late replay appears at original time, while the independent SLO observer retains on-time arrival history.

Pinned query editor contract: https://github.com/grafana/clickhouse-datasource/blob/v4.23.2/src/types/sql.ts (`editorType=sql`, `queryType=timeseries/table`, sqlds format0/1).
