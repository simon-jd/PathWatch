# Network integration tests

`go test ./infra/test -run TestNetwork -v -timeout 60m` 编译 pinned Terratest0.48.2 + Go1.27.3，默认 **SKIP TestNetworkAzure**，不调用 Azure CLI、不读取凭证、不创建资源。它同时运行 backend isolation 和四种 cleanup outcome 的本地单元测试。native Terraform mock plans 验证配置；二者都不能算真实 Azure network 完成。

仅获授权控制者可按下列顺序分别对 westus10.10/16、francecentral10.20/16 运行实际测试。不能在 PR CI 启用 opt-in。Task1 shared RG/state 必须已存在，Microsoft.Network 已注册；现有短期登录、scope 权限、订阅政策、fresh price evidence、ledger<=18USD 都必须成立。无需 VM SKU，不创建公共 IP、VM、NAT 或收费网关。

```bash
source scripts/dev-env.sh
export AZURE_SUBSCRIPTION_ID=YOUR_AUTHORIZED_SUBSCRIPTION_UUID
python3 scripts/prepare_azure_network_test.py \
  --subscription "$AZURE_SUBSCRIPTION_ID" --location westus \
  --address-space 10.10.0.0/16 --ttl VALID_UTC_EXPIRY_WITHIN_4_HOURS \
  --wireguard-peer YOUR_ACTUAL_OWNED_PUBLIC_IPV4/32
# 用上一步返回的 absolute run_dir：
export PATHWATCH_AZURE_TEST_RUN_DIR=/ABS/CHECKOUT/.cache/azure-network-tests/pathwatch-it-UNIQUE_SUFFIX
FIXTURE="$PATHWATCH_AZURE_TEST_RUN_DIR/infra/test/fixtures/azure"
make -C "$FIXTURE" ROOT="$PATHWATCH_ROOT" VARS=terraform.tfvars.json plan
terraform -chdir="$FIXTURE" show -no-color reviewed.tfplan
python3 scripts/network_proposal.py --plan "$FIXTURE/reviewed.tfplan" --out "$FIXTURE/proposal.json"
make -C "$FIXTURE" ROOT="$PATHWATCH_ROOT" reserve
# 审阅 canonical ledger，然后导出已审阅 plan 的准确 SHA：
export PATHWATCH_AZURE_TEST_REVIEWED_PLAN_SHA256=THE_REVIEWED_PLAN_SHA256
PATHWATCH_RUN_AZURE_NETWORK_TESTS=1 go test ./infra/test -run '^TestNetworkAzure$' -count=1 -v -timeout 60m
```

三个 network Make 入口共用 `infra/mk/network.mk`；copied fixture 必须显式传 `ROOT=checkout` 来使用同一受审阅 gate/include。

准备脚本只复制本项目 module/fixture 源码与 lockfile，生成唯一 `pathwatch-it-...` 名称和 inputs/manifest，**不复制 credential、backend metadata、state 或 plan**。所有文件留在 ignored `.cache/azure-network-tests`，fixture 隐式 local backend，不与部署的 blob state 竞争。真实测试先验证 source manifest、live TTL、subscription、reviewed binary digest、canonical precreation reservation，以及无 managed prior state。

真实测试使用 Terratest apply exact saved plan，再通过 Azure ARM CLI readback 断言 VNet /16、三个目的子网、NSG/route associations、显式入站 deny4096、私网9090允许、owned peer /32 UDP51820 和空 UDR 表。apply 部分失败或断言失败也始终走 cleanup：生成本 fixture destroy plan、校验其 digest 和独立 network-only before-state scope guard，再 apply 它。cleanup 属于此显式测试授权，只删 unique test state 内资源。最后验证 managed state 空、三个 tagged parent 在 ARM 已 ResourceNotFound、原共享 RG 仍在。任何 cleanup 失败都会让测试失败并保留本地 state/plan 路径；控制者需检查残留并 reconcile ledger，不得误删整个项目组。

这些断言没有证明 VM 数据面、home route nonoverlap、WireGuard/BGP/BFD、AKS、吞吐或故障收敛；相关任务须另行运行。guard 只接受原代码的 network resource 地址和关系；计划 JSON 中 unknown association 必须引用本模块自身 resource，拒绝任意 foreign ID 或 data-source 偷换。

Terratest 的 Terraform-only import 实测模块 cache217MiB、Go build cache160MiB，checkout约2.2GiB（6GB内），因此保留原计划 Terratest，不引入 Azure/AWS/GCP SDK。`go.mod`/`go.sum` 锁定实际依赖。
