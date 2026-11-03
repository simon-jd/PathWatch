# 本机 ingest 与 ClickHouse

这是 home-ubuntu 的单写者服务。使用现有 Task7 gRPC 协议，按 `(probe_id,batch_id)` 与有序 measurement 内容永久去重；`sent_at` 可更新，不参与内容比较。同键异内容返回 `AlreadyExists`；成功 ACK 总是回显批号和完整 `committed_result_count`。批次同步立即写入，最多 10,000 行，无积攒计时器或部分 ACK。

## 启动

准备专用的本机 ClickHouse Atomic 数据库 `pathwatch` 和仅供此进程使用的凭据。不可将本服务指向已含其他表的数据库，不可让其他程序修改其表或 epoch。ClickHouse HTTP 必须是字面量 loopback 地址，禁止代理、负载均衡与自动重试；同一主机同一持久目录运行一个实例。

```sh
source scripts/dev-env.sh
go build -o .cache/pathwatch-ingest ./ingest/cmd/ingest
# 密码由环境 PATHWATCH_CLICKHOUSE_PASSWORD 提供，不写入命令行参数。
.cache/pathwatch-ingest \
  -clickhouse-url http://127.0.0.1:8123 -database pathwatch \
  -clickhouse-user pathwatch_ingest \
  -journal .cache/ingest-journal -journal-max-bytes 268435456 \
  -listen 127.0.0.1:9443 -tls-cert /path/to/server.pem -tls-key /path/to/server-key.pem \
  -metrics 127.0.0.1:9091
```

TLS 默认必需，最低 TLS 1.2；测试开发才使用 `-development-cleartext`，该选项只接受 `127.0.0.1` 或 `::1` 等字面量 loopback，且不能同时提供 TLS 文件。指标监听也限制在 loopback。TLS 证书需被 Task7 探针信任；TLS 本身不提供探针身份授权，公网暴露或多租户鉴权不在本任务范围。

`/metrics` 有 accepted、duplicate、recovered batch counters，以及固定六类 rejected reason counters。gRPC 最大消息 16MiB、header list 16KiB、每连接最多32流；业务最多32个并发请求，每个请求/SQL受30秒上下文限制。SIGTERM/SIGINT 停止接受请求，最多35秒排空 gRPC，再关闭 metrics 和 journal；强制中断的 pending 批次走恢复流程。

## 持久化与恢复边界

1. journal 目录使用真实 canonical 路径和非阻塞 OS `flock`。本地 owner 文件绑定 endpoint、Atomic database UUID、随机 journal UUID 和目录；ClickHouse `ingest_owner` 的原子 CREATE 与注释将数据库绑定到同一 owner。不同目录/数据库重建不会被静默接管。
2. `ingest_epoch` 是持久元数据、易失内容的 Memory 表。只有唯一 writer 可初始化单行随机 epoch；ClickHouse 进程重启后该行消失。删除/修改此表会破坏 fencing，严禁其他写者这样做。
3. 每个新批在写 pending 前，以只读查询建立/刷新一个 HTTP session 并确认 epoch。pending 后所有 INSERT、ALTER 与验证请求使用 **`session_check=1`**：ClickHouse 重启/会话消失时，服务器在执行 SQL 前报 `SESSION_NOT_FOUND`，不会自动创建会话。一次 store 使用一个 session，批次间串行；pending 后从不重建 session。预写刷新使正常空闲超时不会要求重启。
4. pending 包含完整 deterministic protobuf、SHA256、批次键和 epoch，先写临时文件、文件 fsync、原子 rename、目录 fsync，之后才 INSERT。HTTP POST 无自动应用重试、不带 Idempotency-Key、body 不可自动 rewind；禁用 async inserts 和 ClickHouse 有限窗口 dedup。
5. 同步 INSERT 成功后，分别核实该批 raw 行数与 MV 中 primary-phase sample 数；再原子持久化 committed 标记，才能 ACK。raw 与 aggregate 表均启用 `fsync_after_insert=1`、`fsync_part_directory=1`、`min_rows_to_fsync_after_merge=1`。任何不确定 SQL/文件操作冻结 writer，客户端保留 spool。
6. 正常 ingest 重启、无 pending 时直接恢复服务。有 pending 且 epoch 未变时，启动明确拒绝：**先停止 ingest，再重启专用 ClickHouse，再启动 ingest**。不是传一个“我重启过”参数。新 epoch 被验证后，先将 pending 的 epoch 更新并 fsync；等这两张表未完成的 mutations 全部结束，再同步删除该 batch_key 的 raw 与 aggregate（`mutations_sync=2`），重放和校验，然后提交。恢复任何步骤失败，必须再次重启 ClickHouse；旧后台 mutation 不会在新 INSERT 后继续误删数据。
7. journal 永不淘汰已确认键，默认256MiB，允许1KiB–1GiB并限制65,536批；满时新批返回 ResourceExhausted，已存在的相同批仍可 DUPLICATE。容量计已序列化 owner之外的批次文件，另需预留单个最大24MiB临时文件、文件系统块/元数据及 owner 空间。所有历史完整 payload 会保留，长期部署应预估空间；不可删除 journal 来“解锁”。journal 与 ClickHouse 应一起备份/恢复，不能只恢复其中一方。

此方案优先保护正确性，代价是不确定写后需人工重启专用 ClickHouse，恢复期间服务不可用。原始行和 MV 的跨表可见性仍非原子，未 ACK 批/修复中间态可能被查询到；未来 API/Task9 应在恢复完成且服务 ready 后查询。已 ACK 批次不因普通进程重启重复入库。验证覆盖真实进程重启、ingest SIGKILL、丢响应、MV部分失败和恢复中断；没有进行物理断电或存储硬件失效测试。保证依赖本机文件系统正确实现 fsync、专用数据库权限和没有外部修改。TTL 到期属于明确保留策略，journal dedup 键不会因此删除。

采用标准 HTTP 而不是计划 TechStack 建议的 clickhouse-go，是为使用经版本源码和真实重启测试验证的 HTTP session fencing。不是依靠有限 insert dedup window，也不依靠 ReplacingMergeTree 最终合并。

## 数据与 Task9 查询

`probe_results` 是 MergeTree，严格 `ORDER BY (path_key, started_at)`。`started_at` 为 DateTime64(9)，保留纳秒；TTL 使用 `toDateTime(started_at) + INTERVAL 180 DAY`，因为 ClickHouse TTL 表达式要求 DateTime/Date 类型。原始 phases 为 `Map(String,Int64)`，hops 的 ttl/address/rtt 分别为等长数组，RTT仍为Int64；地址族、实际HTTP协议、TLS版本/恢复标记及远端错误都保留。started_at 限于 Unix epoch 到 4,279,415,295 秒（再加180天仍在 DateTime TTL 范围内）；越界在任何数据库写入前拒绝，避免未来日期溢出窗口或TTL。

路径键精确为 `probe_id→target_id/kind/family`。primary `phase_ms` 映射：dns→resolve，tcp→connect，tls→tls_handshake，http1/2/3→ttfb，trace→total；原始其他 phase 不受影响。primary 缺失时 `phase_present=0`，原始行仍保存，不生成伪造的0ms样本；真实0耗时可以参与分位。DNS错误允许空地址/空family，绝不静默丢行。

`path_quantiles_5m` 为 AggregatingMergeTree，包含 `path_key,window_start,batch_key,phase_ms,samples`。`phase_ms` 是 `quantilesState(0.5,0.95,0.99)`，`samples` 是 countState；额外 batch_key 保留修复所需的可分离状态，业务查询需跨批次 Merge：

```sql
SELECT path_key, window_start,
       quantilesMerge(0.5, 0.95, 0.99)(phase_ms) AS quantiles,
       countMerge(samples) AS sample_count
FROM path_quantiles_5m
GROUP BY path_key, window_start
ORDER BY path_key, window_start;

SELECT path_key, started_at, phases_nanoseconds,
       hop_ttls, hop_addrs, hop_rtt_nanoseconds, err
FROM probe_results
WHERE path_key = 'home→owned/tcp/v4'
ORDER BY started_at;
```

## Task9 异常事件 job

Task8 默认启动参数继续可用；加 `-anomaly-enable` 才运行 job。沿用上面 TLS、ClickHouse 和 journal 参数，再加：

```sh
  -anomaly-enable -anomaly-warmup 20 -anomaly-sigma-floor-ms 1 \
  -anomaly-k 0.5 -anomaly-h 12 -alertmanager-url http://127.0.0.1:9093
```

Alertmanager 只允许字面量 loopback HTTP base URL，job POST `/api/v2/alerts`；不使用代理或跟随重定向。启用后启动时立即跑一轮，之后每五分钟运行；退出时取消并等待 job，随后才关闭 Store。沿用默认 TLS 策略。`/metrics` 增加无路径标签的 `pathwatch_anomaly_{ticks,failures,delivered}_total`，运行失败同时写日志。

| 参数 | 默认值 | 约束 / 单位 |
|---|---:|---|
| `-anomaly-enable` | false | 显式启用 |
| `-anomaly-warmup` | 20 | 每路径最初 2–1000 个真实窗口 |
| `-anomaly-sigma-floor-ms` | 1 | 正的有限毫秒值，处理零方差 |
| `-anomaly-k` | 0.5 | 有限、非负，标准差单位 |
| `-anomaly-h` | 12 | 有限、正，标准差单位（2026-10-27 前默认 5） |
| `-anomaly-max-paths` | 1024 | 1–4096；不淘汰已保存路径 |
| `-anomaly-max-windows` | 128 | 每轮 1–256 个实际窗口 |
| `-anomaly-max-pending` | 64 | 1–256 个待交付事件 |
| `-alertmanager-url` | http://127.0.0.1:9093 | 含端口的本机 base URL |
| `-anomaly-alert-timeout` | 5s | 正、最多 30s |
| `-anomaly-retry-min` | 5s | 正；下一次五分钟 tick 才会重试 |
| `-anomaly-retry-max` | 5m | 不小于 min、最多 1h，指数退避上限 |

每路径 baseline 固定为 warmup 窗口 p95 的均值与样本标准差；sigma 至少为配置 floor。对 `z=(p95_ms-baseline_ms)/sigma_ms` 使用标准上升 CUSUM：`S=max(0,S+z-K)`，`S>H` 报警后清零；不是把 K/H 当毫秒。baseline 不自动学习异常后的新均值。**原计划 H5 下任意 10,000 高斯点零误报的字面断言未满足**：Go 固定种子 1/42/20261024 实际分别 12/8/11 次误报。独立配置 H12 的同种子 fixture 为 0/0/0，6σ 跳变仍在三个窗口内报警；这不保证任意噪声零误报。

2026-10-27 默认值改为 H=12，依据 `docs/evidence/task-09-cusum/`（2000 次 × 10,000 点模拟，K=0.5）：H=5 时 Siegmund 近似 ARL0≈938 个窗口，平均每 10,000 点误报 10.6 次，零误报概率 0；H=12 时零误报概率 0.9875，6σ 跳变平均 2.76 个窗口报警，代价是 1σ 跳变要 24.5 个窗口（H5 为 10.3）。规则是事先定的：取零误报概率 ≥ 0.95 的最小 H。注意 baseline 由 20 个窗口估计时，H=12 的零误报概率只有 0.7455（平均 5.9 次误报），估计误差是误报的主要来源。用 H=5 建立的持久状态带配置指纹，升级后要么继续显式传 `-anomaly-h 5`，要么换新状态。

读窗口通过同一 Store gate，与 Commit/recovery 互斥；closed/frozen/pending 不可读。只处理 `window_start < floor(now,5m)`，跨所有 batch key 做 `quantilesMerge` 与 `countMerge`，按 `(window_start,path_key)` 顺序分页。没有数据的窗口不生成零值。cursor 已越过的迟到数据不重算，已处理窗口不会再修订；它是有界的顺序检测，不提供迟到数据完整重算。每轮 SQL 共用 30 秒 deadline，保留 Task8 SQL 内存 / 线程限制；历史积压按 max-windows 分页。MaxPaths 是持久容量，不是实时规模保证：每五分钟新增的 path-window 数超过 max-windows 会积压，应在首次启用前按实际规模配置。

关联使用同 probe/target/family/实际目的地址的 `trace` 行，之前快照选窗口开始前一小时内最近一次，之后快照选异常窗口内最近一次；不会按无 hops 的 tcp/http 等 kind 找路径。目的地址池混合时记录未知原因，继续保存时延事件，不宣称换路。按 TTL key 比较并规范化 IP 格式；只有双方已知 router 地址不同才报告首个已知差异 TTL。未知回复、尾部截断不能单独证明改变，稀疏/重复 TTL、地址族混用或时间无效则拒绝关联。这是观测关联，不能证明某跳是时延根因。

持久状态在同一 owner 的 `journal/anomaly/state.json`：包括 owner 指纹（现有 endpoint、数据库 UUID、journal UUID、canonical 目录；不含易变 epoch/session）、配置指纹、完整性校验、cursor、warmup/CUSUM 与 pending。使用临时文件 fsync、rename、目录 fsync；首次目录创建也同步父目录。状态上限 4MiB，另预留一个 4MiB 临时文件及块/元数据，不计入原批次 journal 的 256MiB 上限。配置改变、有效 state 误拷到另一 owner、损坏/空文件均拒绝加载；不支持单独恢复这个文件。状态、批次 journal 与 ClickHouse 须成套备份。已有状态后改变 job 配置需要受控迁移；本任务没有自动迁移/重置接口，不可删除 state 绕过。

先原子保存 cursor+pending，再写 `path_events`，再发本机 HTTP；写入/交付失败保留 pending 与退避，重启后继续。状态持久化不确定则当前 job 停止推进，需修复后重新打开；不使未落盘的内存状态成为新 cursor。路径或 pending 容量满会显式报错，不静默淘汰。pending 退避/失败期间暂停读取新窗口。通知 HTTP 在 Store gate 外，有 deadline，等待通知不阻塞原始测量入库。

`path_events` 是按确定性 event_id 的 ReplacingMergeTree，保留 180 天；**所有业务查询必须使用 `FINAL` 或按 event_id 去重**。重试可能有物理重复行，不能声称物理 exactly-once。事件 ID 绑定持久 owner、配置、路径和窗口。Alertmanager 标签只有 alertname/event_id/severity，路径、数值与原窗口在注释里；重试标签、startsAt 与 payload 不变。未知真实恢复时间，因此省略 endsAt，交给 Alertmanager 的收到时间 + resolve_timeout，使积压事件首次送达仍可短暂 firing。这是一次事件通知；不定期续发、不检测恢复，也不保证接收器最终通知。HTTP 可能至少一次，丢成功响应时可重复发送同 fingerprint；本机 httptest 验证 API 交付契约，没有启动新 AM 镜像或联系任何外部接收器。

```sql
SELECT * FROM path_events FINAL ORDER BY window_start, path_key;
```

实现与边界证据见 [Task9 异常检测](../../evidence/README.md#cusum-threshold-simulation-one-machine)。统计依据为 [NIST 上升 CUSUM](https://itl.nist.gov/div898/software/dataplot/refman1/auxillar/cusum.htm)；事件存储语义见 [ReplacingMergeTree](https://clickhouse.com/docs/engines/table-engines/mergetree-family/replacingmergetree)，通知语义见 [官方 Alerts API](https://github.com/prometheus/alertmanager/blob/main/docs/alerts_api.md)。

## 本机验收

```sh
source scripts/dev-env.sh
export DOCKER_CONFIG="$PWD/.cache/docker"
go test ./ingest/... ./probe/... ./proto/... -count=1 -timeout=5m
go test -race ./ingest/internal/server -run 'TestIngest_(Concurrent|AckLoss|RunningWriter|LateSession|SIGKILL|RecoveryMutation)' -count=1 -timeout=5m
go vet ./ingest/...
```

testcontainers-go ClickHouse 模块固定 v0.37.0；镜像固定 `clickhouse/clickhouse-server:26.3.32.14@sha256:456063a689194186633bb3db0862283068f4c2ee538852d3aaffa7eb66f1f841`，官方registry确认后只读复用本机已有镜像。测试禁用Ryuk，不启动额外辅助容器，显式清理该测试容器及匿名数据卷。标签project=pathwatch、owner=codex-pathwatch、task=08；2GiB内存、2CPU、16MiB日志tmpfs、Docker日志2MiB；仅绑定loopback端口，结束检查数据/日志合计低于256MiB。测试脚本不创建云资源、不使用GPU、不留下常驻服务。

主要依据：ClickHouse 官方 [有限去重窗口](https://clickhouse.com/docs/guides/developer/deduplicating-inserts-on-retries)、[MV同步但非原子](https://clickhouse.com/docs/knowledgebase/are_materialized_views_inserted_asynchronously)、[HTTP session/断线查询语义](https://clickhouse.com/docs/interfaces/http)、[Memory引擎](https://clickhouse.com/docs/engines/table-engines/special/memory)、[ALTER DELETE](https://clickhouse.com/docs/sql-reference/statements/alter/delete)。锁定26.3.32.14源码摘录和实际SHOW CREATE见Task8证据。

## Optional bounded measurement HTTP API

Enable `-query-listen 127.0.0.1:9081` (default disabled). Only a literal
loopback address and port 1–65535 are accepted. It shares the **same Store** as
gRPC; never start a second Store just to read. Rust can terminate TLS and forward
HTTP to this listener in the same network namespace. Its current upstream does
not support HTTPS. Metrics/readyz stay on their separate administrative listener.

Only bodyless GET on these exact paths is supported:

| Path | Results |
| --- | --- |
| `/v1/measurements/raw` | Raw stored fields, ordered by timestamp/batch/index |
| `/v1/measurements/quantiles` | p50/p95/p99 merged across all batches in each5m window |
| `/v1/measurements/events` | Stored events, engine FINAL deduplication by event ID |

Each requires single `path_key`, `from`, `to` parameters; optional `limit` defaults
to100, maximum256. No unknown/duplicate parameters or pagination. `path_key` is
1–512 UTF-8 bytes without NUL. Time is UTC RFC3339Nano ending in Z, a half-open
range of at most24h, within the existing DateTime/TTL representable range.
Quantiles require5m-aligned boundaries ending no later than the last completed
window. Unknown routes return404, HEAD/other methods405; URL aliases and malformed
encoding are rejected. URI max4096B, headers16KiB, connections16, query admissions4.
Excess admissions return429. Transport failures before handler parsing retain
net/http status/connection behavior; they cannot promise the API JSON envelope.

Success envelope: `{"api_version":"v1","data":[],"count":0,"range":{"from":"...","to":"..."},"limit":100}`.
Raw fields retain schema names; timestamps are UTC RFC3339Nano. Nanosecond Int64s
(including phase maps/hop arrays) and aggregate UInt64 sample counts are decimal
strings. `phase_present` is boolean; missing primary phase has `phase_ms:null`,
not an invented latency sample. Quantile values are the existing approximate
ClickHouse quantiles, not an average of per-batch quantiles. Events do not assert
that a notification was delivered. No rows is a successful empty array.

Errors have `api_version:v1` and `error:{code,message}` with fixed public text:
400 `invalid_request`,404 `not_found`,405 `method_not_allowed`,413 `result_limit`,
414 `uri_too_long`,429 `overloaded`,503 `store_unavailable`,504 `query_timeout`,
502 `invalid_upstream_response`,500 `internal_error`. SQL, passwords and engine
exception text are never returned. Proxy failures before the API retain the
proxy's own response contract.

The2s query deadline includes gate waiting, epoch checks and SQL. Closed,
poisoned, pending and stale-epoch state fails closed. A post-read epoch check
runs under the same gate. Rows are completely received/validated before releasing
the gate; JSON encoding and slow response writes happen afterward. The response
write deadline is5s, header/body-read deadline2s, idle15s. Shutdown cancels query
contexts and drains/closes the query listener before closing Store. This gives
per-request consistency with commits, not a cross-request snapshot or a
transaction across anomaly state, events and notification delivery.

Fixed typed SQL parameters apply ClickHouse escaped-text encoding then URL
encoding. Engine read settings:2s,2threads,256MiB memory,1,000,000 scanned rows,
128MiB scanned bytes,257 result rows and1MiB result bytes, overflow modes throw.
Both received engine bytes and final encoded JSON have a separate inclusive1MiB
cap. SQL reads limit+1; excess rows/bytes fail413 with no partial success. Narrow
the time range if necessary. Existing Store write request behavior is unchanged.
Queries can delay writers while holding the shared gate; this is not a new
throughput/full-volume capacity proof.

When enabled, `/metrics` adds exactly four initialized fixed outcomes to
`pathwatch_query_requests_total`: success, failure, rejected, canceled. They are
handler diagnostics (successful writes are not proof that a remote client received
every byte), not external availability. Anomaly-enabled ingest now has16 samples,
matching the existing16-sample scrape cap. No path/user/revision labels are added.
The dedicated fixture records a new30-slot end-to-end Rust-TLS query interval,
including missed slots, separately from old readiness history and predeclared
failure intervals. It does not establish99.5%, a35-day observation, or combined
cluster capacity. The permanent query observer remains future integration work.

Task14a originally supplied the typed reader seam. Task14b below adds the private
RPC split. Charts, cluster integration, HA, replication and actual Kubernetes
rollback remain future platform work.

## Task14b: stateless frontend and sole core

`-mode embedded` remains the default above. `-mode core` owns the one Store,
ClickHouse credentials, durable journal, ingestion counters and anomaly job.
`-mode frontend` accepts the original probe Report and optional measurement HTTP
API. It has no Store or anomaly job and rejects explicit owner options and the
ClickHouse password environment variable. Frontends can be replaced concurrently;
core replacement must remain exclusive and serial, preserving the endpoint,
database UUID, canonical journal and owner binding. No replication/HA is added.

Example private options (supply deployment-specific files and addresses):

```
ingest -mode core -core-listen 127.0.0.1:9444 \
  -core-ca ca.pem -core-cert core.pem -core-key core.key \
  -core-server-name core.example \
  -core-frontends spiffe://pathwatch.local/frontend/stable,spiffe://pathwatch.local/frontend/candidate \
  -journal /durable/journal -clickhouse-url http://127.0.0.1:8123

ingest -mode frontend -core-target core.example:9444 \
  -core-ca ca.pem -core-cert frontend-client.pem -core-key frontend-client.key \
  -core-server-name core.example \
  -listen 127.0.0.1:9443 -tls-cert public.pem -tls-key public.key \
  -query-listen 127.0.0.1:9081 -metrics 127.0.0.1:9091
```

Private RPC requires TLS1.3, an explicit CA, exact server DNS/IP SAN (no wildcard
matching), client-auth/server-auth EKUs and an exact authorized frontend URI SAN.
Both local and actual peer identities are checked at startup/handshake and each
call, including expiry on established connections. Certificate rotation requires
restarting each process. Describe must advertise major1, required capability bits
15 and the exact byte limits below before frontend startup succeeds. Metadata
accepts only major1/minor0 or1. Unknown methods/versions fail closed. There is no
SQL RPC or caller-controlled engine setting.

Complete Report validation runs in both frontend and core. The original identity,
SentAt, order and protobuf unknown fields are forwarded once. The frontend only
returns a matching accepted/duplicate ACK after core's existing durable decision.
An unavailable or lost ACK remains a failure; there is no hidden retry, timestamp
refresh or replacement ID. The probe retains its existing spool/retry behavior.
Decoded application errors retain all six original fixed ErrorInfo reasons.
Well-formed Report collection-limit failures detected before allocation retain
InvalidArgument, `pathwatch.ingest`/`invalid_request`, and the original fixed
validation category. For a request with multiple invalid fields, this safety
preflight can report its category before another validation error; first-error
ordering is not guaranteed for such requests. Malformed protobuf/UTF-8 and
transport framing failures remain separate bounded transport errors.
Pinned gRPC1.69.4 drops details on predecode tap rejection: public frontend
transport overload is ResourceExhausted, exact `ingest queue full`, zero details.
Only a verified, negotiated private core call may recognize zero-detail
ResourceExhausted with the exact method literal (`ingest queue full` for Report,
`overloaded` for reads). Malformed/nonempty/wrong details and other text never
use that exception. HTTP read overload remains429 with Retry-After1; result limit
is413, and unknown upstream payloads fail502. No transport error becomes an ACK.

Bounds apply per process and across its connections:

| Boundary | Core | Each frontend |
| --- | --- | --- |
| Accepted TLS connections | 10 | 16 public |
| Concurrent streams per connection | 4 | 2 public |
| Global predecode Report slots | 32 | 4 |
| Global read/Describe slots | 4 combined | HTTP API4 |
| Report/read deadline | 30s /2s | shorter caller propagated |
| Decoded Report charge pool | 1GiB | 768MiB |

Headers are16KiB, stream/connection receive windows64KiB/1MiB; handshake5s,
idle15s and connection age5m (+30s drain). Compression is disabled. Tap acquires
nonblocking global leases before receive/decode. Cancellation releases an
unclaimed lease; once TagRPC claims it, only stats.End releases its slot/charge,
after receive/decode/handler return. Active cancellation cannot admit replacement
memory while the old handler lives. Shutdown stops new admissions, cancels reads
and anomaly work, drains Report RPC up to35s, then forces stop and waits for leases
before closing the sole Store. Partial startup closes already opened resources.

Report wire max16MiB, private query request max4KiB and private reply max1MiB are
inclusive. A structural scan before protobuf allocation rejects >10000 results,
>255 hops/result or >16 distinct phase keys; duplicate maps/singular fields retain
protobuf semantics. The decoded charge is1MiB +4×wire bytes +1024/result +160/hop
+512/distinct phase entry. It preserves the legitimate2.55-million-hop shape.
This accounting is an admission bound, not a certified RSS limit: transport wire
buffers, Store clones, runtime/GC and kernel buffers also require memory budget.
Query framing is checked by the transport's16MiB maximum before the method codec's
4KiB check; bounded read slots limit that pre-codec exposure. Reply shape is
checked before client decode, including256 rows and bounded raw collections.
Final HTTP JSON separately retains its inclusive1MiB bound. A result fitting JSON
may exceed protobuf1MiB and return413; neither representation permits truncation.
Raw optional phase_ms is the sole private presence source (field20 reserved).
Int64 nanoseconds and UInt64 counts reach HTTP as lossless decimal strings.

Split metrics ownership is explicit: core exports the historical9 ingestion and,
when enabled,3 anomaly series. A query-enabled frontend exports4 query outcomes
plus2 fixed Report outcomes (ack/error), with no path/identity/revision labels.
Core exports no query HTTP outcomes. Frontend-rejected traffic never reaches the
core and therefore does not increment core rejection counters; this topology
difference is explicit. Transport failures before handler/admission accounting
are not counted by frontend Report outcome diagnostics. The finite stage TSDB has core12 +two
frontends6 each +15 scrape metadata =39 series, with no rules/self scrape. This
stage is separate from the future full<=128-series system profile; it does not
claim capacity,35-day retention, external SLO, or maxSurge deployment acceptance.
The future fixed-slot127-series proposal and controller/canary integration remain
pending. The source-bound additive protocol compatibility recipe is under
`infra/modules/clickhouse/fixtures/task14-core-rpc-compat/`.

Private typed query responses validate required path keys using the public UTF-8, nonempty, 512-byte, no-NUL contract. Raw measurement identities, kind and metadata reuse the original Report validator without clock-skew checks on historical rows. Batch/event keys must be nonempty valid text; no additional hash or path grammar is imposed. Latency values are finite and nonnegative, quantiles are ordered, and optional primary-phase absence stays absent. Zero result index, zero latency, empty optional protocol/hop address/correlation reason and other legitimate proto3 defaults remain valid; missing required semantics fail as invalid engine response (HTTP502).

## Metrics bridge and readiness checker

`ingest/cmd/adminbridge` is a separate, metrics-only HTTP/1.1 TLS 1.3 server.
It forwards only bodyless, exact `GET /metrics` to a fixed plain-HTTP loopback
administration listener. It adds no metric series. Keep that listener private to
the same network namespace: a rootless Kubernetes loopback is not host loopback.

```sh
adminbridge serve -upstream 127.0.0.1:9091 -listen 0.0.0.0:9445 \
  -ca /run/pathwatch/ca.pem -cert /run/pathwatch/metrics-chain.pem \
  -key /run/pathwatch/metrics-key.pem
adminbridge check-ready -upstream 127.0.0.1:9091 -ready-format frontend
adminbridge check-ready -upstream 127.0.0.1:9091 -ready-format core
```

The CA, certificate chain and key are explicit files. Protect the key with mode
0600. Defaults require the exact server DNS SAN `metrics.pathwatch.local`, its
single URI SAN `spiffe://pathwatch.local/metrics`, and the client's single URI SAN
`spiffe://pathwatch.local/prometheus`. `-server-name`, `-server-uri` and
`-client-uri` can select other exact, distinct identities; wildcards and multiple
URI roles are rejected. ServerAuth/ClientAuth EKUs and full certificate-chain
validity apply. Clients must verify the server DNS and CA. Certificate files are
loaded at startup; rotate by restarting. Established requests recheck chain
validity, and the earliest chain expiry also bounds fetch and response writes.
A request valid at entry may already reach upstream before expiry cancels it.
There is no insecure mode, environment proxy, redirect fallback or HTTP/2.

Upstream accepts only canonical literal `127.0.0.1:port` or `[::1]:port`, with
ports 1–65535; listening accepts a canonical literal IP and port. Queries,
encoded alternate paths, upgrades, request bodies and other methods or paths
are refused before forwarding. Four accepted connections include idle and
handshaking connections; excess connections close immediately. Two fetches are
admitted without waiting; excess fetches return 503. Fetch permits remain held
until the local fetch operation completes, including cancellation cleanup.
Handshake and header phases each have a two-second bound. A handler's fetch and
response write share two seconds, shortened by certificate expiry. Idle
keepalive is five seconds; shutdown cancels connections and joins handlers with
a three-second bound. These are phase limits, not a two-second connection lifetime.

Metrics bodies are limited to 65,536 bytes inclusive and completely read before
committing a downstream response. Valid body bytes and ordinary final error
statuses (including 503) are preserved; redirects, informational responses,
compression, upgrades, ambiguous framing, trailers, truncation and oversize
responses fail closed. Only Content-Type is copied, with Content-Length computed
from the complete body. Each upstream connection serves one exchange. A bounded
raw-header observer preserves original bytes and rejects ambiguous framing before
the standard HTTP parser; its header plus read-ahead buffer is at most 8,192 bytes
per fetch. Chunked bodies without trailers are accepted. Request parsing uses
Go's MaxHeaderBytes 8,192 with its additional 4,096-byte parser allowance;
a separate 8,192-byte normalized aggregate limit (Host, field names, values and
separators) applies before forwarding. This is not an 8,192-byte raw request limit.
Transport buffers, TLS/runtime allocations and kernel buffers also consume memory;
these protocol bounds are not a certified process RSS limit.

`check-ready` calls only `/readyz`, exits zero for a complete healthy response,
and exits one otherwise. It has a two-second total fetch deadline and never
exposes readiness through the metrics listener. The default `frontend` format
requires HTTP 200 and exactly `ready\n`. Explicit `core` requires the existing
ordered JSON producer contract (`windows`, `journal_records`, `journal_bytes`)
with its final newline and default JSON escaping, sorted map keys and preserved
array order. Counts must be nonnegative; windows are canonical decimal int64
Unix seconds aligned to 300 seconds, with nonempty arrays of unique, nonempty
UTF-8 probe IDs. An empty windows object is valid. There is no current-time filter
or format autodetection. Core bodies are bounded to 1,048,576 bytes inclusive.
The core contract has literal fixture coverage; actual Store/cardinality and
platform integration remain future acceptance work. A legitimate larger readiness
body would fail this bound and must be assessed in that integration stage.
