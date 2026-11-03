# lab/hybrid

On-prem side of the Azure ↔ home WireGuard + FRR link used by the failover drill
([results](../../docs/evidence/README.md#wireguard--frr-failover-azure-westus-to-an-on-prem-router)).

- `home.sh build|up|down` creates a labelled Docker bridge (10.30.0.0/16) with a router
  container (FRR, WireGuard; `--cap-drop ALL` plus a short list of container-scoped
  capabilities such as `NET_ADMIN` and `SYS_ADMIN`) and a workload container. It does not change host routes, sysctls or firewall.
- Configs come from `scripts/hybrid_config.py` (profiles `baseline` 60/180 s, `tuned` 3/9 s,
  `bfd` 300 ms x 3); keys from `scripts/hybrid_keys.sh`. The Azure side is
  `infra/envs/dev/azure-hybrid` plus `ansible/hybrid/playbook.yml`.
- `scripts/test_hybrid.py` checks the private path between the two workloads (ping and
  iperf3); `drills/vpn_failover.sh` runs the 3 x 5 failover trials and writes a new
  `run-*` directory per invocation.

Two logical tunnels share one NVA and one physical path, so this measures failure
detection, not high availability. Running it needs a deployed `azure-hybrid` environment.
Operator runbook (Chinese): [docs/dev/notes/lab-hybrid.md](../../docs/dev/notes/lab-hybrid.md).
