# Task 2 Azure 两区域真实网络验收

日期：2026-10-23（America/Los_Angeles；执行UTC2026-10-24）。模块/Make源码90eb682，准入I1修复2a2d9cf，修复证据0462ab4，创建前预算提交ec69aac。Terraform1.17.0、azurerm4.63.0、Go1.27.3、Terratest0.48.2、Azure CLI2.91.0。

## 真实结果

| 区域 | 真实TestNetworkAzure | 创建/销毁资源 | 验证 | 原始输出 |
|---|---|---|---|---|
| westus | PASS，134.19秒 |14创建/14删除 | ARM配置断言、无managed state、三个parent已不存在、共享RG保留 | [actual-westus.txt](raw/task-02-azure/actual-westus.txt) |
| francecentral | PASS，153.27秒 |14创建/14删除 | 同上 | [actual-francecentral.txt](raw/task-02-azure/actual-francecentral.txt) |

实读ARM断言覆盖VNet与三个目的子网、NSG/UDR关联、default deny4096、私网9090与精确owned peer /32 UDP51820、空UDR表。`cleanupNetwork`在成功/失败路径均执行，真实成功路径生成独立destroy plan，通过delete-only scope guard，再apply；读取state无managed资源，实读三个tagged parent均ResourceNotFound，RG ID仍正确。成功cleanup的命令输出由测试helper捕获，console仅记录最终PASS；其判断路径见infra/test/network_test.go:295，不冒充存在逐条销毁console日志。

控制者另读取已执行destroy binary plan与清理后的本地state作为对账：每区14delete/0create/update，managed state=0；汇总和各destroy SHA见[actual-results.json](raw/task-02-azure/actual-results.json)。28行以统一ledger lock设deleted，确认删除UTC2026-10-24T03:53:08Z（确认时间，不是ARM精确删除瞬间），保留原估算，累计仍0.30USD。测试后再次reaper退出0：[记录](raw/task-02-azure/reaper-after-tests.txt)。

## 结论与边界

Azure两区域网络配置的实际创建、读取和受控拆除通过，未留下本次网络资源；共享Task1 RG/Storage后端保留，TTL至2026-10-25T03:03:27Z。未修改/删除rg-mlinfra-westus，未创建禁止服务，无GPU/VM/常驻服务。项目占盘仍约2.2GiB，低于6GB。

这次真实测试使用隔离local state；两个正式regional root的后端与命令配置已本地验证，尚未分别用其正式名称持久部署。Task1已经真实验证Azure Storage/Entra/lease，不能把它写成两个regional backend key各自已部署。本任务没有验证VM分配、数据面、WireGuard/BGP/BFD、跨区连接、AKS或性能；这些保留给后续任务。消费API金额不可见，0.30USD始终为保守估算而非实际账单。
