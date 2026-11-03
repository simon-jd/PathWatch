package spool

import (
	"encoding/json"
	"pathwatch.local/pathwatch/probe/internal/result"
	"strings"
	"testing"
	"time"
)

// A DNS wire response is <=65535B for each A/AAAA query. Even with compressed
// names a resource record consumes >8 wire bytes, hence <16384 returned records
// over both responses. This test bounds JSON escaping, batch/manifest overhead
// and one unacknowledged candidate without dropping any returned measurement.
func TestCapacityDNSOverflowAllocationReserve(t *testing.T) {
	rs := make([]result.Result, 128)
	for i := range rs {
		rs[i] = result.Result{ProbeID: strings.Repeat("p", 16), TargetID: strings.Repeat("t", 24), Kind: result.DNS, Family: "v6", Addr: "ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff", StartedAt: time.Unix(4294967295-180*86400, 999999999), Err: strings.Repeat("\x00", 256), Phases: map[string]time.Duration{"resolve": time.Duration(1<<63 - 1)}}
	}
	q, err := Open(t.TempDir(), 64<<20)
	if err != nil {
		t.Fatal(err)
	}
	defer q.Close()
	if err = q.Append(rs); err != nil {
		t.Fatal(err)
	}
	_, got, _, err := q.Next()
	if err != nil || len(got) != 128 || got[0].Err != rs[0].Err {
		t.Fatal("full escaped results not persisted", err)
	}
	raw, err := json.Marshal(batch{ID: strings.Repeat("b", 64), Results: rs})
	if err != nil {
		t.Fatal(err)
	}
	allocated := (int64(len(raw)) + 4095) / 4096 * 4096
	// 128 overflow batches + one old unACKed batch + two manifests/atomic temp +
	// 20min outage +300s backoff +120s drain (<=17 additional profile rounds).
	forecast := allocated*130 + (2 << 20) + 17*15*4096
	if forecast >= 64<<20 {
		t.Fatalf("spool overflow forecast=%d exceeds64MiB", forecast)
	}
	t.Logf("128_result_JSON=%d allocated_block_bound=%d reserve_forecast=%d", len(raw), allocated, forecast)
}
