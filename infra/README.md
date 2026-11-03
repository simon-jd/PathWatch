# infra

Terraform for the Azure side, plus Terratest.

| Path | Contents |
|---|---|
| `bootstrap/` | resource group, Storage account and container for remote state (Entra auth, blob lease locking) |
| `modules/network-azure` | VNet, subnets, NSG rules (default deny), route tables |
| `modules/nva-azure` | NVA VM with IP forwarding (WireGuard and FRR are configured by `ansible/hybrid`) and a client VM |
| `modules/dns-azure` | Private DNS zone and VNet links, Traffic Manager profile, and small VMs for the DNS failover lab |
| `modules/probe-vm` | probe VM component (mock-tested only; not deployable end to end yet) |
| `modules/iam-oidc` | user-assigned identity with a GitHub OIDC federated credential |
| `modules/clickhouse` | not Terraform: Python harness that runs ClickHouse, ingest, probe and Prometheus on one host |
| `envs/dev/*` | root modules (`global`, `azure-a` westus, `azure-b` francecentral, `azure-hybrid`, `azure-dns`, `azure-probes`, `azure-xdp-runner`) |
| `test/` | Terratest (`TestNetworkAzure`, DNS, kind cluster checks); cloud tests skip unless enabled |

```sh
make test-tf        # terraform fmt check and terraform test with mock providers (no cloud)
make tools-checkov  # once
make infra-check    # validate, tflint, checkov, terraform test
```

Real deployments go through `make up ENV=...` / `make down ENV=...`, which run a cost and
scope guard (`scripts/azure_budget.py`) on the saved plan before applying; `make azure-reap`
deletes expired resources. These need an Azure subscription. Detailed runbook
(prerequisites, state migration, OIDC): [docs/dev/notes/infra.md](../docs/dev/notes/infra.md).
