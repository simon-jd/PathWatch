# Task 13：固定速率本机对照实验

仅在已有授权、资源预估及空闲 CPU 条件下运行。`setup.sh` 下载锁定工具到 `.cache/task13`，不安装系统软件。系统需有 GCC/G++、CMake、Make、OpenSSL3 动态库、taskset、pidstat，以及项目已锁定的 Rust 工具链。脚本按 6 GB 项目磁盘份额检查本 checkout 的实际占用，开跑前和每格之后都要求留出 80 MB。单协议单代理复测用 `make bench-matrix PROTOCOL=h2 PROXY=pathwatch OUTPUT=<新目录名>`（4 档 × 5 轮 × 120 秒）。原 debug target 的一次性退役已有完整哈希记录，脚本不自动删除。

```sh
bench/setup.sh
python3 -m unittest discover -s bench/tests -v
bench/run.sh --output smoke-new
# SOURCE commit、独立协议审查通过后；120测量格，每格120秒，加15秒预热：
bench/run.sh --full --output matrix-new
```

输出名称必须全新，任何失败尝试均保留。`--full` 忽略筛选参数，固定 H1/H2 × 5 轮 × 1k/5k/10k/20k × 3 代理。每轮代理顺序轮转；每格重启代理，先独立预热，再启动新客户端进行测量。Go echo 在整次运行中共享。正常退出以及 SIGINT/SIGTERM 都清理本进程启动的完整session/已验证后代；runner自身启用Linux child-subreaper（仅本进程属性），leader先退出后仍可收养同session子进程。保存PID/starttime/session/group，发信号前复核身份并通过pidfd定位原任务，避免PID复用误杀；6秒优雅退出后最多3秒强制终止。已退休ownership记录不再次接纳新PID。最终同时记录所有已见后代、存活集合和四个原本空闲的loopback端口状态，不只看Popen leader。运行器的 PID、每格 PID、状态和进度保存在 manifest/result/progress 文件中。

## 可比条件

三者仅选同一 HTTP/1.1 上游 `127.0.0.1:19090`，响应固定1024个 `x` 字节、固定 Date/Content-Type/Content-Length。这比较协议/连接池开销，不比较多节点负载均衡算法。NGINX least_conn 并不等价于逐请求 P2C；本实验的单节点选择等价。Task12 的多后端 P2C/Maglev 测试仍是独立证据。

共同监听 `127.0.0.1:18443`，同一临时 EC 证书与私钥，SNI/Host 为 `bench.pathwatch.test`，实际 TLS1.3。运行前用受信 CA 验证证书和 H1/H2 ALPN；实际生成器通过观察 SSL_connect/SSL_do_handshake 再确认 TLS/SNI，h2load 确认 h2 ALPN。wrk2 的 H1 不发送 ALPN，采用代理默认 H1 回退。两个负载工具均不验证证书信任，不能把预检的信任验证归于负载工具。wrk Lua 仅把目标解析为字面127.0.0.1，不更改系统hosts。h2load 使用 --connect-to 指向该字面地址。

代理使用2个处理worker，同样绑CPU16、17；生成器4线程绑18–21；Go GOMAXPROCS=2绑22–23；直方图collector绑24；协调/pidstat绑25。这些是独立物理核，见manifest lscpu；代理辅助管理线程不冒充处理worker，但均在相同CPU预算中。NGINX统计master和实际子worker的CPU，不能仅采master。

共同使用100条下游连接。H1每连接单请求在途，H2每连接允许16流；这不是跨协议相同并发数，跨代理同协议一致。NGINX上下游keepalive_requests均1e9，空闲300秒；Envoy仅下游HCM空闲300秒（未显式覆盖cluster上游idle默认值）、默认连接请求数不设上限；PathWatch在本短实验中无主动空闲轮换。PathWatch全局512物理连接、1024 active/1024 pending，Envoy512连接/1024 pending/1024 requests阈值，NGINX每worker缓存256个空闲上游连接，但不具有相同active连接/排队硬限。记录差异，不能宣称饱和时机制完全等价。三者关闭主动健康检查，NGINX/Envoy不重试；PathWatch固有最多一次有预算重试无法配置关闭，成功固定响应不会触发，故错误/饱和结果必须连同重试差异解释。三者上游成功请求都用HTTP1.1，10秒超时；NGINX为分阶段I/O超时，其余总请求期限也不等价。

## 计时、计数、分位数

wrk2上游提交44a94c17d8e6a0bac8559b53da76848e430cb7a7，`-R`为全局固定计划RPS。保留完整stdout/HDR详细谱。`parse_wrk2_hdr`仅读取Recorded Latency的明确50/99/99.9百分位，统一转为微秒，拒绝空样本或不支持格式。输出本身已四舍五入，不能声称比其小数位更精确。wrk2按计划发送时间计算延迟，处理coordinated omission；初始约10秒校准不进入延迟直方图，但进入整段吞吐/CPU计数。独立15秒预热不取消这个校准行为。官方指出调度精度约±1ms。首次工具构建发现旧LuaJIT的ELF嵌入符号截断，运行时通过绝对LUA_PATH加载该同一提交未修改的src/wrk.lua；manifest记录其SHA256。

h2load1.70.0使用 `--rps=总速率/100`，这是每客户端请求速率，绝不以 `-r`（连接创建速率）代替。原生调度器有最小10ms计时批次，繁忙时积累pending请求；延迟从实际request_time起计，不含全部计划排队等待，因此不与wrk2的CO修正延迟直接合并比较。每协议单独表格。无完成样本时延迟/CPU每完成请求记为未定义，不填0。

`offered_requests=计划RPS×120`；实际started/done/succeeded/failed/errored/timeout取h2load原始stdout，另外记录started−done的期末未完成请求。H1 started为观察到成功SSL_write接受以GET开头请求字节的次数（固定无body、无流水线）；completed和socket/status错误来自wrk2原始stdout。单个请求可以导致多个socket错误，错误类别不能简单相加称为失败请求数。Go管理端独立统计收到的请求/新建/关闭上游TCP连接，不把stats访问计入请求数。请求/新连接比用于观察复用，0新连接时比例不定义，并不表示没有复用。H1/H2握手数独立证明下游复用；无法从成功状态 alone 证明每个body内容正确，预检只验证样本body。

CPU分母为完成响应数（包括错误状态响应），不是计划请求数。`pidstat -h -H -u`逐秒采所有代理进程，累加 `%CPU/100 × 1秒`得到CPU秒，除完成数得到微秒/请求；不使用`-I`，多核100%表示1核。原始/proc utime+stime差作独立交叉核对；边界不足1秒的pidstat区间可能漏掉，报告差值。所有worker的affinity/threads/RSS、宿主loadavg和采样过程保留。共享机器、采集器和调度干扰属于实验限制。

## H2有界保留与反压

h2load `--log-file`写阻塞FIFO；固定3列为epoch微秒起点、status、响应微秒。collector逐行验证，成功2xx/3xx计入延迟直方图；其他HTTP状态和失败流(status=-1)分别完整计数。畸形行、越界、collector退出或数量不匹配均令测量失败，不静默丢样/裁尾。期末h2load关闭FIFO后等待collector排空，逐项核对records=done、success=succeeded、实际FIFO写次数=done。工具源代码表明只对进入测量窗口的done记录日志，未完成请求不虚构为日志样本。

直方图固定65,536个little-endian uint64，512KiB/格；<2048us逐整数精确，更高值每指数1024个mantissa bins，桶宽/值<1/1024≈0.0977%，支持0≤us<2^63，绝不静默截断。原始 `.hist`保留完整桶计数，分位采用nearest-rank并返回桶上下界。60个H2正式格至多31,457,280 B直方图；其他日志、smoke和Git压缩副本一并预留80MB。内存不随请求数增长；FIFO容量由系统固定有界，实际总resident监测限16GB。

观察器仅对真实FIFO write计时/计字节/错误/短写，时间是阻塞+系统调用开销的上界，不是纯等待时间。每0.25秒另采/proc wchan的pipe_write阻塞线程数，collector记录从响应完成到收取日志的最大时差。不得把采样0阻塞解释成没有反压。该观察器本身也有开销，三代理同条件运行；它不修改调度器或代理。必须用实际非零FIFO/TLS事件验证覆盖。histogram计数一致性可发现丢日志，但不能证明采集没有影响负载，这一点在结论中保留。

工具和来源：锁文件保留官方URL/散列；NGINX源包用官网pluknet公钥验证签名；Ubuntu依赖仅解包到项目prefix、验证已签名软件索引中的SHA256；宿主OpenSSL动态库版本/散列纳入运行manifest。完整工具帮助/构建输出/失败smoke位于docs/evidence/raw/task13，未修改代理产品代码。


## Fix1：实际时长与工作进程完整性门禁

每格及每次预热分别保存configured_s、独立monotonic wall_elapsed_s、工具reported_elapsed_s和returncode。两种实际时长都必须达到配置值−0.05秒且不超过配置+20秒，退出码必须0；H2还须出现全部4个worker的原生duration完成回调标记。wrk2原人类输出在120秒会舍入为2.00m，因此通过原生Lua done(summary.duration)输出精确微秒，不从分钟格式推断完整时长。50ms容差对应wrk2 stop_at先于worker创建、runtime起点在创建之后的差，以及h2load十进制显示舍入；正常实测wrk20.001966s/H2等值记录可核对。它不容许拿预热抵扣测量。完整矩阵同时要求120格各自有效、Σmeasurement wall≥14400秒和Σreported≥14400秒，不能只看包含预热的总运行时间。工具原始configured窗口RPS单独保留；报告实际RPS使用完成数/工具实际reported elapsed，五轮合并分母也是实际时长之和。

启动后记录代理完整PID身份集，预热和测量每0.25秒及结束边界都核对。NGINX worker退出/替换、新增/消失进程或PID同号但starttime不同都会使该格CPU会计无效并终止，不继续用旧PID列表伪装完整采样。采样不能证明不存在发生于两个检查之间且不改变期末身份集的短命fork；本次固定daemon-off/固定worker工具没有此正常工作模式。RSS计入可确认的全部存活后代。collector先正常读到EOF/退出0而生成器尚未完全退出，视为待完成状态；生成器有界等待后仍须执行records/done等一致性检查，非0 collector立即失败。

可选故障验证只使用owned loopback进程，绝不算正式矩阵：`python3 bench/fault_checks.py --output docs/evidence/raw/task13/NEW-FAULT-DIR`。它真实中断wrk测量/15秒预热并终止一个NGINX worker，要求失败记录和完整清理；默认单元测试另覆盖真实orphan listener、worker替换、EOF先于generator退出和陈旧PID身份拒绝信号。所有这些人为故障与正常性能证据明确区分。
