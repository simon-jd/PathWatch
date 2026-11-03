# XDP host planner (Task16 B1)

This component runs **offline**. It constructs stable identity books and Maglev
plans and tests shared packet bytes. It does not load Aya/eBPF programs, create
maps, attach XDP, forward live traffic or implement kernel conntrack.

```
xdp-lb ctl plan-backends --vip 10.203.1.100 10.203.0.11 10.203.0.12
xdp-lb ctl init-state --state /private0700/offline.book
xdp-lb ctl plan-backends --vip 10.203.1.100 --state /private0700/offline.book 10.203.0.11
```

The optional state parent must already exist with mode0700; snapshots and lock
files use0600. State-backed planning requires explicit create-only initialization
with `ctl init-state` (library: `IdentityBook::initialize`). Ordinary load/save and
planning fail if the snapshot or lock is missing, including on a fresh path.
Initialization refuses an existing snapshot or lock. Keep the lock permanently:
it records that the location has been initialized even if the snapshot is lost.
Failed or interrupted initialization may leave a marker or partial snapshot and
make that location unusable; use a new private location for a new offline book,
never delete the marker to reset an issued identity namespace. There is no reset,
repair or recovery command. Planning output explicitly says OFFLINE PLAN,
not loaded or published.
Zero MACs in CLI identity records mean MAC/route discovery is unavailable; these
records are not attach-ready and must not be promoted into a live backend map.
`ctl set-backends` exits unsuccessfully with an unavailable message and performs
no state or kernel operation. Kernel-loader code is a later task gate.

Canonical IPv4 addresses map losslessly to zero-extended u64 identities. The
library also preserves arbitrary full-width u64 identities. Tokens1..4095 index
a4096-entry BACKENDS map in the future ABI; zero is invalid. Allocation is
monotonic for the book's lifetime, including partially failed planning. Reorder,
removal and re-addition never renumber an identity. Metadata is immutable and
lifetime capacity exhaustion fails explicitly. An inactive record is retained,
not freed. Existing Task12 `lb::Maglev` constructs the entire65537-value logical
table; the host plan encodes it as tokens and can decode every entry back to the
original u64. This is **planner parity**, not the required real kernel-map parity.

The versioned big-endian snapshot has an FNV-1a64 corruption checksum; this is
not authentication. A persistent private lock serializes initialization, reads
and snapshot replacement; the previous records must be an exact prefix, so a stale writer cannot erase or reassign
reserved tokens. Temporary files are exclusively created, synced, renamed and
followed by directory fsync. Reload/reconcile after a save error: an error after
rename can mean the new file exists. A successful optional save happens before
CLI output. Allocations that existed only in process memory before a failed save
were never durably issued; no kernel or output references them. Do not recreate
an empty book after corruption, loss or exhaustion. The offline snapshot is not
the future kernel PREPARED transaction/journal.

Pure recovery decisions model the proposed commit point: the kernel's observed
candidate ID means published even if disk metadata commit failed. Observed old
ID means not published; unknown ID/query failure retains both generations and
is indeterminate. These host decisions do not establish syscall/crash safety.

The no_std common crate defines deterministic repr(C) ABI values and validates
actual Ethernet/IPv4/TCP bytes before encapsulating into an existing buffer with
20 bytes of headroom. It preserves TCP options/payload, handles odd checksum
lengths, validates IPv4/TCP checksums and lengths, decrements TTL and recomputes
IPv4 checksums, writes real IPIP/Ethernet headers, and returns the exact trimmed
transmit range. Error/Pass leaves the buffer unchanged. `inspect` exposes a
validated tuple and initial-SYN flag for later conntrack integration.

Supported service profile: untagged single-buffer Ethernet, IPv4 IHL5,
unfragmented TCP to one VIP/port, TOS0, inner IP length40..1480, outer MTU<=1500,
fixed unicast L2 metadata, no IPv4 options. TCP header length20..60 and options
are preserved. VLAN/QinQ, fragments, malformed/unsupported VIP packets, TTL<=1,
MTU violations and head/tail adjustment failures drop. Runt Ethernet drops;
unrelated EtherTypes/ARP and well-formed non-VIP IPv4 pass. Only zero Ethernet
padding up to the minimum60-byte frame is trimmed. No NAT, PMTUD/ICMP generation,
IPv6 service, ECN extension, dynamic routing, jumbo/GSO or multi-buffer support
is claimed. The host adjustment-result input does not invoke a kernel helper.

Future CONNTRACK is bounded LRU tuple affinity: no idle timer, FIN deletion,
full TCP sequence state or guarantee of safe five-tuple reuse. Retained entries
keep their original reachable backend after updates/removal; eviction can break
an active flow, and non-SYN misses must drop rather than remap. Draining backend
metadata must stay available for old tuples. No such live flow guarantee is
established by B1.

Run with the repository's retained Rust1.99.1 and offline locked inputs:

```
cargo test --manifest-path xdp/Cargo.toml --locked --offline -p xdp-lb-common -p xdp-lb
cargo fmt --manifest-path xdp/Cargo.toml --all --check
cargo clippy --manifest-path xdp/Cargo.toml --locked --offline -p xdp-lb-common -p xdp-lb --all-targets -- -D warnings
```

Actual Aya compilation/verifier acceptance,65537 kernel readback through real
BACKENDS, atomic expected-old-FD replacement/failure preservation, shared-map
recovery, real3→4 backend TCP/IPIP/removal drain, native/DRV versus generic/SKB
attachment, actual IPVS-mh reference, delivered per-core packet/connection rate
and response tail latency all remain pending under separate resource/privilege
admission. A C capability fixture or host test cannot substitute final Aya
acceptance. See the complete Task16 design and controller rulings for those gates.
