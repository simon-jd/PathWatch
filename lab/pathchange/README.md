# lab/pathchange

Path-change lab on Docker networks, no root needed.
Results: [docs/evidence/README.md](../../docs/evidence/README.md#path-change-detection-replay-docker-lab-one-machine).

```
probe ── r1 ──(b)── r2 ──(d)── target      primary path, 3 hops
           └──(c)── r3 ──(d)──┘             backup path, 20 ms netem delay on r3 towards target
```

- Networks `pathwatch-t9-{a,b,c,d}` (10.219.1-4.0/24), containers `pathwatch-t9-*`, label
  `project=pathwatch`. Routers and target use `ip`, `tc` and python3 from the
  `quay.io/frrouting/frr:10.7.1` image, with `NET_ADMIN` inside the container only.
- The probe container runs with `--cap-drop ALL` as an unprivileged user in the network
  namespace of `pathwatch-t9-pnet`. Traceroute uses UDP probes with `IP_RECVERR`
  (`trace_mode: udp`), so it needs no `CAP_NET_RAW`.
- The ingest address is deliberately unreachable, so every result stays in the spool
  (`.cache/task09/spool`) and is replayed later with `make t9-replay`.
- The path change is a real route change: `make t9-switch` moves r1's next hop for
  10.219.4.0/24 from r2 to r3.

```sh
make t9-probe-bin t9-up t9-start    # record a baseline (the job's warm-up is 20 five-minute windows, about 100 min)
make t9-switch                      # real route change
make t9-status
make t9-down                        # after stopping the probe
make t9-replay OUT=docs/evidence/task-09-replay/replay.json   # needs Docker (ClickHouse testcontainer)
```
