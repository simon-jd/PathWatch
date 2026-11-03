# DNS measurement transports

DNS targets may opt into strict UDP, strict TCP or DNS over HTTPS (RFC 8484).
With no `DNS` object, existing targets keep UDP with TCP fallback on a truncated
reply, and their original target identity. TCP/TLS/HTTP/trace name resolution
continues to use the top-level `dns_server`; a target's `DNS` settings affect
only its DNS measurement.

For example, these entries belong in the existing probe configuration's
`targets` array (set `Public` and the interval for the deployment):

```json
[
  {"ID":"resolver-udp","Host":"owned.test","Public":false,"Kinds":["dns"],
   "DNS":{"transport":"udp","server":"127.0.0.1:1053"}},
  {"ID":"resolver-tcp","Host":"owned.test","Public":false,"Kinds":["dns"],
   "DNS":{"transport":"tcp","server":"127.0.0.1:1053"}},
  {"ID":"resolver-doh","Host":"owned.test","Public":false,"Kinds":["dns"],
   "DNS":{"transport":"doh","doh_url":"https://127.0.0.1:1443/dns-query",
          "ca_file":"/path/to/owned-ca.pem"}}
]
```

UDP/TCP `server` defaults to the existing `dns_server`, or the first system
nameserver if neither is configured. DoH requires an absolute HTTPS endpoint;
`server` is invalid for DoH. `doh_url`/`ca_file` are invalid for UDP/TCP. An empty
`DNS` object is invalid. CA files are loaded at startup and replace system roots
for that endpoint; omit the file to use system trust. Certificate chain and
hostname validation remain enabled. Endpoint hostnames use the OS resolver to
bootstrap their HTTPS connection within the request deadline. Ambient HTTP
proxies and redirects are disabled; only the configured endpoint is queried.

DoH uses POST with wire DNS and `application/dns-message` request/response
headers. It validates status, content type, the 65535-byte body bound, DNS
message parsing, response bit, opcode, ID and question. A successful HTTP status
does not hide a DNS error. A and AAAA are queried in order; partial success and
family-specific failure remain separate results. A round has a single probe
deadline; caller cancellation interrupts network reads. Explicit transports do
not fall back to another transport, including when a reply has the TC bit set.
Explicit DNS measurement targets must be hostnames: an IP literal is rejected
instead of reporting a transport measurement without sending a query.

Explicit DNS results append `:dns:udp`, `:dns:tcp` or `:dns:doh` to the configured
ID. For example, `owned` produces `owned:dns:doh`; changing its configured mode
creates another persisted series. The existing ingest path key already includes
target ID, so each transport and the unsuffixed legacy series stays separate in
raw rows and five-minute aggregates. `Protocol` remains HTTP-only and is empty
for DNS. No protobuf fields, ClickHouse schema, journal or spool formats change.
Existing ingest versions accept these IDs; deploy the probe before enabling the
new target settings. No ingest migration or rewriting of historical data is
needed.

The `:dns:` marker is reserved in **all configured target IDs** to prevent
collisions with generated IDs. An old configuration using that marker must
choose a new base ID; leave its historical namespace unused by newly generated
IDs. Existing persisted spool/journal records are replayed unchanged. Explicit
base IDs must match `[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}` so the generated ID fits
the existing ingest limit of 128 characters. Changing resolver endpoint while
retaining both mode and ID retains the same series: assign a new base ID when
comparing resolver endpoints.

Library callers pass targets to `dnsprobe.New(Config{Targets: ...})`; the
constructor validates and prepares their resolvers. `Probe` rejects an explicit
configuration that was not registered at startup or was changed afterward.
Unconfigured legacy targets remain supported. Public targets retain the existing
60-second minimum round interval.

Protocol reference: [RFC 8484 sections 4 and 6](https://www.rfc-editor.org/rfc/rfc8484.html).
