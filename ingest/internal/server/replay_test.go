package server_test

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"
	"time"

	"google.golang.org/protobuf/types/known/timestamppb"
	"pathwatch.local/pathwatch/ingest/internal/anomaly"
	pb "pathwatch.local/pathwatch/proto/pathwatch/v1"
)

// The probe spool's batch format (probe/internal/spool, result.Result fields).
type spooledBatch struct {
	ID      string
	Results []struct {
		Hops []struct {
			TTL  int
			Addr string
			RTT  time.Duration
		}
		TargetID, ProbeID, Kind, Family, Addr, Err, Protocol string
		StartedAt                                            time.Time
		Phases                                               map[string]time.Duration
	}
}

// TestAnomaly_ReplayRecordedPathChange replays a probe spool recorded in the
// Task 9 lab (make t9-up t9-start t9-switch; lab/pathchange) through
// store.Commit, then runs the anomaly job with the ingest binary's defaults
// over the recorded windows. Only runs with PATHWATCH_T9_SPOOL set.
func TestAnomaly_ReplayRecordedPathChange(t *testing.T) {
	dir := os.Getenv("PATHWATCH_T9_SPOOL")
	if dir == "" {
		t.Skip("PATHWATCH_T9_SPOOL not set (recorded lab spool)")
	}
	raw, err := os.ReadFile(filepath.Join(dir, "manifest.json"))
	if err != nil {
		t.Fatal(err)
	}
	var manifest struct {
		Dropped uint64
		Entries []struct{ ID string }
	}
	if err = json.Unmarshal(raw, &manifest); err != nil {
		t.Fatal(err)
	}
	if manifest.Dropped != 0 || len(manifest.Entries) == 0 {
		t.Fatalf("spool dropped=%d entries=%d", manifest.Dropped, len(manifest.Entries))
	}
	cfg, st, _ := fixture(t)
	db := cfg.Database
	var first, last time.Time
	kinds := map[string]int{}
	traceErrors := 0
	for _, e := range manifest.Entries {
		data, err := os.ReadFile(filepath.Join(dir, e.ID+".batch"))
		if err != nil {
			t.Fatal(err)
		}
		var b spooledBatch
		if err = json.Unmarshal(data, &b); err != nil || b.ID != e.ID {
			t.Fatalf("batch %s: %v", e.ID, err)
		}
		req := &pb.ReportRequest{BatchId: b.ID}
		for _, r := range b.Results {
			req.ProbeId = r.ProbeID
			phases := map[string]int64{}
			for k, v := range r.Phases {
				phases[k] = int64(v)
			}
			x := &pb.ProbeResult{TargetId: r.TargetID, ProbeId: r.ProbeID, Kind: r.Kind, Family: r.Family, Addr: r.Addr, StartedAt: timestamppb.New(r.StartedAt), PhasesNanoseconds: phases, Err: r.Err, Protocol: r.Protocol}
			for _, h := range r.Hops {
				x.Hops = append(x.Hops, &pb.Hop{Ttl: uint32(h.TTL), Addr: h.Addr, RttNanoseconds: int64(h.RTT)})
			}
			req.Results = append(req.Results, x)
			kinds[r.Kind]++
			if r.Kind == "trace" && r.Err != "" {
				traceErrors++
			}
			if first.IsZero() || r.StartedAt.Before(first) {
				first = r.StartedAt
			}
			if r.StartedAt.After(last) {
				last = r.StartedAt
			}
		}
		// Sent at its own recording time: the replay keeps the original clock.
		req.SentAt = timestamppb.New(b.Results[len(b.Results)-1].StartedAt)
		if _, err = st.Commit(context.Background(), req); err != nil {
			t.Fatalf("commit %s: %v", b.ID, err)
		}
	}
	alerts := 0
	am := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { alerts++ }))
	defer am.Close()
	// Same values as ingest/cmd/ingest flag defaults.
	job, err := anomaly.New(st, anomaly.Config{Warmup: 20, SigmaFloor: 1, K: .5, H: 12, MaxPaths: 1024, MaxWindows: 128, MaxPending: 64, AlertmanagerURL: am.URL, Timeout: 5 * time.Second, RetryMin: 5 * time.Second, RetryMax: 5 * time.Minute})
	if err != nil {
		t.Fatal(err)
	}
	closed := last.Truncate(5 * time.Minute) // the last, partial window stays open
	for i := 0; i < 8; i++ {
		if err = job.Tick(context.Background(), closed); err != nil {
			t.Fatal(err)
		}
	}
	windows := sql(t, db, "SELECT path_key, toString(window_start) AS w, round(quantilesMerge(0.5,0.95,0.99)(phase_ms)[2],3) AS p95, countMerge(samples) AS n FROM path_quantiles_5m GROUP BY path_key, window_start ORDER BY window_start, path_key FORMAT JSONEachRow")
	events := sql(t, db, "SELECT path_key, toString(window_start) AS w, round(p95_ms,3) AS p95, round(baseline_ms,3) AS baseline, round(sigma_ms,3) AS sigma, cusum_k, cusum_h, path_changed, first_divergent_ttl, correlation_reason FROM path_events FINAL ORDER BY window_start FORMAT JSONEachRow")
	traces := sql(t, db, "SELECT toString(started_at) AS at, hop_ttls, hop_addrs, err FROM probe_results WHERE kind='trace' ORDER BY started_at FORMAT JSONEachRow")
	// Path snapshots: consecutive distinct hop lists with first/last time seen.
	type span struct {
		Hops        string `json:"hops"`
		First, Last string
		Count       int
	}
	var spans []span
	for _, line := range strings.Split(traces, "\n") {
		var row struct {
			At    string   `json:"at"`
			Addrs []string `json:"hop_addrs"`
			Err   string   `json:"err"`
		}
		if json.Unmarshal([]byte(line), &row) != nil || row.Err != "" {
			continue
		}
		h := strings.Join(row.Addrs, " ")
		if len(spans) == 0 || spans[len(spans)-1].Hops != h {
			spans = append(spans, span{Hops: h, First: row.At})
		}
		spans[len(spans)-1].Last = row.At
		spans[len(spans)-1].Count++
	}
	lines := func(s string) []json.RawMessage {
		var out []json.RawMessage
		for _, l := range strings.Split(s, "\n") {
			if l != "" {
				out = append(out, json.RawMessage(l))
			}
		}
		return out
	}
	sorted := make([]string, 0, len(kinds))
	for k := range kinds {
		sorted = append(sorted, k)
	}
	sort.Strings(sorted)
	report := map[string]any{"spool": dir, "batches": len(manifest.Entries), "results_by_kind": kinds, "trace_errors": traceErrors, "first_result": first, "last_result": last, "closed_before": closed, "trace_method": "UDP probes, ICMP errors via IP_RECVERR (unprivileged, tracepath style)", "path_spans": spans, "windows": lines(windows), "path_events": lines(events), "alertmanager_posts": alerts}
	if out := os.Getenv("PATHWATCH_T9_OUT"); out != "" && !strings.HasSuffix(out, "/") {
		b, _ := json.MarshalIndent(report, "", "  ")
		if err = os.WriteFile(out, append(b, '\n'), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	t.Logf("kinds=%v trace_errors=%d spans=%d\nevents:\n%s", kinds, traceErrors, len(spans), events)
	if !strings.Contains(events, `"path_changed":true`) {
		t.Fatalf("no path-changed event; events=%s", events)
	}
	fmt.Println("replay events:", len(lines(events)))
}
