# Evidence

This directory holds the measured results that the [top-level README](../../README.md) cites:
one write-up per experiment (in Chinese) and the raw outputs under `raw/` and the
`task-*` directories. This page is the English summary. Numbers below were re-read
from the raw files, not copied from the write-ups.

Where things ran:

- **Azure**: short-lived resources in `westus` and `francecentral`, created and destroyed
  in the same session.
- **One machine**: a Linux workstation (Intel i9-13900K, 32 logical CPUs, Linux 7.0)
  that also ran unrelated projects during most measurements. Docker, kind and the
  benchmark all ran there.

## Azure regional networks (Azure, two regions)

[task-02-azure-cloud.md](task-02-azure-cloud.md), raw: [raw/task-02-azure/](raw/task-02-azure/)

Terratest `TestNetworkAzure` applied `infra/modules/network-azure` in westus (10.10.0.0/16)
and francecentral (10.20.0.0/16), checked the resulting ARM configuration (subnets, NSG
rules including default deny, route tables), and destroyed it through a delete-only plan.
Each region: 14 resources created and 14 deleted; PASS in 134.19 s and 153.27 s
([actual-results.json](raw/task-02-azure/actual-results.json)). Scope: network
configuration only. No VMs, data plane or cross-region traffic in this test.

## WireGuard + FRR failover (Azure westus to an on-prem router)

[vpn-failover.md](vpn-failover.md), raw: [run-b2ui6wy4](raw/task-03-azure/cloud-failover/run-b2ui6wy4/)
(`summary.json`, per-trial `fping.jsonl`, `routing.jsonl`, `before/after.json`)

An Azure NVA (FRR 8.4.4) and a router container on the workstation run two WireGuard
tunnels with one eBGP session each; the primary is preferred by local-pref. A trial drops
the primary tunnel's UDP 51820 in both directions (interfaces stay up) and measures the
time until the alternate route appears in the kernel (polled every 250 ms plus command
overhead) and the longest gap in a 100 ms ping stream between two private workloads.
Five trials per profile; medians, rounded up to the millisecond:

| Profile | Route switch (s) | Longest ping gap (s) |
|---|---:|---:|
| BGP keepalive 60 s / hold 180 s | 172.456 | 172.035 |
| BGP keepalive 3 s / hold 9 s | 8.535 | 8.251 |
| BGP 60/180 + BFD 300 ms x 3 | 1.201 | 0.839 |

Limits: both tunnels share one NVA, one host and one physical path, so this measures
failure detection, not high availability. Five trials per profile. The first campaign
failed (no route switch within 230 s; kept as `run-ppi84rri`): a one-way drop let the two
sides expire their hold timers at different times, and `no bgp graceful-restart` in FRR
8.4.4 leaves GR helper mode on. The passing run drops both directions and uses
`bgp graceful-restart-disable`.

## CUSUM threshold (simulation, one machine)

Reproduce (deterministic seed, about 2 s):
`PATHWATCH_ARL_EXPERIMENT=1 make go-test-one PKG=./ingest/internal/anomaly RUN=TestCUSUM_ThresholdExperiment`
([arl_experiment_test.go](../../ingest/internal/anomaly/arl_experiment_test.go)).

2,000 trials of 10,000 N(0,1) points per H, K = 0.5. The rule was fixed before the run:
take the smallest H whose probability of no false alarm in 10,000 windows is at least
0.95 with a known baseline, as long as a 6σ step is still caught within 3 windows on
average. Excerpt of the output:

| H | P(no false alarm), known baseline | mean false alarms / 10k | P(no false alarm), baseline from 20 windows | mean windows to detect 3σ / 6σ |
|---:|---:|---:|---:|---|
| 5 | 0.0000 | 10.620 | 0.1460 | 2.58 / 1.31 |
| 10 | 0.9375 | 0.065 | 0.6325 | 4.59 / 2.24 |
| 12 | 0.9875 | 0.013 | 0.7455 | 5.37 / 2.76 |
| 15 | 1.0000 | 0.000 | 0.8300 | 6.60 / 3.19 |

H = 12 is the ingest default. With the baseline estimated from 20 windows, as the job
does, the probability of no false alarm drops to 0.7455.

### After an alarm: latch, then small shifts (2026-11-01)

Raw: [task-09-cusum/latch-and-small-shifts-20261101.txt](task-09-cusum/latch-and-small-shifts-20261101.txt)
(run 2026-11-01 02:13Z; deterministic, so the machine load of 5.4 does not affect it);
reproduce with
`make go-test-one PKG=./ingest/internal/anomaly RUN='TestCUSUM_ThresholdExperiment|TestCUSUM_SmallShiftExperiment' PATHWATCH_ARL_EXPERIMENT=1`.

Rule, written down and tested (`TestCUSUM_SustainedShiftAlarmsOnceThenRearms`) before
measuring: after an alarm the detector **latches** instead of resetting the sum to 0. The
sum keeps accumulating, clamped at H, no further alarm is raised, and the detector re-arms
when the sum is back at 0, which takes at least H/K = 24 baseline windows (2 hours). A
lasting shift alarms once. Resetting to 0 re-alarmed every few windows for as long as the
shift lasted (every window in the 20 ms lab run); re-learning the baseline after an alarm
would accept the shift as the new normal and leave the path without detection for a new
20-window warm-up.

Small shifts, same design (real `CUSUM` code, fixed seed, 5,000 trials), with the job's
settings: warm-up 20 windows, sigma floor 1 ms, K 0.5, H 12, 36 windows (3 hours)
observed after the step. Because of the 1 ms floor a shift of d ms is d standardized units
per window. Two noise models: `lab` resamples the 21 real pre-switch TCP p95 windows of
the path-change lab (0.26-0.53 ms, far below the floor), `gauss1` is Gaussian noise with
σ = 1 ms (as large as the floor).

| Shift | windows to alarm, lab: mean (p95, max) | windows to alarm, gauss1: mean (p95, max); detected in 36 | alarms in 36 windows, latch / old reset (lab) |
|---:|---|---|---|
| 1 ms | 24.53 (27, 30) | 21.51 (34, 36); 72.2% | 1 / 1 |
| 2 ms | 8.51 (9, 9) | 9.60 (15, 30); 99.98% | 1 / 4 |
| 3 ms | 5.00 (5, 6) | 5.79 (8, 12); 100% | 1 / 7.0 |
| 6 ms | 3.00 (3, 3) | 2.87 (4, 5); 100% | 1 / 12 |
| 20 ms | 1.00 (1, 1) | 1.00 (1, 2); 100% | 1 / 36 |

With 5-minute windows a 2 ms shift is reported after about 45 minutes and a 3 ms shift after
about 25 minutes on lab-like noise; the 20 ms lab step was reported in the first window,
as in the replay.

False-alarm side at H = 12, again. The threshold table above, re-run with the latching
rule and the same seed, gives identical P(no false alarm) and detection delays (the first
alarm does not depend on what happens after it); mean false alarms per 10,000 windows with
a 20-window baseline fall from 5.862 to 3.075 (known baseline: 0.013, unchanged). On
resampled lab windows: 0 false alarms in 200 x 10,000 windows (the noise never exceeds
K x floor = 0.5 ms above the baseline). With σ = 1 ms noise: P(no false alarm in 10,000
windows) 0.81, mean 1.185 alarms with the latch against 2.400 with reset.

Limits: the lab noise comes from 21 windows of one Docker path; resampling them assumes
independent windows. Internet paths with noise near or above the floor behave like
`gauss1` or worse. The latch has not yet been exercised in a new live recording.

## Path-change detection replay (Docker lab, one machine)

[task-09-replay.md](task-09-replay.md), raw: [task-09-replay/replay.json](task-09-replay/replay.json)

`lab/pathchange` runs probe → r1 → (r2 | r3) → target on Docker networks; r3 adds 20 ms
of netem delay. The probe container has no capabilities and uses the unprivileged
UDP + `IP_RECVERR` traceroute mode. After about 1 h 46 min of baseline, r1's next hop
was changed from r2 to r3 at 15:02:00Z. The 1,448 recorded spool batches were replayed
through `store.Commit` into a ClickHouse test container and the anomaly job ran with the
ingest defaults (warm-up 20 windows, K 0.5, H 12, sigma floor 1 ms).

- 1,444 TCP and 1,444 traceroute results, 0 traceroute errors. Path snapshots: 1,267 on
  the old path, 177 on the new one; the first trace after the switch (0.68 s later)
  already showed the new hop.
- TCP connect p95 per 5-minute window: baseline mean 0.358 ms (20 windows); the 15:00
  window 20.633 ms → `path_events` row with `path_changed=true`, `first_divergent_ttl=2`.
  No event in the 21 windows before the switch.
- The alarm repeated in the following windows (15:05, 15:10): at the time the detector
  reset its sum after each alarm. Since 2026-11-01 it latches instead (section above), which
  would give one alarm for this step.
  With the job running every 5 minutes, a live alert would fire about 3 minutes after
  the switch.

Limits: three hops, a large step (about 57 times the baseline), no loss or ICMP rate
limiting. Not an internet path.

## Ingest outage and backfill (one machine)

[ingest-outage.md](ingest-outage.md), raw: [raw/task10-real-outage-20261024/](raw/task10-real-outage-20261024/)

One probe, ingest, ClickHouse and Prometheus on the workstation. From a baseline with no
page firing, ingest was stopped for 1,200.4 s. Both burn-rate pages fired (readiness after
30.436 s, freshness after 605.464 s). After restart the probe's spool drained: 11 batches /
70 results committed, 0 duplicate `(batch_key, result_index)` rows, 0 spool drops
([controller-proof.json](raw/task10-real-outage-20261024/controller-proof.json),
[result.json](raw/task10-real-outage-20261024/result.json)).
This is a single-probe drill, not an SLO result: the cumulative SLI counters at the end
showed 15 of 19 windows bad.

## Edge proxy vs Envoy and NGINX (one machine)

[proxy-bench.md](proxy-bench.md), raw: [raw/task13/](raw/task13/), harness: [bench/](../../bench/)

Fixed-rate load through TLS 1.3 to one Go HTTP/1.1 echo backend (1,024-byte body).
Each proxy gets 2 workers pinned to the same two cores; the load generators, backend and
collector are pinned to other cores. 100 downstream connections (H1: wrk2, one request in
flight per connection, latency corrected for coordinated omission; H2: h2load, up to 16
streams per connection). 4 rates x 5 rounds x 120 s per proxy and protocol, 15 s warm-up,
proxy order rotated each round. Latency is the median over the 5 rounds of each
percentile. Envoy 1.39.5, NGINX 1.30.6. All error counters were 0 in every cell.

Same-run comparison, after the TCP_NODELAY fix (H2 on 2026-10-27; H1 rounds 0-2 on
10-27 and rounds 3-4 on 10-28, about 3.5 h later). p50 in ms, CPU in µs per completed
request:

| Protocol | Rate | p50 PathWatch / Envoy / NGINX | CPU PathWatch / Envoy / NGINX |
|---|---:|---|---|
| H2 | 1k | 0.520 / 1.078 / 0.135 | 64.18 / 115.06 / 53.30 |
| H2 | 10k | 0.318 / 0.861 / 0.108 | 50.27 / 83.90 / 45.27 |
| H2 | 20k | 0.614 / 2.200 / 0.197 | 42.53 / 72.57 / 36.24 |
| H1 | 1k | 0.940 / 1.140 / 0.900 | 64.80 / 117.21 / 49.58 |
| H1 | 10k | 1.070 / 1.750 / 0.980 | 40.22 / 80.04 / 32.82 |
| H1 | 20k | 1.350 / 3.420 / 1.160 | 36.77 / 74.80 / 29.84 |

All four rates, p99 and p99.9: [sameday-20261027-h2-summary.md](raw/task13/sameday-20261027-h2-summary.md),
[sameday-h1-summary.md](raw/task13/sameday-h1-summary.md). Envoy's H1 p99 at 20k was
948.22 ms; the cause was not investigated.

TCP_NODELAY: the first full run (2026-10-25, [matrix01-summary.md](raw/task13/matrix01-summary.md))
showed a 10 ms H2 p50 plateau. Accepted downstream sockets did not set `TCP_NODELAY`,
so with several streams in flight Nagle's algorithm held responses until the client's
delayed ACK. After the fix, PathWatch H2 p50 went from 1.867 / 5.040 / 10.168 / 10.376 ms
to 0.530 / 0.480 / 0.342 / 0.658 ms at 1k / 5k / 10k / 20k
([h2-nodelay-20261027-b-summary.md](raw/task13/h2-nodelay-20261027-b-summary.md)).
The two runs were on different days under different background load.

A later 30-second diagnostic ([diag-20261028-h2-workers-2-summary.md](raw/task13/diag-20261028-h2-workers-2-summary.md))
ran the same binary with 1 instead of 2 tokio workers: H2 p50 at 1k went from 0.455 to
0.216 ms (NGINX 0.146) but p99 went from 3.166-3.167 ms to 10.664-10.671 ms.

A per-core prototype (`[runtime] mode = "per_core"`: two current-thread runtimes with
`SO_REUSEPORT` listeners and one upstream pool each) was compared with the default
multi-thread runtime in a 30-second H2 diagnostic on 2026-10-31
([diag-20261031-h2-percore-2-summary.md](raw/task13/diag-20261031-h2-percore-2-summary.md)).
It was not better: p50 at 1k / 5k / 10k / 20k was 0.394 / 0.338 / 0.320 / 0.866 ms against
0.197 / 0.381 / 0.321 / 0.314 ms, and p99 at 20k was 31.09 against 4.14 ms, although CPU per
request was 6-13% lower. The multi-thread runtime remains the default and the full matrix
was not re-run with the prototype.

Limits: one backend, so this compares per-request proxy overhead, not load-balancing
algorithms. Background load from other projects (median 1-minute load average 5.08 during
the H2 measurement windows, 7.03 and 7.92 during the two H1 parts, on 32 CPUs) means results are comparable only
within one run. Re-running `bench/analyze.py` on the retained raw cells reproduces the
summary files exactly.

## Canary rollback with Argo Rollouts (kind, one machine)

[canary-rollback.md](canary-rollback.md), raw: [task-14-canary/](task-14-canary/) (per run:
`summary.json`, `timeline.jsonl`, `querygen.jsonl`, `analysisruns.json`)

Single-node kind cluster (Kubernetes 1.36.4) with Argo CD 3.5.6 (core), Argo Rollouts
1.10.1 and Prometheus 3.14.0; desired state comes from a local Git repository. A client
sends 20 requests/s through the Rust edge-proxy sidecar to the query API. The bad build
returns HTTP 500 for every query. The canary fails if more than 5% of the candidate's
requests fail, or if it served fewer than 5. Runs on 2026-10-28 (4 replicas, first step
25%, analysis after 20 s then every 10 s); times are seconds after the desired-state commit:

| Run | Result | Time (s) | Client non-200 / sent |
|---|---|---:|---|
| bad500 #1 | rolled back | 22.9 | 117 / 640 |
| bad500 #2 | rolled back | 22.7 | 105 / 620 |
| bad500 #3 | rolled back | 22.1 | 98 / 620 |
| control (good build, new tag) | promoted | 75.4 | 17 / 1560 |

`git revert` of the bad commit brought all apps back to Synced/Healthy in 2.7-2.8 s.
The earlier configuration (2 replicas, 50%, 40 s initial delay; 2026-10-27) took
41.7-43.1 s to roll back. The NetworkPolicy check passed 9 of 9 reachability probes with
numeric ports ([phase4/netpol-numeric-ports.json](task-14-canary/phase4/netpol-numeric-ports.json)).

2026-11-01 rerun (kind again; measured on a shared machine with 1-minute load 10.5-15.7
on 32 CPUs). The frontend now has a 10 s `preStop` sleep: pod deletion had removed the
endpoint and stopped the query listener at the same moment. The client also sends 2
Report RPCs/s over one long-lived gRPC connection.

| Run | Result | Time (s) | Query non-200 / sent | Report non-OK / sent |
|---|---|---:|---|---|
| bad500 (5 runs) | rolled back | 22.0, 22.3, 22.3, 23.4, 23.5 | 86-98 / 600-640 (one run 194 / 640) | 0/60, 0/64, 38/64 (3 runs invalid, journal full) |
| control (3 runs) | promoted | 74.0, 74.1, 75.0 | 2 / 1600, 0 / 1600, 1 / 1620 (all 429) | 0/160, 127/162 (1 run invalid) |

The control-run scale-event errors are gone (17 → at most 2, and those are admission 429s
also seen in steady state). New, not fixed: a Report connection pinned to a pod that is
being deleted gets `Unavailable` for about 63 s, from SIGTERM until the pod exits. The
edge-proxy sidecar keeps the client connection open after the frontend has stopped. The
lab's never-evicting core journal filled after about 10 h of Report traffic, which
invalidated three runs' Report counts. Details are in canary-rollback.md.

Limits: kind, not k3s or AKS. The egress rules added to the NetworkPolicy have not been
through `make kind-netpol`; the drills ran with them and the traffic flowed.

## Anycast and BGP incident detection (Docker networks, one machine)

[bgp-lab.md](bgp-lab.md), raw: [task-17-bgp/](task-17-bgp/)

Nine FRR 10.7.1 routers on plain Docker networks (not containerlab): three PoPs announce
the same /24, two transit ASes, three customer ASes, and a GoBGP 4.10.0 collector. A
Krill 0.16.0 test trust anchor signs a ROA and Routinator 0.15.3 validates it over RRDP.

- `test_anycast_nearest_pop` passed: c1 → pop1, c2 → pop2, c3 → pop3
  ([nearest-final.json](task-17-bgp/nearest-final.json)).
- Route leak, 5 runs: detected 5/5, first alert 0.167-0.415 s after injection (median 0.178 s).
- /25 hijack, 5 runs: detected 5/5 as RPKI-invalid, 0.178-0.426 s (median 0.179 s).
- 0 alerts in the 5 s baseline before each injection.

Limits: the detector polls every 0.25 s, so these times are mostly polling interval and
CLI overhead; they say nothing about internet propagation. Leak detection uses the known
AS relationships of this topology. Routers detect but do not filter RPKI-invalid routes.
