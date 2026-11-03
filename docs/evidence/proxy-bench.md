# Envoy / NGINX / PathWatch 固定速率对照（Task13）

完整120格矩阵已实际运行，独立数据复审待完成。结果没有支持“PathWatch全面优于成熟代理”的结论：H1延迟接近NGINX，CPU略高；H2在10k/20k RPS时PathWatch的p50约10ms，明显落后于NGINX，也高于Envoy的p50。三者均接近计划吞吐、未报告请求错误，但仍有窗口末未完成及未启动的计划请求，不能称100%计划负载完成。

SOURCE：`24f13eddaf2235beebb65bcb5ae05e9de80b2280`；实际RUN_HEAD：`4db893d9633fb0523b57dc10794b367a83c4be58`。2026-10-25 **09:31:54–14:04:00 UTC**连续运行，同一session91672正常exit0；总墙钟16,326.509秒（4小时32分6.509秒）。120格原生实际测量总计**14,400.309319秒**，独立wall总计14,421.867298秒；预热原生总计1,800.577735秒、wall总计1,817.837974秒。每格配置120秒+15秒，原生测量范围119.999768–120.011012秒、预热14.998787–15.010000秒，符合已审查50ms启动/显示容差。没有借预热补测量、跳格、重跑、拼接smoke或改动产品。

[完整manifest](raw/task13/matrix01/manifest.json)、[终止回执](raw/task13/matrix01-terminal.json)、[24组机器可读汇总](raw/task13/matrix01-summary.json)、[原始分析输出](raw/task13/matrix01-summary.md)、[逐格审计](raw/task13/matrix01-audit.json)与[审计脚本](raw/task13/matrix01-audit.py)可核对。原始每格目录保留load、warmup、pidstat、进程快照、监测、TLS观察、结果；60个H2目录另保留完整有界直方图和collector统计。独立数据复审前，本文不自行批准Task13完成。

## 条件和复现

本机13th Gen Intel Core i9-13900K、Linux7.0.0-38、32逻辑CPU，非专用服务器或跨机器容量结论。三代理共用一个Go stdlib HTTP/1.1 echo，固定1024B响应；等价的是单端点选择，**不是多后端LB算法比较**，NGINX least_conn不等价于逐请求P2C。

代理2处理worker绑定CPU16–17，生成器4线程18–21、Go2线程22–23、collector24、协调/pidstat25。NGINX master+2workers全部采CPU；PathWatch/Envoy包括同进程辅助线程。共同100下游连接，同证书/SNI、TLS1.3，实际每格100次TLS握手；H1每连接单在途，H2每连接最多16流。测量前独立15秒预热，然后建立新下游连接；上游池可延续预热连接。四个速率1k/5k/10k/20k，五轮轮转代理顺序，H1和H2各60格。

工具为wrk2固定`44a94c17d8e6a0bac8559b53da76848e430cb7a7`、nghttp2/h2load1.70.0、Envoy1.39.5、NGINX1.30.6、Go1.27.3；PathWatch使用现有Rust1.99.1离线locked release构建。工具来源/散列见[锁文件](../../bench/tools.lock.json)。manifest绑定48个源文件、配置、二进制、Lua模块、证书、实际动态库及相关执行文件，记录环境和实际命令，前后全部hash不变。wrk的绝对LUA_PATH指向同一固定提交未修改的wrk.lua；不依赖运行cwd下的可变模块。

详细机制与复现入口见[bench/README.md](../../bench/README.md)。H1用wrk2 `-R`，H2用h2load `--rps=总RPS/100`（不是`-r`连接速率）。同协议代理条件一致，但保留原产品机制差异：NGINX空闲cache不是相同active连接硬限，PathWatch固有最多一次有预算重试，Envoy300秒idle只配置在下游HCM、上游默认未覆盖；超时和排队机制亦非完全等价。NGINX上下游keepalive请求数上限均1e9。负载工具不验证证书信任，受信CA验证只属于单独预检；预检校验样本body，完整H2只有状态和总body字节交叉核对，不能声称逐响应内容校验。

```sh
# 重新实验须重新确认资源与CPU空闲，并使用全新输出名
bench/run.sh --full --output NEW-MATRIX
# 本次已完成证据的分析，无需重跑负载
python3 bench/analyze.py docs/evidence/raw/task13/matrix01 --output /tmp/task13-summary.json
```

## 五轮结果

实际RPS=五轮完成数/五轮工具实际时长；延迟为**五轮各分位数的中位数**，不是合并样本分位数，也没有置信区间。CPU为五轮代理CPU秒/五轮完成数，单位µs/完成。H1延迟使用wrk2 Recorded HDR，包含CO修正、约10秒原生校准不进入延迟谱以及约±1ms调度精度限制；H2延迟从实际request_time算起，不含全部计划排队等待，原生调度有10ms批次。因此分开两表，不能据此横向宣称H2比H1更快。

H2每格65536个uint64桶（512KiB），<2048µs逐整数精确，其余桶宽/值<1/1024≈0.0977%；表中保留nearest-rank量化上下界。成功状态与失败流分母分开；没有clip高延迟、删除错误或取最好一轮。H1原输出自身有舍入，显示三位小数不增加测量精度。

### H1

|协议|RPS|代理|实际RPS|p50 ms|p99 ms|p99.9 ms|CPU us/完成|错误类别|期末未完成|
|---|---:|---|---:|---:|---:|---:|---:|---|---:|
|h1|1000|pathwatch|996.59|1.410|4.950|7.220|177.75|http=0, connect=0, read=0, write=0, timeout=0|136|
|h1|1000|envoy|997.79|1.560|3.500|4.900|315.06|http=0, connect=0, read=0, write=0, timeout=0|82|
|h1|1000|nginx|998.62|1.120|3.100|4.370|152.97|http=0, connect=0, read=0, write=0, timeout=0|15|
|h1|5000|pathwatch|4985.93|1.190|3.690|5.640|120.11|http=0, connect=0, read=0, write=0, timeout=0|46|
|h1|5000|envoy|4983.90|1.740|6.350|8.510|145.13|http=0, connect=0, read=0, write=0, timeout=0|103|
|h1|5000|nginx|4987.90|1.210|3.590|5.650|114.05|http=0, connect=0, read=0, write=0, timeout=0|61|
|h1|10000|pathwatch|9967.62|1.160|3.270|4.640|72.57|http=0, connect=0, read=0, write=0, timeout=0|139|
|h1|10000|envoy|9971.56|1.600|4.060|7.610|86.68|http=0, connect=0, read=0, write=0, timeout=0|101|
|h1|10000|nginx|9963.65|1.340|3.790|5.220|66.15|http=0, connect=0, read=0, write=0, timeout=0|108|
|h1|20000|pathwatch|19903.47|1.410|3.090|5.080|44.17|http=0, connect=0, read=0, write=0, timeout=0|176|
|h1|20000|envoy|19926.85|3.030|7.410|18.030|71.87|http=0, connect=0, read=0, write=0, timeout=0|250|
|h1|20000|nginx|19950.67|1.470|3.260|5.020|41.09|http=0, connect=0, read=0, write=0, timeout=0|166|

### H2

|协议|RPS|代理|实际RPS|p50 ms|p99 ms|p99.9 ms|CPU us/完成|错误类别|期末未完成|
|---|---:|---|---:|---:|---:|---:|---:|---|---:|
|h2|1000|pathwatch|999.95|1.867|10.720–10.727|14.640–14.647|156.62|failed=0, errored=0, timeout=0|0|
|h2|1000|envoy|999.95|7.060–7.063|18.544–18.559|21.280–21.295|235.29|failed=0, errored=0, timeout=0|0|
|h2|1000|nginx|999.97|0.710|5.292–5.295|8.880–8.887|149.97|failed=0, errored=0, timeout=0|0|
|h2|5000|pathwatch|4999.31|5.040–5.043|21.712–21.727|23.120–23.135|125.87|failed=0, errored=0, timeout=0|310|
|h2|5000|envoy|4999.67|2.656–2.657|8.912–8.919|13.552–13.559|151.03|failed=0, errored=0, timeout=0|0|
|h2|5000|nginx|4999.73|0.320|3.348–3.349|5.556–5.559|116.25|failed=0, errored=0, timeout=0|0|
|h2|10000|pathwatch|9998.90|10.168–10.175|11.600–11.607|13.544–13.551|73.93|failed=0, errored=0, timeout=0|472|
|h2|10000|envoy|9999.76|0.966|3.734–3.735|7.072–7.075|90.33|failed=0, errored=0, timeout=0|63|
|h2|10000|nginx|9999.76|0.187|2.116–2.117|4.920–4.923|69.24|failed=0, errored=0, timeout=0|33|
|h2|20000|pathwatch|19996.86|10.376–10.383|11.344–11.351|14.096–14.103|44.60|failed=0, errored=0, timeout=0|1050|
|h2|20000|envoy|19998.43|2.240–2.241|37.472–37.503|111.104–111.167|74.29|failed=0, errored=0, timeout=0|398|
|h2|20000|nginx|19998.57|0.193|2.466–2.467|5.940–5.943|43.68|failed=0, errored=0, timeout=0|64|

H1完成64,581,578、started64,582,961、窗口末未完成1,383；H2完成/成功64,795,344、started64,797,734、窗口末未完成2,390。每协议计划64,800,000。H1的HTTP/connect/read/write/timeout计数均0；H2的failed/errored/timeout均0、全部日志状态200。相应报告错误率为0，但各错误类别可能重叠，不能相加当独立失败请求数，未完成不能当成功。本实验未确定三者的饱和点或最大容量；接近固定负载上限不能证明最大吞吐能力。吞吐与计划的差距保留：例如20k H1 PathWatch为19,903.47RPS，未因不足20k而重测。

## CPU、内存和连接复用

pidstat逐秒Σ%CPU/100，不用`-I`归一化；分母是完成响应，不是计划请求。全部120格完整PID集覆盖，NGINX每格3个进程；进程身份/affinity未变。原始/proc ticks独立对照，最大单格边界差0.0743 CPU秒。下表CPU跨该协议全部20格累计；owned RSS是0.25秒采样的代理、backend、生成器、collector等已知存活任务合计，非代理独占峰值；代理RSS仅测量前后边界快照，非连续峰值，也非独占物理内存。

|协议|代理|pidstat CPU秒|/proc CPU秒|单格最大绝对差秒|owned RSS采样峰值MiB|代理边界RSS最大MiB|
|---|---|---:|---:|---:|---:|---:|
|h1|pathwatch|1427.0754|1427.32|0.0287|53.18|14.75|
|h1|envoy|2000.5807|2000.96|0.0364|112.46|73.59|
|h1|nginx|1320.3262|1320.52|0.0269|70.96|32.49|
|h2|pathwatch|1450.2254|1450.77|0.0489|124.16|24.25|
|h2|envoy|2027.7119|2028.43|0.0743|214.23|112.05|
|h2|nginx|1378.2437|1378.82|0.0478|138.30|35.54|

每代理/协议20格均2000次下游TLS握手，证明大量请求在连接上复用。上游统计是**测量窗口增量**，预热已建连接不进入新增数，所以requests/新增TCP只是观察比，不是全生命周期每连接请求数。零新增格的单格比值记未定义，不能解释为没有复用；下表比值合并所有20格增量。

|协议|代理|完成数|测量窗口新上游TCP|零新增连接格数/20|上游requests/新增TCP|完成/下游TLS握手|
|---|---|---:|---:|---:|---:|---:|
|h1|pathwatch|21,512,455|180|9|119,516.4|10,756.2|
|h1|envoy|21,528,367|112|8|192,222.3|10,764.2|
|h1|nginx|21,540,756|161|6|133,795.7|10,770.4|
|h2|pathwatch|21,597,444|560|6|38,570.1|10,798.7|
|h2|envoy|21,598,913|523|4|41,298.9|10,799.5|
|h2|nginx|21,598,987|607|12|35,583.2|10,799.5|

H2 backend收到的请求总数分别为PathWatch21,599,271、Envoy21,599,349、NGINX21,599,032，比各自started少5/25/52，且大于完成数；边界在途计数属于不同事件，未将它们伪装完全一致。全部H2完成数=FIFO writes=collector records，success=histogram总数；body字节数=1024×success，60格差值均0。

## H2采集影响与落后项

真实非零事件证明观察器覆盖：H2共64,795,344次FIFO写入，60格无write error/partial write；120格各100次TLS事件。PathWatch/Envoy/NGINX累计FIFO调用内时间分别62.145/44.732/62.656秒，最大单次15.939/8.188/18.230ms，collector最大完成到收取时差15.971/8.209/18.317ms。调用时间包括系统调用开销与可能阻塞且可并行，不能直接当墙钟阻塞占比。0.25秒采样未见pipe_write阻塞，不证明没有反压；短暂阻塞可能漏采。日志计数一致证明没有静默丢失，**不能证明采集没有改变延迟或负载**。直方图固定容量、全部尾部保留；没有额外关闭采集的实验来量化扰动。

PathWatch在20k H1的p50/p99/p99.9为1.410/3.090/5.080ms，NGINX为1.470/3.260/5.020ms，CPU44.17比41.09µs/完成高约7.5%。1k H1 PathWatch p99也高于NGINX；不能用单一有利rate概括全部结果。PathWatch在20k H2的p50为10.376–10.383ms，约为NGINX0.193ms的53.8倍、Envoy2.240–2.241ms的4.6倍；但Envoy p99.9为111.104–111.167ms，尾部在该rate比PathWatch更差。PathWatch在1k H2 p50低于Envoy，但所有H2 rate均高于NGINX。CPU在全部8个协议×速率对照中，PathWatch对相应NGINX高约2.1%–16.2%，对Envoy则较低；没有把延迟与CPU折成一个“胜出”分数。

只读源码给出后续定位线索，**尚非因果证明**：[listener.rs](../../proxy/crates/edge-proxy/src/listener.rs)接受下游socket后未显式set_nodelay，[upstream.rs](../../proxy/crates/edge-proxy/src/upstream.rs)则对上游明确set_nodelay(true)，并为lease/body期限使用锁、任务和池归还调度。这些TCP/TLS帧与每次exchange调度路径、H2批次请求和池机制都值得后续单变量诊断；当前没有受控改动实验，不能断言Nagle或某个锁就是根因。成功HTTP1上游的高复用不等于证明下游或调度无开销。本任务未调产品，也未另开实验寻找更好结果。

## 2026-10-27 H2 复测：下游 TCP_NODELAY

- 原因：多个 H2 流同时在途时，Nagle 让同一连接上的第二个响应等客户端的延迟 ACK。h2load 每 10 ms 发一批请求（顺带 ACK），所以表现为 p50 约 10 ms。
- 复现测试 `proxy/crates/edge-proxy/tests/h2.rs` 的 `h2_small_request_latency`（1 条连接、每轮 4 个并发流、400 个样本）：修复前两次 p90 = 45.668 ms、42.669 ms（Linux 延迟 ACK 40 ms），修复后 p90 约 4 ms。日志：`task-13-h2/test-before-fix-{1,2}.log`。
- 修复：`listener.rs` 对 accept 的下游 socket `set_nodelay(true)`（commit a7b255b）。
- 复测：`make bench-matrix PROTOCOL=h2 PROXY=pathwatch OUTPUT=h2-nodelay-20261027-b`，只跑 PathWatch，4 档 × 5 轮 × 120 秒，条件与 matrix01 相同。原始数据 `raw/task13/h2-nodelay-20261027-b/`，汇总 `raw/task13/h2-nodelay-20261027-b-summary.{json,md}`。`raw/task13/h2-nodelay-20261027/` 是第一次启动被磁盘检查拒绝后留下的空目录。

| RPS | p50 修复前 → 后 (ms) | p99 修复前 → 后 (ms) | p99.9 修复前 → 后 (ms) | CPU µs/完成 前 → 后 | NGINX p50 / p99（10-25） |
|---:|---|---|---|---|---|
| 1000 | 1.867 → 0.530 | 10.720 → 8.036 | 14.640 → 12.432 | 156.62 → 76.96 | 0.710 / 5.292 |
| 5000 | 5.040 → 0.480 | 21.712 → 5.848 | 23.120 → 7.728 | 125.87 → 67.22 | 0.320 / 3.348 |
| 10000 | 10.168 → 0.342 | 11.600 → 1.866 | 13.544 → 5.232 | 73.93 → 55.03 | 0.187 / 2.116 |
| 20000 | 10.376 → 0.658 | 11.344 → 2.444 | 14.096 → 5.880 | 44.60 → 43.25 | 0.193 / 2.466 |

（H2 分位数取量化下界，上界差 < 0.01 ms，见汇总文件。）20 格全部 measured，failed/errored/timeout 都是 0，期末未完成合计 0/0/79/79（修复前 0/310/472/1050）。

结论和边界：
- 10 ms 平台消失；20k rps 的 p50 降到原来的 1/16，CPU 每请求在低速率下也减半。
- 仍落后 NGINX：p50 是它的约 0.75–3.4 倍，1k/5k 的 p99 也更高（8.0 比 5.3 ms，5.8 比 3.3 ms）。原因没查。
- NGINX、Envoy 没有同日重跑，对比用的是 10-25 的 matrix01；同机同配置，但不是同一次运行。
- H1 也走同一个下游 socket，这次没重测，H1 表格仍是修复前的数字。（10-27/28 已补测，见下一节。）

## 2026-10-27/28 三个代理重跑（H2 同日；H1 分两天）

- 源码：PathWatch 含 TCP_NODELAY 修复；bench 输入与 10-25 相同；4 档 × 5 轮 × 120 s，三个代理每轮交替顺序，绑核相同。为了让每段后台任务不超过 2 h，按轮次拆成 4 段（`--rounds`），用 `analyze.py` 合并：
  - H2：`raw/task13/sameday-20261027-h2-r012/`（10-27 15:52–17:14）+ `sameday-20261027-h2-r34/`（17:14 起）→ `sameday-20261027-h2-summary.{json,md}`
  - H1：`sameday-20261027-h1-r012/`（10-27 20:03–21:25）+ `sameday-20261028-h1-r34/`（10-28 01:03 起）→ `sameday-h1-summary.{json,md}`。会话中断，第 3–4 轮晚了约 3.5 小时才跑，**H1 不是同一天、同一负载**。
- 120 格全部 measured，所有错误计数为 0。
- **其他项目同时在跑**。每格每 0.25 s 采一次主机 1 分钟 loadavg（nproc=32）：H2 两段合计 min/中位/max = 1.87/5.08/12.41；H1 第 0–2 轮 1.62/7.03/12.1；H1 第 3–4 轮 2.05/7.85/16.63。比较只在同一次运行、相同负载下成立。和 10-25 的绝对数字不能直接比：三个代理的 CPU µs/请求这次都低很多（例如 NGINX H1 1k 是 49.6 对 153.0），说明机器条件不同。

| 协议 | RPS | p50 ms PathWatch / Envoy / NGINX | p99 ms PathWatch / Envoy / NGINX | CPU µs/完成 PathWatch / Envoy / NGINX |
|---|---:|---|---|---|
| H2 | 1k | 0.520 / 1.078 / 0.135 | 6.59 / 13.86 / 1.24 | 64.2 / 115.1 / 53.3 |
| H2 | 5k | 0.349 / 0.948 / 0.129 | 2.87 / 4.13 / 1.22 | 57.3 / 92.3 / 54.6 |
| H2 | 10k | 0.318 / 0.861 / 0.108 | 1.43 / 2.95 / 0.63 | 50.3 / 83.9 / 45.3 |
| H2 | 20k | 0.614 / 2.200 / 0.197 | 2.29 / 35.46 / 1.82 | 42.5 / 72.6 / 36.2 |
| H1 | 1k | 0.940 / 1.140 / 0.900 | 2.59 / 3.34 / 2.23 | 64.8 / 117.2 / 49.6 |
| H1 | 5k | 0.990 / 1.410 / 1.040 | 2.96 / 5.74 / 2.70 | 46.5 / 89.3 / 38.6 |
| H1 | 10k | 1.070 / 1.750 / 0.980 | 3.17 / 10.02 / 3.19 | 40.2 / 80.0 / 32.8 |
| H1 | 20k | 1.350 / 3.420 / 1.160 | 5.14 / 948.22 / 7.26 | 36.8 / 74.8 / 29.8 |

（H2 取量化下界，完整上下界、p99.9 和期末未完成数在汇总文件里。）

结论：
- H2：PathWatch 在所有档位的 p50 和 p99 都优于 Envoy（p50 是 Envoy 的 0.28–0.48 倍），但仍明显落后 NGINX：p50 是它的 2.7–3.9 倍，p99 是 1.3–5.3 倍。修复 TCP_NODELAY 之后剩下的差距是真实的，原因没查。基准的上游是 HTTP/1.1，所以差距出在下游 H2 处理或每请求的池租用路径上，具体是哪个还没有做单变量实验。
- H1：PathWatch 的 p50 和 NGINX 接近（0.95–1.16 倍），20k 时 p99 反而更低（5.14 比 7.26 ms），但 p99.9 高于 NGINX 的有 1k/10k 两档。CPU 每请求是 NGINX 的 1.21–1.31 倍。
- Envoy H1 20k 的 p99 为 948 ms：这是测量到的事实（5 轮中位数），原因没查；同一档 H2 没有出现。
- 这仍然只有单后端，不是负载均衡算法的对比。

## 2026-10-28 H2 差距的单变量诊断：tokio worker 数

方法：`make bench-diag VARIANTS=pathwatch,pathwatch-w1,nginx OUTPUT=diag-20261028-h2-workers-2`（`bench/run.py --diag`），只跑 H2，1k 和 10k 两档，每格 30 s（预热 5 s），3 轮，每轮三个变体轮换顺序。`pathwatch-w1` 和 `pathwatch` 是同一个二进制，唯一改动是 `TOKIO_WORKER_THREADS=1`（默认 2，和 NGINX 的 2 个 worker、Envoy 的 `--concurrency 2` 对齐）。绑核、负载生成器、上游都不变。时间：10-28 13:03–13:15 UTC。**其他项目同时在跑**：nproc=32，每格的 1 分钟 loadavg 中位数 6.8–11.1。这是 30 s 短格，只在本次运行内部比较，不是验收数据。第一次尝试（`diag-20261028-h2-workers/`）跑到第 4 格时，`/proc/<pid>/io` 对正在退出的进程返回 EACCES，harness 崩溃；修复后重跑，原始输出保留。

| RPS | 变体 | p50 ms | p99 ms | CPU µs/完成 | 上下文切换/完成 | write 类调用/完成 |
|---:|---|---:|---:|---:|---:|---:|
| 1k | pathwatch（2 worker） | 0.455 | 3.17 | 62.0 | 0.22 | 1.16 |
| 1k | pathwatch-w1（1 worker） | 0.216 | 10.66 | 60.9 | 0.32 | 1.34 |
| 1k | nginx | 0.146 | 1.63 | 55.1 | 0.71 | 3.01 |
| 10k | pathwatch（2 worker） | 0.378 | 4.89 | 51.7 | 0.20 | 1.12 |
| 10k | pathwatch-w1（1 worker） | 0.279 | 2.40 | 48.9 | 0.18 | 1.21 |
| 10k | nginx | 0.129 | 1.39 | 46.3 | 0.59 | 3.00 |

（三轮分位数的中位数，H2 量化下界；完整数据在 `raw/task13/diag-20261028-h2-workers-2-summary.{json,md}`。）

结论：
- 只把 worker 从 2 个改成 1 个，p50 在 1k 从 0.455 降到 0.216 ms，相当于和 NGINX 差距（0.309 ms）的 77%；在 10k 从 0.378 降到 0.279 ms，相当于差距（0.249 ms）的 40%。CPU/请求基本不变。所以 p50 差距里很大一部分和"两个 worker"有关，而不是计算量。推断的机制（没有直接测量）：hyper 的 H2 server 给每个 stream 单独起任务，再加上上游连接任务，同一个请求要在多个任务之间唤醒，两个 worker 时这些任务可能落在不同的核上。
- 1 个 worker 不是修复：1k 的 p99 从 3.17 ms 变成 10.66 ms（3 轮都在 10–12.5 ms），原因没查（可能是单 worker 上偶发的长任务挡住了其他连接）。10k 的 p99 反而更好（2.40 比 4.89 ms）。5k 和 20k 档这次没测。
- 1 个 worker 时 10k 的 p50 仍是 NGINX 的 2.2 倍，剩下的部分不是跨 worker 交接造成的，原因还没定位（候选：每个 stream 单独起任务、headers 和 data 分两次 flush）。
- 计数器的局限：`/proc/<pid>/io` 的 syscr/syscw 只统计 read/write/readv/writev 这一类，不统计 recv/send。PathWatch（mio）读用 recv，所以 read 计数是 0，两边的调用次数不能直接比。上下文切换只算当时还活着的线程。PathWatch 每请求的上下文切换（0.20–0.32）少于 NGINX（0.59–0.71），所以差距不是"切换次数多"；更可能是唤醒后要等另一个 worker 去执行，这一点是推断，没有直接测量。
- 没有改产品代码：要真正修，需要每个核一个 current_thread runtime（SO_REUSEPORT 监听、每核独立连接池），这是架构改动，留到后续。

## 2026-10-31 Per-core runtime prototype (H2 diagnostic)

What changed: `[runtime] mode = "per_core"` in the proxy config (default stays
`multi_thread`) starts one tokio current-thread runtime per worker. Each runtime binds the
listeners with `SO_REUSEPORT` and keeps its own upstream pool. The bench variant
`pathwatch-percore` is the default bench config plus `[runtime] mode = "per_core", workers = 2`
(`bench/pathwatch-proxy-percore.toml`), so both PathWatch variants have 2 threads on the
same 2 pinned cores.

Method: `make bench-diag VARIANTS=pathwatch,pathwatch-percore,nginx RATES=1000,5000,10000,20000 OUTPUT=diag-20261031-h2-percore-2`,
H2 only, 30 s cells (5 s warm-up), 3 rounds, variant order rotated per round, source
01a78d0. 36 cells, 2026-10-31 21:13–21:35 UTC. Machine: nproc 32; 1-minute load average
2.77 when the run started (after waiting for it to fall below 4); median per cell
6.7–9.3 during the run (other projects running). Numbers compare only within this run.

The first attempt (raw files deleted on 2026-11-01 with the user's approval; commit `ad06df3` has them) stopped at its second
cell: the per-core proxy failed to bind 18443 with `EADDRINUSE`, because the multi-thread
cell before it left TIME_WAIT sockets from a plain listener and the reuseport bind did not
set `SO_REUSEADDR`. Fixed with a unit test that reproduces that sequence (5507640).

Medians over 3 rounds (lower quantile bound), ms; CPU in µs per completed request:

| RPS | p50 multi-thread / per-core / NGINX | p99 multi-thread / per-core / NGINX | CPU multi-thread / per-core |
|---:|---|---|---|
| 1k | 0.197 / 0.394 / 0.137 | 2.27 / 2.98 / 2.40 | 64.2 / 56.1 |
| 5k | 0.381 / 0.338 / 0.125 | 2.15 / 2.22 / 1.84 | 55.0 / 51.4 |
| 10k | 0.321 / 0.320 / 0.171 | 4.09 / 4.48 / 2.40 | 50.5 / 47.7 |
| 20k | 0.314 / 0.866 / 0.258 | 4.14 / 31.09 / 23.12 | 44.7 / 40.1 |

Full table: [diag-20261031-h2-percore-2-summary.md](raw/task13/diag-20261031-h2-percore-2-summary.md).

Result: the prototype is not better, so the multi-thread runtime stays the default and the
full H2 matrix (4 rates x 5 rounds) was not re-run with it; the current numbers in this
file remain the reference.

- p50 is worse at 1k (2 of 3 rounds) and at 20k (all 3 rounds: 0.677–0.925 ms against
  0.279–0.658 ms), slightly better at 5k, equal at 10k. p99 is worse at 1k, 10k and 20k
  (20k: 31.1 ms in 2 of 3 rounds).
- CPU per request is 6–13% lower with per-core runtimes, and context switches per request
  fall (20k: 0.06 against 0.27), which is what the design aimed at.
- Likely reason (not measured directly): `SO_REUSEPORT` assigns each of h2load's 100
  long-lived connections to one runtime by hash, and a current-thread runtime cannot hand
  work to the other one. When the split is uneven or one runtime gets a burst, its
  connections queue while the other core idles; at 20k this shows as the p99 tail. The
  multi-thread runtime steals work across both threads. A per-connection distribution
  count would confirm this; it was not added.
- Round-to-round spread is large in this run (multi-thread p50 at 1k: 0.177–0.450 ms), and
  NGINX's 20k p99 of 23 ms is far above its 120 s matrix value, so background load affected
  these 30 s cells. The 20k p50 gap holds in every round.
- Prototype limits (also in `proxy/README.md`): health probes run once per runtime, only the
  first runtime serves `/metrics` (covering itself), `pool.max_connections` applies per runtime.
