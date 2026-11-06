# PathWatch

PathWatch measures network paths and serves traffic at the edge. Go probes run DNS, TCP,
TLS, HTTP/1.1-3 and traceroute measurements, buffer results on disk and report them to an
ingest service that deduplicates them by batch and stores them in ClickHouse. A CUSUM job
flags latency shifts per path and checks traceroute snapshots to say whether the route
changed and at which hop. A Rust L7 edge proxy (TLS/SNI, HTTP/1.1 and HTTP/2, connection pooling, load
balancing, health checks, retry budgets, hot reload) fronts the service. Underneath sit
Terraform-managed Azure networks and a WireGuard + FRR (BGP/BFD) link to an on-prem
router. The goal was to build each layer and measure it under failure: route
convergence, outage backfill, proxy overhead against Envoy and NGINX, canary rollback,
and BGP incident detection.

## Architecture

```
  probe (Go)                         ingest (Go)                        ClickHouse
  dns/tcp/tls/http1-3/trace          frontend ──mTLS──▶ core ──HTTP──▶  probe_results
  scheduler, durable spool  ─gRPC──▶ (stateless)        journal,        path_quantiles_5m
  retries until acked        (TLS)                      CUSUM job ───▶  path_events
                                                           │
                                                           └──▶ Alertmanager
  edge-proxy (Rust)  TLS/SNI, H1/H2, pooling, P2C least-request / Maglev, active and
                     passive health checks, retry budget, SIGHUP reload
                     (sidecar in front of the ingest frontend in the Kubernetes deployment)

  Azure (Terraform)  westus VNet ── NVA (FRR) ══ 2 WireGuard tunnels, eBGP (+BFD) ══ on-prem router
                     francecentral VNet          Private DNS, Traffic Manager (partial)
  Kubernetes         Helm charts, Argo CD app-of-apps, Argo Rollouts canary with a
                     Prometheus error-rate analysis (run on kind)
```

## Results

All numbers were re-read from the raw outputs; the [evidence index](docs/evidence/README.md)
gives the setup, method and limits of each. "One machine" is a single Linux workstation
(i9-13900K, 32 logical CPUs) that was also running unrelated workloads.

| What | Where it ran | Result | Evidence |
|---|---|---|---|
| Azure regional networks | Azure, westus and francecentral | Terratest created, checked and destroyed 14 network resources per region; PASS in 134.19 s and 153.27 s | [Azure networks](docs/evidence/README.md#azure-regional-networks-azure-two-regions) |
| BGP failover over WireGuard | Azure westus NVA ↔ on-prem router container, one physical path | Median time to switch to the backup route, 5 trials each: 172.456 s (BGP 60/180 s), 8.535 s (3/9 s), 1.201 s (BFD 300 ms x 3) | [failover](docs/evidence/README.md#wireguard--frr-failover-azure-westus-to-an-on-prem-router) |
| CUSUM threshold | Simulation, one machine | H = 12 chosen by a rule fixed in advance: P(no false alarm in 10,000 windows) = 0.9875 with a known baseline, 0.7455 with a 20-window baseline; a 6σ step is caught in 2.76 windows on average | [CUSUM](docs/evidence/README.md#cusum-threshold-simulation-one-machine) |
| Path-change detection | Docker lab, one machine | Real route change: alarm in the first 5-minute window (TCP p95 20.633 ms vs 0.358 ms baseline) with `path_changed=true` at TTL 2; no alarm in the 21 windows before | [replay](docs/evidence/README.md#path-change-detection-replay-docker-lab-one-machine) |
| Ingest outage | One machine, one probe | 1,200.4 s outage: both burn-rate pages fired; 70 results in 11 batches backfilled, 0 duplicates, 0 spool drops | [outage](docs/evidence/README.md#ingest-outage-and-backfill-one-machine) |
| H2 latency fix (`TCP_NODELAY`) | One machine | PathWatch H2 p50 at 10k rps: 10.168 ms → 0.342 ms (runs on different days) | [proxy bench](docs/evidence/README.md#edge-proxy-vs-envoy-and-nginx-one-machine) |
| Proxy vs Envoy and NGINX, same run | One machine, one backend, 5 rounds x 120 s per cell | p50 at 10k rps, PathWatch / Envoy / NGINX: H2 0.318 / 0.861 / 0.108 ms, H1 1.070 / 1.750 / 0.980 ms. CPU per request at H1 10k: 40.22 / 80.04 / 32.82 µs | [proxy bench](docs/evidence/README.md#edge-proxy-vs-envoy-and-nginx-one-machine) |
| Canary rollback | kind (single node), one machine | Build returning HTTP 500 rolled back 22.1-22.9 s after the commit (3 runs); clients saw non-200 responses for about 22 s, 15.81-18.28% of the requests in each drill window; good build promoted at 75.4 s | [canary](docs/evidence/README.md#canary-rollback-with-argo-rollouts-kind-one-machine) |
| Report RPCs on pod deletion | kind (single node), one machine, 5 deletions each | Deleting the frontend pod that holds the client's long-lived HTTP/2 connection failed 160-161 of 212-214 Reports over 81 s each time; after the edge-proxy drain fix (GOAWAY from the preStop hook), 0 of 50-54 | [canary](docs/evidence/README.md#canary-rollback-with-argo-rollouts-kind-one-machine) |
| Anycast and BGP incidents | Docker networks, one machine | Each client reaches its nearest of 3 anycast PoPs; route leak detected 5/5 in 0.167-0.415 s, /25 hijack 5/5 as RPKI-invalid in 0.178-0.426 s (0.25 s polling) | [BGP lab](docs/evidence/README.md#anycast-and-bgp-incident-detection-docker-networks-one-machine) |

What the proxy numbers do and do not say: in the same-day run PathWatch had lower p50 and
p99 than Envoy at all four rates (1k-20k rps) on both protocols. Against NGINX its HTTP/1.1
p50 was close (0.940-1.350 ms vs 0.900-1.160 ms), its HTTP/2 p50 clearly higher
(0.318-0.614 ms vs 0.108-0.197 ms), and its CPU per request higher in every cell. There is
a single backend, so load-balancing algorithms are not compared.

## Running it

Linux amd64 with Python 3, [uv](https://docs.astral.sh/uv/), make and curl. Toolchains
are installed inside the checkout (`.tools/`, caches in `.cache/`), nothing system-wide.

```sh
make tools                                # Go 1.27.3, Python 3.12 venv, terraform, tflint
python3 scripts/bootstrap-tools.py --rust # Rust 1.99 toolchain
make test     # offline suites: Go, Rust (proxy, xdp), Python, terraform test (mocks), proto check
make check    # gofmt, go vet, cargo fmt and clippy -D warnings, terraform fmt
```

Suites that need Docker or a live environment are skipped unless switched on:

```sh
PATHWATCH_TEST_DOCKER=1 make test-go      # ingest against a ClickHouse testcontainer
make deploy-check                         # helm lint + strict kubeconform of the charts
make lab-up lab-rpki lab-test lab-down    # BGP lab on Docker networks (lab/bgp)
make t9-probe-bin t9-up t9-start          # path-change lab (lab/pathchange), then t9-switch, t9-down, t9-replay
make kind-up kind-images kind-publish     # kind + Argo CD + Rollouts; full drill sequence in deploy/README.md
make bench-matrix PROTOCOL=h2 PROXY=pathwatch OUTPUT=new-run   # proxy benchmark, after bench/setup.sh
```

Each component README has the details: [probe](probe/README.md), [ingest](ingest/README.md),
[proxy](proxy/README.md), [deploy](deploy/README.md), [bench](bench/README.md),
[lab/bgp](lab/bgp/README.md), [lab/pathchange](lab/pathchange/README.md),
[infra](infra/README.md). The Azure parts (`make up ENV=...`) need a subscription and are not
part of the local workflow.

## Repository layout

| Path | Contents |
|---|---|
| `probe/` | Go probe agent: measurement kinds, scheduler, spool, report client |
| `ingest/` | Go ingest: gRPC server, ClickHouse store and journal, CUSUM job, query API, frontend/core split |
| `proto/` | protobuf definitions and generated Go code |
| `proxy/` | Rust edge proxy (`config`, `lb`, `health`, `edge-proxy` crates) |
| `xdp/` | Rust offline Maglev planner and packet parsing for a future XDP load balancer |
| `infra/` | Terraform modules and environments for Azure, Terratest |
| `ansible/` | probe role and the NVA (WireGuard + FRR) playbook |
| `deploy/` | Helm charts, Argo CD apps, Rollouts canary, kind lab |
| `lab/` | Docker labs: BGP/anycast, path change, on-prem side of the hybrid link |
| `drills/` | failover and outage drill scripts |
| `bench/` | proxy benchmark harness |
| `slo/`, `dashboards/` | Sloth SLO definitions and Prometheus rules, Grafana dashboard (Terraform) |
| `scripts/` | tool bootstrap, Azure cost guard and reaper, drill runtimes |
| `docs/evidence/` | write-ups and raw outputs behind the results above |
| `docs/dev/` | detailed per-component development notes (mostly Chinese) |

## Not done and limitations

Partly done:

- **Remote state and CI**: Terraform for an Azure Storage state backend, a GitHub OIDC
  identity and a CI workflow exist; the workflow has never run and OIDC login is unverified.
- **Private DNS / Traffic Manager**: the only live DNS failover drill failed (Azure rejected
  two VNet links) and was not repeated; cross-site resolution and failover time are not
  measured.
- **Probe deployment, SLOs, dashboards**: one probe on one machine. No Azure probe was
  deployed, there is no multi-week run or weekly SLO report, and a capacity check did not pass.
- **Kubernetes**: charts, Argo CD and the canary run on kind only. No k3s or AKS cluster, no
  Terraform-managed Helm releases.
- **XDP load balancer**: only the offline Maglev planner, packet parsing and atomic update
  logic (19 tests). No eBPF program, no namespace tests, no IPVS comparison.

Not started: upgrade under load, region failover with Traffic Manager, drift detection.

Limitations of the results above:

- The two WireGuard tunnels share one NVA, one host and one physical path; the failover
  drill measures failure detection, not high availability.
- The path-change lab has three hops on Docker networks and a large latency step. Since
  2026-11-01 a detector latches after an alarm (one alarm per lasting shift); this rule is
  measured in simulation and on resampled lab windows, not yet in a new live run.
- HTTP/2: after the `TCP_NODELAY` fix PathWatch's p50 is still well above NGINX's (0.318 vs
  0.108 ms at 10k rps) and the cause is not located. One tokio worker instead of two cut
  the 1k p50 from 0.455 to 0.216 ms but raised p99; an optional one-runtime-per-core mode
  was benchmarked and was not better (worse p50 and p99 at 20k rps).
- Benchmarks and labs ran next to other workloads; compare numbers only within one run.
- 1 of 9 runs of the ClickHouse integration suite failed after a container restart
  (ClickHouse stopped answering HTTP); the cause was not found, and the other 8 runs passed.
- Canary: the good-build control run's 17 non-200 responses out of 1,560 came from pod
  deletion removing the endpoint and sending SIGTERM at the same time; a 10 s preStop sleep
  removed them (later controls 2/1,600, 0/1,600 and 1/1,620, all 429). One control drill
  after the Report drain fix had a 3 s burst of query errors (41 transport errors, 60 429s),
  as one 11-01 run had before it; its cause was not investigated. Egress NetworkPolicy
  rules were added after the drills and have not been through one.
- BGP detection times are dominated by the 0.25 s polling interval and say nothing about
  internet propagation; routers detect but do not filter RPKI-invalid routes.
