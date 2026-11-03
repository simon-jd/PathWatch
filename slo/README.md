# Task10A SLO contract

The canonical physical roster is a JSON mapping of probe IDs to **distinct actual sites**. Use `infra/modules/clickhouse/roster.home.json` for local verification. Future A/home/B roster changes require a new explicitly recorded observation epoch/state; two processes on home are never two physical observers.

* Freshness objective: **99% of closed UTC tumbling five-minute windows** contain at least `max(1,N-1)` configured observers with a measurement started in that window and a successful new durable ingest commit in that same window. Remote measurement failures are still real ingested measurements. The `max(1,...)` home-only diagnostic is stronger than vacuous N−1=0 and does not constitute Azure multisite acceptance.
* Ingest-readiness API objective: **99.5%** of independent scheduled 15-second `GET /readyz` observations succeed. The endpoint uses the existing Store's gate, rejects closed/frozen/pending states and verifies the ClickHouse epoch. It measures this current administrative API, not every gRPC Report call or a future Rust/query API. Bounded loopback requests supply a nonzero denominator even without application traffic.
* Only accepted new durable commits update the volatile two-window summary. Duplicate ACKs, late backfill, future timestamps, raw/MV unacknowledged visibility and process-up signals cannot create freshness. Summary loss on ingest restart can conservatively fail a window. Polling can miss a commit at a window boundary and conservatively fail it; it never repairs a closed window later.
* The observer lives outside ingest, fsyncs counters and current-window state, and counts missed 15-second slots and closed windows after a restart as failures. Missing/stale metrics or no completed observation are errors in Sloth queries. A last-closed-window gauge handles the five-minute counter boundary without inventing zero-traffic success. Prometheus itself being down prevents live alert evaluation; it is not evidence of health, and the persisted observer history remains authoritative for reporting.

`pathwatch.yaml` is the Sloth source. `generated.rules.yaml` is generated using **Sloth v0.13.0**, Linux amd64 SHA256 `8fb845e6f1ab2c0af2b48e0b4849b42b72ecb422fadabc8a800d3d6deeca3ff2`. Obtain that exact release from https://github.com/slok/sloth/releases/tag/v0.13.0 and verify its checksum before use. Generate with `.tools/bin/sloth generate -i slo/pathwatch.yaml -o slo/generated.rules.yaml`.

Prometheus/promtool is pinned to **v3.14.0**, image `prom/prometheus@sha256:5ce7540c3c00ef4ab0c9d2c995c6a5b9c421f44b4a115d97a2c7af3b1c21cbb0`. Reuse the existing shared image read-only; never retag/delete it. The home supervisor runs its native `/bin/prometheus` copied into project `.tools/bin/prometheus`, so loopback scraping does not require a container host network namespace. Count both the full image and copied 142 MB binary in the quota.

```sh
python3 -m unittest discover -s slo/tests -v
DOCKER_CONFIG="$PWD/.cache/docker" docker run --rm --network none \
  --label project=pathwatch --label owner=codex-pathwatch --label task=10 \
  --memory 256m --cpus 1 --read-only --tmpfs /tmp:rw,size=32m --cap-drop ALL \
  -v "$PWD/slo:/work:ro" -w /work --entrypoint /bin/promtool \
  prom/prometheus@sha256:5ce7540c3c00ef4ab0c9d2c995c6a5b9c421f44b4a115d97a2c7af3b1c21cbb0 \
  test rules tests/rules.test.yaml
```

The rule tests include healthy intervals, the counter-boundary regression, missing roster members, ingest outage, missing telemetry, recovery and a **simulated** 20-minute multi-window page/ticket burn. They do not prove an elapsed real outage or delivered external notifications. There are no external receivers configured.

For weekly evidence, copy the durable `slo-state.json`, its physical roster, current UTC observation time, actual start time, last observed slot, `windows_total/windows_bad`, `api_total/api_bad`, and service/source hashes into an append-only dated entry in `docs/evidence/slo-weekly.md`. Use deltas against the previous snapshot, explicitly include missed slots, partial initial windows and monitoring gaps, and do not label a partial observation period as a week. No scheduled external action is installed by this task.
