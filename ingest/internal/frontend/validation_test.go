package frontend

import (
	"context"
	"fmt"
	"strings"
	"testing"
	"time"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
	"pathwatch.local/pathwatch/ingest/internal/corerpc"
	"pathwatch.local/pathwatch/ingest/internal/report"
	"pathwatch.local/pathwatch/ingest/internal/server"
	pb "pathwatch.local/pathwatch/proto/pathwatch/v1"
)

func TestFullValidationMatrixBeforeStoreOrForward(t *testing.T) {
	now := time.Unix(1700000000, 123)
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
		{"clock-before", func(r *pb.ReportRequest) { r.SentAt = timestamppb.New(now.Add(-5*time.Minute - time.Nanosecond)) }},
		{"clock-after", func(r *pb.ReportRequest) { r.SentAt = timestamppb.New(now.Add(5*time.Minute + time.Nanosecond)) }},
	} {
		t.Run(tc.name, func(t *testing.T) {
			r := request(now)
			tc.change(r)
			want := report.Check(r, now)
			if want == nil {
				t.Fatal("case accepted")
			}
			clock := func() time.Time { return now }
			// A nil Store would panic if validation touched durability. The core
			// invokes this exact service; transport shape errors are tested separately.
			owner := server.New(nil, clock)
			core := &corerpc.Service{Reporter: owner}
			capture := &capture{}
			front := New(capture, clock)
			for name, call := range map[string]func(context.Context, *pb.ReportRequest) (*pb.ReportResponse, error){"embedded": owner.Report, "core-service": core.Report, "frontend": front.Report} {
				ack, got := call(context.Background(), r)
				if ack != nil || !proto.Equal(status.Convert(got).Proto(), status.Convert(want).Proto()) {
					t.Fatalf("%s got=%v want=%v", name, got, want)
				}
			}
			if capture.calls != 0 {
				t.Fatal("invalid input forwarded")
			}
		})
	}
}

func TestInclusiveValidationBoundaries(t *testing.T) {
	now := time.Unix(1700000000, 123)
	for _, offset := range []time.Duration{-5 * time.Minute, 5 * time.Minute} {
		r := request(now)
		r.SentAt = timestamppb.New(now.Add(offset))
		r.ProbeId = strings.Repeat("p", 128)
		r.BatchId = strings.Repeat("b", 128)
		v := r.Results[0]
		v.ProbeId = r.ProbeId
		v.TargetId = strings.Repeat("t", 128)
		v.Err = strings.Repeat("x", 4096)
		v.StartedAt = timestamppb.New(time.Unix(4294967295-180*86400, 999999999))
		v.TlsVersion = 65535
		v.TlsResumed = true
		v.PhasesNanoseconds = map[string]int64{}
		for i := 0; i < 16; i++ {
			v.PhasesNanoseconds[fmt.Sprint(i)] = int64(i)
		}
		for i := 1; i <= 255; i++ {
			v.Hops = append(v.Hops, &pb.Hop{Ttl: uint32(i), Addr: "::1", RttNanoseconds: 1})
		}
		if e := report.Check(r, now); e != nil {
			t.Fatal(e)
		}
		c := &capture{ack: &pb.ReportResponse{BatchId: r.BatchId, Status: pb.AckStatus_ACK_STATUS_ACCEPTED, CommittedResultCount: 1}}
		if _, e := New(c, func() time.Time { return now }).Report(context.Background(), r); e != nil || c.r != r || c.calls != 1 {
			t.Fatal(e, c.calls)
		}
	}
	for _, kind := range []string{"dns", "tcp", "tls", "http1", "http2", "http3", "trace"} {
		r := request(now)
		r.Results[0].Kind = kind
		if e := report.Check(r, now); status.Code(e) != codes.OK {
			t.Fatal(kind, e)
		}
	}
}
