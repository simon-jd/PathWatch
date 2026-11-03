package corerpc

import (
	"google.golang.org/protobuf/types/known/timestamppb"
	"math"
	"pathwatch.local/pathwatch/ingest/internal/report"
	"pathwatch.local/pathwatch/ingest/internal/store"
	cp "pathwatch.local/pathwatch/proto/pathwatch/core/v1"
	pb "pathwatch.local/pathwatch/proto/pathwatch/v1"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"
)

func finite(v ...float64) bool {
	for _, x := range v {
		if math.IsNaN(x) || math.IsInf(x, 0) {
			return false
		}
	}
	return true
}

// Keep the public query path contract; paths need not follow a new grammar.
func pathKey(s string) bool { return requiredText(s) && len(s) <= 512 }
func requiredText(s string) bool {
	return s != "" && utf8.ValidString(s) && !strings.ContainsRune(s, 0)
}
func nonnegative(v ...float64) bool {
	if !finite(v...) {
		return false
	}
	for _, x := range v {
		if x < 0 {
			return false
		}
	}
	return true
}
func stamp(s string) (*timestamppb.Timestamp, error) {
	t, e := time.Parse(time.RFC3339Nano, s)
	if e != nil {
		return nil, store.ErrQueryResponse
	}
	p := timestamppb.New(t)
	if p.CheckValid() != nil {
		return nil, store.ErrQueryResponse
	}
	return p, nil
}
func stampString(p *timestamppb.Timestamp) (string, error) {
	if p == nil || p.CheckValid() != nil || p.Seconds < 0 || p.Seconds > 4294967295-180*86400 {
		return "", store.ErrQueryResponse
	}
	return p.AsTime().UTC().Format(time.RFC3339Nano), nil
}
func rawToWire(v store.RawResult) (*cp.RawRow, error) {
	t, e := stamp(v.StartedAt)
	if e != nil {
		return nil, e
	}
	r := &cp.RawRow{BatchKey: v.BatchKey, ResultIndex: v.ResultIndex, ProbeId: v.ProbeID, BatchId: v.BatchID, TargetId: v.TargetID, Kind: v.Kind, Family: v.Family, Addr: v.Addr, PathKey: v.PathKey, StartedAt: t, Phases: map[string]int64{}, Err: v.Err, Protocol: v.Protocol, HopTtls: v.HopTTLs, HopAddrs: v.HopAddrs, TlsVersion: v.TLSVersion, TlsResumed: v.TLSResumed, PhaseMs: v.PhaseMS}
	if v.PhasePresent != (v.PhaseMS != nil) {
		return nil, store.ErrQueryResponse
	}
	for k, x := range v.Phases {
		n, e := strconv.ParseInt(x, 10, 64)
		if e != nil {
			return nil, store.ErrQueryResponse
		}
		r.Phases[k] = n
	}
	for _, x := range v.HopRTTs {
		n, e := strconv.ParseInt(x, 10, 64)
		if e != nil {
			return nil, store.ErrQueryResponse
		}
		r.HopRtts = append(r.HopRtts, n)
	}
	_, e = rawFromWire(r)
	return r, e
}
func rawFromWire(r *cp.RawRow) (store.RawResult, error) {
	bad := store.ErrQueryResponse
	if r == nil {
		return store.RawResult{}, bad
	}
	s, e := stampString(r.StartedAt)
	if e != nil {
		return store.RawResult{}, e
	}
	if !requiredText(r.BatchKey) || r.ResultIndex >= 10000 || !pathKey(r.PathKey) || len(r.Phases) > 16 || len(r.HopTtls) > 255 || len(r.HopAddrs) != len(r.HopTtls) || len(r.HopRtts) != len(r.HopTtls) || len(r.Err) > 4096 || r.TlsVersion > 65535 || (r.TlsResumed && r.TlsVersion == 0) || (r.PhaseMs != nil && !nonnegative(*r.PhaseMs)) {
		return store.RawResult{}, bad
	}
	// Reuse original measurement semantics with a valid synthetic envelope.
	// Validate (not Check) preserves historical timestamps without clock-skew
	// rejection. All actual raw identities and metadata remain in the result.
	measurement := &pb.ProbeResult{ProbeId: r.ProbeId, TargetId: r.TargetId, Kind: r.Kind, Family: r.Family, Addr: r.Addr, StartedAt: r.StartedAt, PhasesNanoseconds: r.Phases, Err: r.Err, Protocol: r.Protocol, TlsVersion: r.TlsVersion, TlsResumed: r.TlsResumed}
	for i, ttl := range r.HopTtls {
		measurement.Hops = append(measurement.Hops, &pb.Hop{Ttl: ttl, Addr: r.HopAddrs[i], RttNanoseconds: r.HopRtts[i]})
	}
	if report.Validate(&pb.ReportRequest{ProbeId: r.ProbeId, BatchId: r.BatchId, SentAt: r.StartedAt, Results: []*pb.ProbeResult{measurement}}) != nil {
		return store.RawResult{}, bad
	}
	v := store.RawResult{BatchKey: r.BatchKey, ResultIndex: r.ResultIndex, ProbeID: r.ProbeId, BatchID: r.BatchId, TargetID: r.TargetId, Kind: r.Kind, Family: r.Family, Addr: r.Addr, PathKey: r.PathKey, StartedAt: s, Phases: map[string]string{}, Err: r.Err, Protocol: r.Protocol, HopTTLs: r.HopTtls, HopAddrs: r.HopAddrs, TLSVersion: r.TlsVersion, TLSResumed: r.TlsResumed, PhaseMS: r.PhaseMs, PhasePresent: r.PhaseMs != nil, HopRTTs: []string{}}
	for k, n := range r.Phases {
		if n < 0 {
			return store.RawResult{}, bad
		}
		v.Phases[k] = strconv.FormatInt(n, 10)
	}
	for i, n := range r.HopRtts {
		if n < 0 || r.HopTtls[i] == 0 || r.HopTtls[i] > 255 {
			return store.RawResult{}, bad
		}
		v.HopRTTs = append(v.HopRTTs, strconv.FormatInt(n, 10))
	}
	if v.HopTTLs == nil {
		v.HopTTLs = []uint32{}
	}
	if v.HopAddrs == nil {
		v.HopAddrs = []string{}
	}
	return v, nil
}
func quantileToWire(v store.Quantile) (*cp.QuantileRow, error) {
	t, e := stamp(v.WindowStart)
	if e != nil {
		return nil, e
	}
	n, e := strconv.ParseUint(v.Samples, 10, 64)
	if e != nil {
		return nil, store.ErrQueryResponse
	}
	r := &cp.QuantileRow{PathKey: v.PathKey, WindowStart: t, P50: v.P50, P95: v.P95, P99: v.P99, Samples: n}
	_, e = quantileFromWire(r)
	return r, e
}
func quantileFromWire(r *cp.QuantileRow) (store.Quantile, error) {
	if r == nil || r.Samples == 0 || !pathKey(r.PathKey) || !nonnegative(r.P50, r.P95, r.P99) || r.P50 > r.P95 || r.P95 > r.P99 {
		return store.Quantile{}, store.ErrQueryResponse
	}
	s, e := stampString(r.WindowStart)
	return store.Quantile{PathKey: r.PathKey, WindowStart: s, P50: r.P50, P95: r.P95, P99: r.P99, Samples: strconv.FormatUint(r.Samples, 10)}, e
}
func eventToWire(v store.Event) (*cp.EventRow, error) {
	t, e := stamp(v.WindowStart)
	if e != nil {
		return nil, e
	}
	r := &cp.EventRow{EventId: v.ID, PathKey: v.PathKey, WindowStart: t, P95: v.P95, Baseline: v.Baseline, Sigma: v.Sigma, K: v.K, H: v.H, PathChanged: v.PathChanged, FirstDivergentTtl: uint32(v.FirstDivergentTTL), CorrelationReason: v.CorrelationReason}
	_, e = eventFromWire(r)
	return r, e
}
func eventFromWire(r *cp.EventRow) (store.Event, error) {
	if r == nil || !requiredText(r.EventId) || !pathKey(r.PathKey) || !utf8.ValidString(r.CorrelationReason) || r.FirstDivergentTtl > 65535 || !nonnegative(r.P95, r.Baseline, r.Sigma, r.K, r.H) {
		return store.Event{}, store.ErrQueryResponse
	}
	s, e := stampString(r.WindowStart)
	return store.Event{ID: r.EventId, PathKey: r.PathKey, WindowStart: s, P95: r.P95, Baseline: r.Baseline, Sigma: r.Sigma, K: r.K, H: r.H, PathChanged: r.PathChanged, FirstDivergentTTL: uint16(r.FirstDivergentTtl), CorrelationReason: r.CorrelationReason}, e
}
