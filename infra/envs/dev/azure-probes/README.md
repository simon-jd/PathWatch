# Task10B: one probe/NVA per isolated Azure state

This root creates **all 21 managed objects**, including a new network, NIC and PIP;
`module.probe` runs on that NIC as both observer and NVA. No Task3 network is assumed
present. A is westus/10.10.0.0/16 (one lifetime <=840h); B is
swedencentral/10.20.0.0/16 (<=48h, at most two campaigns including Task4 DNS).
Each region still has a fresh 6-vCPU limit check. B2pts ARM64 and B2ats x64 are
explicit candidates, not allocation guarantees. Select only a freshly unrestricted,
priced candidate and a pinned matching Ubuntu24.04 image. There is no fallback SKU.

**Current canonical ledger is frozen. Do not execute reserve/up/renewal now.**
Real bootstrap parents have only their old TTL and lack required audited identity
metadata; the gate deliberately refuses them. All successful tests use fixtures.

## Controller prerequisites and retained parent renewal

The controller first reconciles actual historical charges/unknown usage without
releasing old reservation credit. Obtain a fresh `terraform show -json` of the exact
bootstrap state and four current read-only ARM records: RG `az group show`, storage,
container and role assignment `az resource show --ids EXACT_STATE_ID`. Preserve the
raw commands/results; do not discover a replacement object by matching a name.
Wrap the four records as `{subscription_id,captured_at,resources:[...]}` and run:

```sh
source scripts/dev-env.sh
python3 scripts/bootstrap_identity_evidence.py --state-json .cache/bootstrap-state.json \
  --current-arm-json .cache/bootstrap-current-arm.json --subscription-id "$AZURE_SUBSCRIPTION_ID" \
  --out .cache/bootstrap-identities.json
```

This **identity-only supporting report** compares four exact ARM IDs and current
parent ownership/TTL tags to state. It does not certify all ARM security properties,
modify ledger metadata, clear reconciliation, or approve renewal. The controller
must compare current security/ACL/RBAC properties against bootstrap state and
reconcile costs before recording audited identities. Missing/stale/mismatched
identity evidence is refused; no prior-state identity fallback is offered.

If parent TTL does not cover the proposed child, create a saved bootstrap plan
changing only the RG/storage `ttl` and justified `est_usd_per_hour` tags. Container
and role assignment must remain exact no-ops. Use `parent_renewal_proposal.py --help`
with fresh Retail storage capacity/read/write/other/egress meters and bounded counts.
It produces a reviewable proposal; normal `azure_budget.py reserve/check` supplies
locking and cumulative <=18 USD admission. The renewal appends conservative cost
for **now through the new TTL**, preserving old overlap charges. Exact saved-plan
check is idempotent; a new renewal identity cannot reset/release old reservations.
No generic parent-tag update or Azure CLI tag mutation is supported.

## Saved-plan deployment (future, after reconciliation)

Use a separate working copy/`TF_DATA_DIR` and backend key for each site:
`azure-probe-a.tfstate` / `azure-probe-b.tfstate`. Never change a live A workspace to
B or reuse a campaign UUID. Copy the examples to ignored local files and record
current host overlap proof. Configure `hourly.vm` to include bounded egress and
`hourly.disk` to include bounded transactions; PIP/HDD quantities round hours up.
Then obtain fresh evidence (this command only calls read APIs):

```sh
python3 scripts/probe_preflight.py --subscription-id "$AZURE_SUBSCRIPTION_ID" \
  --site azure-a --sku Standard_B2pts_v2 --image-version PINNED_VERSION \
  --out .cache/probe-preflight.json
```

Capture fresh exact regional Retail responses using `azure_budget.py capture`.
A meter manifest maps `vm_hours`, `ip_hours`, `disk_month`, `transactions`,
`egress_gb` to `{capture,selector}`; selector includes exact meterId, skuId,
productName, skuName, meterName, armRegionName, unitOfMeasure and tierMinimumUnits.
`network_capture` names a complete regional network Retail response. The probe
adapter validates all category/product/unit/tier matches and capture age, not just
meter names. Windows/Spot, pagination and missing Items are rejected. Paths are
absolute or project-relative, not relative to the manifest.

From this root use `make plan BACKEND=...`, then `make proposal PREFLIGHT=... \
METER_MANIFEST=... START_UTC=...`. Review the saved plan, full proposal, exact scope,
parent TTL, pricing, quota and cumulative estimate. Only then `make reserve` and
`make up`. The module apply wrapper also accepts only this complete root and
requires its canonical saved-plan reservation. A partial apply must follow exact
reviewed deletion/reconciliation; it cannot recreate an applied VM from lost state.

Before leaving a live VM, capture a fresh complete current **no-op** plan and a
complete `make down-plan`; independently review the exact destroy hash. Run:

```sh
python3 scripts/probe_usage_watch.py --current-plan PATH/current.tfplan \
  --destroy-plan PATH/destroy.tfplan --reviewed-destroy-sha256 REVIEWED_SHA256 \
  --ssh-key .cache/probe/id --known-hosts .cache/probe/known_hosts \
  --terminal-output .cache/probe/terminal.json
```

The controller uses strict SSH host-key verification and reads actual boot-inclusive
HDD IO and physical-NIC TX counters, including WG/BFD control traffic. At 80% of
10,000 transactions/hour or 0.005 GB/hour reservation, or at TTL, counter reset or
sampling failure, it executes only the reviewed exact root deletion. Revalidates
the destroy hash/scope before apply. Last successful terminal sample survives
cleanup; errors are explicit, not substituted with zero. Evidence write failure
still attempts cleanup. A sampling failure exits nonzero even when cleanup succeeds.
No Azure credentials or deletion API are placed on the guest. Controller/watchdog
failure itself still requires human exact-down supervision; this is not a hosted
reliable scheduler. Bounds may stop BFD-heavy workloads early and do **not** prove
five-week capacity. Final billing remains unknown until separately reconciled.

## WG/FRR transport and boundaries

`scripts/probe_transport.py` renders Task3's pure WG/FRR configuration for two
independent peers/tunnels per site. Store fresh keys/public keys as mode0600
`.cache/keys/{azure-a,azure-b}-{cloud,home}-{0,1}.{key,pub}`. Run with `--site`,
`--endpoint CURRENT_OWNED_PIP`, `--keys-dir` and `--out .cache/...`. Home initiates
both tunnels; public ingress is restricted to the exact owner public /32.

`scripts/probe_transport_home.py up --site SITE --config .cache/.../home \
--image-id sha256:REVIEWED_EXISTING_IMAGE` uses an existing project-labelled Task3
router image. It never pulls/builds. The image must contain Task3 entrypoint,
WireGuard and FRR; no matching image currently exists locally, so no native FRR
runtime acceptance has occurred. Budget/review any future build separately.
`down --site SITE` removes only the exact labelled router and, when empty, its exact
owned bridge. No host sysctl, route, global iptables or system service is changed.

The new bridge is `pathwatch-probe-home`, gateway10.30.0.1; home-A router is .2 and
home-B .3. A uses transit .1/.2 and .5/.6; B uses .9/.10 and .13/.14, ASN65020.
Home-A exports home+B, home-B exports home+A, each with a static other-site route
through its peer home router. Cloud imports exactly home+other and exports exactly
its own /16. No default/unrelated prefixes are permitted. The cloud Zebra route-map
sets source10.10.1.4/10.20.1.4 for locally originated remote probes; official syntax:
https://docs.frrouting.net/en/stable-8.3/zebra.html (`set src ADDRESS`). Actual FRR
parse/kernel source selection and failover still need native acceptance.

Container-only forwarding allows the exact cloud .1.4/32 pair for TCP/UDP18443 and
ICMP without SNAT. Only cloud .1.4/32 -> home gateway TCP19443 receives namespace
SNAT. SNAT success proves ingestion return routing, not original source transparency.
Use `ansible/probe-transport.yml` with the supplied inventory example for the exact
owned remote Ubuntu guest; it reuses A's unchanged probe role/binary interface.
Render first, use reviewed architecture-matched probe binary hash, verify guest
host keys, and enable no public echo or public ingest ingress.

`lab/probe-transport/home-tls.overlay.json` uses A's existing native ingest listen,
tls_cert/tls_key interface at10.30.0.1:19443; configure a private certificate with
IP SAN10.30.0.1 and give cloud probes its CA. Apply only through the supported A
configuration lifecycle, after the bridge exists; this task never changes A's
running home configuration. Template targets stay empty until real owned services
are deployed, and consequently are not a ready-to-run measurement profile.

**C handoff code gaps:** guest/home private echo service deployment and valid SAN
certificates; native home observer entering an owned router namespace so it can
reach cloud private prefixes. Native host home observer currently has no private
route; B's ingestion SNAT and A<->B routes do not supply that route. Do not add
host-global routes to hide this gap. No live cloud TLS/WG/BGP/echo/source-transparency
acceptance or three-station measurement coverage is claimed by these offline tests.
