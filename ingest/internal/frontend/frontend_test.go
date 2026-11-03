package frontend

import (
	"context"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
	"net"
	"net/http/httptest"
	"pathwatch.local/pathwatch/ingest/internal/corerpc"
	"pathwatch.local/pathwatch/ingest/internal/report"
	pb "pathwatch.local/pathwatch/proto/pathwatch/v1"
	"strings"
	"testing"
	"time"
)

type capture struct {
	r     *pb.ReportRequest
	ack   *pb.ReportResponse
	err   error
	calls int
}

func (c *capture) Report(_ context.Context, r *pb.ReportRequest) (*pb.ReportResponse, error) {
	c.r = r
	c.calls++
	return c.ack, c.err
}
func request(now time.Time) *pb.ReportRequest {
	return &pb.ReportRequest{ProbeId: "probe", BatchId: "batch", SentAt: timestamppb.New(now), Results: []*pb.ProbeResult{{ProbeId: "probe", TargetId: "target", Kind: "dns", Err: "test failure", StartedAt: timestamppb.New(now), PhasesNanoseconds: map[string]int64{"dns": 123}}}}
}
func TestOriginalIdentityAndACK(t *testing.T) {
	now := time.Now()
	c := &capture{ack: &pb.ReportResponse{BatchId: "batch", Status: pb.AckStatus_ACK_STATUS_DUPLICATE, CommittedResultCount: 1}}
	s := New(c, func() time.Time { return now })
	r := request(now)
	r.ProtoReflect().SetUnknown([]byte{0xa0, 6, 1})
	before := proto.Clone(r)
	ack, e := s.Report(context.Background(), r)
	if e != nil || ack.Status != pb.AckStatus_ACK_STATUS_DUPLICATE || c.r != r || !proto.Equal(before, c.r) {
		t.Fatal(ack, e, "identity mutated")
	}
	for _, a := range []*pb.ReportResponse{nil, {BatchId: "wrong", Status: 1, CommittedResultCount: 1}, {BatchId: "batch", Status: 0, CommittedResultCount: 1}, {BatchId: "batch", Status: 1, CommittedResultCount: 2}} {
		c.ack = a
		if _, e = s.Report(context.Background(), r); status.Code(e) != codes.Unavailable {
			t.Fatal(e)
		}
	}
	w := httptest.NewRecorder()
	s.Metrics(w, nil)
	if strings.Contains(w.Body.String(), "pathwatch_ingest_") || !strings.Contains(w.Body.String(), `outcome="acknowledged"} 1`) {
		t.Fatal(w.Body.String())
	}
}
func TestValidationParityAndNoForward(t *testing.T) {
	now := time.Now()
	mutations := []func(*pb.ReportRequest){func(r *pb.ReportRequest) { r.BatchId = "!" }, func(r *pb.ReportRequest) { r.SentAt = nil }, func(r *pb.ReportRequest) { r.SentAt = timestamppb.New(now.Add(-6 * time.Minute)) }, func(r *pb.ReportRequest) { r.Results = nil }, func(r *pb.ReportRequest) { r.Results[0].ProbeId = "other" }, func(r *pb.ReportRequest) { r.Results[0].StartedAt = timestamppb.New(time.Unix(-1, 0)) }, func(r *pb.ReportRequest) { r.Results[0].Kind = "bad" }, func(r *pb.ReportRequest) { r.Results[0].Err = string([]byte{255}) }, func(r *pb.ReportRequest) { r.Results[0].Addr = "bad-ip" }, func(r *pb.ReportRequest) { r.Results[0].Addr = "127.0.0.1"; r.Results[0].Family = "v6" }, func(r *pb.ReportRequest) { r.Results[0].Protocol = "h4" }, func(r *pb.ReportRequest) { r.Results[0].TlsResumed = true }, func(r *pb.ReportRequest) { r.Results[0].PhasesNanoseconds["dns"] = -1 }, func(r *pb.ReportRequest) { r.Results[0].Hops = []*pb.Hop{{Ttl: 0}} }}
	for i, m := range mutations {
		r := request(now)
		m(r)
		c := &capture{}
		s := New(c, func() time.Time { return now })
		_, e := s.Report(context.Background(), r)
		want := report.Check(r, now)
		if e == nil || status.Code(e) != status.Code(want) || status.Convert(e).Message() != status.Convert(want).Message() || c.calls != 0 {
			t.Fatalf("%d got=%v want=%v calls=%d", i, e, want, c.calls)
		}
	}
}
func TestNoHiddenRetryOnLostACK(t *testing.T) {
	now := time.Now()
	c := &capture{err: report.Unavailable()}
	s := New(c, nil)
	if _, e := s.Report(context.Background(), request(now)); status.Code(e) != codes.Unavailable || c.calls != 1 {
		t.Fatal(e, c.calls)
	}
}

type blockedForwarder struct {
	entered chan struct{}
	release chan struct{}
}

func (b *blockedForwarder) Report(ctx context.Context, r *pb.ReportRequest) (*pb.ReportResponse, error) {
	b.entered <- struct{}{}
	select {
	case <-ctx.Done():
		return nil, report.Unavailable()
	case <-b.release:
		return &pb.ReportResponse{BatchId: r.BatchId, Status: 1, CommittedResultCount: uint32(len(r.Results))}, nil
	}
}
func TestPublicPredecodeOverloadWireContract(t *testing.T) {
	b := &blockedForwarder{make(chan struct{}, 4), make(chan struct{})}
	s := New(b, nil)
	a := corerpc.NewAdmission(false, nil, s.Overloaded, s.SemanticRejected)
	g := grpc.NewServer(a.Options(nil)...)
	corerpc.Register(g, &pb.Ingest_ServiceDesc, s)
	l, e := net.Listen("tcp", "127.0.0.1:0")
	if e != nil {
		t.Fatal(e)
	}
	done := make(chan struct{})
	go func() { defer close(done); g.Serve(corerpc.LimitListener(l, 16)) }()
	defer func() { a.Stop(); g.Stop(); <-done; a.Wait() }()
	clients := []pb.IngestClient{}
	for i := 0; i < 5; i++ {
		cc, e := grpc.NewClient("passthrough:///"+l.Addr().String(), grpc.WithTransportCredentials(insecure.NewCredentials()), grpc.WithDisableRetry())
		if e != nil {
			t.Fatal(e)
		}
		defer cc.Close()
		clients = append(clients, pb.NewIngestClient(cc))
	}
	answers := make(chan error, 4)
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	for _, c := range clients[:4] {
		go func(c pb.IngestClient) { _, e := c.Report(ctx, request(time.Now())); answers <- e }(c)
	}
	for i := 0; i < 4; i++ {
		select {
		case <-b.entered:
		case <-ctx.Done():
			t.Fatal(ctx.Err())
		}
	}
	_, e = clients[4].Report(ctx, request(time.Now()))
	st := status.Convert(e)
	if st.Code() != codes.ResourceExhausted || st.Message() != "ingest queue full" || len(st.Proto().Details) != 0 {
		t.Fatal(st)
	}
	close(b.release)
	for i := 0; i < 4; i++ {
		if e := <-answers; e != nil {
			t.Fatal(e)
		}
	}

	r := request(time.Now())
	r.Results[0].Hops = make([]*pb.Hop, 256)
	if _, err := clients[4].Report(ctx, r); status.Code(err) != codes.InvalidArgument {
		t.Fatal(err)
	}
	r = request(time.Now())
	r.Results[0].Kind = "invalid"
	if _, err := clients[4].Report(ctx, r); status.Code(err) != codes.InvalidArgument {
		t.Fatal(err)
	}
	w := httptest.NewRecorder()
	s.Metrics(w, nil)
	if !strings.Contains(w.Body.String(), `outcome="acknowledged"} 4`) || !strings.Contains(w.Body.String(), `outcome="error"} 3`) {
		t.Fatal(w.Body.String())
	}
	if len(b.entered) != 0 {
		t.Fatal("rejected request forwarded")
	}
}

func TestSixDecodedErrorReasonsPreserved(t *testing.T) {
	for _, v := range []struct {
		code            codes.Code
		reason, message string
	}{
		{codes.InvalidArgument, "invalid_request", "invalid request: kind"},
		{codes.FailedPrecondition, "clock_skew", "clock_skew"},
		{codes.AlreadyExists, "conflict", "batch key has different ordered content"},
		{codes.ResourceExhausted, "capacity", "journal capacity reached"},
		{codes.Unavailable, "unavailable", "durable write unavailable; retain batch"},
		{codes.ResourceExhausted, "overloaded", "ingest queue full"},
	} {
		want := report.Error(v.code, v.reason, v.message)
		c := &capture{err: want}
		_, got := New(c, nil).Report(context.Background(), request(time.Now()))
		if !proto.Equal(status.Convert(want).Proto(), status.Convert(got).Proto()) || c.calls != 1 {
			t.Fatal(v.reason, got, c.calls)
		}
	}
}
