# Task 17 BGP 实验：anycast 最近 PoP、路由泄露和 /25 劫持的检测（2026-10-27）

- 环境：home-ubuntu，Docker bridge 网络（不是 containerlab），FRR 10.7.1、GoBGP 4.10.0（release tar sha256 05a1c9fa… 已核对）、Krill 0.16.0、Routinator 0.15.3。机器上同时有其他项目在跑，另外 Task 13 的基准在绑核运行。nproc=32，load average 3.9–6.9（每次的值在 `summary.json` 里）。
- 拓扑和怎么复现：`lab/bgp/README.md`、`lab/bgp/topology.yml`、`lab/bgp/frr/*.conf`
- 原始数据：`docs/evidence/task-17-bgp/`。`test-lab-live.log`；`20261027T231254Z-leak/`、`20261027T231427Z-hijack/`（每次的 `detector-N.jsonl` 和 `summary.json`）；`collector-neighbors.txt`；`nearest-final.json`

## 最近 PoP（`test_anycast_nearest_pop` 通过）

| 客户 | traceroute 第一跳 | 应答的 PoP（向 198.51.100.1:80 发 HTTP） | AS path |
|---|---|---|---|
| c1（AS64701） | 10.117.6.3（t1a） | pop1 | 64601 64500 |
| c2（AS64702） | 10.117.7.3（t1b） | pop2 | 64601 64500 |
| c3（AS64703） | 10.117.9.3（t2） | pop3 | 64602 64500 |

最后一跳都是 198.51.100.1。是哪个 PoP，由该地址上的服务回答自己的名字来判断，因为 traceroute 看不出同一个 anycast 地址后面是哪台。

## RPKI

Krill testbed 的 `testbed` CA 签发 ROA 198.51.100.0/24-24 => AS64500。Routinator 只加载这个 TAL，经 RRDP 取回对象并做密码学验证。验证 API 的结果：AS64500 /24 valid；AS64703 /25 invalid；AS64500 /25 invalid（超出 maxLength）；AS64701 192.0.2.0/26 not-found。GoBGP 和 Routinator 的 RTR 会话 Up，收到 1 条 VRP。这是隔离的测试信任锚，不代表任何真实 RIR 的授权。

## 事件与检测时间（各 5 次）

时间从"在路由器上执行注入配置之前"算起，到检测器打印告警为止，用的是主机挂钟。检测器每 0.25 s 轮询一次，所以分辨率约 0.25 s。注入前有 5 s 基线，5+5 次基线里告警都是 0。

| 事件 | 规则 / 来源 feed | 检测时间 min / 中位 / max (s) | 数据面影响 | 撤销后恢复 |
|---|---|---|---|---|
| 路由泄露（c2 把上游路由导出给所有邻居） | route-leak，c2 feed（stub 宣告 cone 外前缀） | 0.167 / 0.178 / 0.423 | t2 到 c1 的最优路径变成 `64702 64601 64701`（经 c2 绕行；客户路由 lp 200 压过 peer）；anycast 最近 PoP 不变 | 0.9–1.0 s |
| | route-leak，t2 feed（valley：AS64702 把从 AS64601 学到的路由转给了 AS64602） | 0.415 / 0.449 / 0.457 | | |
| /25 劫持（c3 宣告 198.51.100.0/25） | rpki-invalid，t2 feed | 0.178 / 0.179 / 0.443 | 三个客户都去了 c3（c1/c2 的路径是 `64601 64602 64703`），anycast 服务全部不可达（HTTP 无应答） | 2.5–2.6 s |
| | rpki-invalid，t1a / t1b feed | 0.426–0.447 / 0.435–0.456 | | |

每次第一条告警的时间：泄露 0.415、0.167、0.176、0.178、0.183 s；劫持 0.426、0.179、0.179、0.179、0.178 s。

## 边界

- 不是 containerlab。路由器容器加了 SYS_ADMIN（FRR 10.x 的要求），这一点和 10-25 预检时 FRR 8.4.4 只需要 NET_ADMIN 的结论不同。
- 检测时间几乎全是轮询间隔和 gobgp CLI 调用的开销。这里的 FRR 都在同一台机器上，eBGP 传播约在亚秒级；真实互联网上的传播和采集器延迟要大得多，这些数字不能外推。
- 泄露检测依赖已知的 AS 关系表（`topology.yml`）。真实环境里关系要靠推断（例如 CAIDA AS-rank），会有误报和漏报。这次的基线没有误报，只说明在这个小拓扑里没有。
- ROV 只用于检测，路由器不丢弃 invalid 路由，所以劫持照样传播。没有测"开启 ROV 过滤后劫持被阻止"。
- 没做 ASPA；Krill 支持，但计划里没有要求。
