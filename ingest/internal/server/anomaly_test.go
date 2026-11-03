package server_test

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync/atomic"
	"syscall"
	"testing"
	"time"

	"google.golang.org/protobuf/types/known/timestamppb"
	"pathwatch.local/pathwatch/ingest/internal/anomaly"
	"pathwatch.local/pathwatch/ingest/internal/store"
	pb "pathwatch.local/pathwatch/proto/pathwatch/v1"
)

// Catches per-batch quantile reads, latency-kind hop lookups, cursor loss and
// dropping pending notifications on HTTP errors/restart.
func TestAnomaly_ClosedMergedWindowsAndDelayed503RestartDelivery(t *testing.T) {
	cfg, st, _ := fixture(t)
	base := now.Add(-time.Hour).Truncate(5 * time.Minute)
	put := func(id, kind, addr string, at time.Time, ms int64, router string) {
		t.Helper()
		r := batch(id, 1)
		x := r.Results[0]
		x.Kind = kind
		x.Addr = addr
		x.StartedAt = timestamppb.New(at)
		x.PhasesNanoseconds = map[string]int64{"connect": ms * 1e6}
		if kind == "trace" {
			x.PhasesNanoseconds = map[string]int64{}
			for ttl := 1; ttl <= 4; ttl++ {
				a := "192.0.2.1"
				if ttl == 4 {
					a = router
				}
				x.Hops = append(x.Hops, &pb.Hop{Ttl: uint32(ttl), Addr: a})
			}
		}
		if _, e := st.Commit(context.Background(), r); e != nil {
			t.Fatal(e)
		}
	}
	for i, ms := range []int64{50, 50, 80} {
		put(fmt.Sprint("lat", i), "tcp", "127.0.0.1", base.Add(time.Duration(i)*5*time.Minute+time.Minute), ms, "")
	}
	// A second batch at the alarm window must be merged, not processed separately.
	put("lat-other-batch", "tcp", "127.0.0.1", base.Add(11*time.Minute), 80, "")
	put("trace-before", "trace", "127.0.0.1", base.Add(9*time.Minute), 0, "192.0.2.4")
	put("trace-after", "trace", "127.0.0.1", base.Add(11*time.Minute), 0, "192.0.2.9")
	put("unrelated", "trace", "127.0.0.2", base.Add(12*time.Minute), 0, "192.0.2.99")
	put("future", "trace", "127.0.0.1", base.Add(16*time.Minute), 0, "192.0.2.99")
	for _, field := range []string{"probe", "target"} {
		r := batch("wrong-"+field, 1)
		x := r.Results[0]
		x.Kind = "trace"
		x.PhasesNanoseconds = map[string]int64{}
		x.StartedAt = timestamppb.New(base.Add(12 * time.Minute))
		x.Hops = []*pb.Hop{{Ttl: 1, Addr: "192.0.2.99"}}
		if field == "probe" {
			r.ProbeId = "other"
			x.ProbeId = "other"
		} else {
			x.TargetId = "other"
		}
		if _, e := st.Commit(context.Background(), r); e != nil {
			t.Fatal(e)
		}
	}
	windows, e := st.ReadAnomalyWindows(context.Background(), time.Time{}, "", base.Add(15*time.Minute), 64)
	if e != nil || len(windows) != 3 || windows[2].Samples != 2 || windows[2].P95 != 80 {
		t.Fatalf("merged windows=%+v error=%v", windows, e)
	}
	var calls atomic.Int32
	var received [][]byte
	sink := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		defer r.Body.Close()
		var payload json.RawMessage
		_ = json.NewDecoder(r.Body).Decode(&payload)
		received = append(received, append([]byte(nil), payload...))
		if calls.Add(1) == 1 {
			http.Error(w, "local injected failure", 503)
			return
		}
		w.WriteHeader(200)
	}))
	defer sink.Close()
	jc := anomaly.Config{Warmup: 2, SigmaFloor: 5, K: .5, H: 5, MaxPaths: 8, MaxWindows: 64, MaxPending: 8, AlertmanagerURL: sink.URL, Timeout: time.Second, RetryMin: time.Second, RetryMax: time.Minute}
	job, e := anomaly.New(st, jc)
	if e != nil {
		t.Fatal(e)
	}
	tick := base.Add(15 * time.Minute)
	if e = job.Tick(context.Background(), tick); e == nil || !strings.Contains(e.Error(), "503") {
		t.Fatalf("expected visible delivery error, got %v", e)
	}
	if got := sql(t, cfg.Database, "SELECT count(),any(first_divergent_ttl),any(path_changed) FROM path_events FINAL"); got != "1\t4\ttrue" {
		t.Fatal(got)
	}
	id := sql(t, cfg.Database, "SELECT event_id FROM path_events FINAL")
	if e = job.Tick(context.Background(), tick); e != nil {
		t.Fatal(e)
	}
	if calls.Load() != 1 {
		t.Fatal("retry ignored backoff")
	}
	if e = st.Close(); e != nil {
		t.Fatal(e)
	}
	st, e = store.Open(context.Background(), cfg)
	if e != nil {
		t.Fatal(e)
	}
	defer st.Close()
	job, e = anomaly.New(st, jc)
	if e != nil {
		t.Fatal(e)
	}
	if e = job.Tick(context.Background(), tick.Add(10*time.Minute+2*time.Second)); e != nil {
		t.Fatal(e)
	}
	if e = job.Tick(context.Background(), tick.Add(15*time.Minute)); e != nil {
		t.Fatal(e)
	}
	if calls.Load() != 2 {
		t.Fatalf("calls=%d", calls.Load())
	}
	if got := sql(t, cfg.Database, "SELECT count(),any(event_id) FROM path_events FINAL"); got != "1\t"+id {
		t.Fatal(got)
	}
	if len(received) == 2 && strings.Contains(string(received[1]), "endsAt") {
		t.Fatal("delayed restart delivery contains an expired endsAt", string(received[1]))
	}
	if len(received) != 2 || string(received[0]) != string(received[1]) || !strings.Contains(string(received[1]), id) || !strings.Contains(string(received[1]), "startsAt") {
		t.Fatalf("retry alert payloads=%q", received)
	}
	if !strings.Contains(string(received[1]), base.Add(10*time.Minute).Format(time.RFC3339)) {
		t.Fatal("missing original observed window", string(received[1]))
	}
	t.Log("merged windows", windows, "logical event", id, "local alert", string(received[1]))
	q := "SELECT path_key,window_start,quantilesMerge(0.5,0.95,0.99)(phase_ms),countMerge(samples) FROM path_quantiles_5m GROUP BY path_key,window_start ORDER BY window_start,path_key FORMAT JSONEachRow"
	t.Log("actual merged-state SQL:", q, "result:", sql(t, cfg.Database, q))
	q = "SELECT * FROM path_events FINAL FORMAT JSONEachRow"
	t.Log("actual event SQL:", q, "result:", sql(t, cfg.Database, q))
	t.Log("actual path_events SHOW CREATE:", sql(t, cfg.Database, "SHOW CREATE TABLE path_events"))
}

// Catches reading partially committed MV state or ignoring the writer fence.
func TestAnomaly_StableReadWaitsForCommitAndRejectsFrozen(t *testing.T) {
	gate := newRequestGate()
	var inject atomic.Bool
	proxy := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		forwardFaultRequest(w, r, endpoint+r.URL.RequestURI(), b, func() bool {
			if strings.HasPrefix(string(b), "INSERT INTO probe_results") && inject.CompareAndSwap(true, false) {
				gate.wait()
				dropFaultResponse(w)
				return true
			}
			return false
		})
	}))
	defer gate.closeProxy(proxy)
	cfg, st, _ := fixtureURL(t, proxy.URL)
	inject.Store(true)
	done := make(chan error, 1)
	go func() { _, e := st.Commit(context.Background(), batch("inflight", 1)); done <- e }()
	select {
	case <-gate.reached:
	case <-time.After(3 * time.Second):
		t.Fatal("commit did not reach fault gate")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()
	if _, e := st.ReadAnomalyWindows(ctx, time.Time{}, "", now, 8); !errors.Is(e, context.DeadlineExceeded) {
		t.Fatalf("inflight read=%v", e)
	}
	gate.release()
	if e := <-done; e == nil {
		t.Fatal("ambiguous commit succeeded")
	}
	if _, e := st.ReadAnomalyWindows(context.Background(), time.Time{}, "", now, 8); e == nil {
		t.Fatal("read frozen raw/MV")
	}
	st.Close()
	if other, e := store.Open(context.Background(), cfg); e == nil {
		other.Close()
		t.Fatal("pending owner reopened without recovery")
	}
}

func TestAnomaly_MainRuntime(t *testing.T) {
	cfg, st, _ := fixture(t)
	base := now.Add(-time.Hour).Truncate(5 * time.Minute)
	for i, ms := range []int64{50, 50, 80} {
		r := batch(fmt.Sprint("runtime", i), 1)
		r.Results[0].StartedAt = timestamppb.New(base.Add(time.Duration(i) * 5 * time.Minute))
		r.Results[0].PhasesNanoseconds["connect"] = ms * 1e6
		if _, e := st.Commit(context.Background(), r); e != nil {
			t.Fatal(e)
		}
	}
	st.Close()
	got := make(chan string, 1)
	sink := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		select {
		case got <- string(b):
		default:
		}
		w.WriteHeader(200)
	}))
	defer sink.Close()
	binary := filepath.Join(t.TempDir(), "ingest")
	build := exec.Command("go", "build", "-o", binary, "../../cmd/ingest")
	if b, e := build.CombinedOutput(); e != nil {
		t.Fatalf("build: %v %s", e, b)
	}
	logs, e := os.CreateTemp(t.TempDir(), "runtime-")
	if e != nil {
		t.Fatal(e)
	}
	defer logs.Close()
	metricsPort := freePort()
	cmd := exec.Command(binary, "-clickhouse-url", cfg.URL, "-database", cfg.Database, "-clickhouse-user", cfg.Username, "-journal", cfg.JournalDir, "-development-cleartext", "-listen", "127.0.0.1:"+freePort(), "-metrics", "127.0.0.1:"+metricsPort, "-anomaly-enable", "-anomaly-warmup", "2", "-anomaly-sigma-floor-ms", "5", "-alertmanager-url", sink.URL)
	cmd.Env = append(os.Environ(), "PATHWATCH_CLICKHOUSE_PASSWORD="+cfg.Password)
	cmd.Stdout = logs
	cmd.Stderr = logs
	if e = cmd.Start(); e != nil {
		t.Fatal(e)
	}
	waited := false
	defer func() {
		if !waited {
			cmd.Process.Kill()
			cmd.Wait()
		}
	}()
	select {
	case payload := <-got:
		if !strings.Contains(payload, "PathLatencyCUSUM") {
			t.Fatal(payload)
		}
	case <-time.After(10 * time.Second):
		b, _ := os.ReadFile(logs.Name())
		t.Fatalf("runtime did not deliver local alert: %s", b)
	}
	client := &http.Client{Timeout: time.Second}
	resp, e := client.Get("http://127.0.0.1:" + metricsPort + "/metrics")
	if e != nil {
		t.Fatal(e)
	}
	b, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if !strings.Contains(string(b), "pathwatch_anomaly_ticks_total 1") {
		t.Fatalf("job metrics missing: %s", b)
	}
	if e = cmd.Process.Signal(syscall.SIGTERM); e != nil {
		t.Fatal(e)
	}
	e = cmd.Wait()
	waited = true
	if e != nil {
		t.Fatal(e)
	}
	if n := sql(t, cfg.Database, "SELECT count() FROM path_events FINAL"); n != "1" {
		t.Fatal(n)
	}
}

// Catches acknowledging an uncertain CH event write without a durable retry.
func TestAnomaly_EventWriteLossIsLogicallyIdempotent(t *testing.T) {
	var inject atomic.Bool
	proxy := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		forwardFaultRequest(w, r, endpoint+r.URL.RequestURI(), b, func() bool {
			if strings.HasPrefix(string(b), "INSERT INTO path_events") && inject.CompareAndSwap(true, false) {
				dropFaultResponse(w)
				return true
			}
			return false
		})
	}))
	defer proxy.Close()
	cfg, st, _ := fixtureURL(t, proxy.URL)
	base := now.Add(-time.Hour).Truncate(5 * time.Minute)
	for i, ms := range []int64{50, 50, 80} {
		r := batch(fmt.Sprint("eventloss", i), 1)
		r.Results[0].StartedAt = timestamppb.New(base.Add(time.Duration(i) * 5 * time.Minute))
		r.Results[0].PhasesNanoseconds["connect"] = ms * 1e6
		if _, e := st.Commit(context.Background(), r); e != nil {
			t.Fatal(e)
		}
	}
	var calls atomic.Int32
	sink := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { calls.Add(1); w.WriteHeader(200) }))
	defer sink.Close()
	jc := anomaly.Config{Warmup: 2, SigmaFloor: 5, K: .5, H: 5, MaxPaths: 8, MaxWindows: 64, MaxPending: 8, AlertmanagerURL: sink.URL, Timeout: time.Second, RetryMin: time.Second, RetryMax: time.Minute}
	job, e := anomaly.New(st, jc)
	if e != nil {
		t.Fatal(e)
	}
	inject.Store(true)
	tick := base.Add(15 * time.Minute)
	if e = job.Tick(context.Background(), tick); e == nil {
		t.Fatal("lost event response was swallowed")
	}
	if calls.Load() != 0 {
		t.Fatal("HTTP notification before CH write confirmation")
	}
	id := sql(t, cfg.Database, "SELECT event_id FROM path_events FINAL")
	if len(id) != 64 {
		t.Fatal(id)
	}
	st.Close()
	st, e = store.Open(context.Background(), cfg)
	if e != nil {
		t.Fatal(e)
	}
	defer st.Close()
	job, e = anomaly.New(st, jc)
	if e != nil {
		t.Fatal(e)
	}
	if e = job.Tick(context.Background(), tick.Add(2*time.Second)); e != nil {
		t.Fatal(e)
	}
	if got := sql(t, cfg.Database, "SELECT count(),any(event_id) FROM path_events FINAL"); got != "1\t"+id {
		t.Fatal(got)
	}
	if calls.Load() != 1 {
		t.Fatal(calls.Load())
	}
	t.Log("uncertain event write retried with stable ID", id, "physical rows", sql(t, cfg.Database, "SELECT count() FROM path_events"))
}

// Catches fabricating gap windows, processing the open window, mixing addresses,
// and silently evicting path state when cardinality is exhausted.
func TestAnomaly_GapsClosedWindowAndCapacity(t *testing.T) {
	cfg, st, _ := fixture(t)
	base := now.Add(-time.Hour).Truncate(5 * time.Minute)
	for i, offset := range []time.Duration{0, 10 * time.Minute, 20 * time.Minute, 25 * time.Minute} {
		r := batch(fmt.Sprint("gaps", i), 1)
		r.Results[0].StartedAt = timestamppb.New(base.Add(offset))
		r.Results[0].PhasesNanoseconds["connect"] = 50e6
		if i >= 2 {
			r.Results[0].PhasesNanoseconds["connect"] = 80e6
		}
		if _, e := st.Commit(context.Background(), r); e != nil {
			t.Fatal(e)
		}
	}
	r := batch("pooled-address", 1)
	r.Results[0].StartedAt = timestamppb.New(base.Add(21 * time.Minute))
	r.Results[0].Addr = "127.0.0.2"
	r.Results[0].PhasesNanoseconds["connect"] = 80e6
	if _, e := st.Commit(context.Background(), r); e != nil {
		t.Fatal(e)
	}
	sink := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(200) }))
	defer sink.Close()
	jc := anomaly.Config{Warmup: 2, SigmaFloor: 5, K: .5, H: 5, MaxPaths: 1, MaxWindows: 64, MaxPending: 8, AlertmanagerURL: sink.URL, Timeout: time.Second, RetryMin: time.Second, RetryMax: time.Minute}
	job, e := anomaly.New(st, jc)
	if e != nil {
		t.Fatal(e)
	}
	if e = job.Tick(context.Background(), base.Add(25*time.Minute)); e != nil {
		t.Fatal(e)
	}
	if got := sql(t, cfg.Database, "SELECT count(),any(path_changed),any(correlation_reason) FROM path_events FINAL"); got != "1\tfalse\tmultiple or unknown destination addresses" {
		t.Fatal(got)
	}
	// Add an old row behind the cursor: the documented late-data policy skips it.
	r = batch("late", 1)
	r.Results[0].StartedAt = timestamppb.New(base.Add(time.Minute))
	r.Results[0].PhasesNanoseconds["connect"] = 1000e6
	if _, e = st.Commit(context.Background(), r); e != nil {
		t.Fatal(e)
	}
	if e = job.Tick(context.Background(), base.Add(25*time.Minute)); e != nil {
		t.Fatal(e)
	}
	if n := sql(t, cfg.Database, "SELECT count() FROM path_events FINAL"); n != "1" {
		t.Fatal("late/open data replayed", n)
	}
	r = batch("newpath", 1)
	r.Results[0].StartedAt = timestamppb.New(base.Add(25 * time.Minute))
	r.Results[0].TargetId = "other"
	if _, e = st.Commit(context.Background(), r); e != nil {
		t.Fatal(e)
	}
	if e = job.Tick(context.Background(), base.Add(30*time.Minute)); e == nil || !strings.Contains(e.Error(), "path capacity") {
		t.Fatalf("capacity error=%v", e)
	}
	if e = job.Tick(context.Background(), base.Add(30*time.Minute)); e == nil {
		t.Fatal("capacity silently evicted")
	}
}

func TestAnomaly_StateFailureAndCorruption(t *testing.T) {
	cfg, st, _ := fixture(t)
	base := now.Add(-time.Hour).Truncate(5 * time.Minute)
	r := batch("state", 1)
	r.Results[0].StartedAt = timestamppb.New(base)
	if _, e := st.Commit(context.Background(), r); e != nil {
		t.Fatal(e)
	}
	sink := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(200) }))
	defer sink.Close()
	jc := anomaly.Config{Warmup: 2, SigmaFloor: 5, K: .5, H: 5, MaxPaths: 8, MaxWindows: 64, MaxPending: 8, AlertmanagerURL: sink.URL, Timeout: time.Second, RetryMin: time.Second, RetryMax: time.Minute}
	job, e := anomaly.New(st, jc)
	if e != nil {
		t.Fatal(e)
	}
	blocked := filepath.Join(cfg.JournalDir, "anomaly", "state.json")
	if e = os.MkdirAll(blocked, 0700); e != nil {
		t.Fatal(e)
	}
	if e = job.Tick(context.Background(), base.Add(5*time.Minute)); e == nil {
		t.Fatal("persistence failure hidden")
	}
	if e = os.Remove(blocked); e != nil {
		t.Fatal(e)
	}
	if e = job.Tick(context.Background(), base.Add(5*time.Minute)); e == nil {
		t.Fatal("uncertain in-memory state reused")
	}
	job, e = anomaly.New(st, jc)
	if e != nil {
		t.Fatal(e)
	}
	if e = job.Tick(context.Background(), base.Add(5*time.Minute)); e != nil {
		t.Fatal(e)
	}
	altered := jc
	altered.H = 12
	if _, e = anomaly.New(st, altered); e == nil {
		t.Fatal("configuration change silently reused detector state")
	}
	// A valid JSON object with a broken cursor must not silently replay history.
	data, e := st.LoadAnomalyState(context.Background())
	if e != nil {
		t.Fatal(e)
	}
	var state map[string]any
	if e = json.Unmarshal(data, &state); e != nil {
		t.Fatal(e)
	}
	state["Cursor"] = "0001-01-01T00:00:00Z"
	data, _ = json.Marshal(state)
	if e = st.SaveAnomalyState(context.Background(), data); e != nil {
		t.Fatal(e)
	}
	if _, e = anomaly.New(st, jc); e == nil {
		t.Fatal("corrupt cursor accepted")
	}
	if e = os.WriteFile(blocked, nil, 0600); e != nil {
		t.Fatal(e)
	}
	if _, e = anomaly.New(st, jc); e == nil {
		t.Fatal("truncated empty state accepted as new job")
	}
}

func TestAnomaly_AlertWaitDoesNotBlockIngest(t *testing.T) {
	_, st, _ := fixture(t)
	base := now.Add(-time.Hour).Truncate(5 * time.Minute)
	for i, ms := range []int64{50, 50, 80} {
		r := batch(fmt.Sprint("outsidegate", i), 1)
		r.Results[0].StartedAt = timestamppb.New(base.Add(time.Duration(i) * 5 * time.Minute))
		r.Results[0].PhasesNanoseconds["connect"] = ms * 1e6
		if _, e := st.Commit(context.Background(), r); e != nil {
			t.Fatal(e)
		}
	}
	gate := newRequestGate()
	sink := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { gate.wait(); w.WriteHeader(200) }))
	defer gate.closeProxy(sink)
	jc := anomaly.Config{Warmup: 2, SigmaFloor: 5, K: .5, H: 5, MaxPaths: 8, MaxWindows: 64, MaxPending: 8, AlertmanagerURL: sink.URL, Timeout: 100 * time.Millisecond, RetryMin: time.Second, RetryMax: time.Minute}
	job, e := anomaly.New(st, jc)
	if e != nil {
		t.Fatal(e)
	}
	done := make(chan error, 1)
	go func() { done <- job.Tick(context.Background(), base.Add(15*time.Minute)) }()
	select {
	case <-gate.reached:
	case <-time.After(3 * time.Second):
		t.Fatal("local alert did not reach gate")
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	if _, e = st.Commit(ctx, batch("while-notifying", 1)); e != nil {
		t.Fatalf("Alertmanager wait blocked raw ingest: %v", e)
	}
	select {
	case e = <-done:
		if e == nil {
			t.Fatal("local alert timeout swallowed")
		}
	case <-time.After(2 * time.Second):
		t.Fatal("alert request exceeded deadline")
	}
	gate.release()
	if e = job.Tick(context.Background(), base.Add(15*time.Minute+2*time.Second)); e != nil {
		t.Fatal("timeout lost pending retry", e)
	}
}

func TestAnomaly_PendingCapacityRetainsCursorAndEvents(t *testing.T) {
	cfg, st, _ := fixture(t)
	base := now.Add(-time.Hour).Truncate(5 * time.Minute)
	for i, ms := range []int64{50, 50, 80, 80} {
		r := batch(fmt.Sprint("pendingcap", i), 1)
		r.Results[0].StartedAt = timestamppb.New(base.Add(time.Duration(i) * 5 * time.Minute))
		r.Results[0].PhasesNanoseconds["connect"] = ms * 1e6
		if _, e := st.Commit(context.Background(), r); e != nil {
			t.Fatal(e)
		}
	}
	var calls atomic.Int32
	sink := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { calls.Add(1); w.WriteHeader(200) }))
	defer sink.Close()
	jc := anomaly.Config{Warmup: 2, SigmaFloor: 5, K: .5, H: 5, MaxPaths: 8, MaxWindows: 64, MaxPending: 1, AlertmanagerURL: sink.URL, Timeout: time.Second, RetryMin: time.Second, RetryMax: time.Minute}
	job, e := anomaly.New(st, jc)
	if e != nil {
		t.Fatal(e)
	}
	tick := base.Add(20 * time.Minute)
	if e = job.Tick(context.Background(), tick); e == nil || !strings.Contains(e.Error(), "pending capacity") {
		t.Fatalf("capacity error=%v", e)
	}
	job, e = anomaly.New(st, jc)
	if e != nil {
		t.Fatal(e)
	}
	if e = job.Tick(context.Background(), tick); e != nil {
		t.Fatal(e)
	}
	if got := sql(t, cfg.Database, "SELECT count() FROM path_events FINAL"); got != "2" {
		t.Fatal("event lost at pending capacity", got)
	}
	if calls.Load() != 2 {
		t.Fatal(calls.Load())
	}
}

func TestAnomaly_RestartPreservesPartialCUSUM(t *testing.T) {
	cfg, st, _ := fixture(t)
	base := now.Add(-time.Hour).Truncate(5 * time.Minute)
	put := func(i int, ms int64) {
		t.Helper()
		r := batch(fmt.Sprint("partialsum", i), 1)
		r.Results[0].StartedAt = timestamppb.New(base.Add(time.Duration(i) * 5 * time.Minute))
		r.Results[0].PhasesNanoseconds["connect"] = ms * 1e6
		if _, e := st.Commit(context.Background(), r); e != nil {
			t.Fatal(e)
		}
	}
	put(0, 50)
	put(1, 50)
	put(2, 60)
	sink := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(200) }))
	defer sink.Close()
	jc := anomaly.Config{Warmup: 2, SigmaFloor: 5, K: .5, H: 5, MaxPaths: 8, MaxWindows: 64, MaxPending: 8, AlertmanagerURL: sink.URL, Timeout: time.Second, RetryMin: time.Second, RetryMax: time.Minute}
	job, e := anomaly.New(st, jc)
	if e != nil {
		t.Fatal(e)
	}
	if e = job.Tick(context.Background(), base.Add(15*time.Minute)); e != nil {
		t.Fatal(e)
	}
	if n := sql(t, cfg.Database, "SELECT count() FROM path_events FINAL"); n != "0" {
		t.Fatal(n)
	}
	st.Close()
	st, e = store.Open(context.Background(), cfg)
	if e != nil {
		t.Fatal(e)
	}
	defer st.Close()
	put(3, 60)
	put(4, 60)
	put(5, 60)
	job, e = anomaly.New(st, jc)
	if e != nil {
		t.Fatal(e)
	}
	if e = job.Tick(context.Background(), base.Add(30*time.Minute)); e != nil {
		t.Fatal(e)
	}
	if n := sql(t, cfg.Database, "SELECT count() FROM path_events FINAL"); n != "1" {
		t.Fatal("partial CUSUM lost across restart", n)
	}
}

// Catches accepting a valid cursor/pending-state file copied from another owner.
func TestAnomaly_StateRejectsOtherOwnerAndSurvivesCHRestart(t *testing.T) {
	cfgA, a, _ := fixture(t)
	_, b, _ := fixture(t)
	base := now.Add(-time.Hour).Truncate(5 * time.Minute)
	r := batch("owner-state", 1)
	r.Results[0].StartedAt = timestamppb.New(base)
	if _, e := a.Commit(context.Background(), r); e != nil {
		t.Fatal(e)
	}
	sink := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(200) }))
	defer sink.Close()
	jc := anomaly.Config{Warmup: 2, SigmaFloor: 5, K: .5, H: 5, MaxPaths: 8, MaxWindows: 64, MaxPending: 8, AlertmanagerURL: sink.URL, Timeout: time.Second, RetryMin: time.Second, RetryMax: time.Minute}
	job, e := anomaly.New(a, jc)
	if e != nil {
		t.Fatal(e)
	}
	if e = job.Tick(context.Background(), base.Add(5*time.Minute)); e != nil {
		t.Fatal(e)
	}
	data, e := a.LoadAnomalyState(context.Background())
	if e != nil {
		t.Fatal(e)
	}
	if e = b.SaveAnomalyState(context.Background(), data); e != nil {
		t.Fatal(e)
	}
	if _, e = anomaly.New(b, jc); e == nil || !strings.Contains(e.Error(), "owner") {
		t.Fatalf("other owner state accepted: %v", e)
	}
	a.Close()
	b.Close()
	restart(t)
	a, e = store.Open(context.Background(), cfgA)
	if e != nil {
		t.Fatal(e)
	}
	defer a.Close()
	job, e = anomaly.New(a, jc)
	if e != nil {
		t.Fatal("same persistent owner rejected after CH epoch changed", e)
	}
	if e = job.Tick(context.Background(), base.Add(5*time.Minute)); e != nil {
		t.Fatal(e)
	}
}
