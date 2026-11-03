# Task10 本机真实20分钟 ingest 中断验收

2026-10-24 本机 N=1 owned pipeline，健康基线20:15:37UTC无page firing，随后停止ingest，最后暂停快照达到 **1200.229s**；计入resume控制调用的脚本结果为 **1200.444101604051s**。未传 `--fixture-seconds`，[实际结果](raw/task10-real-outage-20261024/result.json)为 `fixture:false`、`real_20_minute_outage:true`。42次实际快照保留UTC及monotonic elapsed，没有加速时间。

两page均从健康基线转为firing：readiness在20:16:08UTC（30.436s），freshness在20:25:43UTC（605.464s）。[Controller proof](raw/task10-real-outage-20261024/controller-proof.json)从原始快照提取转变，原始Prometheus alert payload全部保留。原result.scope沿用了fixture的通用提示，字段原样保留；本次判定结合fixture=false、无firing基线及后续真实firing，不用该文字掩盖或替代证据。

20:35:38UTC恢复ingest后等待实际重试退避，20:38:11UTC观察到spool drained。演练期间捕获的 **11批/70个结果**全部在原journal为Committed，并逐批核对CH raw精确计数；物理 `(batch_key,result_index)` 重复为0，spool drops为0。实际分批含2和5结果的partial batch，不能按11×7推算77。原pending已在19:39单独恢复；本次沿用同一data/journal/spool/SLI目录、owner哈希一致，没有清除245个已有readiness失败或过去窗口。

两个执行会话（Supervisor54414、watcher29785）均exit0。20:39:02UTC[核验](raw/task10-real-outage-20261024/operator-v2-cleanup-verified.json)精确owned容器已不存在、native进程0、控制socket删除、owner flock释放、7个TCP端口关闭。私有data/journal/spool/Prometheus/SLI和密钥仍保留供后续审计，不含在公开原始目录里的密码或私钥。全部原始数据与哈希见[清单](raw/task10-real-outage-20261024/manifest.json)。

运行使用A冻结Go二进制源e0553fb（ingest/probe/echo哈希在runtime manifest）及Approved线程池配置512/free128/queue512（配置SHA061c8c...，恢复源码9bcc9cb），源码准备时checkout8451a3d；不是之后C1 DNS二进制。DNS仍literal shortcut、native trace仍权限错误，另有独立C1及raw组件证据。SLO最终快照仍last_window_bad=1/current_fresh=0；本验收证明补传完整和告警转变，未等待新闭合窗口/告警恢复健康。全部历史累计19窗口/15坏、377 readiness slots/325坏，不是99%或99.5%达成，更不是周报或多站验收。没有创建Azure资源，Task10其余源码/部署/容量/Grafana条件仍未完成。
