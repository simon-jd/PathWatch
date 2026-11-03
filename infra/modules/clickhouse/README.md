# Dedicated home ClickHouse and ingest (Task10A)

This directory implements the Azure/home override: **no cloud ClickHouse**, no Compose platform, no host service/sysctl changes. `home.py` owns one labelled ClickHouse container plus native ingest, probe, SLO observer, optional owned echo and Prometheus child processes. UNIX control socket mode0600 accepts only pause/resume/status/snapshot/shutdown; pause stops only this supervisor's ingest child. Normal process SIGINT/SIGTERM and errors stop owned children and remove only the owned container. Data, journal, spool and logs remain for audit/recovery.

Prerequisites: existing pinned CH/Prometheus images, project Go binaries, native Prometheus copied from its pinned image, Docker access, a new project `.cache` state directory, a >=16-character `PATHWATCH_CLICKHOUSE_PASSWORD` in the environment. Optional `PATHWATCH_GRAFANA_CH_PASSWORD` creates a separate read-only query user; passwords and generated config remain mode0600 in ignored local storage. Never commit state or credentials. Dashboard Terraform state contains datasource credentials and must stay private and ignored.

The Grafana reader retains `readonly=1`, a default 10s execution limit and 64MiB
query memory limit. Its only setting exception is `max_execution_time` in 1..15s:
the pinned Grafana ClickHouse plugin 4.23.2 uses clickhouse-go 2.49.0, whose
[context handling](https://github.com/ClickHouse/clickhouse-go/blob/v2.49.0/context.go)
adds five seconds to the remaining client deadline. The dashboard's 10s timeout
therefore needs this bounded allowance. It does not allow unlimited time (`0`),
memory expansion, disabling read-only mode or writes. The project server config
explicitly enables `settings_constraints_replace_previous`, already enabled by
the pinned image, because ClickHouse requires it for
[`changeable_in_readonly`](https://clickhouse.com/docs/concepts/features/configuration/settings/constraints-on-settings).
Recreate the owned home service through its normal lifecycle to regenerate the
profile; preserve its existing data, journal, spool and SLI history.

Check generated XML and the prerequisite without starting services:

```sh
python3 -B -m unittest discover -s infra/modules/clickhouse -p test_grafana_reader.py -v
```

This checks configuration generation; native datasource health and permission
enforcement require the separate Grafana/ClickHouse validation.

```sh
source scripts/dev-env.sh
mkdir -p .cache/task10c2-bin .cache/task10c2-go-build .cache/task10c2-tmp .cache/task10-home
export GOCACHE="$PWD/.cache/task10c2-go-build" TMPDIR="$PWD/.cache/task10c2-tmp" GOMAXPROCS=2 GOPROXY=off
go build -p 2 -o .cache/task10c2-bin/ingest ./ingest/cmd/ingest
go build -p 2 -o .cache/task10c2-bin/probe ./probe/cmd/probe
go build -p 2 -o .cache/task10c2-bin/echo ./infra/modules/clickhouse/echo
cp infra/modules/clickhouse/home.example.json .cache/task10-home/config.json
cp infra/modules/clickhouse/probe.example.json .cache/task10-home/probe.json
cp infra/modules/clickhouse/roster.home.json .cache/task10-home/roster.json
# Fill config.binary_sha256 with the approved SHA256 of each just-built artifact.
sha256sum .cache/task10c2-bin/{ingest,probe,echo}
# Supply credentials through environment; do not put values in shell history.
export DOCKER_CONFIG="$PWD/.cache/docker"
python3 infra/modules/clickhouse/home.py run --config .cache/task10-home/config.json
```

The example uses explicit **literal-loopback plaintext only for ingest**, and a self-signed, explicitly trusted loopback echo fixture for TCP/TLS1.3/HTTP1/2/3. It keeps all seven probe kinds at 120s. DNS supports UDP/TCP fallback only; this literal-loopback target performs no DNS network lookup. DoH remains a separate root-spec gap assigned to Task10C. The fixture certificate is trusted only via the child's `SSL_CERT_FILE`; it does not alter system trust. Native unprivileged private traceroute records a visible CAP_NET_RAW permission failure; successful raw trace requires future guest verification and is not claimed by this home fixture. IPv6 traceroute remains unsupported. Real remote ingest must instead set `development_cleartext=false`, `tls_cert/tls_key`, a private reachable listen address and trusted CA in probe `ingest_ca`; no insecure verification switch exists. A cloud private URL alone does not establish routing: Task10B must implement/review the Azure A WireGuard/NVA and home transport dependencies.

Only the future explicitly owned guest is configured by `ansible/probe.yml`. The guest unit grants CAP_NET_RAW only, has 256MiB MemoryMax, restricted filesystem writes, bounded restart, guest-specific sysctl, Ubuntu unattended security upgrades and log rotation. Application upgrades are idempotent replacement of a locally SHA256-verified binary via the role, followed by restart. Do not run this guest playbook against home.

## Capacity and durability

The Task8 journal keeps full payloads forever with **65,536 records / 256MiB**, without dedup-key eviction. Do not delete journal files to keep running. The supervisor stops the workload at 52,428 records or 214,748,364 journal/spool bytes (80%), at dedicated CH data256MiB, at total project allocated disk 5.7 billion bytes (300MB emergency reserve), or native RSS >12GiB plus the CH container's 2GiB bound. It checks every30s; small workload and 300MB reserve cover checking/termination latency. It counts allocated checkout bytes (tools, caches, data, logs, build artifacts), the full CH image1,183,513,909B, full Prometheus image371,925,065B and conservative old shared builder550,000,000B. Additional project images/data must be added to this accounting **before** use; Grafana/Alertmanager are not started. CH tmpfs/logs add a conservative18MiB to the accounting; startup reserves256MiB data headroom. Native Prometheus blocks retain64MiB plus bounded ongoing head/WAL overhead; all actual files are included in the global hard stop. Journal256MiB, spool256MiB, CH256MiB, Prometheus~192MiB and logs~38MiB cannot all reach their individual maxima under this checkout budget: the shared5.7GB hard stop takes precedence. These are local safety bounds, not a five-week capacity allocation. Local logs are truncated to their last1MiB when exceeding4MiB. No shared image/builder cleanup occurs.

At120s and two physical observers over35days, even one batch/round gives50,400 batches before jitter, run time or B activity. Seven serial protocols can flush separately each second, so the worst case is352,800 batches before B; it exceeds journal record capacity. Payload forecasts must use actual round durations, batch counts and bytes; targets returning multiple addresses also increase results and bytes. The full-protocol workload and hard stop are intentional. **Five-week continuous deployment remains blocked until a separately reviewed capacity solution fits this workload and SLO.** Hard stops preserve bounded safety; they are not SLO or five-week success.

For an uncertain pending batch: stop this supervisor, restart its dedicated CH through the lifecycle, then reopen ingest with the same journal/data. The existing Store verifies epoch before repair. Do not point this module at another project's database or shared service. Ordinary lifecycle preserves all durable directories.

The pinned ClickHouse 26.3.32.14 image uses approximately121 global workers for background tasks with this deployment. The previous128-worker ceiling starved eight simultaneous MV confirmation queries. The512-worker limit leaves query headroom; the512-job queue bounds scheduled work, and the128-idle-worker limit allows unused threads to be released. These settings follow the [same-version upstream definitions](https://github.com/ClickHouse/ClickHouse/blob/v26.3.32.14-lts/src/Core/ServerSettings.cpp). Container2GiB/server1.5GiB memory limits and the ingest30s query deadline still apply. This fixes the reproduced configuration defect; it does not establish five-week capacity or explain every historical timeout.

Run the focused native regression from the checkout root with Python3.10+ and Docker access (no Python packages or Go build required). The pinned ClickHouse image above must already exist locally; the check uses `--pull=never`. Use a new evidence filename each time:

```sh
python3 infra/modules/clickhouse/native_concurrency.py --out .cache/task10-concurrency-result.json
```

The check starts one exactly labelled, unprivileged, read-only-rootfs container with2CPU/2GiB, a50MiB data tmpfs,16MiB log tmpfs,8MiB temporary tmpfs and2MiB Docker log bounds. A stable project flock excludes another invocation; port18124 must be free (`--port` can select another loopback port). It refuses an existing output file or insufficient project disk headroom. It generates private per-run credentials, creates a fresh seven-row MergeTree/MV fixture, and synchronizes eight `countMerge(samples)` queries. Every query must return7 within the production30s execution deadline (35s transport timeout); exact raw/MV counts and raw uniqueness must remain7. The JSON report records actual server settings, thread counts, command argv/exit/stdout/stderr, source/image hashes, sampled resources and cleanup. Finally it verifies/removes only the exact owned container ID, removes its private scratch directory and verifies port/lock release. SIGKILL or Docker daemon failure can prevent cleanup; inspect the recorded ID and labels before any manual cleanup. Existing campaign data, journal, spool and binaries are never opened by this fixture.

Evidence-write errors also fail the check: cleanup never depends on saving the report. A create timeout may recover the ID only through this run's pre-generated exact nonce name and all ownership labels. If container absence cannot be confirmed, the private directory remains for investigation. Persistent evidence failure returns nonzero and prints `evidence_complete=false` plus cleanup status to stdout and a persistence error to stderr; the JSON file may be stale or incomplete. Check the small lifecycle tests without starting Docker:

```sh
python3 -B -m unittest discover -s infra/modules/clickhouse -p test_native_concurrency.py -v
```

This is a small concurrent-query regression, not a durability or outage drill. The actual original pending batch was recovered through Store/Open and a new CH epoch with raw/MV7 and physical duplicates0. Do not edit that journal back to pending. Existing Task8 durability checks and the separately operated1200s drill remain the relevant acceptance boundaries.

`drills/check_probes.sh --roster .cache/task10-home/roster.json --url http://127.0.0.1:18123` checks the actual10min `uniqExact(probe_id)` query plus exact roster IDs. It is a raw presence diagnostic, not the on-time SLO.

Run `drills/ingest_outage.sh --config .cache/task10-home/config.json --out .cache/task10-outage-RUN` asynchronously, with regular progress checks. Production mode requires an actual healthy Prometheus baseline, stops owned ingest for1200 monotonic seconds, captures30s snapshots, restores on errors/signals, then verifies retained backlog IDs have committed journals, exact raw row counts, zero physical measurement duplicates and no spool drops. Both page burn alerts must be observed. `--fixture-seconds` is explicitly a short fixture and never20min evidence. Snapshot files preserve probe IDs/window counters, journal/spool bytes and alert payloads. Do not run against shared ingest.

For automated tool sessions, stop via `python3 infra/modules/clickhouse/home.py shutdown` and verify the control socket is removed, owner flock is released, loopback ports are closed, and the exact labelled container is absent. A PTY tool interruption is not proof that a process in another PID namespace exited. Snapshots include child PID namespace/arguments and source hashes for exact ownership verification. SIGKILL, tool namespace teardown and daemon failure cannot guarantee a finally block executes; never use broad name-based process killing or shared Docker cleanup.

## Private echo and home namespace profile (C2)

`binary_dir` selects only a dedicated project `.cache` artifact directory;
`binary_sha256` must match every native executable before any ClickHouse start.
The loopback echo must report `pathwatch-echo-private-v1` with `-version`. Old
Task10 binaries lack the readiness protocol and are refused; preserved campaign
binaries must never be overwritten. The fixture retains its PEM paths across
restart but verifies a new generation token, certificate hash, and actual TLS
handshake before starting the probe.

For private deployment, build the same echo artifact for the reviewed guest
architecture, then issue into a new private directory:

```sh
.cache/task10c2-bin/echo -mode issue -pki-dir .cache/private-echo-epoch1
.cache/task10c2-bin/echo -mode check -site home -listen 10.30.0.2:18443 \
  -cert .cache/private-echo-epoch1/home-cert.pem -key .cache/private-echo-epoch1/home-key.pem \
  -ca .cache/private-echo-epoch1/ca.pem
```

Issue creates a 0700 directory, 0600 private keys, a 90-day CA and default 45-day
non-CA leaves for home10.30.0.2, azure-a10.10.1.4 and azure-b10.20.1.4. Existing
directories are refused. Check requires at least35 days remaining; provided
service startup checks current validity, SAN, chain and key pairing without
rewriting inputs. Deployment copies only the site's leaf/key and public CA; the
CA key stays offline in ignored project storage. Plan a controlled stop, new
epoch issuance, trust distribution, certificate replacement and restart before
expiry (renew within10 days when each deployment must still cover35 days).
This has a maintenance interruption and does not demonstrate35 days elapsed.

`ansible/private-echo.yml` installs only on explicitly owned remote Ubuntu24.04
Azure probe guests with matching architecture, artifact hash and actual private
interface address. Supply the role's `pathwatch_echo_*` variables; never target
home. Configure `pathwatch_probe_target_ca_file` for TLS/HTTP trust through the
probe unit's `SSL_CERT_FILE`; `pathwatch_probe_ca_file`/`ingest_ca` remain separate
ingest trust. No system-wide trust store is modified.

For home, add `namespace_observer` to the home JSON, containing `parent_id`
(exact running home-A router64hexID), `image_id` (the pinned existing CH digest),
`probe_binary`, `probe_sha256`, `echo_binary`, `echo_sha256`, `ca`, `cert`, `key`,
and `ingest_ca` (all absolute private project paths). Set `owned_echo=false`,
`probe_metrics_port=9464`, and the home probe's `metrics_addr=10.30.0.2:9464`,
`ingest=10.30.0.1:19443`, matching `ingest_ca`, `probe_id=home`, and the same
persistent host `spool_dir`. Home ingest must already use a valid SAN certificate
at10.30.0.1:19443. Populate explicit owned cloud/home echo targets at18443.

The home supervisor launches `scripts/probe_home_observer.py`; it shares only
the validated home-A router namespace, with exact image and read-only binary/CA
mounts and a host-readable persistent0600 spool. A container-only `setpriv`
launcher briefly has SETUID/SETGID/SETPCAP/NET_RAW; the actual probe must have
host UID/GID and only NET_RAW in all five capability sets, verified continuously.
The echo has no capabilities. Parent replacement/stopping or child failure
terminates the observer and cleans its exact IDs; router down first removes its
observers. Metrics remain included in home snapshots and Prometheus at.2:9464,
with router INPUT allowing only loopback and gateway.1. No extra observer identity
is added to the roster.

Home-A's private echo returns to A through A's WG route and to B through the
existing static route via home-B. Only exact .2↔cloud .1.4 and A↔B pairs may
forward TCP/UDP18443 or ICMP. Existing ingest-only SNAT to gatewayTCP19443 is
unchanged. Home BGP route maps select .2/.3 as source for the exact imported
cloud prefix; peer static routes remain unchanged. Actual WG/FRR/BGP routing,
kernel preferred source, guest installation and private multi-site return paths
require their own native validation; an owned bridge stub proves none of those.

The namespace probe profile accepts a positive seconds-only `shutdown_timeout` up to `5s` (for example `1s` or `0.5s`; omitted/empty uses `5s`). Normal cleanup stops the exact probe with SIGTERM before echo, permits 15 seconds for its two bounded shutdown scopes plus margin, then permits 5 seconds for echo, verifies termination, and removes stopped containers without force. The client stop limits are 20 seconds for probe and 10 seconds for echo, leaving margin within the home supervisor’s existing 40-second child wait. A client stop failure or unconfirmed probe shutdown retains echo/parent for exact-owner recovery and returns an error; Docker timeout/exit 137 is never reported as a normal stop. Older live observer containers without the reviewed stop-profile label require explicit recovery rather than an assumed shutdown deadline.

## Explicit five-week capacity profile (C3)

`capacity_profile: "five-week-v1"` is opt-in. Legacy probe flush (1s),
measurements and ingest default65,536 records/256MiB remain unchanged.
The profile fixes120s cadence, batch128, spool64MiB, probe timeout≤5s and
RPC timeout≤10s. It covers **all seven kinds**: one owned private IPv4 literal
runs TCP/TLS/HTTP1/HTTP2/HTTP3/trace; three hostname targets independently run
explicit UDP/TCP/DoH DNS, with one declared private A and one declared private
AAAA address. Non-DNS IPv6, extra targets, multiple A/AAAA answers and public
targets require another reviewed capacity model. Probe/target IDs are≤16 ASCII
bytes; DNS transport suffixes remain distinct. The profile assumes the current
CLI trace ceiling30 hops. General CLI capabilities are retained outside it.

New profile measurements limit `Err` to256 UTF-8 bytes. Valid short diagnostics
are unchanged. Long or invalid-UTF8 diagnostics become a valid prefix plus an
explicit `truncated` marker, original byte length and SHA256 of original bytes.
**The original long diagnostic text is not recoverable from the digest.** This
happens before collection/counting/spooling. Store retains every byte of that
final produced protobuf payload forever, including diagnostic metadata; old
payloads, duplicate keys, owner identity, epochs and recovery semantics do not
change. Unexpected DNS addresses/counts retain **all** returned normalized
measurements, then stop scheduling and flush the collector. No address list is
truncated. Review the preserved spool before restarting after a profile breach.
DoH in the C2 namespace may reuse only its already reviewed public target CA;
`ca_file` maps to the existing read-only `/trust/ca.pem`, with no new mounts.

The profile collector flushes at most60s after collection, or at128 results.
The forecast counts timer flushes **plus** filled batches and final partial
flushes; it never assumes one batch per round. It reserves two35d campaigns and
the remaining B48h, plus32 aggregate controlled probe restarts. For35 run segments,
`sum(floor(segment_seconds/60)+2)` is bounded by103,750 timer/final batches;
filled batches add6,755. Minimum108s jitter yields57,635 round combinations and
864,525 results, at most110,505 normal records. The extra128-record reserve
covers one oversized DNS response pair (≤16,384 results); further uncontrolled
restarts/crashes or external ingest traffic invalidate the forecast.

`ingest -journal-max-records` accepts1..262144, default65536. This profile selects
131072 and the existing `-journal-max-bytes 1073741824`. Smaller limits reject
oversized existing journals without deleting keys. Home's opt-in90% record/byte
watermarks use those same limits. Existing bytes/records are added to the fixed campaign
remaining horizon; existing allocated files remain included in global `du`.
4KiB allocated block rounding, base64 padding, directory/temporary files, one
oversized DNS response, an unACKed batch,20min outage +300s retry +120s drain,
CH raw/MV with180d TTL and merge overlap, Prometheus head/WAL/blocks and
logs are explicitly reserved. Thirty-five days do not trigger the180d TTL.

Use `probe -config <file> -check-config` for validation without workers. Prepare
three actual reviewed configurations as in `capacity.plan.example.json`, then
add `capacity_plan`, `capacity_evidence` (CH) and
`capacity_prometheus_evidence` to the home config. Run:

```sh
python3 infra/modules/clickhouse/capacity.py \
  --config .cache/task10c3-home/home.json \
  --directory .cache/task10c3-home
```

Preflight exits nonzero for unsupported or corrupt configuration; exit2 reports
insufficient actual resources or missing complete full-volume evidence. Home invokes the same gate before
starting services. Evidence must match reviewed source and runtime binary hashes,
exact volume, raw/unique/MV counts, duplicate/recovery checks, Store.Open≤60s and
CH's candidate768MiB merge envelope and Prometheus's candidate384MiB
envelope. Neither candidate is accepted without matching complete evidence. `long_deployment_ready` means only this
bounded capacity admission; `physical_elapsed_verified` and `slo_achieved` remain
false. Cloud/PKI/transport deployment gates still apply independently.

The healthy freshness budget is132s maximum wait +45s aggregate probe work +60s
flush +10s **successful durable RPC including queueing** +.25s reporter polling
=247.25s. This is a conditional engineering budget, not proof of99% freshness or
99.5% availability. Retry backoff may separately reach300s; failed services,
resource guards and controlled maintenance can miss windows. Home samples actual
allocated disk at30s intervals and stops at5.7e9 bytes; startup forecast must fit
5.6e9 under the user's6e9 cap. Memory budgets count native ingest/Prometheus/probe/
echo/observer, CH2GiB, namespace probe256MiB/echo64MiB, both256MiB routers and1GiB
reserve within decimal16e9 bytes. No tool deletion is automatic.

The opt-in `capacity_fixture.py` runs a fresh synthetic, high-entropy data set
with the pinned image, allocated-disk/RSS sampling, real CH restart, raw/MV
recovery, full-payload journal reopening and duplicate/conflict checks. It can
remove only its own exact-labelled synthetic container and newly created scratch
data. It never accesses historical campaigns. Small/medium/full volumes test
capacity envelopes; none represents35 days elapsed or a physical SLO campaign.


A plan requires an explicit `campaign_id` and each observer's `start_utc`.
The corresponding probe config must contain matching `capacity_start_utc` and
`capacity_deadline_utc` (start+35d for A/home, start+48h for B). A/home share their
start; B's whole48h fits inside that35d interval. The probe waits for a future
start, permits a late resume only until the original deadline, and anchors the
remaining budget to a monotonic timer. A clock discontinuity over5s stops it.
Expiry cancels measurements and uses the existing final collector flush.

Read-only preflight never creates campaign state. First actual home startup,
under its existing owner lock, atomically creates owned0600
`capacity-campaign.json` and refuses a first start already in the past. The
normalized plan hash and original deadlines cannot be extended or reset.
Failed startup consumes a home start and retains those deadlines. A claimed
campaign with a past home start and missing SLI history cannot resume into an
empty SLI epoch. Existing journal without matching valid campaign state,
corruption, clock rollback and expiry fail closed. Home's deadline alarm also
interrupts a blocked resource scan. At most33 home starts are allowed; the
**32 aggregate controlled probe restarts across all sites remain an operator
audit budget**, not permission for32 restarts at every site. Existing immutable
SLI windows are never reset or revised. Future B still reserves its whole48h.

The profile keeps anomaly detection disabled, matching the existing home
integration. It supports raw/MV ingestion and empty events-table metadata;
**events growth and Task9 live anomaly integration are not proven** and remain
C5. A capacity config requesting anomaly enablement is rejected. This scope is
not a claim that the complete Task10 system fits five weeks.

C3 Prometheus uses the original three fixed scrapes and generated recording /
alert rules, adds `sample_limit: 16` to each scrape, selects16MiB WAL and block chunk segments,
and retains256MiB of blocks for35d. The candidate total384MiB reserve includes
WAL/head/compaction overlap. Runtime rejects more than128 head series. The
controlled series bound is48 scraped +15 scrape metadata +30 recording rules
+12 alert-state series =105, below the128-series synthetic envelope. Full
synthetic retention, accepted sample counts, restart and allocated peak must
be verified; HTTP200/204 alone is insufficient. Calibration replays the complete
historical real9/9/12 exporter samples, checks unchanged formatters against the
review base, and verifies current Prometheus `up=1` and scrape counts under
`sample_limit: 16`. This proves limit compatibility, not a new C3 live chain or
current health. Its disposable TSDB must never supply real dashboards or weekly
SLI reports. Legacy settings stay unchanged.

Startup/preflight counts all files. Only after successful Store/Spool Open and
single-owner operation may the profile scan exclude the exact owned
`journal/.pw-tmp-*`, `spool/.pw-tmp-*`, and campaign atomic prefix. Every scan
charges24MiB+4KiB,16MiB+4KiB, and16KiB respectively, regardless of whether a
file exists. These are existing maximum encoded record/batch/manifest bounds
under one writer; campaign encoding is explicitly capped16KiB. Final records
and nested/foreign prefixes remain counted. The extra40MiB accounting reserve
is included in the forecast. CH/Prom directories receive no exclusions. A
maximum of five audited retries is allowed only for owned disappearing paths;
permission/I/O/foreign/mixed errors remain fatal. The profile's logical spool
snapshot uses one atomic manifest, so an ACK unlink cannot turn a healthy
snapshot into a missing-file error. Global allocated scans still count final
payloads; diagnostic and payload retention semantics remain unchanged.

The full CH fixture uses150k-row loading segments and waits at most600s for
normal inactive-part GC between segments, under a60min total timeout. It leaves
ClickHouse's original480s old-part lifetime and merge/fsync settings intact,
and records active/inactive parts, merges and actual allocated peaks. Store.Open
must separately finish within60s. Synthetic journal seeding uses complete
canonical O_EXCL0600 files plus directory sync; it deliberately bypasses
per-batch Store.Commit durability for loading speed. Actual pending persistence,
same-epoch refusal, CH restart, recovery and duplicate/conflict checks use the
production path. Reopen timing is measured with warm filesystem caches, not a
cold-disk or power-loss guarantee. The final fixture checks three probe IDs and
45 distinct paths (six v4 non-DNS plus three DNS transports×v4/v6/unknown per
probe), with synthetic timestamps over35d/35d/48h. High-entropy maximum-size
fields stress encoding; they are not a replay of physical measurements.

Preflight also locks the existing home spool read-only, verifies owned regular0600
manifest/batches, exact size/SHA256, profile batch count and recognized files.
A new campaign cannot adopt nonempty old backlog or any historical Dropped count.
An active resume requires intact spool history and both actual committed bytes
and full allocated directory bytes (including empty manifest and4KiB slack) plus
34,025,472 bytes of new overflow/outage reserve≤64MiB. The full allocated scan
runs under the existing spool lock with no temporary-file exclusion. Unknown/uncommitted files
require operator review; preflight never repairs, deletes or resets them. Future
campaign waiting may still have no spool. Home checks only its local spool;
remote A/B first starts require fresh empty spools, and their resumes require the
same local history/remaining-space check. Home admission does not verify remote
filesystem state or authorize adopting arbitrary old remote backlogs.

## Explicit live anomaly configuration (C5 component)

Home now forwards a strict `anomaly` object to the existing ingest process:

```json
{"anomaly":{"enabled":true,"warmup":20,"sigma_floor_ms":1,"k":0.5,"h":5,"max_paths":1024,"max_windows":128,"max_pending":64,"alertmanager_url":"http://127.0.0.1:9093"}}
```

Omission or `{"enabled":false}` disables detection. Disabled objects cannot hide
unused options. `anomaly_enable`/`anomaly_enabled`, string booleans, unknown
nested keys, invalid bounds and non-loopback destinations fail before services
start. The same parser controls capacity admission and the actual command.
The URL must be a literal loopback HTTP base URL with an explicit port; operate
an owned local receiver before enabling. Home checks ingest readiness and all
three anomaly metric names before starting downstream services or completing a
resume. Startup errors propagate through the existing owned cleanup path.

The only new exporter samples are `pathwatch_anomaly_ticks_total`,
`pathwatch_anomaly_failures_total`, and `pathwatch_anomaly_delivered_total`,
without path/event labels. Ingest's sample count becomes12, within the C3 scrape
sample_limit16. State remains under the same journal owner at
`journal/anomaly/state.json`, bounded4MiB with an atomic replacement. Do not
reset, edit or adopt another owner's state; configuration changes fail closed
against its stored fingerprint. Since 2026-10-27 the ingest default is H12
(H5 averaged ~10.6 false alarms per 10,000 windows; see
`docs/evidence/task-09-cusum/`); state created under H5 needs `-anomaly-h 5`.

`capacity_plan` still refuses **all enabled anomaly combinations**. The new
`anomaly_candidate` diagnostic on disabled preflight reports includes fixed
3×15-path windows, retained-history inputs, uncertain INSERT/restart allowance,
state/atomic space and metrics. With no measured engine INSERT/merge cadence
credit, its conservative parts candidate exceeds the project budget. The
separate compact batched storage experiment proves neither engine single-row
parts peak nor full raw/MV/events/Prometheus coexistence. Existing full capacity
and physical35d/SLO gates remain pending and old evidence hashes are invalidated
by changed capacity inputs.

An owned local component fixture requires no compiler or downloads. It verifies
the preserved C3 ingest artifact against its historical manifest and unchanged
Go/proto source; it does not overwrite original binaries. Pick five fresh,
unused high ports and a new output path:

```sh
python3 infra/modules/clickhouse/live_anomaly_fixture.py \
  --port-base 18335 --out .cache/task10c5-evidence/native-new.json
```

Its historical Report timestamps and separate high-entropy storage data are
synthetic. The real gRPC/Store/anomaly pipeline produces the event; actual process
restarts preserve state and retry a local503 notification. A separate table
holds explicitly synthetic storage rows. The fixture keeps default480s old-part
protection, TTL180d and fsync settings, observes active/inactive parts without
waiting for zero inactive parts, records actual elapsed time, then removes only
its own labelled container and fresh data. It preserves failed outputs. The
short real Prometheus scrape checks12 samples and3 anomaly metrics; it cannot
replace full35d WAL/head/block/retention proof.
