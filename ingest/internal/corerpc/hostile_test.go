package corerpc

import (
	"bytes"
	"context"
	"crypto/tls"
	"fmt"
	"net"
	"os"
	"runtime"
	"strings"
	"testing"
	"time"

	"golang.org/x/net/http2"
	"golang.org/x/net/http2/hpack"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"pathwatch.local/pathwatch/ingest/internal/report"
	"pathwatch.local/pathwatch/ingest/internal/server"
	cp "pathwatch.local/pathwatch/proto/pathwatch/core/v1"
	pb "pathwatch.local/pathwatch/proto/pathwatch/v1"
)

func rawHeaders(t *testing.T, fr *http2.Framer, id uint32, compression string, public ...bool) {
	t.Helper()
	var b bytes.Buffer
	e := hpack.NewEncoder(&b)
	method := CorePrefix + "Report"
	if len(public) > 0 && public[0] {
		method = "/pathwatch.v1.Ingest/Report"
	}
	for _, v := range [][2]string{{":method", "POST"}, {":scheme", "https"}, {":path", method}, {":authority", "core.test"}, {"content-type", "application/grpc"}, {"te", "trailers"}, {"pathwatch-core-major", "1"}, {"pathwatch-core-minor", "0"}} {
		if err := e.WriteField(hpack.HeaderField{Name: v[0], Value: v[1]}); err != nil {
			t.Fatal(err)
		}
	}
	if compression != "" {
		e.WriteField(hpack.HeaderField{Name: "grpc-encoding", Value: compression})
	}
	if err := fr.WriteHeaders(http2.HeadersFrameParam{StreamID: id, BlockFragment: b.Bytes(), EndHeaders: true}); err != nil {
		t.Fatal(err)
	}
}

func rawTLS(t *testing.T, target string, p *testPKI) (*tls.Conn, *http2.Framer) {
	t.Helper()
	tr, err := LoadTrust(p.cert(t, "spiffe://pathwatch.local/frontend/stable", false, time.Now().Add(time.Hour)), false)
	if err != nil {
		t.Fatal(err)
	}
	tr.config.NextProtos = []string{"h2"}
	c, err := tls.DialWithDialer(&net.Dialer{Timeout: time.Second}, "tcp", target, tr.config)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { c.Close() })
	c.SetDeadline(time.Now().Add(5 * time.Second))
	if _, err = c.Write([]byte(http2.ClientPreface)); err != nil {
		t.Fatal(err)
	}
	fr := http2.NewFramer(c, c)
	if err = fr.WriteSettings(); err != nil {
		t.Fatal(err)
	}
	fr.ReadMetaHeaders = hpack.NewDecoder(4096, nil)
	return c, fr
}

func grpcTrailer(t *testing.T, fr *http2.Framer, want string) {
	t.Helper()
	for {
		f, err := fr.ReadFrame()
		if err != nil {
			t.Fatal(err)
		}
		if h, ok := f.(*http2.MetaHeadersFrame); ok {
			for _, v := range h.Fields {
				if v.Name == "grpc-status" {
					if v.Value != want {
						t.Fatalf("status=%s want=%s", v.Value, want)
					}
					return
				}
			}
		}
	}
}

func TestPlaintextCompressionAndPartialReceiveRejected(t *testing.T) {
	f := &fakeStore{}
	target, p, a := startCore(t, f)
	cc, err := grpc.NewClient(target, grpc.WithTransportCredentials(insecure.NewCredentials()), grpc.WithNoProxy(), grpc.WithDisableRetry())
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	_, err = cp.NewCoreClient(cc).Describe(ctx, &cp.DescribeRequest{})
	cancel()
	cc.Close()
	if err == nil {
		t.Fatal("plaintext accepted")
	}
	c, fr := rawTLS(t, target, p)
	rawHeaders(t, fr, 1, "gzip")
	grpcTrailer(t, fr, "12")
	c.Close()
	for _, framed := range []bool{false, true} {
		for _, reset := range []bool{false, true} {
			c, fr := rawTLS(t, target, p)
			rawHeaders(t, fr, 1, "")
			// Only four of the five gRPC prefix bytes: no message can decode.
			body := []byte{0, 1, 0, 0}
			if framed {
				body = []byte{0, 0, 1, 0, 0, 34, 0}
			}
			if err := fr.WriteData(1, false, body); err != nil {
				t.Fatal(err)
			}
			await(t, func() bool { return len(a.reports) == 1 })
			a.mu.Lock()
			n := a.bytes
			a.mu.Unlock()
			if n != 0 {
				t.Fatal("partial prefix decoded", n)
			}
			if reset {
				if err := fr.WriteRSTStream(1, http2.ErrCodeCancel); err != nil {
					t.Fatal(err)
				}
			} else {
				c.Close()
			}
			await(t, func() bool { return len(a.reports) == 0 })
			c.Close()
		}
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.commits != 0 {
		t.Fatal("transport rejection invoked handler")
	}
}

type unusedPublicReport struct{ pb.UnimplementedIngestServer }

func (unusedPublicReport) Report(context.Context, *pb.ReportRequest) (*pb.ReportResponse, error) {
	panic("HEADERS-only must not invoke public handler")
}

func TestMaxConnectionsAndHeadersBeforeDecode(t *testing.T) {
	if testing.Short() {
		t.Skip("opens many TLS connections; skipped with -short")
	}
	for _, private := range []bool{true, false} {
		t.Run(fmt.Sprint("private=", private), func(t *testing.T) {
			p := pki(t)
			files := p.cert(t, "core.test", true, time.Now().Add(time.Hour))
			tr, err := LoadTrust(files, true)
			if err != nil {
				t.Fatal(err)
			}
			a := NewAdmission(private, nil, nil, nil)
			n, streams, slots := 16, 2, 4
			var opts []grpc.ServerOption
			if private {
				n, streams, slots = 10, 4, 32
				a.authenticate = tr.Authenticate
				opts = a.Options(tr.Credentials())
			} else {
				opts, err = server.Options(server.TransportConfig{Listen: "127.0.0.1:1", CertFile: files.Cert, KeyFile: files.Key})
				if err != nil {
					t.Fatal(err)
				}
				opts = append(opts, a.Options(nil)...)
			}
			g := grpc.NewServer(opts...)
			if private {
				Register(g, &cp.Core_ServiceDesc, &Service{Reporter: &fakeStore{}})
			} else {
				Register(g, &pb.Ingest_ServiceDesc, unusedPublicReport{})
			}
			listener, err := net.Listen("tcp", "127.0.0.1:0")
			if err != nil {
				t.Fatal(err)
			}
			capped := LimitListener(listener, n).(*cappedListener)
			done := make(chan struct{})
			go func() { defer close(done); g.Serve(capped) }()
			t.Cleanup(func() { a.Stop(); g.Stop(); <-done; a.Wait() })
			target := listener.Addr().String()
			var conns []*tls.Conn
			var frames []*http2.Framer
			for i := 0; i < n; i++ {
				c, fr := rawTLS(t, target, p)
				conns = append(conns, c)
				frames = append(frames, fr)
			}
			await(t, func() bool { return len(capped.slots) == n })
			for i, fr := range frames {
				for j := 0; j < streams; j++ {
					rawHeaders(t, fr, uint32(j*2+1), "", !private)
				}
				if i < slots/streams {
					count := (i + 1) * streams
					await(t, func() bool { return len(a.reports) == count })
				}
			}
			for _, fr := range frames[slots/streams:] {
				for i := 0; i < streams; i++ {
					grpcTrailer(t, fr, "8")
				}
			}
			a.mu.Lock()
			charge := a.bytes
			a.mu.Unlock()
			if len(a.reports) != slots || charge != 0 {
				t.Fatal(len(a.reports), charge)
			}
			excessID := uint32(2*streams + 1)
			rawHeaders(t, frames[0], excessID, "", !private)
			for {
				v, err := frames[0].ReadFrame()
				if err != nil {
					t.Fatal(err)
				}
				if r, ok := v.(*http2.RSTStreamFrame); ok && r.StreamID == excessID {
					if r.ErrCode != http2.ErrCodeRefusedStream {
						t.Fatal(r.ErrCode)
					}
					break
				}
			}
			clientTrust, err := LoadTrust(p.cert(t, "spiffe://pathwatch.local/frontend/stable", false, time.Now().Add(time.Hour)), false)
			if err != nil {
				t.Fatal(err)
			}
			clientTrust.config.NextProtos = []string{"h2"}
			extra, err := tls.DialWithDialer(&net.Dialer{Timeout: 200 * time.Millisecond}, "tcp", target, clientTrust.config)
			if err == nil {
				extra.Close()
				t.Fatal("excess TLS connection accepted")
			}
			for _, c := range conns {
				c.Close()
			}
			await(t, func() bool { return len(a.reports) == 0 && len(a.reads) == 0 && len(capped.slots) == 0 })
			a.mu.Lock()
			defer a.mu.Unlock()
			if a.bytes != 0 {
				t.Fatal(a.bytes)
			}
			t.Logf("TLS connections=%d streams/connection=%d active leases=%d overloads=%d excess stream/connection refused; zero charge/connection/call permits", n, streams, slots, n*streams-slots)
		})
	}
}

func TestSequentialLimitWireAllocation(t *testing.T) {
	if testing.Short() {
		t.Skip("allocates max-size messages; skipped with -short")
	}
	f := &fakeStore{}
	target, p, a := startCore(t, f)
	c := connect(t, target, p)
	for _, kind := range []string{"valid", "malformed", "empty-submessages"} {
		t.Run(kind, func(t *testing.T) {
			runtime.GC()
			var before, after runtime.MemStats
			runtime.ReadMemStats(&before)
			var wire []byte
			switch kind {
			case "valid":
				r := validReport()
				head, err := proto.Marshal(r)
				if err != nil {
					t.Fatal(err)
				}
				wire = append(head, sizedUnknown(report.MaxMessageBytes-len(head))...)
			case "malformed":
				wire = make([]byte, report.MaxMessageBytes)
			case "empty-submessages":
				wire = bytes.Repeat([]byte{34, 0}, report.MaxMessageBytes/2)
			}
			ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
			defer cancel()
			var reply pb.ReportResponse
			var activeCharge int64
			if kind == "valid" {
				f.mu.Lock()
				f.block = make(chan struct{})
				f.entered = make(chan struct{}, 1)
				f.mu.Unlock()
			}
			answer := make(chan error, 1)
			go func() {
				answer <- c.conn.Invoke(ctx, CorePrefix+"Report", wire, &reply, grpc.ForceCodec(bytesCodec{}), grpc.MaxCallSendMsgSize(report.MaxMessageBytes))
			}()
			if kind == "valid" {
				select {
				case <-f.entered:
				case <-ctx.Done():
					t.Fatal(ctx.Err())
				}
				a.mu.Lock()
				activeCharge = a.bytes
				a.mu.Unlock()
				want, e := report.Charge(wire)
				if e != nil || activeCharge != want {
					t.Fatal("active decode charge", activeCharge, want, e)
				}
				close(f.block)
			}
			err := <-answer
			if kind == "valid" {
				if err != nil || reply.Status != pb.AckStatus_ACK_STATUS_ACCEPTED {
					t.Fatal(err, &reply)
				}
			} else if status.Code(err) != codes.InvalidArgument {
				t.Fatal("malformed wire accepted/wrong bounded failure", err)
			}
			await(t, func() bool { return len(a.reports) == 0 })
			a.mu.Lock()
			charge := a.bytes
			a.mu.Unlock()
			if charge != 0 {
				t.Fatal(charge)
			}
			runtime.ReadMemStats(&after)
			t.Logf("wire=%d total_alloc_delta=%d heap_inuse=%d active_server_charge=%d residual_charge=%d", len(wire), after.TotalAlloc-before.TotalAlloc, after.HeapInuse, activeCharge, charge)
			if after.TotalAlloc-before.TotalAlloc > 512<<20 {
				t.Fatal("sequential transport allocation forecast exceeded")
			}
			if b, err := os.ReadFile("/proc/self/status"); err == nil {
				for _, line := range strings.Split(string(b), "\n") {
					if strings.HasPrefix(line, "VmHWM:") || strings.HasPrefix(line, "VmRSS:") {
						t.Log(line)
					}
				}
			}
		})
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.commits != 1 {
		t.Fatal("invalid payload reached commit", f.commits)
	}
}
