package corerpc

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
	"pathwatch.local/pathwatch/ingest/internal/queryapi"
	"pathwatch.local/pathwatch/ingest/internal/store"
	cp "pathwatch.local/pathwatch/proto/pathwatch/core/v1"
)

func validRawRow() *cp.RawRow {
	return &cp.RawRow{BatchKey: "key", ProbeId: "p", BatchId: "b", TargetId: "t", Kind: "dns", PathKey: "p→t/dns/", StartedAt: timestamppb.New(time.Unix(300, 0)), Err: "remote failure"}
}

// An independent DTO projection deliberately bypasses conversion validation, so
// each malformed field is tested at both boundaries rather than prefiltered.
func rawDTO(r *cp.RawRow) store.RawResult {
	v := store.RawResult{BatchKey: r.BatchKey, ResultIndex: r.ResultIndex, ProbeID: r.ProbeId, BatchID: r.BatchId, TargetID: r.TargetId, Kind: r.Kind, PathKey: r.PathKey, StartedAt: r.StartedAt.AsTime().UTC().Format(time.RFC3339Nano), Family: r.Family, Addr: r.Addr, Err: r.Err, Protocol: r.Protocol, TLSVersion: r.TlsVersion, TLSResumed: r.TlsResumed, Phases: map[string]string{}, HopTTLs: r.HopTtls, HopAddrs: r.HopAddrs, PhaseMS: r.PhaseMs, PhasePresent: r.PhaseMs != nil}
	for k, n := range r.Phases {
		v.Phases[k] = strconv.FormatInt(n, 10)
	}
	for _, n := range r.HopRtts {
		v.HopRTTs = append(v.HopRTTs, strconv.FormatInt(n, 10))
	}
	return v
}
func quantileDTO(r *cp.QuantileRow) store.Quantile {
	return store.Quantile{PathKey: r.PathKey, WindowStart: r.WindowStart.AsTime().UTC().Format(time.RFC3339Nano), P50: r.P50, P95: r.P95, P99: r.P99, Samples: strconv.FormatUint(r.Samples, 10)}
}
func eventDTO(r *cp.EventRow) store.Event {
	return store.Event{ID: r.EventId, PathKey: r.PathKey, WindowStart: r.WindowStart.AsTime().UTC().Format(time.RFC3339Nano), P95: r.P95, Baseline: r.Baseline, Sigma: r.Sigma, K: r.K, H: r.H, PathChanged: r.PathChanged, FirstDivergentTTL: uint16(r.FirstDivergentTtl), CorrelationReason: r.CorrelationReason}
}

func TestRequiredRowSemanticsAtBothBoundaries(t *testing.T) {
	for _, tc := range []struct {
		name   string
		change func(*cp.RawRow)
	}{
		{"kind-empty", func(r *cp.RawRow) { r.Kind = "" }}, {"kind-invalid", func(r *cp.RawRow) { r.Kind = "other" }},
		{"batch-key-empty", func(r *cp.RawRow) { r.BatchKey = "" }}, {"batch-key-nul", func(r *cp.RawRow) { r.BatchKey = "x\x00" }}, {"batch-key-utf8", func(r *cp.RawRow) { r.BatchKey = string([]byte{255}) }},
		{"probe-empty", func(r *cp.RawRow) { r.ProbeId = "" }}, {"probe-invalid", func(r *cp.RawRow) { r.ProbeId = "!" }},
		{"batch-empty", func(r *cp.RawRow) { r.BatchId = "" }}, {"batch-invalid", func(r *cp.RawRow) { r.BatchId = "!" }},
		{"target-empty", func(r *cp.RawRow) { r.TargetId = "" }}, {"target-invalid", func(r *cp.RawRow) { r.TargetId = "!" }},
		{"result-index", func(r *cp.RawRow) { r.ResultIndex = 10000 }},
		{"missing-address-without-error", func(r *cp.RawRow) { r.Err = "" }}, {"family", func(r *cp.RawRow) { r.Family = "other" }},
		{"address", func(r *cp.RawRow) { r.Addr = "bad" }}, {"address-family", func(r *cp.RawRow) { r.Addr = "127.0.0.1"; r.Family = "v6" }},
		{"protocol", func(r *cp.RawRow) { r.Protocol = "h4" }}, {"error-utf8", func(r *cp.RawRow) { r.Err = string([]byte{255}) }},
		{"phase-name", func(r *cp.RawRow) { r.Phases = map[string]int64{"!": 0} }}, {"phase-negative", func(r *cp.RawRow) { r.Phases = map[string]int64{"resolve": -1} }},
		{"hop-address", func(r *cp.RawRow) { r.HopTtls = []uint32{1}; r.HopAddrs = []string{"bad"}; r.HopRtts = []int64{0} }},
		{"hop-no-address", func(r *cp.RawRow) { r.HopTtls = []uint32{1}; r.HopAddrs = []string{""}; r.HopRtts = []int64{1} }},
		{"tls-resumed", func(r *cp.RawRow) { r.TlsResumed = true }}, {"phase-ms-negative", func(r *cp.RawRow) { r.PhaseMs = proto.Float64(-1) }},
	} {
		t.Run("raw/"+tc.name, func(t *testing.T) {
			r := validRawRow()
			tc.change(r)
			if _, e := rawFromWire(r); e != store.ErrQueryResponse {
				t.Errorf("from: %v", e)
			}
			if _, e := rawToWire(rawDTO(r)); e != store.ErrQueryResponse {
				t.Errorf("to: %v", e)
			}
		})
	}
	for _, key := range []string{"", strings.Repeat("x", 513), "nul\x00key", string([]byte{255})} {
		t.Run(fmt.Sprintf("all-paths/%q", key), func(t *testing.T) {
			r := validRawRow()
			r.PathKey = key
			q := &cp.QuantileRow{PathKey: key, WindowStart: r.StartedAt, Samples: 1}
			e := &cp.EventRow{EventId: "event", PathKey: key, WindowStart: r.StartedAt}
			if _, err := rawFromWire(r); err != store.ErrQueryResponse {
				t.Errorf("raw from %v", err)
			}
			if _, err := rawToWire(rawDTO(r)); err != store.ErrQueryResponse {
				t.Errorf("raw to %v", err)
			}
			if _, err := quantileFromWire(q); err != store.ErrQueryResponse {
				t.Errorf("quantile from %v", err)
			}
			if _, err := quantileToWire(quantileDTO(q)); err != store.ErrQueryResponse {
				t.Errorf("quantile to %v", err)
			}
			if _, err := eventFromWire(e); err != store.ErrQueryResponse {
				t.Errorf("event from %v", err)
			}
			if _, err := eventToWire(eventDTO(e)); err != store.ErrQueryResponse {
				t.Errorf("event to %v", err)
			}
		})
	}
	for _, tc := range []struct {
		name   string
		change func(*cp.QuantileRow)
	}{
		{"samples", func(r *cp.QuantileRow) { r.Samples = 0 }}, {"negative", func(r *cp.QuantileRow) { r.P50 = -1 }}, {"unordered", func(r *cp.QuantileRow) { r.P50 = 2; r.P95 = 1 }}, {"nonfinite", func(r *cp.QuantileRow) { r.P99 = math.Inf(1) }},
	} {
		t.Run("quantile/"+tc.name, func(t *testing.T) {
			r := &cp.QuantileRow{PathKey: "path", WindowStart: timestamppb.New(time.Unix(300, 0)), Samples: 1}
			tc.change(r)
			if _, e := quantileFromWire(r); e != store.ErrQueryResponse {
				t.Errorf("from %v", e)
			}
			if _, e := quantileToWire(quantileDTO(r)); e != store.ErrQueryResponse {
				t.Errorf("to %v", e)
			}
		})
	}
	for _, tc := range []struct {
		name   string
		change func(*cp.EventRow)
	}{
		{"id-empty", func(r *cp.EventRow) { r.EventId = "" }}, {"id-nul", func(r *cp.EventRow) { r.EventId = "a\x00" }}, {"id-utf8", func(r *cp.EventRow) { r.EventId = string([]byte{255}) }}, {"reason-utf8", func(r *cp.EventRow) { r.CorrelationReason = string([]byte{255}) }},
		{"p95-negative", func(r *cp.EventRow) { r.P95 = -1 }}, {"baseline-negative", func(r *cp.EventRow) { r.Baseline = -1 }}, {"sigma-negative", func(r *cp.EventRow) { r.Sigma = -1 }}, {"k-negative", func(r *cp.EventRow) { r.K = -1 }}, {"h-negative", func(r *cp.EventRow) { r.H = -1 }}, {"nonfinite", func(r *cp.EventRow) { r.P95 = math.NaN() }},
	} {
		t.Run("event/"+tc.name, func(t *testing.T) {
			r := &cp.EventRow{EventId: "event", PathKey: "path", WindowStart: timestamppb.New(time.Unix(300, 0))}
			tc.change(r)
			if _, e := eventFromWire(r); e != store.ErrQueryResponse {
				t.Errorf("from %v", e)
			}
			if _, e := eventToWire(eventDTO(r)); e != store.ErrQueryResponse {
				t.Errorf("to %v", e)
			}
		})
	}
}

func TestLegitimateZeroAndOptionalRowSemantics(t *testing.T) {
	for _, kind := range []string{"dns", "tcp", "tls", "http1", "http2", "http3", "trace"} {
		r := validRawRow()
		r.Kind = kind
		r.PathKey = "unicode→/'\"\\ odd path"
		r.HopTtls = []uint32{1}
		r.HopAddrs = []string{""}
		r.HopRtts = []int64{0}
		for _, phase := range []*float64{nil, proto.Float64(0)} {
			r.PhaseMs = phase
			v, e := rawFromWire(r)
			if e != nil || v.PhasePresent != (phase != nil) {
				t.Fatal(v, e)
			}
			again, e := rawToWire(v)
			if e != nil || !proto.Equal(again, r) {
				t.Fatal(again, e)
			}
		}
	}
	q := &cp.QuantileRow{PathKey: " ", WindowStart: timestamppb.New(time.Unix(0, 0)), Samples: 1}
	v, e := quantileFromWire(q)
	if e != nil {
		t.Fatal(e)
	}
	again, e := quantileToWire(v)
	if e != nil || !proto.Equal(again, q) {
		t.Fatal(again, e)
	}
	r := &cp.EventRow{EventId: "event", PathKey: "odd/'\"path", WindowStart: timestamppb.New(time.Unix(0, 0))}
	event, e := eventFromWire(r)
	if e != nil {
		t.Fatal(e)
	}
	eventAgain, e := eventToWire(event)
	if e != nil || !proto.Equal(eventAgain, r) {
		t.Fatal(eventAgain, e)
	}
}

type rowReplyFixture struct {
	*Service
	mu       sync.Mutex
	raw      *cp.RawRow
	quantile *cp.QuantileRow
	event    *cp.EventRow
	calls    int
}

func (f *rowReplyFixture) ReadRaw(context.Context, *cp.QueryRequest) (*cp.RawResponse, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls++
	return &cp.RawResponse{Rows: []*cp.RawRow{proto.Clone(f.raw).(*cp.RawRow)}}, nil
}
func (f *rowReplyFixture) ReadQuantiles(context.Context, *cp.QueryRequest) (*cp.QuantilesResponse, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls++
	return &cp.QuantilesResponse{Rows: []*cp.QuantileRow{proto.Clone(f.quantile).(*cp.QuantileRow)}}, nil
}
func (f *rowReplyFixture) ReadEvents(context.Context, *cp.QueryRequest) (*cp.EventsResponse, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls++
	return &cp.EventsResponse{Rows: []*cp.EventRow{proto.Clone(f.event).(*cp.EventRow)}}, nil
}
func startTypedFixture(t *testing.T, p *testPKI, tr *Trust, service cp.CoreServer, observed ...credentials.TransportCredentials) string {
	t.Helper()
	a := NewAdmission(true, tr.Authenticate, nil, nil)
	cred := tr.Credentials()
	if len(observed) > 0 {
		cred = observed[0]
	}
	g := grpc.NewServer(a.Options(cred)...)
	Register(g, &cp.Core_ServiceDesc, service)
	l, e := net.Listen("tcp", "127.0.0.1:0")
	if e != nil {
		t.Fatal(e)
	}
	done := make(chan struct{})
	go func() { defer close(done); g.Serve(LimitListener(l, 10)) }()
	t.Cleanup(func() { a.Stop(); g.Stop(); <-done; a.Wait() })
	return l.Addr().String()
}
func TestMalformedSuccessTypedRPCBecomesHTTP502(t *testing.T) {
	p := pki(t)
	tr, e := LoadTrust(p.cert(t, "core.test", true, time.Now().Add(time.Hour)), true)
	if e != nil {
		t.Fatal(e)
	}
	f := &rowReplyFixture{Service: &Service{Ready: func(context.Context) error { return nil }}}
	target := startTypedFixture(t, p, tr, f)
	c := connect(t, target, p)
	h := httptest.NewServer(queryapi.New(c))
	defer h.Close()
	b := store.QueryBounds{PathKey: "path", From: time.Unix(300, 0), To: time.Unix(600, 0), Limit: 1}
	for _, tc := range []struct {
		name, route string
		change      func()
	}{
		{"raw-batch-key", "raw", func() { f.raw.BatchKey = "" }}, {"raw-probe-empty", "raw", func() { f.raw.ProbeId = "" }}, {"raw-batch-empty", "raw", func() { f.raw.BatchId = "" }}, {"raw-target-empty", "raw", func() { f.raw.TargetId = "" }},
		{"raw-kind-invalid", "raw", func() { f.raw.Kind = "other" }}, {"raw-path-nul", "raw", func() { f.raw.PathKey = "x\x00" }}, {"quantile-path-nul", "quantiles", func() { f.quantile.PathKey = "x\x00" }}, {"event-path-nul", "events", func() { f.event.PathKey = "x\x00" }}, {"event-id-nul", "events", func() { f.event.EventId = "x\x00" }},
		{"raw-path", "raw", func() { f.raw.PathKey = "" }}, {"raw-kind", "raw", func() { f.raw.Kind = "" }}, {"raw-identity", "raw", func() { f.raw.ProbeId = "!" }}, {"quantile-path", "quantiles", func() { f.quantile.PathKey = "" }}, {"event-path", "events", func() { f.event.PathKey = "" }}, {"event-id", "events", func() { f.event.EventId = "" }},
		{"raw-zero", "raw", nil}, {"quantile-zero", "quantiles", nil}, {"event-zero", "events", nil},
	} {
		t.Run(tc.name, func(t *testing.T) {
			f.mu.Lock()
			f.raw = validRawRow()
			f.raw.PathKey = "path"
			f.quantile = &cp.QuantileRow{PathKey: "path", WindowStart: f.raw.StartedAt, Samples: 1}
			f.event = &cp.EventRow{EventId: "event", PathKey: "path", WindowStart: f.raw.StartedAt}
			if tc.change != nil {
				tc.change()
			}
			before := f.calls
			f.mu.Unlock()
			var err error
			switch tc.route {
			case "raw":
				_, err = c.ReadRaw(context.Background(), b)
			case "quantiles":
				_, err = c.ReadQuantiles(context.Background(), b)
			case "events":
				_, err = c.ReadEvents(context.Background(), b)
			}
			want := http.StatusOK
			if tc.change != nil {
				want = http.StatusBadGateway
				if err != store.ErrQueryResponse {
					t.Errorf("client error: %v", err)
				}
			} else if err != nil {
				t.Fatal(err)
			}
			values := url.Values{"path_key": {b.PathKey}, "from": {b.From.UTC().Format(time.RFC3339Nano)}, "to": {b.To.UTC().Format(time.RFC3339Nano)}, "limit": {"1"}}
			response, e := http.Get(h.URL + "/v1/measurements/" + tc.route + "?" + values.Encode())
			if e != nil {
				t.Fatal(e)
			}
			body, readErr := io.ReadAll(io.LimitReader(response.Body, store.QueryMaxBytes+1))
			response.Body.Close()
			if readErr != nil {
				t.Fatal(readErr)
			}
			if tc.change != nil && (!strings.Contains(string(body), `"code":"invalid_upstream_response"`) || strings.Contains(string(body), `"data"`)) {
				t.Errorf("wrong error payload %s", body)
			}
			if tc.name == "raw-zero" && (!strings.Contains(string(body), `"phase_ms":null`) || !strings.Contains(string(body), `"phase_present":false`)) {
				t.Errorf("absent phase synthesized: %s", body)
			}
			if response.StatusCode != want {
				t.Errorf("HTTP %d want %d", response.StatusCode, want)
			}
			f.mu.Lock()
			defer f.mu.Unlock()
			if f.calls != before+2 {
				t.Fatal("did not exercise actual typed success", f.calls, before)
			}
			t.Logf("actual mTLS success replies=2; client=%v; HTTP=%d", err, response.StatusCode)
		})
	}
}

// Exact valid DTO vectors retained from SOURCE7e native-final.json SHA256 8502f785cf76dac0b43a6c0ee107b397ad50ffd844e2cf000201cd7273bfa57a.
// This repeats conversion checks only; it is not a new native-engine run.
func TestRetainedNativeDTOsRemainValid(t *testing.T) {
	const fixture = `{"raw":[{"batch_key":"66bc6342ef391b27be59105b0dff663327fd632605173556aa975f3d7b79b5f2","result_index":0,"probe_id":"home","batch_id":"two","target_id":"target","kind":"tcp","family":"v4","addr":"127.0.0.1","path_key":"home\u2192target/tcp/v4","started_at":"2026-10-25T19:00:00.000000123Z","phases_nanoseconds":{"connect":"300000000"},"err":"","protocol":"","hop_ttls":[],"hop_addrs":[],"hop_rtt_nanoseconds":[],"tls_version":0,"tls_resumed":false,"phase_ms":300,"phase_present":true},{"batch_key":"68cc4f38a16b6523a518c3f648aa9ea4c8e84eb63a528ce728cdabb4bf7eb8c8","result_index":0,"probe_id":"home","batch_id":"one","target_id":"target","kind":"tcp","family":"v4","addr":"127.0.0.1","path_key":"home\u2192target/tcp/v4","started_at":"2026-10-25T19:00:00.000000123Z","phases_nanoseconds":{"connect":"10000000"},"err":"","protocol":"","hop_ttls":[],"hop_addrs":[],"hop_rtt_nanoseconds":[],"tls_version":0,"tls_resumed":false,"phase_ms":10,"phase_present":true}],"quantiles":[{"path_key":"home\u2192target/tcp/v4","window_start":"2026-10-25T19:00:00Z","p50_ms":155,"p95_ms":285.5,"p99_ms":297.1,"samples":"2"}],"events":[{"event_id":"f3d4d17214044db5ebbcc8ef9acc4daea1cf9e7158d0961e00793fac49ef7c63","path_key":"home\u2192target/tcp/v4","window_start":"2026-10-25T19:00:00Z","p95_ms":285.5,"baseline_ms":10,"sigma_ms":5,"cusum_k":0.5,"cusum_h":5,"path_changed":false,"first_divergent_ttl":0,"correlation_reason":"missing same-destination trace snapshot"}]}`
	var v struct {
		Raw       []store.RawResult `json:"raw"`
		Quantiles []store.Quantile  `json:"quantiles"`
		Events    []store.Event     `json:"events"`
	}
	if err := json.Unmarshal([]byte(fixture), &v); err != nil {
		t.Fatal(err)
	}
	if len(v.Raw) != 2 || len(v.Quantiles) != 1 || len(v.Events) != 1 {
		t.Fatal("missing vectors")
	}
	for _, r := range v.Raw {
		wire, e := rawToWire(r)
		if e != nil {
			t.Fatal(e)
		}
		if _, e = rawFromWire(wire); e != nil {
			t.Fatal(e)
		}
	}
	for _, r := range v.Quantiles {
		wire, e := quantileToWire(r)
		if e != nil {
			t.Fatal(e)
		}
		if _, e = quantileFromWire(wire); e != nil {
			t.Fatal(e)
		}
	}
	for _, r := range v.Events {
		wire, e := eventToWire(r)
		if e != nil {
			t.Fatal(e)
		}
		if _, e = eventFromWire(wire); e != nil {
			t.Fatal(e)
		}
	}
}
