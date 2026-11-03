# Task 9：真实 hop 回放 CUSUM + 路径关联（2026-10-28）

**hop 来自 UDP traceroute（UDP 探测 + IP_RECVERR 错误队列，类似 tracepath，非特权）**，不是 raw ICMP。环境是本机 Docker 网络上的实验路径（`lab/pathchange/`），不是互联网路径，也不是 Azure。其他项目同时在跑（nproc=32，结束时 loadavg 3.1）。

## 做了什么

- 拓扑：probe → r1 →(r2 | r3)→ target，4 个 Docker bridge 网络 10.219.1–4.0/24。r3 往 target 方向有 20 ms netem 延迟。
- 探针容器：`--cap-drop ALL --user 1000:1000 --security-opt no-new-privileges`，`trace_mode: udp`，每 5 s 对 target 做一次 TCP connect 和一次 traceroute（`task-09-replay/probe.json`）。ingest 地址故意不通，结果全部留在 spool。
- 13:16:18Z 起记录基线；15:02:00Z 在 r1 上把 10.219.4.0/24 的下一跳从 r2（10.219.2.3）改成 r3（10.219.3.4），这是真实的路由变化（`switch.json`、`status-after-switch.txt`）；15:16:47Z 停止并删除全部容器和网络。
- 回放：`make t9-replay`（`TestAnomaly_ReplayRecordedPathChange`）把 1448 个 spool 批次按原样经 `store.Commit` 写进 ClickHouse 测试容器，再用 ingest 的默认参数（warmup 20、K=0.5、H=12、sigma 下限 1 ms）跑 anomaly job，截止到最后一个完整窗口（15:15Z 之前）。

## 结果（`task-09-replay/replay.json`）

- 1444 条 TCP + 1444 条 trace，trace 错误 0；spool 丢弃 0。
- 路径快照：13:16:18–15:01:55 共 1267 次都是 `10.219.1.2 10.219.2.3 10.219.4.10`；15:02:00.68 起 177 次都是 `10.219.1.2 10.219.3.4 10.219.4.10`。切换后第一次 trace（0.68 s 后）就是新路径。
- TCP connect 的 5 分钟 p95：基线 20 个窗口（13:15–14:50）均值 0.358 ms（0.26–0.53 ms，样本标准差远小于 1 ms，所以用了 1 ms 下限）；14:55 窗口 0.324 ms 无报警；15:00 窗口 20.633 ms。

| path_events | 窗口 | p95 ms | 基线 ms | path_changed | first_divergent_ttl | correlation_reason |
|---|---|---:|---:|---|---:|---|
| tcp | 15:00 | 20.633 | 0.358 | **true** | **2** | — |
| trace | 15:00 | 20.642 | 0.268 | false | 0 | trace latency has no independent latency path |
| tcp | 15:05 | 20.542 | 0.358 | false | 0 | — |
| trace | 15:05 | 20.717 | 0.268 | false | 0 | 同上 |
| tcp | 15:10 | 20.503 | 0.358 | false | 0 | — |
| trace | 15:10 | 20.588 | 0.268 | false | 0 | 同上 |

- 计划的验收条件达成：`path_events` 有写入，`Correlate` 给出变化的 TTL = 2（r2 → r3，正是改动的那一跳）。6 个事件都送到了测试用 Alertmanager（6 次 POST）。
- 第一个 TCP 报警落在切换所在的窗口（15:00–15:05）。真实运行时 job 每 5 分钟跑一次，所以报警最早在 15:05 的 tick 发出，即切换后约 3 分钟。

## 要注意的地方

- 延迟跳变很大（约 57 倍基线），所以在第一个窗口就报警，这不能说明 H=12 对小跳变的灵敏度（那个看 `README.md` 里 `TestCUSUM_ThresholdExperiment` 的阈值实验）。
- 跳变持续期间每个窗口都会再报一次（15:05、15:10），因为 CUSUM 报警后不会重置或抑制。后两个 TCP 事件的 path_changed=false 是对的：它们的"前一个快照"已经是新路径。是否要在报警后重置或抑制，还没决定。
- trace 本身的总耗时也被当成一条延迟路径来检测，并且也报警了；job 按设计拒绝给它做路径关联。
- 这是 Docker 网络上的人为切换，hop 数少（3 跳），也没有丢包或 ICMP 限速；不能代表互联网路径。setcap 或 Azure 探针（raw ICMP 模式）仍是用户的选项，没有用到。
- 原始 spool（约 25 MB）留在 `.cache/task09/spool`，没有提交；`replay.json` 里有全部窗口、事件和路径快照。
