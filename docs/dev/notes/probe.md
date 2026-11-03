# 探针库契约（Task 5–6）

构造 `dnsprobe.New(dnsprobe.Config{ProbeID: "home", Server: "127.0.0.1:5353", Timeout: 5*time.Second})`；TCP 使用同形 `tcpprobe.Config`。构造器要求非空 `ProbeID`。`Server` 为 IP literal 的 host:port（IPv6 加方括号），为空时使用 `/etc/resolv.conf` 第一台 nameserver。解析只查询给定绝对 DNS 名称的 A/AAAA，无 search suffix、自动 nameserver 切换或额外重试。

`Target.Host` 接受主机名、IPv4、带或不带方括号的 IPv6，不接受内嵌端口；TCP 端口来自 `Target.Port`，范围 1–65535。DNS 接受 literal 并返回该地址，`resolve=0` 表示未发 DNS 请求。TCP 分别使用 tcp4/tcp6 连接每个地址，随后立即关闭连接。A 与 AAAA 顺序查询，返回 v4 在前、v6 在后，各族保留 DNS answer 顺序（不去重）。某一族 NODATA 不产生失败，双族无地址、rcode 错误、网络错误均产生带 `Err` 的测量结果。

一次 `Probe` 共用一个总 timeout，默认 5 秒，包括解析、UDP 截断后的 TCP 回退和全部 TCP 连接。配置和目标不合法返回顶层 error。远端拒绝、DNS 失败和库配置 timeout 写入 `Result.Err`，顶层 error 为 nil；调用方取消或其 deadline 返回 `context.Canceled` / `context.DeadlineExceeded`，可同时携带已测量的部分结果。一个地址失败不阻止其余地址测量，剩余总时间不足时连接会得到 deadline 错误。没有后台探测循环。

`StartedAt` 是轮次开始的报告墙钟，可通过 `WallNow` 注入；所有真实网络阶段用 `time.Now()` 起点与 `time.Since()` 计算，保留 Go 的单调时钟部分。`WallNow` 不参与 duration；共享实例并发使用时，注入回调须自行保证线程安全。`resolve` 是对应单次 DNS query（含 UDP/TCP 回退）的总耗时，同一个 query 返回的多个地址共享此值；双族 NODATA、尚未开始的下一族超时或无法归属某族的整体解析失败，记录本次解析截至失败时的总耗时。下一族尚未开始的超时会另有该族失败结果，保留之前的部分答案；不能归属某族的错误使用空 Family。`connect` 是该地址一次新 TCP 连接的耗时。失败也保留已花费的阶段时间。

公共目标的相邻轮次必须由调用方调度器保证每 probe/target 至少 60 秒，限 DNS/TCP/TLS/HEAD/GET 首页。本库不调度；UDP traceroute 仅允许自有私有目标，测试只访问自有 loopback。

`proto/pathwatch/v1/probe.proto` 映射共享模型全部字段，`phases_nanoseconds` 使用 int64 纳秒，测量时间与 `sent_at` 分开。Report ACK 是整批持久化成功或相同批次已存在；以 `(probe_id,batch_id)` 去重，`batch_id` 必须匹配，`committed_result_count` 必须匹配整批结果数量，UNSPECIFIED 不算 ACK。相同键不同有序测量结果必须返回 RPC error；重试可以更新 `sent_at`，它不属于去重内容比较。Task 7/8 实现发送、持久化与去重。

生成绑定：`source scripts/dev-env.sh && make proto-generate`；验证无漂移：`make proto-check`。固定 protoc 3.21.12、Go 1.27.3、protoc-gen-go v1.36.1、protoc-gen-go-grpc v1.5.1。运行时依赖由 go.mod/go.sum 固定；生成器通过明确版本与 Go module 校验下载，仅写项目 `.tools/bin` 和 `.cache`。`make probe-test` 执行本地探针与生成 package 编译。


TLS 使用 `tlsprobe.New(tlsprobe.Config{ProbeID: "home", Timeout: 5*time.Second})`；HTTP 使用 `httpprobe.NewWithConfig("h2", httpprobe.Config{ProbeID: "home"})`。简便构造器 `httpprobe.New("h1"|"h2"|"h3")` 默认 ProbeID 为 `local`，部署时应显式设身份。两者共用现有 A/AAAA resolver，一次解析保留所有地址、族与部分失败；之后只拨对应 literal IP，不再次解析。证书校验默认开启，SNI/证书身份始终是原始 hostname（literal 使用 IP SAN 校验）；`RootCAs` 只显式注入可信 CA，构造时克隆。没有 InsecureSkipVerify 选项。TLS/HTTP 的 Target.Port 必填，IPv6 的 hostport 由 net.JoinHostPort 构造。

TLS 仅允许 TLS 1.3，阶段 `dns`、`connect`、`tls_handshake` 分别是对应 DNS query、TCP 建连和完成验证的握手耗时。`TLSVersion` 是成功握手的实际版本（TLS 1.3=`0x0304`），`TLSResumed` 来自真实 `ConnectionState.DidResume`；握手失败时版本=0，不能据 `TLSResumed=false` 推断握手成功。每个 Probe 保存线程安全、最多 64 项的 LRU session cache，key 包括原始 host+port。每次调用仍新建连接；首次握手后被动读取 TLS 记录至多 50ms、至多一个应用字节，让 Go 处理 TLS 1.3 的 post-handshake NewSessionTicket。此读不发送应用请求，其超时/EOF 不否定已成功的握手，也不计入 `tls_handshake`。远端可能不发 ticket、需先收应用数据或拒绝恢复，故不保证后续连接恢复。自有 httptest TLS 服务实际发送 ticket，测试在两个独立 Probe 调用验证第二次 DidResume=true；没有伪造标志或对公共目标快速重复调用。调用方取消仍返回顶层 context error。

HTTP 固定发送一次 `HEAD /`，不跟随 Location、不启用代理或 Alt-Svc；响应头上限 64KiB，立即关闭响应 body（HEAD 没有正文，不做无界 drain）。h1/h2 用标准库 httptrace，h3 用固定 quic-go v0.64.1 的 http3.Transport 及其真实 httptrace 回调。`ttfb` 从该地址请求开始至首个响应字节，`total` 从请求开始至响应 body 关闭，均包含该地址的连接/握手但不包含前置公共 DNS；`dns` 单独保留。未收到首字节时没有 ttfb；DNS 失败时没有请求阶段。每个地址使用独立 transport，完成后关闭 TCP idle connections 或整个 QUIC transport。`Protocol` 记录实际响应协议 h1/h2/h3；请求 h2 却得到 h1 会记录 Protocol=h1 和 Result.Err，不能冒充 h2 成功。证书拒绝、连接超时或收到有效响应前取消不填写协议。HTTP 状态码（含 3xx/4xx）本身不是传输失败。

私有 traceroute 使用 `traceprobe.New(traceprobe.Config{ProbeID: "home", MaxTTL: 30, HopTimeout: 250*time.Millisecond})`；Target.Port=0 默认从 33434 起，每一 TTL 加一，验证完整端口范围。`Target.Public=true` 在 DNS/打开 socket 前拒绝；即使 Public=false，解析出的每一个实际目的地址也必须是 RFC1918 私有或 loopback。调用者须保证这些私有目标归己方所有。IPv6 私有/loopback 返回明确 unsupported Result.Err，不创建 socket；不宣称实现 IPv6 trace。两种模式都发 UDP 探测、逐跳加 TTL，区别只在 ICMP 错误怎么收回来：
- `Mode: "icmp"`（默认，配置 `trace_mode` 省略或 `"icmp"`）：raw IPv4 ICMP socket 收包，运行时需 CAP_NET_RAW（Task 10 的 systemd unit 授予）。
- `Mode: "udp"`（配置 `"trace_mode": "udp"`，2026-10-28 加入，Linux）：探测 socket 打开 `IP_RECVERR`，用 `recvmsg(MSG_ERRQUEUE)` 读内核排进本 socket 错误队列的 ICMP 错误（类似 tracepath），**不需要任何特权**。内核已经按 socket 匹配了被引用的报文（所以源端口隐含匹配）；`msg_name` 给出原始目的地址和端口，按每个 TTL 的目的端口匹配；只接受 `SO_EE_ORIGIN_ICMP` 的 Time Exceeded(code0) 和来自目标本身的 Port Unreachable(code3)，规则和 icmp 模式相同。Task 9 的真实 hop（`lab/pathchange`）用的是这个模式，证据里都标为 UDP/IP_RECVERR。
单元测试注入 PacketConn；`TestRecvErr_LoopbackPortUnreachable` 是一次真实的非特权 loopback 探测。

traceroute 的 `Phases` 只有 `total`，是每地址校验/探测至 socket 关闭的真实耗时，不含前置共享 DNS；DNS 失败仍保存单独错误结果。`Hops []result.Hop` 顺序为 TTL=1..MaxTTL；匹配 hop 的 RTT 用真实 time.Since，未回复/只收到无关 ICMP 的 hop 表示为 Addr=""、RTT=0。内层 IPv4 版本/IHL/协议/fragment offset/total length、UDP length、目的 IP、UDP source+destination ports 均校验；仅接受 Time Exceeded(code0) 或 Port Unreachable(code3)。终点 Port Unreachable 的外层发送者还必须是目标 IP。每跳最多读 1500-byte 缓冲中的 256 个 ICMP 包，且受 HopTimeout/总 context 双重限制；未知包不能延长 deadline。匹配终点则停止，MaxTTL 耗尽表示有界部分路径，无额外错误；socket/总 timeout 则保存 Result.Err。PacketConn.Close 须可并发中断读写，与 net.PacketConn 约定一致。未做公共 traceroute、MTR 或调度。

Task 6 的 wire 扩展只追加 ProbeResult protocol=9、hops=10、tls_version=11、tls_resumed=12；旧字段 1..8 不变。Hop wire 字段为 ttl=1、addr=2、rtt_nanoseconds=3；为 Task 7/8/9 的 spool、ingest 和展示保留完整共享字段，无服务业务实现。阶段和 RTT 都是纳秒，零 RTT 只表示未知 hop。

## Task 7：调度、持久队列与上报

构建和启动（不安装或部署 systemd）：

```bash
source scripts/dev-env.sh
go build -o .tools/bin/pathwatch-probe ./probe/cmd/probe
.tools/bin/pathwatch-probe -config probe/config.example.json
```

配置示例需替换为自己的 ingest 地址和目标；示例只包含自有 loopback 目标。生产 ingest 使用受信任 TLS 证书，默认系统 CA 且验证 endpoint 的 hostname/IP SAN；私有 CA 可配置 `ingest_ca` PEM 文件。没有跳过证书验证的选项。仅本地开发可显式设置 `development_cleartext: true`，此时 ingest 必须是 `127.0.0.1` 或 `::1` 等 literal loopback，拒绝 hostname、非 loopback、resolver URL 和同时指定 CA。`rpc_timeout` 限制每个 RPC（含连接不可用）。

`probe_id`、`ingest`、`spool_dir` 和至少一个 target 必填。配置严格拒绝未知 JSON 字段和多余 JSON 值，上限 1MiB；构造器/调度器在网络测量前检查重复 target ID、无 kinds、未知/重复/未注册 kind、host/port、公共 trace 及非正间隔。`dns_server` 可指定自有 DNS literal host:port，否则使用既有 resolver 契约。Target 的 `public` 是调用者对公共目标的明确分类，必须如实设置；私有 trace 仍会在实际地址解析后独立拒绝所有非私有/非 loopback 目的地址。

`schedule.Run(ctx, targets, probers, every, clock, out)` 每个 target 串行执行请求的 kinds，各 prober 保留既有地址顺序。一轮所有 kinds 完成后等待 `every × [0.9,1.1]`；公共目标额外 clamp 到至少 60 秒，因此 nominal=60s 的抖动等待门槛为 60–66s，nominal=120s 为 108–132s；探测和其他目标的耗时可使实际轮次间隔更长。私有目标可更快，但间隔须为正。慢轮次不补跑，不因 kind 失败、耗时或墙钟改变提前调度；真实 `Clock.Now` 保留 Go 单调部分，注入的 fake clock 也须表示单调经过时间。远端错误照常输出 `Result.Err`，配置/编程错误由 Run 返回。计时等待、阻塞输出及探测在 context 取消时退出；调用者拥有 output channel。

`spool.Open(dir,maxBytes)` 使用 Linux OS flock 强制单进程目录所有权；Close 或进程退出释放锁。每个 Append 保存有序结果到独立 JSON envelope，随机 128-bit batch ID 一并持久化，manifest 保存 FIFO 顺序、大小、SHA-256 和淘汰总数。每次写均为 `fsync(file) → rename → fsync(directory)`。先持久化候选 batch，再原子替换 manifest，同时提交新队列和 drop 计数，最后删除已 ACK/已淘汰的旧文件。manifest 是唯一提交点；恢复只清理本格式的 `.pw-tmp-*` 和未被 manifest 引用的 batch，逐批检查所有已提交内容。已提交批损坏、缺失、超限，或存在 batch 却缺 manifest 时明确报错，不跳过后继续报告成功。

容量是 **已提交 batch envelope 的实际序列化字节之和**，包含 ID、所有结果和 JSON 换行；`0 < maxBytes ≤ 1GiB`。文件系统块分配、锁、manifest 和原子写候选峰值不计入逻辑容量。单批另外限制 16MiB/10000 条结果，manifest ≤16MiB，队列 ≤65536 批，目录恢复以每次 128 个名字扫描，损坏文件按有界长度读取。峰值可额外有一个 16MiB 候选 batch、一份 manifest 临时文件和旧 manifest；实际块开销取决于文件系统。应为这些开销预留空间。降低容量到小于现有已提交总量会拒绝 Open，先以原容量排空后再调整。

超大 Append 在任何淘汰前拒绝。满容量或满批数时淘汰 FIFO 最老批；`pathwatch_spool_dropped_total` **按批**计数，4 批进 3 批容量得到 1，计数和队列在同一次 manifest 事务持久化。该值跨进程重启保留，换全新 spool 目录才重置；正常 ACK 不增加它。`Next` 返回独立结果快照、稳定 ID 和绑定该 ID 的 ACK closure；重复或容量淘汰后的 stale ACK 是安全 no-op，不能删另一批。Close 后的 ACK 返回 ErrClosed。

写失败不会在新批提交前删除旧批。若 manifest rename 已发生而目录 fsync 失败，操作返回明确的 `ErrUncertain` 并冻结该实例，所有旧文件和候选均保留；调用者必须 Close/Open，根据可恢复 manifest 判定提交结果，不能把该错误理解成“肯定未提交”。若提交成功后旧文件清理失败，也明确返回“已提交、清理失败”并冻结，防止持续累积垃圾。突然杀进程测试验证完整已提交前缀及恢复；没有模拟硬件损坏或声称验证实际断电的文件系统保证。

`report.Client.Run` 只接受一个 worker；每次同步发一个有 deadline 的 gRPC 调用，持有一个 batch 的固定有序内容。ACK 丢失或 RPC 错误均重发同 ID，只有每次请求的 `sent_at` 更新，StartedAt、各阶段纳秒、协议、TLS 元数据、hop 等保持原值。仅匹配 batch_id、ACCEPTED/DUPLICATE、整批 committed_result_count 的 ACK 才删除；错 ID、部分和 UNSPECIFIED 都重试。退避 cap 为 1s、2s、4s…300s，实际 full jitter 独立取 `[0,cap]`，实际随机等待不要求单调；成功批次后重置到 1s。空队列等待 250ms。这里验证的是客户端重试/ACK 语义，数据库去重属于 Task 8。

命令把全部已有 DNS/TCP/TLS/HTTP1/2/3/private trace probers 接到调度器，最多缓存 `batch_size + 128` 条待持久化结果，达到 batch_size 或每秒 flush。SIGTERM/取消先停止生产和 RPC，再在默认 5 秒关闭窗口内排空已产生结果并持久化最后部分批次；磁盘失败返回可见错误，不声称已持久化。默认 `/metrics` 仅监听 `127.0.0.1:9464`，可显式配置 IP:port。暴露 spool bytes/batches/drop、report attempts/ack/failures、measurements、operational errors 和 up，无 target/batch 高基数标签。数据路径须是项目/服务用户可写目录；日后 systemd 应设置 WorkingDirectory、绝对 config/spool 路径和服务用户权限，Task 7 不部署服务、不改主机 DNS/防火墙/全局服务。trace 所需 CAP_NET_RAW 留给 Task 10。

本地验证（自有 loopback/bufconn，无公共探测）：

```bash
source scripts/dev-env.sh
go test ./probe/... ./proto/...
go test -race ./probe/internal/schedule ./probe/internal/spool ./probe/internal/report ./probe/cmd/probe
go vet ./probe/... ./proto/...
```
