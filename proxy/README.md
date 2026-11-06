# Rust L7 proxy core

Run from the checkout (Rust 1.99.1):

```sh
source scripts/dev-env.sh
cargo run --locked --manifest-path proxy/Cargo.toml -p edge-proxy -- config.toml
```

Minimal configuration:

```toml
listeners = [{ addr = "127.0.0.1:8080" }]
routes = [{ host = "api.example.test", path_prefix = "/", cluster = "api" }]
clusters = [{ name = "api", endpoints = ["http://127.0.0.1:9000"], lb = "least_request" }]

[pool]
max_connections = 4
request_timeout_ms = 10000
drain_timeout_ms = 5000
```

For TLS termination, a listener accepts `tls = { certs = [{ sni =
"api.example.test", cert = "cert.pem", key = "cert.key" }] }`. Paths are relative
to the process working directory. Every certificate must match its SNI and key;
unknown SNI fails the TLS handshake. ALPN selects HTTP/2 or HTTP/1.1, with HTTP/1.1
as the no-ALPN fallback. Plain listeners detect HTTP/1.1 and prior-knowledge h2.

Upstream `http://host:port` selects HTTP/1.1; `h2c://host:port` selects explicit
prior-knowledge HTTP/2. Ports are mandatory. Upstream TLS is unsupported and
rejected. Requests only reach configured endpoints; CONNECT and upgrades return
501. Hosts match case-insensitively, ignoring the optional port and final dot.
The longest literal path prefix wins; duplicate normalized host/prefix rules are
rejected. No URL decoding or segment-boundary matching occurs. Path and query are
preserved, and Host is rewritten to the selected upstream authority. HTTP/1.1
Host and absolute URI/h2 authority must agree on the normalized hostname.

`max_connections` bounds all active and idle physical upstream sockets globally.
Each exchange leases a slot until response EOF, cancellation, or its total request
deadline. Reuse additionally requires upload completion: if the upstream finishes
its response while upload is unfinished, the remaining upload and that upstream
connection are canceled immediately. The already published response is preserved.
A socket also owns a physical permit until it closes. Slots can evict
idle sockets for another endpoint. HTTP/2 upstream requests currently serialize
per slot; multiplexing across downstream streams remains supported. The request
deadline includes pool wait, DNS/connect, upload, headers and response streaming;
a watchdog releases stalled body leases even under downstream flow control.
A per-connection executor tracks and cancels Hyper's transport, upload and dispatch
tasks together. Its non-cloned owner closes task admission before awaiting cleanup;
retained downstream bodies cannot keep an abandoned upstream socket alive.
Upstream failures return 502, pre-header deadlines return 504, and late body
failures terminate the stream. No request is implicitly retried.

`health = {}` enables active probes with `/healthz`, 5-second intervals,
1-second total probe deadlines, three consecutive failures to become unhealthy,
and two consecutive successes to recover. The existing explicit health fields
can override these defaults. There is one sequential probe worker per cluster;
it shares the global physical socket pool and discards response frames without
aggregating them. A saturated pool can cause a probe timeout. Probes do not mint
request/retry credit. Passive health is always enabled: five consecutive upstream
5xx responses eject that endpoint for 30 monotonic seconds. A non-5xx resets the
consecutive count. Active success does not shorten passive ejection.

Clusters accept `circuit = { max_requests = 128, max_pending = 128,
request_timeout_ms = 10000 }`. The timeout is optional and otherwise inherits
`pool.request_timeout_ms`. Admission rejects excess pending work with 503;
`max_pending = 0` disables waiting for a cluster slot. An active original holds
its circuit permit and selected endpoint count through body EOF, drop, or the
total deadline, including upstream socket wait. Neither response headers nor a
retry release the original's circuit slot.

`lb = "least_request"` compares two distinct eligible endpoints and reserves
the less busy one. `lb = "maglev"` hashes the original path and query, selecting
only eligible endpoint IDs. Health-set changes rebuild its table. Empty eligible
sets return 503. No caller-provided label enters metrics.

Retries are limited to one extra attempt for GET/HEAD whose incoming body is
already definitely empty. Only transport failures or upstream 502–504 responses
qualify; a consumed/streaming body and POST are never replayed. Both attempts
share the original total deadline. Hyper's low-level senders have no hidden retry.
Two independent retry limits apply per cluster:

- Within a fixed monotonic one-second bucket, retry reservations are at most
  `max(3, floor(admitted_originals_in_this_bucket / 5))`.
- Concurrent retry leases are at most
  `max(3, floor((active_originals + pending_originals) / 5))`.

Each admitted caller contributes once, including accepted pending work. Local
circuit rejection and retry attempts contribute no original credit. Retry leases
remain live through response completion/drop/deadline. Bucket credit never
accumulates, and an outstanding retry still counts against concurrency after a
bucket rolls over. The three-per-bucket floor explicitly exempts low load from a
cumulative 20% ratio; this is not a strict sliding-window rate (up to six floor
reservations can straddle a bucket boundary). The high-load acceptance test
measures the original-request denominator separately from physical attempts.

SIGHUP rereads the same configuration file, validates the entire configuration
and every certificate/key, and atomically swaps the generation on the same
bound listeners. Invalid candidates retain the working generation. Existing
HTTP/1 connections stop keep-alive admission and HTTP/2 connections drain through
GOAWAY; their active requests can finish for up to 30 seconds, then are canceled.
New connections receive the new routing/TLS configuration. Per-name cluster
admission, retry buckets, counters and stable-endpoint loads survive overlapping
generations. Validated new limits apply before publication; a lower limit can
reject new admissions until existing work drains. Management includes live
retired clusters, and weak registries prune entries once their generations and
exchanges are gone. Passive ejection and unchanged active-health history/probe
deadlines survive reload. Active-policy changes reset only active history;
current-generation probes start after the old workers have stopped.
The global physical
pool remains shared; bound listener addresses/order, the management address,
and `max_connections` cannot change on reload and require a restart. SIGINT
or SIGTERM stops accepts and drains until the startup `drain_timeout_ms`, then
cancels work.
Startup validates all certificates and binds all sockets before starting workers;
partial failure releases already acquired sockets.

An optional `[runtime]` section picks the threading model at startup; a reload
cannot change it. The default `mode = "multi_thread"` is one work-stealing tokio
runtime (`workers` sets its thread count; unset, tokio's default honours
`TOKIO_WORKER_THREADS`). `mode = "per_core"` is a prototype: `workers` (default:
available parallelism) current-thread runtimes, each binding the listeners with
`SO_REUSEPORT` and owning its own upstream pool, so `max_connections` applies per
runtime and the process total is `workers` times that. In this mode each runtime
also runs its own health probes, only the first runtime binds the management
listener (its metrics cover that runtime only), and SIGHUP is read by the first
runtime and published to the others.

An optional `management = { addr = "127.0.0.1:9091" }` at the TOML root enables
HTTP/1 `GET /metrics` on a distinct loopback listener. It accepts at most 16
concurrent connections with a five-second lifetime. Metrics use fixed configured
cluster names and hexadecimal stable endpoint IDs: admitted `requests_total`,
`retries_total`, `rejected_total`, `errors_total`, `active_requests`,
`pending_requests`, `endpoint_healthy`, plus process reload success/failure totals,
all prefixed `pathwatch_proxy_`. Errors count each admitted caller at most once
when it encounters a transport/status/body error (even if a retry later succeeds).
`POST /drain` puts the process in drain mode (`pathwatch_proxy_draining 1`), which
lasts until it exits: the listeners keep accepting and serving, but every downstream
connection is shut down gracefully once it has served a request (HTTP/2 GOAWAY,
HTTP/1.1 `Connection: close`), including connections accepted later. Clients
reconnect, through the Service, to another pod. A pod's preStop hook calls it when
deletion starts, because a native sidecar only receives SIGTERM after the app has
exited. Management exposes no configuration write API. This task does not add the proxy
to the existing three-target Prometheus scrape profile.

Maglev's version-1 interoperability contract is in `lb`: unsigned stable u64
endpoint IDs, sorted and deduplicated, a 65,537-entry table, SplitMix64 finalizer
for `offset = mix(id) % 65537`, `skip = 1 + mix(id XOR 0x9e3779b97f4a7c15) % 65536`,
and `mix(key) % 65537` lookup. Endpoints derive IDs from FNV-1a64 of UTF-8
`scheme://lowercase-authority`; indexes are never identities. The table exports
u64 IDs; serialize IDs/flow-key integers in big-endian byte order at the kernel
boundary, preserving the declared field ordering of the future flow tuple.
Task 16 must reuse this table/hash contract; no kernel parity is claimed here.

Tests use owned ephemeral loopback endpoints and temporary private-key directories.
The real reload drill verifies 20 upstream-active five-second requests before
SIGHUP, split across HTTP/1 and HTTP/2. Paused clocks cover exact 30-second health
and forced-drain boundaries; they do not replace the real elapsed drill. These
checks make no production capacity, benchmark, cloud availability or SLO claim.
