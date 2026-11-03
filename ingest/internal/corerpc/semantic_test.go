package corerpc

import (
	"context"
	"net"
	cp "pathwatch.local/pathwatch/proto/pathwatch/core/v1"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"pathwatch.local/pathwatch/ingest/internal/report"
	"pathwatch.local/pathwatch/ingest/internal/server"
	pb "pathwatch.local/pathwatch/proto/pathwatch/v1"
)

func TestSemanticPredecodeLimitsKeepApplicationStatus(t *testing.T) {
	var coreRejected, publicRejected, coreOverload, publicOverload atomic.Int64
	f := &fakeStore{}
	overloaded := func(n *atomic.Int64) func() error {
		return func() error {
			n.Add(1)
			return report.Error(codes.ResourceExhausted, "overloaded", "ingest queue full")
		}
	}
	target, p, a := startCore(t, f, func(a *Admission) {
		a.semanticReportRejected = func() { coreRejected.Add(1) }
		a.overload = overloaded(&coreOverload)
	})
	core := connect(t, target, p)
	files := p.cert(t, "core.test", true, time.Now().Add(time.Hour))
	options, err := server.Options(server.TransportConfig{Listen: "127.0.0.1:1", CertFile: files.Cert, KeyFile: files.Key})
	if err != nil {
		t.Fatal(err)
	}
	publicAdmission := NewAdmission(false, nil, overloaded(&publicOverload), func() { publicRejected.Add(1) })
	g := grpc.NewServer(append(options, publicAdmission.Options(nil)...)...)
	Register(g, &pb.Ingest_ServiceDesc, server.New(nil, time.Now))
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	done := make(chan struct{})
	go func() { defer close(done); g.Serve(LimitListener(l, 16)) }()
	t.Cleanup(func() { publicAdmission.Stop(); g.Stop(); <-done; publicAdmission.Wait() })
	trust, err := LoadTrust(p.cert(t, "spiffe://pathwatch.local/frontend/stable", false, time.Now().Add(time.Hour)), false)
	if err != nil {
		t.Fatal(err)
	}
	cc, err := grpc.NewClient(l.Addr().String(), grpc.WithTransportCredentials(trust.Credentials()), grpc.WithNoProxy(), grpc.WithDisableRetry())
	if err != nil {
		t.Fatal(err)
	}
	defer cc.Close()
	public := pb.NewIngestClient(cc)
	for _, tc := range []struct {
		name   string
		change func(*pb.ReportRequest)
	}{
		{"results", func(r *pb.ReportRequest) {
			v := r.Results[0]
			r.Results = make([]*pb.ProbeResult, 10001)
			for i := range r.Results {
				r.Results[i] = v
			}
		}},
		{"hops", func(r *pb.ReportRequest) {
			for i := 0; i < 256; i++ {
				r.Results[0].Hops = append(r.Results[0].Hops, &pb.Hop{Ttl: 1})
			}
		}},
		{"phases", func(r *pb.ReportRequest) {
			r.Results[0].PhasesNanoseconds = map[string]int64{}
			for i := 0; i < 17; i++ {
				r.Results[0].PhasesNanoseconds[strings.Repeat("x", i+1)] = 1
			}
		}},
		{"final-key", func(r *pb.ReportRequest) {
			r.Results[0].PhasesNanoseconds = map[string]int64{strings.Repeat("x", 129): 1}
		}},
		{"compound-preflight-order", func(r *pb.ReportRequest) {
			r.SentAt = nil
			r.Results[0].PhasesNanoseconds = map[string]int64{strings.Repeat("x", 129): 1}
		}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			r := validReport()
			tc.change(r)
			want := report.Check(r, time.Now())
			if want == nil {
				t.Fatal("case accepted")
			}
			if tc.name == "compound-preflight-order" {
				// The safety bound precedes the missing timestamp on this multiply
				// invalid request. Its original category/details are still fixed.
				want = report.Error(codes.InvalidArgument, "invalid_request", "invalid request: phase name/duration")
			}
			if _, err := proto.Marshal(r); err != nil {
				t.Fatal("semantic case must have valid wire", err)
			}
			for name, call := range map[string]func(context.Context, *pb.ReportRequest) (*pb.ReportResponse, error){"private-client": core.Report, "public-wire": func(ctx context.Context, r *pb.ReportRequest) (*pb.ReportResponse, error) {
				return public.Report(ctx, r)
			}} {
				ctx, cancel := context.WithTimeout(context.Background(), time.Second)
				ack, got := call(ctx, r)
				cancel()
				if ack != nil || !proto.Equal(status.Convert(want).Proto(), status.Convert(got).Proto()) {
					t.Errorf("%s got=%v want=%v details=%v", name, got, want, status.Convert(got).Details())
				}
			}
		})
	}
	await(t, func() bool { return len(a.reports) == 0 && len(publicAdmission.reports) == 0 })
	if coreRejected.Load() != 5 || publicRejected.Load() != 5 || coreOverload.Load() != 0 || publicOverload.Load() != 0 {
		t.Fatalf("semantic counts core=%d public=%d overload=%d/%d", coreRejected.Load(), publicRejected.Load(), coreOverload.Load(), publicOverload.Load())
	}
	// Malformed Report, query decode and unknown methods never count semantic
	// Report failures. These calls use the actual established TLS connections.
	for _, conn := range []*grpc.ClientConn{core.conn, cc} {
		prefix := CorePrefix
		if conn == cc {
			prefix = "/pathwatch.v1.Ingest/"
		}
		for _, method := range []string{"Report", "ReadRaw", "Unknown"} {
			ctx, cancel := context.WithTimeout(context.Background(), time.Second)
			var reply []byte
			err := conn.Invoke(ctx, prefix+method, []byte{0xff}, &reply, grpc.ForceCodec(bytesCodec{}))
			cancel()
			if err == nil {
				t.Fatalf("malformed %s succeeded", method)
			}
		}
	}
	// A typed query reply is not an inbound Report and must not touch counters.
	if _, err := cp.NewCoreClient(core.conn).Describe(context.Background(), &cp.DescribeRequest{}); err != nil {
		t.Fatal(err)
	}
	await(t, func() bool { return len(a.reports) == 0 && len(a.reads) == 0 && len(publicAdmission.reports) == 0 })
	for _, admission := range []*Admission{a, publicAdmission} {
		admission.mu.Lock()
		admission.limit = 0
		admission.mu.Unlock()
	}
	for name, call := range map[string]func(context.Context, *pb.ReportRequest) (*pb.ReportResponse, error){"private": core.Report, "public": func(ctx context.Context, r *pb.ReportRequest) (*pb.ReportResponse, error) {
		return public.Report(ctx, r)
	}} {
		ack, err := call(context.Background(), validReport())
		want := report.Error(codes.ResourceExhausted, "overloaded", "ingest queue full")
		if ack != nil || !proto.Equal(status.Convert(err).Proto(), status.Convert(want).Proto()) {
			t.Fatalf("%s weighted refusal: %v %v", name, ack, err)
		}
	}
	await(t, func() bool { return len(a.reports) == 0 && len(publicAdmission.reports) == 0 })
	if coreRejected.Load() != 5 || publicRejected.Load() != 5 || coreOverload.Load() != 1 || publicOverload.Load() != 1 {
		t.Fatalf("final counts core=%d public=%d overload=%d/%d", coreRejected.Load(), publicRejected.Load(), coreOverload.Load(), publicOverload.Load())
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.commits != 0 {
		t.Fatal("rejected request reached store", f.commits)
	}
	t.Logf("actual TLS semantic rejects core=5 public=5; weighted overload core=1 public=1; malformed/query/unknown add zero; Store commits=0")
}
