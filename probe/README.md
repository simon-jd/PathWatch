# probe

Go measurement agent. Each round it runs the configured measurement kinds against each
target, writes the results to a durable on-disk spool, and reports batches to ingest over
gRPC.

| Kind | What it measures |
|---|---|
| `dns` | A/AAAA resolution over UDP (TCP fallback on truncation), strict UDP, strict TCP or DoH ([transports](../docs/dns-transports.md)) |
| `tcp` | TCP connect time to every resolved address |
| `tls` | TLS 1.3 handshake with certificate verification; session resumption is recorded |
| `http1`, `http2`, `http3` | `HEAD /` time to first byte and total; the protocol actually negotiated is recorded |
| `trace` | UDP traceroute to private targets only; ICMP errors read from a raw socket (`trace_mode: icmp`, needs `CAP_NET_RAW`) or from the socket error queue (`trace_mode: udp`, `IP_RECVERR`, unprivileged) |

Scheduling adds ±10% jitter and keeps public targets at least 60 s apart. The spool commits
through an fsync'd manifest, evicts the oldest batch when full and counts drops. The report
client resends the same batch ID until ingest acknowledges the whole batch (capped
exponential backoff with full jitter). Metrics are served on `127.0.0.1:9464` by default.

```sh
make tools                                   # once: Go and other pinned tools into .tools/
source scripts/dev-env.sh
go build -o .tools/bin/pathwatch-probe ./probe/cmd/probe
.tools/bin/pathwatch-probe -config probe/config.example.json   # edit ingest address and targets first
make probe-test                              # probe and proto unit tests
```

Not supported: IPv6 traceroute, traceroute to public targets.
Detailed behaviour notes (Chinese): [docs/dev/notes/probe.md](../docs/dev/notes/probe.md).
