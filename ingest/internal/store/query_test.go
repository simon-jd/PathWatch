package store

import (
	"context"
	"encoding/json"
	"errors"
	"google.golang.org/protobuf/types/known/timestamppb"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	pb "pathwatch.local/pathwatch/proto/pathwatch/v1"
	"strings"
	"testing"
	"time"
)

func bounds() QueryBounds {
	return QueryBounds{"p→t/tcp/v4", time.Unix(1800000000, 0).UTC(), time.Unix(1800000300, 0).UTC(), 100}
}
func TestQueryBounds(t *testing.T) {
	b := bounds()
	if e := b.Validate(false, time.Now()); e != nil {
		t.Fatal(e)
	}
	for _, mutate := range []func(*QueryBounds){func(b *QueryBounds) { b.Limit = 257 }, func(b *QueryBounds) { b.To = b.From }, func(b *QueryBounds) { b.PathKey = "a\x00b" }, func(b *QueryBounds) { b.To = b.From.Add(24*time.Hour + time.Nanosecond) }} {
		c := b
		mutate(&c)
		if c.Validate(false, time.Now()) == nil {
			t.Fatalf("accepted %+v", c)
		}
	}
}
func TestQueryEscapesAndParameters(t *testing.T) {
	if got := escapeParameter("a\t\n\r\\n'&+%\x00\b\f"); got != "a\\t\\n\\r\\\\n'&+%\\0\\b\\f" {
		t.Fatalf("escape %q", got)
	}
	s, close := queryStore(t, func(w http.ResponseWriter, r *http.Request) {
		if !strings.Contains(r.URL.Query().Get("param_path"), `\t`) {
			t.Error("TSV escape missing")
		}
		if r.URL.Query().Get("max_rows_to_read") != "1000000" || r.URL.Query().Get("max_bytes_to_read") != "134217728" {
			t.Error("scan limits missing")
		}
		w.Write([]byte(""))
	})
	defer close()
	b := bounds()
	b.PathKey = "literal\t' OR 1=1 --"
	if _, e := s.ReadRaw(context.Background(), b); e != nil {
		t.Fatal(e)
	}
}
func queryStore(t *testing.T, read http.HandlerFunc) (*Store, func()) {
	t.Helper()
	h := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Query().Get("param_path") == "" {
			w.Write([]byte("epoch"))
			return
		}
		read(w, r)
	}))
	s := &Store{cfg: Config{URL: h.URL, Database: "test"}, gate: make(chan struct{}, 1), client: h.Client(), epoch: "epoch", session: "owned", records: map[string]record{}}
	s.gate <- struct{}{}
	return s, h.Close
}
func TestQueryRejectsUncertainAndCanceled(t *testing.T) {
	s, close := queryStore(t, func(w http.ResponseWriter, r *http.Request) { t.Error("query must not run") })
	defer close()
	s.poison = errors.New("uncertain")
	if _, e := s.ReadRaw(context.Background(), bounds()); !errors.Is(e, ErrQueryUnavailable) {
		t.Fatal(e)
	}
	s.poison = nil
	s.records["pending"] = record{}
	if _, e := s.ReadRaw(context.Background(), bounds()); !errors.Is(e, ErrQueryUnavailable) {
		t.Fatal(e)
	}
	delete(s.records, "pending")
	<-s.gate
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, e := s.ReadRaw(ctx, bounds()); !errors.Is(e, context.Canceled) {
		t.Fatal(e)
	}
	s.gate <- struct{}{}
}

func TestQueryResponseBoundsAndShape(t *testing.T) {
	for _, size := range []int{QueryMaxBytes - 1, QueryMaxBytes, QueryMaxBytes + 1} {
		s, close := queryStore(t, func(w http.ResponseWriter, r *http.Request) { w.Write([]byte(strings.Repeat(" ", size))) })
		_, e := s.ReadRaw(context.Background(), bounds())
		close()
		if (size > QueryMaxBytes) != errors.Is(e, ErrQueryLimit) {
			t.Fatalf("size=%d err=%v", size, e)
		}
	}
	for _, body := range []string{`{"batch_key":"missing"}`, `{"broken":`, "[]", "null", "{}\nCode: 159 trailing exception"} {
		s, close := queryStore(t, func(w http.ResponseWriter, r *http.Request) { w.Write([]byte(body)) })
		_, e := s.ReadRaw(context.Background(), bounds())
		close()
		if !errors.Is(e, ErrQueryResponse) {
			t.Fatalf("%q: %v", body, e)
		}
	}
}
func TestQueryWaitGateAndPostEpoch(t *testing.T) {
	calls := 0
	h := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Query().Get("param_path") == "" {
			calls++
			if calls > 1 {
				w.Write([]byte("new-epoch"))
			} else {
				w.Write([]byte("epoch"))
			}
		}
	}))
	defer h.Close()
	s := &Store{cfg: Config{URL: h.URL, Database: "test"}, gate: make(chan struct{}, 1), client: h.Client(), epoch: "epoch", session: "owned", records: map[string]record{}}
	done := make(chan error, 1)
	go func() { _, e := s.ReadRaw(context.Background(), bounds()); done <- e }()
	select {
	case e := <-done:
		t.Fatalf("did not wait: %v", e)
	case <-time.After(20 * time.Millisecond):
	}
	s.gate <- struct{}{}
	if e := <-done; !errors.Is(e, ErrQueryUnavailable) {
		t.Fatalf("post epoch: %v", e)
	}
	if s.poison != nil {
		t.Fatal("read altered poison")
	}
	s.closed = true
	if _, e := s.ReadRaw(context.Background(), bounds()); !errors.Is(e, ErrQueryUnavailable) {
		t.Fatal(e)
	}
}
func TestQueryTimeoutReleasesGate(t *testing.T) {
	s, close := queryStore(t, func(w http.ResponseWriter, r *http.Request) { io.Copy(io.Discard, r.Body); <-r.Context().Done() })
	defer close()
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
	defer cancel()
	_, e := s.ReadRaw(ctx, bounds())
	if !errors.Is(e, context.DeadlineExceeded) {
		t.Fatal(e)
	}
	select {
	case <-s.gate:
		s.gate <- struct{}{}
	default:
		t.Fatal("gate retained")
	}
}

// TestQueryEngine is opt-in and only accepts a fixture-owned, labelled CH.
// Direct Commit/event data here are synthetic engine fixtures, not real probe traffic.
func TestQueryEngine(t *testing.T) {
	endpoint := os.Getenv("PATHWATCH_QUERY_TEST_URL")
	if endpoint == "" {
		t.Skip("owned engine fixture not requested")
	}
	container := os.Getenv("PATHWATCH_QUERY_TEST_CONTAINER")
	label, e := exec.Command("docker", "inspect", "--format", `{{index .Config.Labels "purpose"}}`, container).Output()
	if e != nil || strings.TrimSpace(string(label)) != "task14-query-api" {
		t.Fatal("requires exact owned fixture container")
	}
	client := &http.Client{Transport: &http.Transport{Proxy: nil}, Timeout: 5 * time.Second}
	admin := func(sql string) {
		t.Helper()
		r, e := client.Post(endpoint, "text/plain", strings.NewReader(sql))
		if e != nil {
			t.Fatal(e)
		}
		defer r.Body.Close()
		b, _ := io.ReadAll(r.Body)
		if r.StatusCode != 200 {
			t.Fatalf("engine admin %s", b)
		}
	}
	admin("CREATE DATABASE querytest ENGINE=Atomic")
	cfg := Config{URL: endpoint, Database: "querytest", Username: "default", JournalDir: filepath.Join(os.Getenv("PATHWATCH_QUERY_TEST_WORK"), "query-journal"), MaxJournalBytes: 1 << 20}
	s, e := Open(context.Background(), cfg)
	if e != nil {
		t.Fatal(e)
	}
	defer func() { s.Close() }()
	s.client.Transport = queryAudit{next: s.client.Transport, t: t}
	at := time.Now().UTC().Truncate(5 * time.Minute).Add(-10 * time.Minute)
	b := QueryBounds{"p→t/tcp/v4", at, at.Add(5 * time.Minute), 100}
	makeBatch := func(id string, ms int64) *pb.ReportRequest {
		return &pb.ReportRequest{ProbeId: "p", BatchId: id, SentAt: timestamppb.Now(), Results: []*pb.ProbeResult{{ProbeId: "p", TargetId: "t", Kind: "tcp", Family: "v4", Addr: "127.0.0.1", StartedAt: timestamppb.New(at.Add(time.Nanosecond)), PhasesNanoseconds: map[string]int64{"connect": ms * 1000000}, Err: "literal\t\n\\n"}}}
	}
	for i, id := range []string{"one", "two"} {
		if _, e = s.Commit(context.Background(), makeBatch(id, int64(10+20*i))); e != nil {
			t.Fatal(e)
		}
	}
	missing := makeBatch("missing-primary", 0)
	missing.Results[0].PhasesNanoseconds = nil
	if _, e = s.Commit(context.Background(), missing); e != nil {
		t.Fatal(e)
	}
	raw, e := s.ReadRaw(context.Background(), b)
	if e != nil || len(raw) != 3 || raw[0].StartedAt != at.Add(time.Nanosecond).Format(time.RFC3339Nano) || raw[0].Err != "literal\t\n\\n" {
		t.Fatalf("raw=%+v e=%v", raw, e)
	}
	foundMissing := false
	for _, r := range raw {
		if r.BatchID == "missing-primary" {
			foundMissing = true
			if r.PhasePresent || r.PhaseMS != nil || len(r.Phases) != 0 {
				t.Fatal("missing primary was fabricated", r)
			}
		}
	}
	if !foundMissing {
		t.Fatal("missing primary raw row missing")
	}
	q, e := s.ReadQuantiles(context.Background(), b)
	if e != nil || len(q) != 1 || q[0].Samples != "2" || q[0].P95 < 10 || q[0].P95 > 30 {
		t.Fatalf("quantile=%+v e=%v", q, e)
	}
	ev := AnomalyEvent{ID: strings.Repeat("a", 64), PathKey: b.PathKey, Window: at, P95: 30, Mean: 10, Sigma: 1, K: .5, H: 5, CorrelationReason: "explicit synthetic engine fixture"}
	for i := 0; i < 2; i++ {
		if e = s.WriteAnomalyEvent(context.Background(), ev); e != nil {
			t.Fatal(e)
		}
	}
	events, e := s.ReadEvents(context.Background(), b)
	if e != nil || len(events) != 1 || events[0].ID != ev.ID {
		t.Fatalf("events=%+v e=%v", events, e)
	}
	short := b
	short.Limit = 1
	if _, e = s.ReadRaw(context.Background(), short); !errors.Is(e, ErrQueryLimit) {
		t.Fatal("row bound", e)
	}
	for _, value := range []string{"\t", "\n", "\r", "\\n", "\\t", "\\", "' OR 1=1 --", "&+%", "路径→x", "\b\f", "\\N", "\x00"} {
		p := b
		p.PathKey = value
		body, e := s.measurementRequest(context.Background(), "SELECT {path:String} AS value FORMAT JSONEachRow", p)
		var result struct {
			Value string `json:"value"`
		}
		if e != nil || json.Unmarshal(body, &result) != nil || result.Value != value {
			t.Fatalf("parameter input=%q got=%q body=%q e=%v", value, result.Value, body, e)
		}
	}
	for _, sql := range []string{"SELECT sum(number) FROM numbers(1000001) FORMAT JSONEachRow", "SELECT repeat('xx',600000) AS value FORMAT JSONEachRow", "SELECT number FROM numbers(258) FORMAT JSONEachRow"} {
		if _, e := s.measurementRequest(context.Background(), sql, b); !errors.Is(e, ErrQueryLimit) {
			t.Fatalf("actual engine limit %q: %v", sql, e)
		}
	}
	// A genuine engine INSERT succeeds, but its acknowledgement is lost locally.
	transport := s.client.Transport
	s.client.Transport = lostQueryAck{next: transport}
	if _, e = s.Commit(context.Background(), makeBatch("uncertain", 50)); e == nil {
		t.Fatal("expected uncertain write")
	}
	if _, e = s.ReadRaw(context.Background(), b); !errors.Is(e, ErrQueryUnavailable) {
		t.Fatal("uncertain read", e)
	}
	s.Close()
	s.client.Transport = transport
	if bad, e := Open(context.Background(), cfg); e == nil {
		bad.Close()
		t.Fatal("pending same epoch reopened")
	}
	if out, e := exec.Command("docker", "restart", "--timeout", "10", container).CombinedOutput(); e != nil {
		t.Fatalf("owned restart: %s %v", out, e)
	}
	deadline := time.Now().Add(20 * time.Second)
	for {
		r, e := client.Get(endpoint + "/ping")
		if e == nil {
			r.Body.Close()
			if r.StatusCode == 200 {
				break
			}
		}
		if time.Now().After(deadline) {
			t.Fatal("restart timeout")
		}
		time.Sleep(100 * time.Millisecond)
	}
	s, e = Open(context.Background(), cfg)
	if e != nil {
		t.Fatal(e)
	}
	if raw, e = s.ReadRaw(context.Background(), b); e != nil || len(raw) != 4 {
		t.Fatalf("recovered rows=%d err=%v", len(raw), e)
	}
	if dup, e := s.Commit(context.Background(), makeBatch("uncertain", 50)); e != nil || !dup {
		t.Fatalf("dedupe %v %v", dup, e)
	}
	t.Log("real engine raw, merged quantiles, synthetic event FINAL, typed parameters, uncertain write, same-epoch refusal and actual restart/recovery verified")
}

type lostQueryAck struct{ next http.RoundTripper }

func (l lostQueryAck) RoundTrip(r *http.Request) (*http.Response, error) {
	b, e := io.ReadAll(r.Body)
	if e != nil {
		return nil, e
	}
	r.Body = io.NopCloser(strings.NewReader(string(b)))
	resp, e := l.next.RoundTrip(r)
	if e == nil && strings.HasPrefix(string(b), "INSERT INTO probe_results ") {
		io.Copy(io.Discard, resp.Body)
		resp.Body.Close()
		return nil, errors.New("owned test lost insert acknowledgement")
	}
	return resp, e
}

type queryAudit struct {
	next http.RoundTripper
	t    *testing.T
}

func (a queryAudit) RoundTrip(r *http.Request) (*http.Response, error) {
	resp, e := a.next.RoundTrip(r)
	if e == nil && resp.StatusCode != 200 {
		b, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		resp.Body.Close()
		a.t.Logf("owned CH error status=%d code=%s body=%s", resp.StatusCode, resp.Header.Get("X-ClickHouse-Exception-Code"), b)
		resp.Body = io.NopCloser(strings.NewReader(string(b)))
	}
	return resp, e
}
