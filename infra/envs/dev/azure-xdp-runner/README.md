# Task16 ARM runner: offline prerequisite component

This root describes one temporary `Standard_B2ps_v2` runner in `westus`, with the
pinned Canonical Ubuntu 24.04 ARM64 Gen2 image, a 32 GiB `Standard_LRS` OS disk,
one static IPv4 public IP, and SSH from one verified operator IPv4 `/32`.
The eight managed addresses are fixed by `scripts/azure_xdp_scope.py`. The disk
is explicitly tagged through a tags-only `azapi_update_resource`; VM tags do
not propagate to it. VM deletion is configured to delete the OS disk, but a
later live cleanup must independently prove actual disk absence.

**No live admission is implemented.** `azure_budget.py reserve/check` always
refuses explicit XDP scopes with `PENDING`, even when supplied approval flags
or otherwise complete synthetic receipts. Neither this README nor a successful
offline check authorizes Terraform initialization, apply, resource creation,
ledger reservation, guest commands, or an XDP benchmark.

Versions are Terraform 1.17.0, azurerm 4.63.0 and azapi 2.8.0. The component's
Python validation baseline is the available `/usr/bin/python3.14` (3.14.4).
The source brief's retained-Python-3.12.13 assumption was disproved on resume;
3.12 tests were not run and compatibility is not claimed. This corrects this
component's validation baseline without changing project-wide Python policy.
The lockfile is an unchanged copy of
`infra/envs/dev/azure-hybrid/.terraform.lock.hcl`. Azure provider binaries were
unavailable during this source stage. Native `init`, `validate`, and
`tests/runner.tftest.hcl` execution are **PENDING**. Plan-shaped Python fixtures
do not establish provider defaults, native plan compatibility, image disk
minimum, actual capacity, or cloud ownership.

## Offline interfaces

The explicit proposal scope is `xdp-bootstrap` or `xdp-runner`. Runner plans
also contain `xdp_scope = "xdp-runner"`; bootstrap plans retain the original
four addresses and gain no new HCL variable. Bootstrap context must carry
`source_hashes` equal to the frozen six-file `BOOTSTRAP_HASHES` manifest in
`azure_xdp_scope.py`. The helpers reject unknown scopes and mixed resources.
Ordinary bootstrap admission retains its `.02/hour` limit through a narrow
guard; the shared HCL upper bound is `.10` to represent the full XDP backend
scenario. Ordinary bootstrap `down` remains refused.

The Python test fixtures document complete input shapes. They use fabricated
subscription IDs, keys, receipts, inventory and prices. To prepare a reviewed
context offline, use an existing owned private `0700` parent and a private
`0600` JSON context file:

```text
python3 scripts/azure_xdp_lifecycle.py prepare --parent PRIVATE_PARENT --context PRIVATE_CONTEXT
```

Preparation verifies and copies exactly the existing bootstrap's `main.tf`,
`variables.tf`, `versions.tf`, `outputs.tf`, `.terraform.lock.hcl` and
`backend.azurerm.tf.example`. It creates a new `0700` run directory with `0600`
files, a separate `tf-data` directory, explicit run variables, and `run.json`.
The new account is `pw<RUN_ID>` and distinct state keys are
`<RUN_ID>/bootstrap.tfstate` and `<RUN_ID>/runner.tfstate`. It does not initialize
state or invent a lineage. Reusing a run directory fails; partial output is
preserved. Do not copy old state, tfvars, backend configuration or credentials.

Offline teardown validation is available through:

```text
python3 scripts/azure_teardown.py --plan PRIVATE_BINARY_PLAN --reviewed-sha256 SHA256 --xdp-receipt PRIVATE_RECEIPT
python3 scripts/azure_reaper.py xdp-check --receipt PRIVATE_RECEIPT --inventory PRIVATE_INVENTORY
```

The first command hashes the binary before and after `terraform show -json`.
The second validates normalized exact-owned inventory and returns candidate
IDs only. Association IDs are Terraform logical identifiers, not commands to
send to Azure. Neither command executes cleanup. A bootstrap receipt requires
three distinct private exports: remote bootstrap, migrated local bootstrap,
and empty runner state, with matching hashes, correct run resources, lineage,
serial and counts. Local fsync and matching files cannot establish remote
migration or lease release; those remain PENDING. Supplying `--xdp-receipt` to
ordinary reaper `down` cannot select this path.

For a failed disk-tag patch, an untagged disk is only an offline cleanup
candidate when its exact ID is linked through the matching owned VM's
`storageProfile.osDisk.managedDisk.id`. A generated bootstrap role ID must
match the verified private export. Tags alone never identify resources for
cleanup, and foreign resources are refused.

## Cost and later admission gates

The pure `azure_xdp_budget.evaluate` function reviews synthetic/offline inputs;
production admission does not call it. Its full two-hour campaign includes
10,000 requests in **each** backend category, one aggregate GB-month including
retained versions, 20 million 16 KiB HDD operations, and 2 GB combined runner
and backend egress. Each category is rounded up to cents with `Decimal`.
The retained arithmetic scenario is **$1.53**, added to history **$2.21263205**
for **$3.74263205**. It is not a current price quote or reservation. Disk storage
and operations are assigned once to the disk-tag identity; backend categories
are assigned once to the account. The reduced $1.43 scenario is not used.

Before any future live enablement, separately review native provider plans,
fresh exact prices and subscription-bound eligibility, the pinned image's
32 GiB compatibility, verified operator SSH, ARM tool availability and build
resources, a locked canonical reservation, and actual lifecycle/reaper wiring.
A verified independent watcher must enforce one deadline from the first
bootstrap action: stop work at 80% of quantity bounds, begin cleanup by
90 minutes, finish by 120 minutes, with no rolling renewal. B-series CPU-credit
effects remain unproved for fair final benchmarks.

Later cleanup must destroy the runner, verify state zero and actual VM, disk,
PIP, NIC, NSG and VNet absence, then make durable private state exports and
verify migration back to a new local backend with no concurrent lease before
destroying the backend and resource group. Partial cleanup must fail visibly;
deallocation does not establish deletion. Ordinary reaper operations do not
provide this XDP lifecycle today. No private state or credentials belong in
public evidence. Original Task16 Aya/kernel/IPIP/IPVS packet-rate and latency
acceptance, Task14/15 runtime gates, and the overall Goal remain incomplete.
