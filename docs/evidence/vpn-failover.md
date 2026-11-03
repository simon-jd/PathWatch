# Azure A ↔ home 收敛演练（15次真实通过）

日期：2026-10-23 America/Los_Angeles（2026-10-24 UTC）。执行时HEAD 34e8442；实际runtime源码5a7456c（GR幂等修复）及1e58b12（正确hyphen命令/模式门禁）、9d2b061（双向UDP故障）、d9bd132（恢复前判据）均已独立复审。完整正式run为 [run-b2ui6wy4](raw/task-03-azure/cloud-failover/run-b2ui6wy4/run.json)，状态passed；15个result与summary齐全。旧失败run未混入统计。

实际两端为westus Azure NVA/workload与home-ubuntu隔离router/workload，FRR8.4.4、WireGuard-tools1.0.20210914；Azure/home kernel分别7.0.0-1017-azure和7.0.0-38-generic。Azure两VM均B2pts_v2/2vCPU/1GB/Arm64、30GB Standard_LRS；home容器内存合计384MiB。完整组件实读见[版本](raw/task-03-azure/actual-runtime-versions.json)。

故障只在home隔离router namespace双向DROP发往/来自实际owned NVA的primary UDP51820，接口保持UP，alternate51821继续可用。每轮先确认双边2BGP Established、近期WG握手与primary内核路由，故障后实际切换双边pw-wg1。BFD组每轮before中实读两边各2session up、TX/RX300ms、multiplier3。GR在四个peer上真实Disable*，不是仅通过配置parser。

| 配置 | 次数 | 路由切换观察中位数 秒 | 每轮最长连续丢包中位数 秒 |
|---|---:|---:|---:|
| BGP keepalive60/hold180 | 5 | 172.455803 | 172.034557 |
| BGP keepalive3/hold9 | 5 | 8.534232 | 8.250763 |
| BGP60/180 + BFD300ms×3 | 5 | 1.200798 | 0.838829 |

| 配置 / 轮次 | 路由观察 秒 | 最长连续丢包 秒 | 丢包数 | 恢复前连续成功包 |
|---|---:|---:|---:|---:|
| baseline / 1 | 174.997865 | 174.837350 | 1750 | 52 |
| baseline / 2 | 174.560878 | 174.240704 | 1743 | 52 |
| baseline / 3 | 171.482414 | 171.040668 | 1712 | 31 |
| baseline / 4 | 172.455803 | 172.034557 | 1722 | 15 |
| baseline / 5 | 171.198659 | 170.943372 | 1710 | 51 |
| tuned / 1 | 8.534232 | 8.250763 | 83 | 53 |
| tuned / 2 | 8.608099 | 8.485561 | 86 | 29 |
| tuned / 3 | 8.809481 | 8.537304 | 86 | 51 |
| tuned / 4 | 8.336950 | 7.941339 | 81 | 29 |
| tuned / 5 | 7.997404 | 7.737773 | 78 | 54 |
| bfd / 1 | 1.216662 | 0.940390 | 10 | 52 |
| bfd / 2 | 1.200798 | 0.847243 | 9 | 51 |
| bfd / 3 | 1.178174 | 0.836997 | 10 | 50 |
| bfd / 4 | 1.337224 | 0.838829 | 10 | 41 |
| bfd / 5 | 1.168825 | 0.748241 | 8 | 51 |

主控独立核验了全部15轮原始包序列连续性、实际首个WG1路由时间、两站前后内核路由、时间差计算、所有丢包区间已恢复，以及capture cutoff严格早于restore开始；每轮至少3个terminal成功包且最后包距cutoff不超过500ms。见[独立核验](raw/task-03-azure/cloud-failover/run-b2ui6wy4/controller-validation.json)、[summary](raw/task-03-azure/cloud-failover/run-b2ui6wy4/summary.json)及同run各轮fping/routing/before/after/impair/restore/result原始文件。

路由观察值从开始执行DROP前的单调时间到首次观察到alternate内核route；不是配置hold值。连续丢包值按fping样本接收的单调时间计算，原始Unix输出时间同时保留。fping采样100ms；路由poll间隔250ms外加Docker执行/调度耗时，管道接收和双边SSH快照也有开销。因此BFD数据只能支持此环境约1秒量级的恢复，不声称精确900ms或任意链路性能。每类仅5次；两条逻辑隧道共享同一NVA、home宿主和物理路径，不能推出地区HA。所有快速包均发往owned私网workload。

结束后实际核验双边baseline60/180、四邻居Disable*与Established、kernel回primary、INPUT/OUTPUT均无项目故障规则，见[最终恢复](raw/task-03-azure/post-campaign-restoration.json)。正式run的绝对磁盘事务峰值NVA261036/client386241，所有非WG出口峰值约174.88MB/86.89MB，均低于每盘1M事务/每VM2GB预留的80%停止线；这些是包含boot/apt的保守工程上界，不是Azure账单。

清理完成：32项精确delete-only计划SHA bbdf6e208f0fac18c3814be74d11d5769eff437808e13e6057631b3139c0aa1f实际0add/0change/32destroy；2026-10-24T06:38:44Z已[实查ARM不存在与hybrid state为空](raw/task-03-azure/actual-cleanup-arm.json)，[home两个容器/专属网络不存在](raw/task-03-azure/actual-cleanup-home.json)，shared RG/backend保留。canonical32行持统一锁记deleted，历史累计0.90USD不减少。早于原08:25:19Z截止；未触及rg-mlinfra-westus。Task3按Azure资源覆盖后的验收完成。

## 历史失败与修复记录（以下为当时状态）

2026-10-23 America/Los_Angeles：真实基线已双向100/100包及约20Mbps通过，但首个15trial campaign的baseline-1在230s内未观察到WG1路由而失败。该run保留[failed状态](raw/task-03-azure/cloud-failover/run-ppi84rri/run.json)、逐包fping/route/usage及restore.json；没有summary，不计入任何5次组或中位数。finally已删除精确impair规则、停止fping并恢复baseline profile。

后续有界tuned3/9诊断[活动快照](raw/task-03-azure/one-way-active-diagnostic.json)显示：单向OUTPUT丢弃后，Azure先HoldTimerExpired进入Connect，home仍收最后的反向keepalive，随后home也到期并切WG1。这支持默认组可能叠加两端hold窗口，不能把配置180s视为已测收敛，也不应只加长验收超时。实读另确认原no bgp graceful-restart仍是FRR默认Helper模式，原文声称禁用不成立；需显式disable并在当前实际daemon实读确认。

R5将修为同一home隔离namespace内，仅owned NVA IPv4/UDP51820双向drop，保留51821、WG行政状态和所有路由；并显式关闭GR helper。230s deadline、三包terminal恢复、500ms新鲜度、恢复前验证、run隔离及费用/timebox门禁保持。待独立复审后运行全新完整15次，不混入旧失败run。临时32项必须2026-10-24T08:25:19Z前同会话销毁；累计估算仍0.90USD。

2026-10-24T05:56Z补充实测：R5空格形式在FRR8.4.4被当作隐藏 `disable-eor` 的缩写，原生parser干净通过并不足以说明GR关闭。实际source部署及live profile后仍为Helper*、running config也无GR-disable行，见[失败模式实读](raw/task-03-azure/gr-space-command-failed-actual-mode.json)。[版本固定上游源码](https://github.com/FRRouting/frr/blob/frr-8.4.4/bgpd/bgp_vty.c#L3026)要求准确命令 `bgp graceful-restart-disable`；其JSON继承状态叫 `Disable*`。controller用准确CLI诊断、reset后，双方全部四邻居实际Disable*且running config出现正确行，见[语义诊断](raw/task-03-azure/gr-hyphen-command-diagnostic.json)。源代码最小修正和语义门禁带入Task4前置集成，之后仍须新完整15次与清理；上述诊断不冒充源码验收或收敛测量。
