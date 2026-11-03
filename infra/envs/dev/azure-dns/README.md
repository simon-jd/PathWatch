# Bounded Azure DNS lab

This separate state owns 39 resources: one ARM64 B2pts VM, 30GB Standard_LRS OS disk, NIC/PIP, VNet/subnet/NSG and seven inbound rules in each of westus and swedencentral; one Private DNS zone, three A records, two VNet links; one priority Traffic Manager profile and two Azure PIP endpoints. It retains the shared RG/backend. No FRR changes or Task3 resource reuse. Static authenticated WG suffices for private DNS forwarding.

Source/mocks/skip do not constitute actual Azure acceptance. Controller must finish the Task3 reviewed 32-delete plan and home cleanup, reconcile its ledger rows as deleted, then independently review Task4. The DNS guard rejects uncleared Task3 ledger rows even if the explicit cleanup flag is true. Task3 historical 0.90USD still counts. Region B has at most two durable measurement campaign UUIDs, including reserved/failed/deleted campaigns; retries preserve the same campaign ID and original time window. The same binary plan uses its existing reservation; a newly reviewed retry plan receives new conservative rows under the same campaign and cumulative cap. Never invent a new ID to bypass history or extend a previous campaign window.

## Short actual path (controller only)

1. Before creating anything, recheck allowed SKU, pinned ARM64 image and shared regional total/Bpsv2 quotas. Confirm the retained parent TTL. Refresh complete Retail captures if older than 24h. Select one <=2h timebox; never extend resources past parent expiry. Optional VPN/AKS remain off.
2. Prepare private project SSH key and independently verified `known_hosts` in `.cache`. Generate WG keys with `lab/dns/home.sh keys` (project image, unprivileged/no-network generation). Keys are never printed. Copy this root's tfvars/backend examples into ignored private files and set actual owner public /32, subscription, public key, new UUID and `task3_cleanup_confirmed=true`. Do not edit the active Task3 inventory.
3. From this root run `make plan BACKEND=... VARS=...`. Run `make proposal START_UTC=... CAPTURE_DATE=2026-10-23`. Review the exact saved binary plan and proposed rows, global/regional selectors, timestamps, TTL and quota before `make reserve`. `make up` checks the exact digest reservation under the canonical lock before apply. No source workflow auto-reserves or auto-creates resources.
4. Save `terraform output -json inventory` to `.cache/dns-inventory.json` (Terraform output value, not full state). Add absolute `ssh_private_key` and `known_hosts` paths. Save only private files, never public IP/key inventory into git. The inventory already includes exact ARM IDs, distinct site regions, campaign UUID, TTL and TM domain. `Runtime.attest` checks live guest IMDS VM identity/location before setup or measurements.
5. From repo root render and install:

   ```sh
   python3 scripts/dns_config.py --keys .cache/dns-keys --out .cache/dns-config --inventory .cache/dns-inventory.json
   python3 scripts/dns_setup.py --inventory .cache/dns-inventory.json --keys .cache/dns-keys --out .cache/dns-setup --confirm-owned-endpoints
   lab/dns/home.sh up .cache/dns-config
   python3 scripts/test_dns.py --inventory .cache/dns-inventory.json --out .cache/dns-private --confirm-owned-endpoints
   drills/dns_failover.sh --inventory .cache/dns-inventory.json --out .cache/dns-failover --confirm-owned-endpoints
   ```

   The equivalent opt-in Go entry is `PATHWATCH_DNS_ACTUAL=1 PATHWATCH_DNS_INVENTORY=/absolute/.cache/dns-inventory.json PATHWATCH_DNS_EVIDENCE=/absolute/.cache/dns-private go test ./infra/test -run '^TestDNSCrossResolution$' -v -timeout 5m`. Skipped default is not a pass for actual resolution.

6. If the managed endpoints are not both Online yet, the drill fails before stopping anything. Wait for Azure to converge and rerun into a fresh directory; cross-process local rate history preserves >=60s observer/target cadence even across retries. Custom queries run at HOME and over exact SSH to A/B, with their own local monotonic/realtime stamps. An unchanged primary-only baseline is required at all three; the backup must remain Online and return its owned `pathwatch-dns-b` HTTP root marker before/after the experiment. The managed HTTP `/healthz` probe is Azure's 30s probe, separate from custom >=60s probes.
7. Always run `lab/dns/home.sh down`. Generate `make down-plan`, review its exact delete-only digest, then `make down REVIEWED_PLAN_SHA256=...`. Verify VM/OSdisk/NIC/PIP/NSG/VNet, private zone/links/records and TM profile/endpoints are absent and backend/RG remain. Reconcile all canonical rows' actual deletion time under the ledger lock and mirror the spend record; historical estimates and B campaign history never disappear. VM OS disk deletion is configured in the provider; disk tag manager deletion alone is not proof of disk deletion.

## Bounds and interpretation

CoreDNS 1.12.4 official ARM64 archive SHA256 is fixed in `scripts/dns_config.py`; download verifies it before extracting one regular file. CoreDNS binds only private NIC/WG IPs and forwards only `dns.pathwatch.internal` to Azure 168.63.129.16. No public listener mapping, host resolver edit or host global firewall change. Home has its own labeled container and refuses to start while Task3 home containers exist. Private resolution checks all three actual owned addresses from each site, including both Azure forwarders from home; a DNS result alone does not assert IP reachability.

Each cloud command has a guest-side absolute-counter watchdog (including boot/apt), bounded runtime and process-group cleanup, plus the controller's 5s monitor. Reserve per VM exactly 0.25GB egress and 1,000,000 conservative 16KiB disk transactions. Stop at 80% or 90s before TTL; the only monitor bypass is the exact primary HTTP restore and status check. If boot already exceeds the threshold, do not install packages or increase a limit in place: stop/delete and have the controller price/review a new suitable plan before a new create. Keep the remaining 20% for restoration/deletion.

TM endpoints reserve a full monthly paid endpoint each (currently 0.36USD); no hourly conversion of unit `1`. Private zone reserves every touched UTC day using monthly/28, plus 10k private queries; TM profile reserves 10k public queries. Query journals stop at 80% and are durable in `.cache`; keep them throughout the reservation. Public DNS journals include HTTP calls conservatively. Tagged TM parent hourly estimate covers its untaggable endpoint children. VM implicit OS disks receive all four tags via exact disk-tag-only AzAPI. Subnets, NSG rules, NSG associations and TM endpoints have no provider tag fields: their exact named parents own their lifecycle; DNS records and VNet links do support tags and are tagged.

Reported failover is a last-primary/first-backup interval on each observer's monotonic clock, at >=60s resolution. Do not subtract clocks across hosts. `30*(2+1)+TTL30=120s` is theoretical context, not a promised measurement; managed probe aggregation and DNS caching add uncertainty. Any command, backup validation, restoration or monitor-exit failure leaves a failed fresh run and no success summary. SIGTERM/KeyboardInterrupt after attempted stop enters restoration. Never combine old completed runs or turn all-unhealthy best-effort DNS into a success claim.

### Reservation and final usage gates

Keep `AZURE_SUBSCRIPTION_ID` exported for runtime commands. Inventory campaign UUID and TTL must match the canonical ledger's complete active 39-resource reservation, including subscription, original <=2h window and exact query/usage bounds. A same-campaign no-op is free of new reservations, but cannot change UUID, renew TTL, revive deleted rows or reset query counters. The lock-protected check rejects unreserved inventories before any SSH/HTTP/DNS action.

Guest commands check absolute usage and TTL again after a successful child exit. The controller joins its monitor and performs one final bounded two-guest sample before any successful result is published. The exact primary restore/status exception remains available for cleanup; final sampling failures still prevent PASS.

The pinned provider's unconfigured `termination_notification` and `soa_record` blocks may be unknown only in the observed create shape (`after=null`, `after_unknown=true`, absent configuration expression). The Regular VM default `max_bid_price=-1` likewise requires an absent configuration expression. Known no-op/delete state must retain the disabled termination/default Azure SOA shapes; explicit configuration or a different default fails closed and needs review.
