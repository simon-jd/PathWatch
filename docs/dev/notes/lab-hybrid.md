# Home隔离与真实验收

作用域是Azure A ↔ home，不是地理HA：两个逻辑WG隧道共享NVA/主机/物理路径，只用于控制面计时器与BFD对比。互联Azure10.10/16/home10.30/16，WG0=10.255.0.0/30、WG1=10.255.0.4/30；AzureASN65010/home64512。不同密钥/UDP51820、51821，home主动连Azure，25s keepalive。FRR exactprefix import/export+max-prefix1，拒绝默认/其它路由；primary localpref200、alternate100；WG Table=off不安装远端路由，FRR/zebra安装BGP route。Azureaggregate经10.10.1.1fabric转发，homeaggregate真实connected Docker network，不用黑洞来凑network语句。

以下仅controller审查精确本地范围后可执行。`home.sh`建本project标签的bridge10.30/16、router.2、workload.10；Docker自身维护这个bridge的局部路由/NAT，脚本不改宿主sysctl、默认路由或全局防火墙，也不接管已有Docker网络。NET_ADMIN只在新container namespace内，未用privileged/hostnetwork/hostPID/GPU；Ubuntu zebra实际启动请求CAP_SYS_ADMIN（缺失时privs_init cap_set_proc failed），frrinit保存frr-owned文件权限另需FOWNER，故这两cap只加router，client不加，保留Docker默认AppArmor/seccomp。实际最小权限失败记录由controller保存，不能声称此包不需SYS_ADMIN；router256MiB/client128MiB，总远低16GB。构建前后记image实际Size、checkout du和磁盘余量（6GB项目新增限额），不重复pull、不global prune。

```bash
bash lab/hybrid/home.sh build
bash scripts/hybrid_keys.sh
# Azure endpoint来自真实terraform output；4份key只保存在.cache/hybrid/keys。
python3 scripts/hybrid_config.py --site azure --profile baseline --keys-dir .cache/hybrid/keys --out .cache/hybrid/azure
python3 scripts/hybrid_config.py --site home --profile baseline --keys-dir .cache/hybrid/keys --out .cache/hybrid/home --azure-endpoint ACTUAL_NVA_PUBLIC_IP
bash lab/hybrid/home.sh up .cache/hybrid/home
# copy inventory example到.cache/hybrid/inventory.yml，实际public IP/私钥/目录；先核验SSH hostkey。
python3 scripts/hybrid_usage_watch.py --inventory .cache/hybrid/runtime.json --out docs/evidence/raw/task-03-azure/config-usage --confirm-owned-private-endpoints -- ansible-playbook -i .cache/hybrid/inventory.yml ansible/hybrid/playbook.yml
```

Ansible只针对显式owned Azure guest；forwarding的sysctl只写Azureguest文件。guest firewall project chain只拒绝非WG的TCP179/UDP3784,3785,4784，NSG也不开放这些公网端口。role配置可重复运行；更新WG只重启自己的units。Homecontainer公网上行没有host监听端口，Azure NSG仅实际owner/32能到两WG端口。

上述runtime.json须先填好才能带监测运行Ansible；watch wrapper单命令最多15min，读取计数失败/80%停止主命令。controller仍须立即受控down，不由wrapper绕过cloudguard删除。

私密`.cache/hybrid/runtime.json`格式：

```json
{"scope":"pathwatch-azure-home-owned","nva_public_ip":"ACTUAL_OWNED_PUBLIC_IP","client_public_ip":"ACTUAL_OWNED_PUBLIC_IP","ssh_private_key":"/absolute/project/.cache/hybrid/id_ed25519","known_hosts":"/absolute/project/.cache/hybrid/known_hosts"}
```

SSH严格hostkey验证，known_hosts需controller先核验；不会自动信任未知key。测试不会输出WG privatekey，`wg show all latest-handshakes`只保留epoch而不保存peer公钥。

```bash
python3 scripts/test_hybrid.py --inventory .cache/hybrid/runtime.json --out docs/evidence/raw/task-03-azure/cloud-baseline --confirm-owned-private-endpoints
bash drills/vpn_failover.sh --inventory .cache/hybrid/runtime.json --out docs/evidence/raw/task-03-azure/cloud-failover --confirm-owned-private-endpoints
```

TestHybrid只私有owned workload endpoints10.10.0.4 ↔10.30.0.10，双向ping100；iperf3每方向30s/P4/每stream5Mbps（约总20Mbps），受限baseline不能当链路峰值。双边2BGP Established、recent2WG handshake、实际BGP/内核remote /16路由及primary必须先真实满足，poll最多15min；BFD trial需2BFD up。公网目标始终>=60s，这些rapid probes不面向公网。

Failover每profile5次：60/180 default、3/9 tuned、60/180+BFD300ms×3。仅在home router namespace OUTPUT目的/INPUT来源为**实际Azure公网IP**、UDP源/目的端口均51820的两条精确规则丢包，interface始终UP，另一个51821保留。每trial先恢复/等待双邻居+primary，再采fping100ms raw实时+monotonic、2s后impair、routepoll250ms（实际SSH耗时也记录），看到真实alternate kernel route后再采2s恢复traffic。每trial finally删除两个方向的exact own rules，停own fping；取消/失败还恢复baselineprofile。每trial<=230s、ready<=240s、总<=1h；default五次本身约15min，不能把15min polling限误当完整15trial上限。每条raw保留fping Unix时间与接收时monotonic/realtime；parser算实际loss interval/route-switch latency，带100ms采样分辨率/SSH观察延迟，不拿holdtimer伪造结果。中位数只在15真实trial全完成后生成，错误run不得说PASS。

后台monitor每5s采两guest计数（单次SSH有连接/命令timeout），覆盖阻塞ping/iperf/routepoll；计数读取失败也停止run。超80%通过signal中断主流程，finally恢复，controller立即受控down。StandardHDD S4/S6 16KiB request拆分：completedreads+writes+discards+flushes + ceil(transferredsectors/32)是保守upper，包含boot/apt的绝对值；不是Azure账单实测。guestNIC所有非WG outbound也保守计入2GB。已给各VM预留固定2GB与各disk1M ops；不能用更小台账配更大monitor。cloud apply前与配置期间同样需要controller检查counter，保护配置消耗；不是脚本完成后才补记费用。

FRR配置官方语义：[BGP](https://docs.frrouting.org/en/stable-10.4/bgp.html)、[BFD](https://docs.frrouting.org/en/stable-10.4/bfd.html)、[WireGuard keepalive](https://www.wireguard.com/quickstart/)。distro实际版本须记录并用`vtysh`/daemon dry-run验证本机包能解析，不以字符串测试代替实际FRR parser。若kernelWG/NET_ADMIN受限，记录具体错误并继续controller云验收前置检查，绝不说隧道已通。

独立审查I1/I2后的验收判定：route切到alternate后，在UDP51820仍DROP时结束并join fping capture，再记录cutoff并解析；拒绝任何末尾未恢复区间，至少3个连续terminal成功样本必须出现在route-switch观察之后、cutoff之前，最后样本距cutoff最多500ms。不接受先前短暂恢复或恢复impairment后才出现的成功包。只有验证通过才写trial result；finally另记restore开始/结束时间，失败仍保留raw并恢复。

`--out`现在表示运行集合：每次baseline/failover调用都创建并打印独立`run-*`目录，所有raw、acceptance/summary、原子`run.json`状态均位于该run内。正常完成为passed，失败/取消为failed且移除该run的acceptance/summary；Monitor退出才报错也不能留下PASS文件。旧run保留历史，新run不复用/append旧trial目录；读取本次打印的路径，不能把集合目录下旧run的PASS当作重跑结果。现有固定命令无需换--out，重试会生成新run，原始证据保持隔离。

SSH运行时与Ansible inventory均显式`-F /dev/null`隔离本机/user全局SSH配置，`IdentitiesOnly=yes`只选项目指定identity，严格使用已验证的project known_hosts（GlobalKnownHostsFile=/dev/null，不借全局trust）。如果宿主`/etc/ssh/ssh_config.d`的文件owner/permissions导致SSH255，不chmod/编辑宿主配置；通过这些项目内参数连接。复制inventory示例至.cache/*.yml后只替换project私钥/known_hosts绝对路径及owned IP，不能改成关闭hostkey检查。此兼容修复本身不证明WG/BGP或数据面通过。

R5：renderer和Runtime.profile均使用FRR8.4的`bgp graceful-restart-disable`显式关闭GR和helper；`no bgp graceful-restart`会回到默认helper，不足以关闭它。单双向丢包不能混为同一实验：INPUT+OUTPUT双向阻断保证primary对等体双边停止接收keepalive，旧单向失败run保留，重跑用新的run目录。规则插入前检查以避免重复；部分插入失败/取消/timeout会撤回两个精确规则，删除失败也先尝试另一个方向再报错，不对宿主全局iptables做修改。

Task4 phase0 adds an actual-neighbor gate: both expected peers must report `gracefulRestartInfo.localGrMode` as `Disable` or `Disable*`; parse success alone is not acceptance.
