# Task 14 金丝雀自动回滚（kind，2026-10-27）

**环境是 kind，不是 k3s 或 AKS。** 单节点 kind 集群 `pathwatch-canary`（Kubernetes v1.36.4，kind v0.33.0），跑在 home-ubuntu 上，Docker 数据在 /mnt/dev-data。测量期间其他项目也在跑：nproc=32，load average 约 6.2–7.6（每次演练的开始和结束值见各 `summary.json`）。时间只能在相同机器负载下比较。

- 源码提交：charts/tooling `d3bdf76`；镜像输入提交 `e7f4721370ba`（镜像 tag `git-e7f4721370ba`，坏版本 `-bad500`，对照 `-control`）
- 组件：Argo CD v3.5.6 Core（core-install.yaml sha256 d3d9d355…，镜像 index 8e5cc5bb…）、Argo Rollouts v1.10.1、Prometheus v3.14.0、ClickHouse 26.3.32.14
- 怎么复现：`make kind-up kind-images kind-publish`，`make kind-replace-core`（core 是 OnDelete，第一次换 spec 要手动），`make kind-wait`，再 `make kind-drill VARIANT=control` 和 `make kind-drill VARIANT=bad500`；最后 `make kind-down`
- 原始数据：`docs/evidence/task-14-canary/<run>/`，里面有 `timeline.jsonl`（每 ~1.2 s 一条，记录 Rollout/pod/AnalysisRun/Argo 状态）、`querygen.jsonl`（客户端每秒的状态码计数）、`analysisruns.json`、`rollout.json`、`applications.json`、`summary.json`

## 部署是否成立

- 4 个应用（root + observability + ingest + api）都是 Synced/Healthy：`TestClusters` 在演练前和三次演练之后各跑一次，都通过（`test-clusters.log`、`test-clusters-after-drills.log`）。
- 真实链路：querygen → `api` Service → frontend pod 的 Rust edge-proxy（TLS，SNI api.pathwatch.local）→ pod 内 loopback 查询 API → mTLS 私有 RPC → core → loopback ClickHouse。稳定状态下每秒 20/20 个 200。
- 期望状态来自本机 Git：repo-server pod 里的只读 git daemon（127.0.0.1:9418），服务 `.cache/task14/gitops/pathwatch-deploy`。每个期望状态版本都是这个仓库里的一个提交，带 `provenance.json`（源码提交、镜像 tag）。没有 push，也没有外部仓库。

## 结果

判定规则（`deploy/rollouts/ingest-rollout.yaml`）：setWeight 50 之后做分析。查询的是候选 pod 1 分钟内的查询失败比例，> 0.05 就失败；请求少于 5 个也算失败（没有数据时不放行）。initialDelay 40 s，interval 15 s，共 6 次，failureLimit 0。

时间都从"把期望状态提交到本机 GitOps 仓库"算起，单位秒。分辨率是轮询间隔，约 1.2 s。

| 演练 | Argo 同步到新版本 | 候选 pod Ready | 分析结果 | 分析判定时间 | 回滚完成* | 客户端非 200 / 总数 | revert 后恢复 Synced/Healthy |
|---|---:|---:|---|---:|---:|---|---:|
| control（好版本，换 tag） | 0.5 | 1.6 | Successful，6 次都是 0 | 115.9 | 晋升于 117.0 | 0 | — |
| bad500 #1 `20261027T224418Z` | 1.6 | 1.6 | Failed，值 1 | 41.7 | **41.7** | 401 / 900（500×10，503×384，502×7） | 2.7 |
| bad500 #2 `20261027T224530Z` | 0.5 | 1.7 | Failed，值 1 | 41.9 | **43.1** | 431 / 940（500×10，503×412，502×9） | 2.6 |
| bad500 #3 `20261027T224618Z` | 1.7 | 1.7 | Failed，值 1 | 41.8 | **41.8** | 398 / 900（500×10，503×384，502×4） | 2.7 |

\* 回滚完成的定义：Rollout `abort=true`，没有候选 hash 的 pod，稳定 hash 的 Ready pod 数等于副本数（2）。

- 从发布坏版本到回滚完成：41.7–43.1 s。其中 40 s 是 initialDelay，判定本身是第一次测量。
- 恢复：回滚后 Rollout 处于 Degraded（Argo 里 ingest 是 Synced/Degraded，期望状态仍是坏版本）。在 GitOps 仓库执行 `git revert` 后，2.6–2.7 s 回到全部 Synced/Healthy。Rollouts 会直接回到已有的稳定 ReplicaSet，不再做分析。
- 正对照：同一份二进制只换 tag，分析 6 次都是 0，116 s 后晋升。说明这套判定不会把好版本也回滚掉。

## 没达到、或者要注意的地方

- **用户看得到的影响大**：每次演练有 44–46% 的请求失败，持续约 41 s。原因是 2 个副本 + setWeight 50，候选拿到一半流量；而且 initialDelay 40 s 期间不做任何判断。500 只有 10 个，因为 edge-proxy 在连续 5 个 5xx 之后会把 pod 内的上游摘掉 30 s，之后返回 503。所以 Prometheus 看到的 frontend 失败请求很少，但比例是 1.0，照样判失败。如果要缩小影响：先 setWeight 更小的值（需要更多副本），或者缩短 initialDelay、配合更短的窗口。这次没有改，数字按当前配置记录。
- 候选 pod 约 1.6 s 就 Ready：单节点、镜像在本地、探针首次立即执行，所以这么快。多节点或要拉镜像时会慢很多。
- 不是 k3s/AKS（k3s 要 root，AKS 要配额）。没有 Terraform `helm_release`：helm provider 要从 registry.terraform.io 下载，不在允许的下载源里。bootstrap 是 `deploy/kind/lab.py` 加 kubectl server-side apply。
- 主机限制的绕法：主机 root 的 inotify instance 已经被其他项目耗尽，kind 默认的 kube-proxy 启动就失败（`too many open files`）。改成不带 `--config` 的等价参数启动（不创建文件 watcher）。调 sysctl 要 root。
- NetworkPolicy 没加（kindnet 是否执行策略没有验证）；mTLS 和 loopback 绑定仍在。
- 只测了查询路由；Report（gRPC）路由在 edge-proxy 配置里有，但演练流量里没有。
- kubeconform 内置类型用的是 v1.34.12 schema（上游最新），集群是 1.36。

## 2026-10-28 改进：更小的金丝雀步长、NetworkPolicy

**仍是 kind，不是 k3s/AKS。** 新建的 `pathwatch-canary` 集群（同样的 kind 和节点镜像，kube-proxy 绕法不变），演练源码 `f645b121a94d`，镜像 `git-bbc66daca5d7`。演练时**其他项目同时在跑**：nproc=32，1 分钟 loadavg 4.4–19.0（每次的值在各 `summary.json`）。演练后集群已删除。

改动（`deploy/charts/ingest`）：
- frontend 4 个副本，第一步 setWeight 25（maxSurge 1：多 1 个候选 pod，占 5 个 pod 中的 1 个）；initialDelay 20 s，interval 10 s，count 6（之前 2 副本、50%、40 s/15 s）。判定规则不变（> 5% 失败或少于 5 个请求都算失败）。
- NetworkPolicy：`pathwatch-app` 默认拒绝入站，只放行 frontend → core:9444、querygen → frontend:8443、`pathwatch-observe` 的 Prometheus → core/frontend:9445。
- `lab.py drill` 的 `summary.json` 新增 `client_impact`（从 querygen 每秒计数算出）。

| 演练 | 分析判定 | 回滚完成 / 晋升 | 客户端非 200 / 总数 | 有非 200 的秒数 | revert 后恢复 |
|---|---:|---:|---|---:|---:|
| control `20261028T133129Z` | 71.6 s Successful | 晋升 75.4 s | 17 / 1560（502×6，transport_error×11） | 4 | — |
| bad500 #1 `20261028T133252Z` | 22.9 s Failed | **22.9 s** | 117 / 640（503×108，500×5，502×4） | 22 | 2.8 s |
| bad500 #2 `20261028T133320Z` | 21.5 s Failed | **22.7 s** | 105 / 620（503×95，500×5，502×5） | 22 | 2.7 s |
| bad500 #3 `20261028T133347Z` | 22.1 s Failed | **22.1 s** | 98 / 620（503×89，500×5，502×4） | 22 | 2.7 s |

- 从发布坏版本到回滚完成：22.1–22.9 s（之前 41.7–43.1 s）；客户端失败比例 15.8–18.3%（之前 44–46%），持续约 22 s（之前约 41 s）。失败比例仍高于"1/5 的 pod"：edge-proxy 摘除上游后返回 503，而 querygen 每个请求新建连接，kube-proxy 随机选 pod，比例本来就有波动；没有再细查。
- 客户端统计窗口从提交前 5 s 开始，#2、#3 的窗口包含前一次演练 revert 末尾的约 1 s（例如 #2 的第一个非 200 秒和 #1 的最后一个相同），计数可能多算 1 s 的量。
- **新问题**：好版本对照出现 17 个非 200（之前 2 副本时是 0），分布在候选 pod 创建时和晋升缩容旧 pod 时。推测是 pod 终止时 endpoint 摘除和 kube-proxy 规则更新之间的空档，没有验证；没有 preStop 延迟。
- 晋升从 117 s 变为 75.4 s（initialDelay 20 s + 6 × 10 s）。
- `TestClusters` 演练前后都通过（`task-14-canary/phase4/test-clusters-{before,after}.log`）。

NetworkPolicy 是否真的执行（`make kind-netpol`，原始输出在 `task-14-canary/phase4/`）：
- 第一版用命名端口（`https` 等）。只从 Prometheus pod 测时，"该通的通、该断的断"，看起来生效（那次的输出没有保存）。但对照显示这个结论不成立：
  - 先试"删除策略再测"作对照（`netpol-control.json`、`netpol-control-2.json`）：Argo CD 的 selfHeal 几秒内就把策略建回来了，所以这种对照无效。
  - 改成"同一端口从被允许的 pod 和不被允许的 pod 各测一次"（`netpol-probe-pods.json`）：被允许的 pod 也连不上 frontend:8443，同时 querygen 的真实请求全部 transport_error。原因：`https` 这个命名端口声明在 edge-proxy 原生 sidecar（initContainer）上，kindnet 没有解析它，结果谁都不放行。
- 改成数字端口后（`netpol-numeric-ports.json`）：9 项检查全部符合预期，包括同一端口 8443 的正对照（querygen 标签的 pod 能连，其他 pod 不能）；querygen 最近 10 s 200/200 全部 200，说明 frontend → core:9444 也通。kindnet 确实执行 NetworkPolicy。
- 仍没做：Report（gRPC）路由没进演练流量；没有 egress 策略。

## 2026-11-01 (phase 5): preStop delay, Report RPCs in the drill traffic

**Still kind, not k3s/AKS**: cluster `pathwatch-canary` (kind, Kubernetes 1.36.4, one node), drill source `31ff93f89c73`, images `git-9a0132c8459b`. **Measured on a shared, loaded machine**: nproc=32, 1-minute load 10.5–15.7 at the start of each round (other projects' jobs; the coordinator said not to wait for a quiet machine). Per-round `uptime` lines: `task-14-canary/phase5-drill-load.txt`; the summaries also carry `machine_load_start/end`. The drill measures control-plane and client behaviour, not CPU latency, so it was run under load. The cluster was deleted afterwards.

**Cause of the 17 non-200 in the 10-28 control run.** Deleting a pod removes its endpoint and sends SIGTERM at the same time. The frontend closed its query listener on SIGTERM while kube-proxy and the edge-proxy sidecar still sent it traffic, giving 502 and transport errors. They appeared when the canary scaled a stable pod down and when promotion removed the old pods. Fix (`01a78d0`): a 10 s `preStop` sleep on the frontend container (`frontend.preStopSleepSeconds`), with a test that the grace period covers the sleep plus the 45 s drain. The edge-proxy native sidecar is only stopped after the frontend exits, so it keeps serving through the sleep.

Traffic: querygen sends 20 queries/s (new connection each) and, new this phase, 2 Report RPCs/s on one long-lived gRPC connection through the edge proxy. `summary.json` now has `client_impact_report` (non-`OK` codes) next to `client_impact`.

| Run | Analysis | Rollback / promotion | Query non-200 / sent | Report non-OK / sent |
|---|---:|---:|---|---|
| 10-28 control (before) | 71.6 s Successful | promoted 75.4 s | 17 / 1560 (502×6, transport_error×11) | not measured |
| control `20261101T072918Z` | 71.7 s Successful | promoted 74.0 s | 2 / 1600 (429×2) | 0 / 160 |
| control `20261101T121713Z` | 71.7 s Successful | promoted 74.1 s | 0 / 1600 | invalid: journal full (see below) |
| control `20261101T122816Z` (fresh cluster) | 72.5 s Successful | promoted 75.0 s | 1 / 1620 (429×1) | 127 / 162 (Unavailable, 64 s) |
| bad500 `20261101T073034Z` | 22.2 s Failed | **23.4 s** | 194 / 640 (503×92, 429×56, transport_error×41, 500×5) | 38 / 64 (Unavailable) |
| bad500 `20261101T121959Z` | 22.3 s Failed | **22.3 s** | 98 / 620 (503×93, 500×5) | invalid: journal full |
| bad500 `20261101T122156Z` | 22.3 s Failed | **22.3 s** | 86 / 620 (503×81, 500×5) | invalid: journal full |
| bad500 `20261101T123102Z` (fresh cluster) | 22.0 s Failed | **22.0 s** | 96 / 600 (503×91, 500×5) | 0 / 60 |
| bad500 `20261101T123259Z` (fresh cluster) | 22.4 s Failed | **23.5 s** | 92 / 640 (503×87, 500×5) | 0 / 64 |

Code totals per run: `task-14-canary/phase5-code-totals.txt`. Old bad500 rollbacks (10-28): 22.9 / 22.7 / 22.1 s.

- **Control queries**: the scale-event errors are gone (17 → 2, 0, 1). The remaining non-200 are single 429s (`overloaded`: the frontend's query admission limit is full), which also appear in steady state (25 of 12,000 queries in a 600 s baseline on this cluster, 0.21%).
- **Bad version**: from publishing it to rollback complete took 22.0–23.5 s (10-28: 22.1–22.9 s). Query failures were 13.9–16.0% over about 21 s in four runs. The fifth (`073034Z`) also had 41 transport errors and 55 429s in the 3 s from 07:30:38. It started 2 s after the previous control run promoted, while that run's four old pods were still shutting down. Later runs had a 90 s gap and had none of these errors. The cause was not investigated.
- **Report, new finding (fixed 2026-11-06, see phase 6)**: Report goes over one long-lived HTTP/2 connection, so it stays pinned to one pod. When that pod is deleted, every Report fails with `Unavailable` from SIGTERM (deletion + the 10 s preStop) until the pod exits about 63 s later. In `122816Z` the failures started at 12:28:28, 10 s after the canary step scaled a stable pod down, and ended at 12:29:31. In `073034Z` they started at 07:30:42, 10 s after the previous promotion deleted the old pods, and lasted until 07:32:01, past the rollback. Likely mechanism (from the timing, not traced): the frontend's gRPC server stops on SIGTERM, but the client's connection to the edge-proxy sidecar stays open until the sidecar is stopped after the frontend exits. The client is never told to reconnect elsewhere. When the client's pod is not the one deleted, Report stays 100% OK (`123102Z`, `123259Z`). This is not caused by the preStop sleep, which only moves the window 10 s later; the 10-28 setup did not carry Report traffic. A likely fix is for the edge proxy to send GOAWAY to downstream connections when its upstream stops serving, or to cap connection age. It is listed in STATUS. Correction from phase 6: the window was about 81 s, not 63 s, because both runs' querygen logs end while Reports were still failing (last lines 12:29:31 and 07:31:00, both Unavailable). The sidecar was not stopped after the frontend exited: it ignored SIGTERM and was killed at the end of the 90 s grace period.
- **Journal capacity (lab artefact)**: after about 10 h of 2 Report/s, core rejected every batch with `ResourceExhausted`. The journal never evicts keys and holds 65,536 records / 32 MiB in the lab chart, so a full journal correctly fails closed. Report numbers from the runs at 12:17–12:22 are therefore invalid. The cluster was recreated with a fresh journal and the control + 2 × bad500 rounds rerun (`1228`–`1233`).
- Run `20261101T073102Z-control` did not complete. It published the control image, which was already the stable image after the previous rollback, so no canary started. It is kept as is. Before the next control run the lab published the good image again.
- `TestClusters` passed before and after the drills (`task-14-canary/phase5-test-clusters-{before,after}.txt`; `.log` is gitignored).

## 2026-11-06 (phase 6): Report connections drained on pod deletion

**Still kind, not k3s/AKS**: a fresh single-node `pathwatch-canary` cluster (Kubernetes 1.36.4) for each side, built with `make kind-up kind-images kind-publish kind-wait`, deleted afterwards. Shared machine: nproc=32, 1-minute load 6.0 at the start of the after run and 6.4 at the end of the before run (its start was not recorded). Before: images `git-49753495aaf4` (the public main). After: `git-3a4fe535c593` (`3a4fe53`). Raw: `raw/task14-report-drain/` (`podkill.jsonl` per deletion, `querygen.jsonl`, `summary.json`).

Method, `make kind-podkill N=5`: before each deletion it waits for 4 Ready frontend pods and 20 s of traffic (after the fix also 10 s of all-OK Reports). It then finds the pod holding querygen's Report connection: the querygen connection ESTABLISHED in two conntrack snapshots on the kind node, 1.5 s apart (queries use a new connection each). It deletes that pod, polls it every 0.5 s until it is gone, keeps 10 s more traffic, and counts querygen's per-second lines from 5 s before the deletion. The window runs from the start of the first second with a failed Report to the end of the last one, in seconds after the deletion (T). This is the span phase 5 called "about 63 s". Phase 5 took the last minus the first failing line, which is 1 s shorter: 80 s instead of 81 s below.

**Mechanism: confirmed, window corrected.** Before the fix, in all five deletions:
- Reports failed with `Unavailable` from T+9.6–10.1 s, when the frontend got SIGTERM at the end of the 10 s preStop sleep (frontend exit code 0, finished at T+9–10 s).
- The edge-proxy sidecar exited with code 137 at T+89.3–89.7 s (3 records; in the other 2 the pod disappeared between polls): it was SIGKILLed at the end of the 90 s grace period. It runs as PID 1 and only handled SIGINT, and PID 1 receives only signals it handles, so the SIGTERM the kubelet sends a native sidecar after the frontend has exited did nothing.
- A side observer sampled querygen's conntrack rows about once a second (`conntrack.txt`). The Report connection to the deleted pod was ESTABLISHED until T+89.3–89.8 s, and the next long-lived connection, to another pod, first appeared at T+90.5–91.5 s. The client was never told to reconnect.

**Fix (`3a4fe53`).** Only the edge proxy can tell the client to reconnect, because it holds the client's connection. It has to do so while the pod still serves. A native sidecar gets SIGTERM only after the frontend has exited, and the frontend's own GOAWAY to the proxy comes after it has already stopped admitting Reports at SIGTERM. So:
- edge proxy: `POST /drain` on its loopback management listener (127.0.0.1:9092) starts drain mode. The listeners keep serving, and every downstream connection is shut down gracefully once it has served a request (HTTP/2 GOAWAY, HTTP/1.1 `Connection: close`). This includes connections accepted later, because a client can reconnect to the same pod before kube-proxy drops its endpoint. SIGTERM is now handled like SIGINT.
- frontend preStop hook: `/adminbridge pre-stop -drain 127.0.0.1:9092 -wait 10s` requests the drain, then waits the same 10 s as the old sleep. The wait runs in full even if the drain request fails.
- Not chosen: a maximum connection age only bounds the window by that age and reconnects every client periodically. Client retry on `Unavailable` would go over the same pinned connection, and every client would need it.

| Deletion | Before: Report non-OK / sent | Before: failing span (s after T) | Before: edge-proxy exit | Before: pod gone (s) | After: Report non-OK / sent | After: edge-proxy exit | After: pod gone (s) | After: Report connection closed (s) |
|---|---|---|---|---:|---|---|---:|---:|
| 1 | 161 / 214 | 10.1–91.1 (81) | 137 at 89.7 | 91.4 | 0 / 52 | 0 at 9.9 | 10.9 | 0.1 |
| 2 | 161 / 214 | 9.7–90.7 (81) | 137 at 89.3 | 91.7 | 0 / 50 | 0 at 10.0 | 10.9 | 0.1 |
| 3 | 160 / 212 | 10.0–91.0 (81) | not seen | 91.0 | 0 / 52 | not seen | 10.9 | 0.1 |
| 4 | 161 / 214 | 10.1–91.1 (81) | 137 at 89.7 | 91.5 | 0 / 52 | 0 at 10.1 | 11.0 | 0.1 |
| 5 | 161 / 212 | 9.6–90.6 (81) | not seen | 91.0 | 0 / 54 | 0 at 10.2 | 11.5 | 0.1 |

All failed Reports before the fix were `Unavailable`. The after windows are shorter (the pod is gone sooner), so fewer Reports fall in them. The window dropped to 0 in all five deletions, at 2 Reports/s and 1 s resolution. Query non-200 in the same windows, all 429: before 0, 1, 3, 1, 0; after 0, 0, 0, 2, 4.

Control drills after the fix, on the after cluster:

| Run | Analysis | Promotion | Query non-200 / sent | Report non-OK / sent |
|---|---:|---:|---|---|
| control `20261106T154714Z` (40 s after the podkill run) | 71.0 s Successful | 73.4 s | 101 / 1580 (transport_error×41, 429×60) | 0 / 158 |
| control `20261106T155239Z` (good build republished first, then 90 s gap) | 71.4 s Successful | 73.8 s | 2 / 1580 (429×2) | 0 / 158 |

The republish rollout between them also had 0 failed Reports (2 query 429s). In the first drill, queries failed with transport errors in the seconds ending 15:47:19–20, then 57 429s in one second. That was about 3 s after the canary step, which started the canary pod and deleted one stable pod at 15:47:15. No stable pod failed readiness at that time; one reported "core unavailable" at 15:47:50, which coincides with the last 3 429s. Phase 5's `073034Z`, before this change, had the same signature (41 transport errors, 55 429s in 3 s). The drain only changes connections on the pod being deleted, and the five podkill deletions had no transport errors, so the burst is not attributed to the drain. Its cause was not investigated. Only `summary.json` and `querygen.jsonl` of the two drills are kept, under `raw/task14-report-drain/`.

Discarded and repeated:
- The first after-fix podkill attempt (05:01 UTC) ran on a cluster whose core had failed closed at startup: ClickHouse logged a broken pipe at 05:01:15, and core `/readyz` failed from then on. Reports were already failing 5 s before the deletion, and the replacement pod never became Ready. The attempt was discarded, and `podkill` now waits for 10 s of OK Reports first (`cc66428`). That cluster then ran for about 10 h, and its journal filled (`ResourceExhausted`) as on 11-01. It was deleted and a fresh cluster was built for the after run.
- The before run's `summary.json` was rebuilt from its records after the run: the `podkill` version at that time stopped at the summary step because the fix was still uncommitted.

Limits: one machine, kind, 5 deletions per side, one client holding one Report connection at 2 RPC/s, and 1 s resolution from querygen's lines. A connection that has not sent a request when the drain starts gets GOAWAY only at its first request.
