package store

import (
	"context"
	"encoding/json"
	"fmt"
	"google.golang.org/protobuf/proto"
	pb "pathwatch.local/pathwatch/proto/pathwatch/v1"
	"strings"
)

// PrimaryPhase chooses the latency used by the five-minute view. A missing
// primary phase remains a raw error row, never a fabricated zero-ms sample.
func PrimaryPhase(r *pb.ProbeResult) (int64, bool) {
	k := map[string]string{"dns": "resolve", "tcp": "connect", "tls": "tls_handshake", "http1": "ttfb", "http2": "ttfb", "http3": "ttfb", "trace": "total"}[r.Kind]
	n, ok := r.PhasesNanoseconds[k]
	return n, ok
}
func (s *Store) insert(ctx context.Context, rec record) error {
	var req pb.ReportRequest
	if e := proto.Unmarshal(rec.Payload, &req); e != nil {
		return e
	}
	var out strings.Builder
	out.WriteString("INSERT INTO probe_results FORMAT JSONEachRow\n")
	samples := 0
	for i, r := range req.Results {
		n, present := PrimaryPhase(r)
		p := 0
		if present {
			p = 1
			samples++
		}
		ttls := []uint32{}
		addrs := []string{}
		rtts := []int64{}
		for _, h := range r.Hops {
			ttls = append(ttls, h.Ttl)
			addrs = append(addrs, h.Addr)
			rtts = append(rtts, h.RttNanoseconds)
		}
		phases := r.PhasesNanoseconds
		if phases == nil {
			phases = map[string]int64{}
		}
		row := map[string]any{"batch_key": rec.Key, "result_index": i, "probe_id": r.ProbeId, "batch_id": req.BatchId, "target_id": r.TargetId, "kind": r.Kind, "family": r.Family, "addr": r.Addr, "path_key": r.ProbeId + "→" + r.TargetId + "/" + r.Kind + "/" + r.Family, "started_at": r.StartedAt.AsTime().UTC().Format("2006-01-02 15:04:05.000000000"), "phases_nanoseconds": phases, "err": r.Err, "protocol": r.Protocol, "hop_ttls": ttls, "hop_addrs": addrs, "hop_rtt_nanoseconds": rtts, "tls_version": r.TlsVersion, "tls_resumed": r.TlsResumed, "phase_ms": float64(n) / 1e6, "phase_present": p}
		b, e := json.Marshal(row)
		if e != nil {
			return e
		}
		out.Write(b)
		out.WriteByte('\n')
	}
	if _, e := s.query(ctx, out.String()); e != nil {
		return e
	}
	raw, e := s.query(ctx, "SELECT count() FROM probe_results WHERE batch_key="+quote(rec.Key))
	if e != nil {
		return e
	}
	agg, e := s.query(ctx, "SELECT countMerge(samples) FROM path_quantiles_5m WHERE batch_key="+quote(rec.Key))
	if e != nil {
		return e
	}
	if raw != fmt.Sprint(len(req.Results)) || agg != fmt.Sprint(samples) {
		return fmt.Errorf("source/MV incomplete: raw=%s samples=%s", raw, agg)
	}
	return nil
}
