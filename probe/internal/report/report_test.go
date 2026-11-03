package report

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/pem"
	"errors"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"
	"google.golang.org/protobuf/proto"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"pathwatch.local/pathwatch/probe/internal/result"
	"pathwatch.local/pathwatch/probe/internal/spool"
	pb "pathwatch.local/pathwatch/proto/pathwatch/v1"
	"reflect"
	"strconv"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

type serverFunc struct {
	pb.UnimplementedIngestServer
	fn func(context.Context, *pb.ReportRequest) (*pb.ReportResponse, error)
}

func (s *serverFunc) Report(ctx context.Context, r *pb.ReportRequest) (*pb.ReportResponse, error) {
	return s.fn(ctx, r)
}
func connection(t *testing.T, fn func(context.Context, *pb.ReportRequest) (*pb.ReportResponse, error)) *Client {
	t.Helper()
	l := bufconn.Listen(1 << 20)
	g := grpc.NewServer()
	pb.RegisterIngestServer(g, &serverFunc{fn: fn})
	go g.Serve(l)
	t.Cleanup(func() { g.Stop(); l.Close() })
	c, err := New(Config{ProbeID: "p", Endpoint: "127.0.0.1:50051", DevelopmentCleartext: true, RPCTimeout: 50 * time.Millisecond, Dialer: func(context.Context, string) (net.Conn, error) { return l.Dial() }})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { c.Close() })
	return c
}
func data() []result.Result {
	return []result.Result{{TargetID: "t", ProbeID: "p", Kind: result.HTTP3, Family: "v6", Addr: "::1", StartedAt: time.Date(2026, 10, 23, 0, 0, 0, 17, time.UTC), Protocol: "h3", TLSVersion: 0x304, TLSResumed: true, Err: "remote error", Phases: map[string]time.Duration{"dns": 3, "connect": 5, "tls_handshake": 7, "ttfb": 11, "total": 13}, Hops: []result.Hop{{TTL: 1, Addr: "127.0.0.1", RTT: 19}, {TTL: 2}}}, {TargetID: "other", ProbeID: "p", Kind: result.TCP, Addr: "127.0.0.1", Family: "v4", StartedAt: time.Date(2026, 10, 23, 0, 0, 1, 0, time.UTC), Phases: map[string]time.Duration{"resolve": 23, "connect": 29}}}
}
func queue(t *testing.T) *spool.Spool {
	t.Helper()
	s, e := spool.Open(t.TempDir(), 1<<20)
	if e != nil {
		t.Fatal(e)
	}
	t.Cleanup(func() { s.Close() })
	if e = s.Append(data()); e != nil {
		t.Fatal(e)
	}
	return s
}
func accepted(r *pb.ReportRequest) *pb.ReportResponse {
	return &pb.ReportResponse{BatchId: r.BatchId, Status: pb.AckStatus_ACK_STATUS_ACCEPTED, CommittedResultCount: uint32(len(r.Results))}
}
func TestReport_NoDuplicateAfterAckLoss(t *testing.T) {
	s := queue(t)
	id, _, _, _ := s.Next()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	var first *pb.ReportRequest
	var calls int
	c := connection(t, func(_ context.Context, r *pb.ReportRequest) (*pb.ReportResponse, error) {
		calls++
		if calls == 1 {
			first = proto.Clone(r).(*pb.ReportRequest)
			return nil, status.Error(codes.Unavailable, "response lost after commit")
		}
		if r.BatchId != first.BatchId || r.BatchId != id || !reflect.DeepEqual(r.Results, first.Results) {
			t.Error("retry changed committed content")
		}
		if !r.SentAt.AsTime().After(first.SentAt.AsTime()) {
			t.Error("sent_at not fresh")
		}
		ack := accepted(r)
		ack.Status = pb.AckStatus_ACK_STATUS_DUPLICATE
		return ack, nil
	})
	var tick atomic.Int64
	c.now = func() time.Time { return time.Unix(100, tick.Add(1)) }
	c.wait = func(ctx context.Context, d time.Duration) error {
		if s.Stats().Batches == 0 {
			cancel()
		}
		return ctx.Err()
	}
	c.random = func() float64 { return 0 }
	if e := c.Run(ctx, s); !errors.Is(e, context.Canceled) {
		t.Fatal(e)
	}
	if calls != 2 || s.Stats().Batches != 0 {
		t.Fatalf("calls=%d stats=%+v", calls, s.Stats())
	}
	r := first.Results[0]
	if r.Protocol != "h3" || r.TlsVersion != 0x304 || !r.TlsResumed || r.Family != "v6" || r.Addr != "::1" || r.Err != "remote error" || r.StartedAt.AsTime() != data()[0].StartedAt || r.PhasesNanoseconds["tls_handshake"] != 7 || r.Hops[0].RttNanoseconds != 19 || r.Hops[1].Ttl != 2 || first.Results[1].TargetId != "other" || first.Results[1].PhasesNanoseconds["resolve"] != 23 {
		t.Fatalf("wire fields lost: %+v", r)
	}
}
func TestReport_InvalidACKAndRestartPreserveBatch(t *testing.T) {
	for _, mode := range []string{"wrong", "partial", "unspecified"} {
		t.Run(mode, func(t *testing.T) {
			s := queue(t)
			id, _, _, _ := s.Next()
			ctx, cancel := context.WithCancel(context.Background())
			c := connection(t, func(_ context.Context, r *pb.ReportRequest) (*pb.ReportResponse, error) {
				ack := accepted(r)
				switch mode {
				case "wrong":
					ack.BatchId = "wrong"
				case "partial":
					ack.CommittedResultCount = 1
				case "unspecified":
					ack.Status = pb.AckStatus_ACK_STATUS_UNSPECIFIED
				}
				return ack, nil
			})
			c.wait = func(context.Context, time.Duration) error { cancel(); return context.Canceled }
			c.Run(ctx, s)
			after, rs, _, e := s.Next()
			if e != nil || id != after || !reflect.DeepEqual(rs, data()) {
				t.Fatal("invalid ACK removed/changed batch")
			}
		})
	}
	// A new process-equivalent owner and new reporter must resend the disk ID.
	dir := t.TempDir()
	s, e := spool.Open(dir, 1<<20)
	if e != nil {
		t.Fatal(e)
	}
	s.Append(data())
	id, _, _, _ := s.Next()
	s.Close()
	s, e = spool.Open(dir, 1<<20)
	if e != nil {
		t.Fatal(e)
	}
	defer s.Close()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	c := connection(t, func(_ context.Context, r *pb.ReportRequest) (*pb.ReportResponse, error) {
		if r.BatchId != id {
			t.Error("restart changed ID")
		}
		return accepted(r), nil
	})
	c.wait = func(context.Context, time.Duration) error { cancel(); return context.Canceled }
	c.Run(ctx, s)
	if s.Stats().Batches != 0 {
		t.Fatal("valid ACK did not remove restarted batch")
	}
}
func TestReport_BackoffSingleInflight(t *testing.T) {
	s := queue(t)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	var active, maxActive atomic.Int32
	var calls atomic.Int32
	c := connection(t, func(ctx context.Context, r *pb.ReportRequest) (*pb.ReportResponse, error) {
		n := active.Add(1)
		defer active.Add(-1)
		for old := maxActive.Load(); n > old && !maxActive.CompareAndSwap(old, n); old = maxActive.Load() {
		}
		calls.Add(1)
		return nil, status.Error(codes.Unavailable, "ingest down")
	})
	var waits []time.Duration
	c.random = func() float64 { return 0.5 }
	c.wait = func(context.Context, time.Duration) error { return nil }
	c.wait = func(_ context.Context, d time.Duration) error {
		waits = append(waits, d)
		if len(waits) == 12 {
			cancel()
			return context.Canceled
		}
		return nil
	}
	if e := c.Run(ctx, s); !errors.Is(e, context.Canceled) {
		t.Fatal(e)
	}
	if maxActive.Load() != 1 || calls.Load() != 12 {
		t.Fatalf("max=%d calls=%d", maxActive.Load(), calls.Load())
	}
	caps := []time.Duration{time.Second, 2 * time.Second, 4 * time.Second, 8 * time.Second, 16 * time.Second, 32 * time.Second, 64 * time.Second, 128 * time.Second, 256 * time.Second, 300 * time.Second, 300 * time.Second, 300 * time.Second}
	for i, w := range waits {
		if w != caps[i]/2 {
			t.Fatalf("wait[%d]=%v cap=%v", i, w, caps[i])
		}
	}
}
func TestReport_DeadlineCancellationAndSingleWorker(t *testing.T) {
	s := queue(t)
	entered := make(chan struct{}, 1)
	var once sync.Once
	c := connection(t, func(ctx context.Context, _ *pb.ReportRequest) (*pb.ReportResponse, error) {
		once.Do(func() { entered <- struct{}{} })
		<-ctx.Done()
		return nil, ctx.Err()
	})
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- c.Run(ctx, s) }()
	<-entered
	if e := c.Run(ctx, s); e == nil {
		t.Fatal("concurrent worker accepted")
	}
	cancel()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("RPC cancellation blocked")
	}
	ctx, cancel = context.WithTimeout(context.Background(), 150*time.Millisecond)
	defer cancel()
	start := time.Now()
	if e := c.Run(ctx, s); !errors.Is(e, context.DeadlineExceeded) {
		t.Fatal(e)
	}
	if time.Since(start) > time.Second {
		t.Fatal("unavailable ingest not bounded")
	}
	if s.Stats().Batches != 1 {
		t.Fatal("deadline deleted batch")
	}
}
func TestReport_BackoffResetsAfterSuccess(t *testing.T) {
	s := queue(t)
	s.Append(data())
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	var calls int
	c := connection(t, func(_ context.Context, r *pb.ReportRequest) (*pb.ReportResponse, error) {
		calls++
		if calls == 3 {
			return accepted(r), nil
		}
		return nil, status.Error(codes.Unavailable, "down")
	})
	var waits []time.Duration
	c.random = func() float64 { return 1 }
	c.wait = func(_ context.Context, d time.Duration) error {
		waits = append(waits, d)
		if len(waits) == 3 {
			cancel()
			return context.Canceled
		}
		return nil
	}
	c.Run(ctx, s)
	if !reflect.DeepEqual(waits, []time.Duration{time.Second, 2 * time.Second, time.Second}) || s.Stats().Batches != 1 {
		t.Fatalf("waits=%v %+v", waits, s.Stats())
	}
}
func TestReport_TLSDefaultsAndCleartextRestrictions(t *testing.T) {
	for _, ep := range []string{"example.com:443", "0.0.0.0:1234", "192.0.2.1:443", "localhost:1234", "dns:///127.0.0.1:1234"} {
		if c, e := New(Config{ProbeID: "p", Endpoint: ep, DevelopmentCleartext: true}); e == nil {
			c.Close()
			t.Fatalf("cleartext accepted %s", ep)
		}
	}
	// A real TLS gRPC listener proves trusted CA+hostname verification, and
	// default roots must reject the exact same self-signed server.
	certServer := httptest.NewTLSServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
	cert := certServer.TLS.Certificates[0]
	certServer.Close()
	l, e := net.Listen("tcp", "127.0.0.1:0")
	if e != nil {
		t.Fatal(e)
	}
	g := grpc.NewServer(grpc.Creds(credentials.NewTLS(&tls.Config{Certificates: []tls.Certificate{cert}, MinVersion: tls.VersionTLS12})))
	pb.RegisterIngestServer(g, &serverFunc{fn: func(_ context.Context, r *pb.ReportRequest) (*pb.ReportResponse, error) { return accepted(r), nil }})
	go g.Serve(l)
	defer g.Stop()
	pem := httptestCA(t, cert)
	path := filepath.Join(t.TempDir(), "ca.pem")
	os.WriteFile(path, pem, 0600)
	for _, mode := range []string{"default-roots", "trusted", "wrong-hostname"} {
		cfg := Config{ProbeID: "p", Endpoint: l.Addr().String(), RPCTimeout: 100 * time.Millisecond}
		if mode != "default-roots" {
			cfg.CAFile = path
		}
		if mode == "wrong-hostname" {
			cfg.Endpoint = net.JoinHostPort("127.0.0.2", strconv.Itoa(l.Addr().(*net.TCPAddr).Port))
			cfg.Dialer = func(ctx context.Context, _ string) (net.Conn, error) {
				return (&net.Dialer{}).DialContext(ctx, "tcp", l.Addr().String())
			}
		}
		c, e := New(cfg)
		if e != nil {
			t.Fatal(e)
		}
		s := queue(t)
		ctx, cancel := context.WithTimeout(context.Background(), 200*time.Millisecond)
		c.Run(ctx, s)
		cancel()
		c.Close()
		if (s.Stats().Batches == 0) != (mode == "trusted") {
			t.Fatalf("TLS mode=%v queue=%+v", mode, s.Stats())
		}
	}
}
func httptestCA(t *testing.T, cert tls.Certificate) []byte {
	t.Helper()
	parsed, e := x509.ParseCertificate(cert.Certificate[0])
	if e != nil {
		t.Fatal(e)
	}
	return pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: parsed.Raw})
}
