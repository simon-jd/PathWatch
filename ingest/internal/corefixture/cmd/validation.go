package main

import (
	"context"
	"errors"
	"fmt"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
	"pathwatch.local/pathwatch/ingest/internal/corerpc"
	"pathwatch.local/pathwatch/ingest/internal/report"
	pb "pathwatch.local/pathwatch/proto/pathwatch/v1"
	"strings"
	"time"
)

func wireValidation(core *corerpc.Client, fronts []pb.IngestClient) ([]map[string]any, error) {
	now := time.Now()
	var out []map[string]any
	for _, tc := range []struct {
		name   string
		change func(*pb.ReportRequest)
	}{
		{"sent-nil", func(r *pb.ReportRequest) { r.SentAt = nil }},
		{"sent-invalid", func(r *pb.ReportRequest) { r.SentAt.Nanos = -1 }},
		{"probe-empty", func(r *pb.ReportRequest) { r.ProbeId = "" }},
		{"probe-long", func(r *pb.ReportRequest) { r.ProbeId = strings.Repeat("p", 129) }},
		{"batch-invalid", func(r *pb.ReportRequest) { r.BatchId = "!" }},
		{"empty-results", func(r *pb.ReportRequest) { r.Results = nil }},
		{"too-many-results", func(r *pb.ReportRequest) {
			v := r.Results[0]
			r.Results = make([]*pb.ProbeResult, 10001)
			for i := range r.Results {
				r.Results[i] = v
			}
		}},
		{"message-limit", func(r *pb.ReportRequest) { r.ProtoReflect().SetUnknown(make([]byte, report.MaxMessageBytes)) }},
		{"nil-result", func(r *pb.ReportRequest) { r.Results[0] = nil }},
		{"probe-mismatch", func(r *pb.ReportRequest) { r.Results[0].ProbeId = "other" }},
		{"target-invalid", func(r *pb.ReportRequest) { r.Results[0].TargetId = "!" }},
		{"started-nil", func(r *pb.ReportRequest) { r.Results[0].StartedAt = nil }},
		{"started-invalid", func(r *pb.ReportRequest) { r.Results[0].StartedAt.Nanos = 1000000000 }},
		{"started-negative", func(r *pb.ReportRequest) { r.Results[0].StartedAt.Seconds = -1 }},
		{"ttl-overflow", func(r *pb.ReportRequest) { r.Results[0].StartedAt.Seconds = 4294967295 - 180*86400 + 1 }},
		{"kind", func(r *pb.ReportRequest) { r.Results[0].Kind = "other" }},
		{"error-long", func(r *pb.ReportRequest) { r.Results[0].Err = strings.Repeat("x", 4097) }},
		{"error-utf8", func(r *pb.ReportRequest) { r.Results[0].Err = string([]byte{255}) }},
		{"phases-count", func(r *pb.ReportRequest) {
			for i := 0; i < 17; i++ {
				r.Results[0].PhasesNanoseconds[fmt.Sprint(i)] = 1
			}
		}},
		{"hops-count", func(r *pb.ReportRequest) { r.Results[0].Hops = make([]*pb.Hop, 256) }},
		{"address-no-error", func(r *pb.ReportRequest) { r.Results[0].Err = "" }},
		{"missing-address-family", func(r *pb.ReportRequest) { r.Results[0].Family = "bad" }},
		{"address-invalid", func(r *pb.ReportRequest) { r.Results[0].Addr = "bad" }},
		{"address-family-empty", func(r *pb.ReportRequest) { r.Results[0].Addr = "127.0.0.1" }},
		{"address-family-mismatch", func(r *pb.ReportRequest) { r.Results[0].Addr = "::1"; r.Results[0].Family = "v4" }},
		{"protocol", func(r *pb.ReportRequest) { r.Results[0].Protocol = "h4" }},
		{"tls-version", func(r *pb.ReportRequest) { r.Results[0].TlsVersion = 65536 }},
		{"tls-resumed", func(r *pb.ReportRequest) { r.Results[0].TlsResumed = true }},
		{"phase-name", func(r *pb.ReportRequest) { r.Results[0].PhasesNanoseconds["!"] = 1 }},
		{"phase-long", func(r *pb.ReportRequest) { r.Results[0].PhasesNanoseconds[strings.Repeat("x", 129)] = 1 }},
		{"phase-negative", func(r *pb.ReportRequest) { r.Results[0].PhasesNanoseconds["dns"] = -1 }},
		{"hop-nil", func(r *pb.ReportRequest) { r.Results[0].Hops = []*pb.Hop{nil} }},
		{"hop-ttl-zero", func(r *pb.ReportRequest) { r.Results[0].Hops = []*pb.Hop{{}} }},
		{"hop-ttl-overflow", func(r *pb.ReportRequest) { r.Results[0].Hops = []*pb.Hop{{Ttl: 256}} }},
		{"hop-rtt-negative", func(r *pb.ReportRequest) { r.Results[0].Hops = []*pb.Hop{{Ttl: 1, RttNanoseconds: -1}} }},
		{"hop-rtt-without-address", func(r *pb.ReportRequest) { r.Results[0].Hops = []*pb.Hop{{Ttl: 1, RttNanoseconds: 1}} }},
		{"hop-address", func(r *pb.ReportRequest) { r.Results[0].Hops = []*pb.Hop{{Ttl: 1, Addr: "bad"}} }},
		{"clock-before", func(r *pb.ReportRequest) { r.SentAt = timestamppb.New(now.Add(-6 * time.Minute)) }},
		{"clock-after", func(r *pb.ReportRequest) { r.SentAt = timestamppb.New(now.Add(6 * time.Minute)) }},
	} {
		r := &pb.ReportRequest{ProbeId: "probe", BatchId: "matrix", SentAt: timestamppb.New(now), Results: []*pb.ProbeResult{{ProbeId: "probe", TargetId: "target", Kind: "dns", Err: "test failure", StartedAt: timestamppb.New(now), PhasesNanoseconds: map[string]int64{"dns": 123}}}}
		tc.change(r)
		want := report.Check(r, now)
		if want == nil {
			return nil, fmt.Errorf("invalid matrix case accepted: %s", tc.name)
		}
		wire, marshalErr := proto.Marshal(r)
		var shapeErr error
		if marshalErr == nil {
			_, shapeErr = report.Charge(wire)
		}
		var semanticLimit *report.SemanticLimit
		semantic := marshalErr == nil && (shapeErr == nil || errors.As(shapeErr, &semanticLimit))
		_, direct := core.Report(context.Background(), r)
		if direct == nil {
			return nil, fmt.Errorf("invalid matrix core ACK: %s", tc.name)
		}
		if semantic && !proto.Equal(status.Convert(want).Proto(), status.Convert(direct).Proto()) {
			return nil, fmt.Errorf("core matrix %s got=%v want=%v", tc.name, direct, want)
		}
		item := map[string]any{"name": tc.name, "semantic_wire": semantic, "core_code": status.Code(direct).String()}
		var codesSeen []string
		for _, front := range fronts {
			ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
			ack, err := front.Report(ctx, r)
			cancel()
			if err == nil || ack != nil {
				return nil, fmt.Errorf("invalid matrix frontend ACK: %s", tc.name)
			}
			if semantic && !proto.Equal(status.Convert(want).Proto(), status.Convert(err).Proto()) {
				return nil, fmt.Errorf("frontend matrix %s got=%v want=%v", tc.name, err, want)
			}
			codesSeen = append(codesSeen, status.Code(err).String())
		}
		item["frontend_codes"] = codesSeen
		out = append(out, item)
	}
	// Inclusive metadata boundaries travel through actual private/public wires.
	r := &pb.ReportRequest{ProbeId: strings.Repeat("p", 128), BatchId: strings.Repeat("b", 128), SentAt: timestamppb.Now()}
	v := &pb.ProbeResult{ProbeId: r.ProbeId, TargetId: strings.Repeat("t", 128), Kind: "dns", Err: strings.Repeat("x", 4096), StartedAt: timestamppb.New(time.Unix(4294967295-180*86400, 999999999)), TlsVersion: 65535, TlsResumed: true, PhasesNanoseconds: map[string]int64{}}
	for i := 0; i < 16; i++ {
		v.PhasesNanoseconds[fmt.Sprint(i)] = int64(i)
	}
	for i := 1; i <= 255; i++ {
		v.Hops = append(v.Hops, &pb.Hop{Ttl: uint32(i), Addr: "::1", RttNanoseconds: 1})
	}
	r.Results = []*pb.ProbeResult{v}
	ack, err := core.Report(context.Background(), r)
	if err != nil || ack.Status != pb.AckStatus_ACK_STATUS_ACCEPTED {
		return nil, fmt.Errorf("boundary core: %v %v", ack, err)
	}
	for _, front := range fronts {
		a, e := front.Report(context.Background(), r)
		if status.Code(e) != codes.OK || a.Status != pb.AckStatus_ACK_STATUS_DUPLICATE || a.CommittedResultCount != 1 {
			return nil, fmt.Errorf("boundary forwarded: %v %v", a, e)
		}
	}
	out = append(out, map[string]any{"name": "inclusive-metadata-boundaries", "direct_accepted": true, "both_forwarded_duplicate": true})
	return out, nil
}
