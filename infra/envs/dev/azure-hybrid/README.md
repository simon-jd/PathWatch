# Azure A ↔ home 临时4h验收根目录

只由controller在独立审查通过、fresh价格、正式canonical ledger reservation之后运行cloud apply。永久探针属于Task10，Task3结束必须销毁。不能直接执行裸apply绕过guard。仅本root管理网络+NVA/client；shared RG/backend保持Task1所有权，原azure-a state必须没有同名资源。

1. 读取现有parent TTL。只读核验`az account show`、westus `az vm list-usage`、`az vm list-skus --location westus --size Standard_B2pts_v2 --all`和`az vm image show --location westus --urn Canonical:ubuntu-24_04-lts:server-arm64:24.04.202610210`。要求2vCPU/Arm64/V2、plan=null、实际OS盘最小要求<=32GB；实配失败不得换大盘或开贵SKU绕过。重新核对home LAN/Docker/VPN实际route。读取provider registration，不修改它。
2. backend配置复制Task1账号/容器的Entra授权参数，独立key=`azure-hybrid.tfstate`，见example。只在ignored tfvars填实际订阅、公网owner/32、公钥，TTL<=当前RG/state保留期限且从计划start起<=4h。例子不是实际地址。hourly保守建议vm=.07/ip=.005/disk=.02（精确guard仍计算），include_client=true。
3. 对同一westus刷新Retail captures（<=24h）：B2pts regular Linux、Standard Regional IPv4、S4 LRS capacity和operations、MGN paid最高egress tier，以及network components source。预留固定每VM2GB outbound、每disk1M保守16KiB transactions（脚本absolute boot counters监测）；每VM/PIP小时必须等于实际时间盒，disk_month=hours/730。每资源都有行，tag manager计disk费，NIC/子网等免费也入表。历史.30USD不删除、不改写、不回收headroom。所有writer沿用同一个ledger_lock/persist_ledger。
4. `make -C infra/envs/dev/azure-hybrid plan BACKEND=backend.tfbackend`。审查实际binaryplan scope后：

```bash
python3 scripts/hybrid_proposal.py --plan infra/envs/dev/azure-hybrid/reviewed.tfplan --start-utc ACTUAL_UTC_START --out infra/envs/dev/azure-hybrid/proposal.json
make -C infra/envs/dev/azure-hybrid reserve
# Review/commit canonical financial journal before creation, then controller only:
make up ENV=infra/envs/dev/azure-hybrid
```

proposal helper只生成/验证proposal，不reserve、不apply。价格文件名固定当前捕获路径，后续日期更新需显式更新路径/证据，不能伪造capture time。预算guard读取真实binaryplan JSON，只允许已知network与四种exact scoped compute资源**create**，没有泛化update/replacement路径。disk tags PATCH仅作为azapi资源首次create的精确受控动作；不能借它修改diskproperties。实际data RG TTL必须覆盖child。所有meter selector/product/SKU/unit/quantity/price均复核，cumulative<=18USD。

5. 立刻开始`hybrid_monitor`监测并使用下面runbook配置/验收；配置期间也可每次把`python3 scripts/hybrid_usage.py`通过SSH stdin送入两个guest取得自boot绝对计数。任一guest磁盘upper>=800000 transactions或非WG NIC tx>=1600000000 bytes（80%），停止负载/恢复impairment并controller走受控down；剩20%留清理。所有过程必须在TTL/4h内结束，不能等reaper兜底。来源16KiB不是旧256KiB。
6. 验收结果/ARM配置/实际生存时间入evidence；明确SSH key与WG key永不日志或git。最后：

```bash
make -C infra/envs/dev/azure-hybrid down-plan BACKEND=backend.tfbackend
sha256sum infra/envs/dev/azure-hybrid/destroy.tfplan
make down ENV=infra/envs/dev/azure-hybrid REVIEWED_PLAN_SHA256=ACTUAL_REVIEWED_HASH
bash lab/hybrid/home.sh down
```

delete-only guard只准本state已知network/NVA资源、before-state exact ARM ID和所有权；azapi删除仅撤销state管理，VM负责真删OS盘。再只读核实该实验VM/managed disk/NIC/PIP和网络都消失，RG/backend仍在，按同一ledger锁追加真实删除时间，不减历史估算。销毁失败不能写成功；先完成可安全清理部分，再报告具体残留。
