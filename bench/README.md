# bench

Fixed-rate benchmark of the edge proxy against Envoy and NGINX on one machine. Results and
method: [docs/evidence/README.md](../docs/evidence/README.md#edge-proxy-vs-envoy-and-nginx-one-machine).

- `setup.sh` downloads and builds the pinned tools (wrk2, h2load, Envoy, NGINX; see
  [tools.lock.json](tools.lock.json)) into `.cache/task13`. Needs GCC/G++, CMake, Make,
  OpenSSL 3, `taskset` and `pidstat`.
- `run.py` starts the Go echo backend (`echo.go`), the proxy under test and the load
  generator pinned to fixed cores, records `pidstat`, process snapshots and an H2 latency
  histogram per cell, and writes everything to a new directory under
  `docs/evidence/raw/task13/`.
- `analyze.py` turns a run directory into the summary tables.

```sh
bench/setup.sh
make bench-matrix PROTOCOL=h2 PROXY=pathwatch OUTPUT=new-run   # 4 rates x 5 rounds x 120 s
bench/run.sh --full --output new-matrix                         # all 3 proxies, H1 and H2 (about 4.5 h)
.tools/py/bin/python bench/analyze.py docs/evidence/raw/task13/new-run --output /tmp/summary.json
```

The full matrix pins cores 16-25 and needs an otherwise idle machine; output names must be
new. The unit tests (`bench/tests`) run as part of `make test`.
Detailed design notes (Chinese): [docs/dev/notes/bench.md](../docs/dev/notes/bench.md).
