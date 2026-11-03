# Azure 两区域网络 roots

`azure-a` 默认 `westus` / `10.10.0.0/16`；`azure-b` 默认 `francecentral` / `10.20.0.0/16`。`location` 可改为当前订阅允许的 westus/francecentral/norwayeast/denmarkeast/swedencentral；guard 还要求该位置的新鲜价格证据。westus2、eastus2、centralus、westus3 均拒绝。选定网络 region 不表示 B2ats_v2 或任何 VM SKU 可用；本任务没有 VM/PIP。

先完成 Task 1 existing RG 和 Azure Storage backend，使用操作者已有的短期/CLI 登录，不复制凭证，不注册订阅 provider。将每个 root 的 `terraform.tfvars.example` 与 `backend.tfbackend.example` 复制成 ignored 实际文件；填写订阅、Storage account、UTC TTL 和已允许的位置。后端用 Entra / blob lease，key 分别 `azure-a.tfstate`、`azure-b.tfstate`，与 bootstrap/global 不同。

以下是获授权控制者真实运行的命令，实施者未执行任何 network cloud mutation：

```bash
source scripts/dev-env.sh
export AZURE_SUBSCRIPTION_ID=YOUR_AUTHORIZED_SUBSCRIPTION_UUID
make -C infra/envs/dev/azure-a plan BACKEND=backend.tfbackend
terraform -chdir=infra/envs/dev/azure-a show -no-color reviewed.tfplan
python3 scripts/network_proposal.py --plan infra/envs/dev/azure-a/reviewed.tfplan --out infra/envs/dev/azure-a/proposal.json
# 核对实际 plan、proposal 与 zero-cost source，然后预登记每项资源：
make -C infra/envs/dev/azure-a reserve
# 审阅 canonical ledger；本次总估算仍须<=18USD：
make up ENV=infra/envs/dev/azure-a
```

三个 network Makefile 通过 `infra/mk/network.mk` 共享同一组 plan/reserve/up/down 实现；每个 root 保留独立入口和 state。

`up` 只 apply 已保存、已 reserve 且复核 receipt 成功的 plan。网络配置免费也不能绕过台账/guard；`network_proposal.py` 只写 proposal，不 reserve，不 apply。每个零金额 row 必须引用 `docs/azure-prices/2026-10-23-network-components.json`；其中官方定价页说明 VNet 本身免费，Retail API captures 列出相关收费 SKU，**不虚构一个0USD Retail meter**。本任务没有 peering、routing appliance、flow logs、VM、IP 或数据流量；这些未来用量必须另行估算。state request/version 用量仍归 Task1 的保守 Storage reservation，不能把没有 consumption 金额当作实际消费0。

```bash
make -C infra/envs/dev/azure-a down-plan BACKEND=backend.tfbackend
terraform -chdir=infra/envs/dev/azure-a show -no-color destroy.tfplan
# 审阅生成的 destroy plan，取得准确 hash 后：
make down ENV=infra/envs/dev/azure-a REVIEWED_PLAN_SHA256=THE_REVIEWED_DESTROY_SHA256
```

`down` 只接受 delete/no-op 的已知 owned network resources，校验实际 before-state ID、父级 tags、子网/NSG/route 关系；不会删除共享 RG/state。`azure-b` 同理。若 guard、费用证据、TTL 或 scope 拒绝，先检查原因并重新审阅，不能直接运行 raw apply 绕过。真实删除后持锁 reconciliation ledger 的 `deleted_utc`，保留历史估算，不移除旧 reservation 释放预算。
