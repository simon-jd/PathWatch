# lab/bgp

Anycast and BGP-incident lab on plain Docker bridge networks (containerlab needs sudo).
`lab.py` builds the topology in [topology.yml](topology.yml): one /29 per link, a monitoring
network 10.117.100.0/28, containers and networks named `pathwatch-bgp-*`.
Results: [docs/evidence/README.md](../../docs/evidence/README.md#anycast-and-bgp-incident-detection-docker-networks-one-machine).

```sh
make lab-up                  # 9 FRR 10.7.1 routers + GoBGP 4.10.0 collector, waits for convergence
make lab-rpki                # Krill 0.16.0 test trust anchor signs 198.51.100.0/24-24 => AS64500; Routinator 0.15.3 uses only that TAL
make lab-test                # offline config checks + live test_anycast_nearest_pop
make lab-drill INCIDENT=leak # or hijack; 5 runs each, results in docs/evidence/task-17-bgp/<run>/
make lab-down
```

- Router containers get `NET_ADMIN` and `SYS_ADMIN` (FRR 10.x does not start without the
  latter) inside the container only; not `--privileged`, default seccomp/AppArmor stay on.
- Nearest PoP: hot-potato routing inside AS64601 sends c1 to pop1 and c2 to pop2; pop2
  prepends twice towards AS64602, so c3 reaches pop3.
- Policy follows Gao-Rexford: customer routes local-pref 200, peers 100, only customer
  routes exported to peers and upstreams. AS64601 filters customer prefixes strictly;
  AS64602 does not, so leaks and hijacks propagate.
- Incidents: `leak` makes c2 export every route to both upstreams and the collector;
  `hijack` makes c3 announce 198.51.100.0/25.
- Detection (`detect.py`, polls every feed's Adj-RIB-In on the collector every 0.25 s): an
  AS path that is not valley-free, or a stub feed announcing a prefix outside its cone, is a
  route leak; an (origin, prefix) that Routinator marks invalid is an RPKI-invalid alert.
  Routers do not filter on ROV.
- Krill's HTTPS certificate and Routinator's RRDP root come from
  `ingest/cmd/labpki -extra-servers krill` (written to `.cache/task17/pki`, not committed).
  Routinator runs RRDP only (rsync off).
