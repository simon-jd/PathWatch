package corerpc

import (
	"bytes"
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"golang.org/x/net/http2"
	"golang.org/x/net/http2/hpack"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
	"math/big"
	"net"
	"net/url"
	"os"
	"path/filepath"
	"pathwatch.local/pathwatch/ingest/internal/report"
	"pathwatch.local/pathwatch/ingest/internal/store"
	cp "pathwatch.local/pathwatch/proto/pathwatch/core/v1"
	pb "pathwatch.local/pathwatch/proto/pathwatch/v1"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

type testPKI struct {
	dir    string
	ca     *x509.Certificate
	key    *ecdsa.PrivateKey
	caFile string
}

func pki(t *testing.T) *testPKI {
	t.Helper()
	dir, e := os.MkdirTemp(os.Getenv("TMPDIR"), "pki-")
	if e != nil {
		t.Fatal(e)
	} // Private keys retained in ignored project TMPDIR; no PEM in public evidence.
	k, e := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if e != nil {
		t.Fatal(e)
	}
	c := &x509.Certificate{SerialNumber: big.NewInt(time.Now().UnixNano()), Subject: pkix.Name{CommonName: "owned-test-ca"}, NotBefore: time.Now().Add(-time.Hour), NotAfter: time.Now().Add(time.Hour), IsCA: true, BasicConstraintsValid: true, KeyUsage: x509.KeyUsageCertSign | x509.KeyUsageDigitalSignature}
	b, e := x509.CreateCertificate(rand.Reader, c, c, &k.PublicKey, k)
	if e != nil {
		t.Fatal(e)
	}
	path := filepath.Join(dir, "ca.pem")
	os.WriteFile(path, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: b}), 0600)
	return &testPKI{dir, c, k, path}
}
func (p *testPKI) cert(t *testing.T, name string, server bool, until time.Time) TLSFiles {
	t.Helper()
	return p.certAt(t, name, server, time.Now().Add(-time.Hour), until)
}
func (p *testPKI) certAt(t *testing.T, name string, server bool, before, until time.Time) TLSFiles {
	t.Helper()
	k, e := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if e != nil {
		t.Fatal(e)
	}
	c := &x509.Certificate{SerialNumber: big.NewInt(time.Now().UnixNano()), NotBefore: before, NotAfter: until, KeyUsage: x509.KeyUsageDigitalSignature}
	if server {
		c.DNSNames = []string{name}
		c.ExtKeyUsage = []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}
	} else {
		u, _ := url.Parse(name)
		c.URIs = []*url.URL{u}
		c.ExtKeyUsage = []x509.ExtKeyUsage{x509.ExtKeyUsageClientAuth}
	}
	b, e := x509.CreateCertificate(rand.Reader, c, p.ca, &k.PublicKey, p.key)
	if e != nil {
		t.Fatal(e)
	}
	key, e := x509.MarshalECPrivateKey(k)
	if e != nil {
		t.Fatal(e)
	}
	id := big.NewInt(time.Now().UnixNano()).String()
	certPath, keyPath := filepath.Join(p.dir, id+".pem"), filepath.Join(p.dir, id+".key")
	os.WriteFile(certPath, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: b}), 0600)
	os.WriteFile(keyPath, pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: key}), 0600)
	return TLSFiles{CA: p.caFile, Cert: certPath, Key: keyPath, ServerName: "core.test", Frontends: []string{"spiffe://pathwatch.local/frontend/stable", "spiffe://pathwatch.local/frontend/candidate"}}
}

type fakeStore struct {
	mu      sync.Mutex
	commits int
	data    map[string][]byte
	block   chan struct{}
	entered chan struct{}
	failure error
}

func (f *fakeStore) Report(ctx context.Context, r *pb.ReportRequest) (*pb.ReportResponse, error) {
	f.mu.Lock()
	block, entered := f.block, f.entered
	f.mu.Unlock()
	if e := report.Check(r, time.Now()); e != nil {
		return nil, e
	}
	if block != nil {
		if entered != nil {
			select {
			case entered <- struct{}{}:
			default:
			}
		}
		select {
		case <-ctx.Done():
			return nil, report.Unavailable()
		case <-block:
		}
	}
	x := proto.Clone(r).(*pb.ReportRequest)
	x.SentAt = nil
	b, _ := proto.MarshalOptions{Deterministic: true}.Marshal(x)
	key := r.ProbeId + "/" + r.BatchId
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.data == nil {
		f.data = map[string][]byte{}
	}
	ack := pb.AckStatus_ACK_STATUS_ACCEPTED
	if old, ok := f.data[key]; ok {
		if !bytes.Equal(old, b) {
			return nil, report.Error(codes.AlreadyExists, "conflict", "batch key has different ordered content")
		}
		ack = pb.AckStatus_ACK_STATUS_DUPLICATE
	} else {
		f.data[key] = b
		f.commits++
	}
	return &pb.ReportResponse{BatchId: r.BatchId, Status: ack, CommittedResultCount: uint32(len(r.Results))}, nil
}
func (f *fakeStore) ReadRaw(ctx context.Context, b store.QueryBounds) ([]store.RawResult, error) {
	f.mu.Lock()
	failure, block, entered := f.failure, f.block, f.entered
	f.mu.Unlock()
	if failure != nil {
		return nil, failure
	}
	if block != nil {
		if entered != nil {
			select {
			case entered <- struct{}{}:
			default:
			}
		}
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-block:
		}
	}
	return []store.RawResult{}, nil
}
func (f *fakeStore) ReadQuantiles(context.Context, store.QueryBounds) ([]store.Quantile, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return []store.Quantile{}, f.failure
}
func (f *fakeStore) ReadEvents(context.Context, store.QueryBounds) ([]store.Event, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return []store.Event{}, f.failure
}
func validReport() *pb.ReportRequest {
	now := time.Now()
	return &pb.ReportRequest{ProbeId: "probe", BatchId: "batch", SentAt: timestamppb.New(now), Results: []*pb.ProbeResult{{ProbeId: "probe", TargetId: "target", Kind: "dns", StartedAt: timestamppb.New(now), Err: "test no address"}}}
}
func startCore(t *testing.T, f *fakeStore, configure ...func(*Admission)) (string, *testPKI, *Admission) {
	t.Helper()
	p := pki(t)
	files := p.cert(t, "core.test", true, time.Now().Add(time.Hour))
	trust, e := LoadTrust(files, true)
	if e != nil {
		t.Fatal(e)
	}
	a := NewAdmission(true, trust.Authenticate, func() error { return report.Error(codes.ResourceExhausted, "overloaded", "ingest queue full") }, nil)
	for _, configureAdmission := range configure {
		configureAdmission(a)
	}
	g := grpc.NewServer(a.Options(trust.Credentials())...)
	Register(g, &cp.Core_ServiceDesc, &Service{Reporter: f, Reader: f, Ready: func(context.Context) error { return nil }})
	l, e := net.Listen("tcp", "127.0.0.1:0")
	if e != nil {
		t.Fatal(e)
	}
	done := make(chan struct{})
	go func() { defer close(done); g.Serve(LimitListener(l, 10)) }()
	t.Cleanup(func() { a.Stop(); g.Stop(); <-done; a.Wait() })
	return l.Addr().String(), p, a
}
func connect(t *testing.T, target string, p *testPKI) *Client {
	t.Helper()
	f := p.cert(t, "spiffe://pathwatch.local/frontend/stable", false, time.Now().Add(time.Hour))
	tr, e := LoadTrust(f, false)
	if e != nil {
		t.Fatal(e)
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	c, e := Dial(ctx, target, tr)
	if e != nil {
		t.Fatal(e)
	}
	t.Cleanup(func() { c.Close() })
	return c
}
func TestStrictTLSAndTypedRoundtrip(t *testing.T) {
	f := &fakeStore{}
	target, p, _ := startCore(t, f)
	c := connect(t, target, p)
	r := validReport()
	r.ProtoReflect().SetUnknown([]byte{0xa0, 6, 1})
	ack, e := c.Report(context.Background(), r)
	if e != nil || ack.Status != pb.AckStatus_ACK_STATUS_ACCEPTED {
		t.Fatal(ack, e)
	}
	ack, e = c.Report(context.Background(), r)
	if e != nil || ack.Status != pb.AckStatus_ACK_STATUS_DUPLICATE {
		t.Fatal(ack, e)
	}
	r.Results[0].Err = "changed"
	if _, e = c.Report(context.Background(), r); status.Code(e) != codes.AlreadyExists {
		t.Fatal(e)
	}
	b := store.QueryBounds{PathKey: "p", From: time.Unix(0, 0), To: time.Unix(300, 0), Limit: 1}
	if _, e = c.ReadRaw(context.Background(), b); e != nil {
		t.Fatal(e)
	}
	if _, e = c.ReadQuantiles(context.Background(), b); e != nil {
		t.Fatal(e)
	}
	if _, e = c.ReadEvents(context.Background(), b); e != nil {
		t.Fatal(e)
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.commits != 1 {
		t.Fatal(f.commits)
	}
}

// Preserve exact server-side TLS handshake evidence when grpc exposes only a
// broken pipe/EOF from the asynchronously rejected connection.
type observedServerCredentials struct {
	credentials.TransportCredentials
	failures chan error
}

func (o *observedServerCredentials) ServerHandshake(c net.Conn) (net.Conn, credentials.AuthInfo, error) {
	conn, info, err := o.TransportCredentials.ServerHandshake(c)
	if err != nil {
		select {
		case o.failures <- err:
		default:
		}
	}
	return conn, info, err
}
func (o *observedServerCredentials) Clone() credentials.TransportCredentials {
	return &observedServerCredentials{o.TransportCredentials.Clone(), o.failures}
}

func TestTLSRejectsWrongCANameRoleMissingExpired(t *testing.T) {
	p := pki(t)
	trust, err := LoadTrust(p.cert(t, "core.test", true, time.Now().Add(time.Hour)), true)
	if err != nil {
		t.Fatal(err)
	}
	// Role negative-control overlay inserts an extra allowlist entry here only.
	var invoked atomic.Int64
	handshakes := &observedServerCredentials{trust.Credentials(), make(chan error, 64)}
	target := startTypedFixture(t, p, trust, &Service{Ready: func(context.Context) error { invoked.Add(1); return nil }}, handshakes)
	other := pki(t)
	for _, kind := range []string{"valid-before", "wrong-ca", "wrong-client-ca", "wrong-purpose", "not-yet-valid", "wrong-san", "wrong-role", "unknown-frontend", "missing", "expired", "valid-after"} {
		t.Run(kind, func(t *testing.T) {
			f := p.cert(t, "spiffe://pathwatch.local/frontend/stable", false, time.Now().Add(time.Hour))
			if kind == "wrong-client-ca" {
				f = other.cert(t, "spiffe://pathwatch.local/frontend/stable", false, time.Now().Add(time.Hour))
				f.CA = p.caFile
			}
			if kind == "wrong-purpose" {
				f = p.cert(t, "core.test", true, time.Now().Add(time.Hour))
			}
			if kind == "not-yet-valid" {
				f = p.certAt(t, "spiffe://pathwatch.local/frontend/stable", false, time.Now().Add(time.Minute), time.Now().Add(time.Hour))
			}
			if kind == "wrong-role" {
				f = p.cert(t, "spiffe://pathwatch.local/not-frontend", false, time.Now().Add(time.Hour))
			}
			if kind == "unknown-frontend" {
				f = p.cert(t, "spiffe://pathwatch.local/frontend/unknown", false, time.Now().Add(time.Hour))
			}
			if kind == "expired" {
				f = p.cert(t, "spiffe://pathwatch.local/frontend/stable", false, time.Now().Add(-time.Second))
			}
			ca, _ := os.ReadFile(f.CA)
			if kind == "wrong-ca" {
				ca, _ = os.ReadFile(other.caFile)
			}
			roots := x509.NewCertPool()
			roots.AppendCertsFromPEM(ca)
			cert, _ := tls.LoadX509KeyPair(f.Cert, f.Key)
			cfg := &tls.Config{MinVersion: tls.VersionTLS13, RootCAs: roots, ServerName: "core.test", Certificates: []tls.Certificate{cert}}
			if kind == "wrong-san" {
				cfg.ServerName = "wrong.test"
			}
			if kind == "missing" {
				cfg.Certificates = nil
			}
			cc, e := grpc.NewClient("passthrough:///"+target, grpc.WithTransportCredentials(credentials.NewTLS(cfg)), grpc.WithNoProxy(), grpc.WithDisableRetry())
			if e != nil {
				t.Fatal(e)
			}
			defer cc.Close()
			// Every certificate case uses the identical raw client, valid protocol
			// metadata and otherwise valid Describe. Protocol rejection cannot mask
			// missing TLS role enforcement.
			ctx, cancel := context.WithTimeout(metadata.NewOutgoingContext(context.Background(), metadata.Pairs("pathwatch-core-major", "1", "pathwatch-core-minor", "0")), time.Second)
			defer cancel()
			for len(handshakes.failures) > 0 {
				<-handshakes.failures
			}
			before := invoked.Load()
			reply, callErr := cp.NewCoreClient(cc).Describe(ctx, &cp.DescribeRequest{})
			if strings.HasPrefix(kind, "valid-") {
				if callErr != nil || reply == nil || !reply.Ready || invoked.Load() != before+1 {
					t.Fatalf("same-path positive control: %v %v calls=%d", reply, callErr, invoked.Load()-before)
				}
				return
			}
			code := status.Code(callErr)
			if callErr == nil || reply != nil || (code != codes.Unavailable && code != codes.Unauthenticated) || strings.Contains(status.Convert(callErr).Message(), "protocol_mismatch") {
				t.Fatalf("expected transport/auth rejection, got reply=%v err=%v", reply, callErr)
			}
			// TLS handshake errors are wrapped by grpc as Unavailable. Require
			// transport evidence rather than accepting an unrelated service error.
			var serverErr error
			if code == codes.Unavailable && !strings.Contains(callErr.Error(), "authentication handshake failed") && !strings.Contains(callErr.Error(), "tls:") {
				select {
				case serverErr = <-handshakes.failures:
				case <-ctx.Done():
					t.Fatal("missing server handshake evidence", callErr)
				}
				if !strings.Contains(serverErr.Error(), "certificate") && !strings.Contains(serverErr.Error(), "unauthorized frontend identity") {
					t.Fatalf("not an authentication rejection: client=%v server=%v", callErr, serverErr)
				}
			}
			if invoked.Load() != before {
				t.Fatalf("unauthorized handler invoked %d times", invoked.Load()-before)
			}
			t.Logf("valid protocol metadata; handler delta=0; client=%v; server-handshake=%v", callErr, serverErr)
		})
	}
}
func TestRemoteQueryErrorsAndGlobalReads(t *testing.T) {
	f := &fakeStore{}
	target, p, a := startCore(t, f)
	c1, c2 := connect(t, target, p), connect(t, target, p)
	b := store.QueryBounds{PathKey: "p", From: time.Unix(0, 0), To: time.Unix(300, 0), Limit: 1}
	for _, e := range []error{store.ErrQueryInvalid, store.ErrQueryLimit, store.ErrQueryUnavailable, store.ErrQueryResponse, store.ErrQueryOverloaded, context.DeadlineExceeded} {
		f.mu.Lock()
		f.failure = e
		f.mu.Unlock()
		if _, got := c1.ReadRaw(context.Background(), b); got != e {
			t.Fatalf("got=%v want=%v", got, e)
		}
	}
	f.mu.Lock()
	f.failure = nil
	f.block = make(chan struct{})
	f.entered = make(chan struct{}, 4)
	f.mu.Unlock()
	done := make(chan error, 4)
	for i := 0; i < 4; i++ {
		c := c1
		if i%2 == 1 {
			c = c2
		}
		go func() { _, e := c.ReadRaw(context.Background(), b); done <- e }()
	}
	for i := 0; i < 4; i++ {
		select {
		case <-f.entered:
		case <-time.After(time.Second):
			t.Fatal("read admission not shared")
		}
	} // Same connection max4 streams allows a fifth on the other client.
	if _, e := c2.ReadRaw(context.Background(), b); e != store.ErrQueryOverloaded {
		t.Fatal(e)
	}
	close(f.block)
	for i := 0; i < 4; i++ {
		if e := <-done; e != nil {
			t.Fatal(e)
		}
	}
	await(t, func() bool { return len(a.reads) == 0 })
}
func TestShortDeadlineAndEstablishedCertificateExpiry(t *testing.T) {
	f := &fakeStore{block: make(chan struct{})}
	target, p, a := startCore(t, f)
	files := p.cert(t, "spiffe://pathwatch.local/frontend/stable", false, time.Now().Add(3*time.Second))
	trust, e := LoadTrust(files, false)
	if e != nil {
		t.Fatal(e)
	}
	c, e := Dial(context.Background(), target, trust)
	if e != nil {
		t.Fatal(e)
	}
	defer c.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
	defer cancel()
	if _, e = c.Report(ctx, validReport()); e == nil {
		t.Fatal("deadline ignored")
	}
	await(t, func() bool { return len(a.reports) == 0 })
	time.Sleep(time.Until(trust.leaf.NotAfter) + time.Millisecond)
	if e = c.Ready(context.Background()); e == nil {
		t.Fatal("expired established identity accepted")
	}
}
func TestReplyPresenceAndWireBound(t *testing.T) {
	r := &cp.RawRow{BatchKey: "key", ProbeId: "p", BatchId: "b", TargetId: "t", Kind: "dns", PathKey: "p→t/dns/", Err: "remote failure", StartedAt: timestamppb.Now()}
	v, e := rawFromWire(r)
	if e != nil || v.PhasePresent || v.PhaseMS != nil {
		t.Fatal(v, e)
	}
	zero := 0.0
	r.PhaseMs = &zero
	v, e = rawFromWire(r)
	if e != nil || !v.PhasePresent || v.PhaseMS == nil {
		t.Fatal(v, e)
	}
	b := &cp.RawResponse{Rows: []*cp.RawRow{r}}
	r.Err = string(make([]byte, MaxReply))
	if _, e = (codec{}).Marshal(b); e == nil {
		t.Fatal("oversized reply accepted")
	}
	if _, e = rawFromWire(&cp.RawRow{}); e == nil {
		t.Fatal("missing row fields fabricated")
	}
}
func TestGlobalThirtyTwoReportsAcrossConnections(t *testing.T) {
	f := &fakeStore{block: make(chan struct{}), entered: make(chan struct{}, 32)}
	target, p, a := startCore(t, f)
	clients := make([]*Client, 9)
	for i := range clients {
		clients[i] = connect(t, target, p)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 32)
	for i := 0; i < 32; i++ {
		c := clients[i/4]
		go func() { _, e := c.Report(ctx, validReport()); done <- e }()
	}
	for i := 0; i < 32; i++ {
		select {
		case <-f.entered:
		case <-time.After(time.Second):
			t.Fatal("missing core admission", i)
		}
	}
	if len(a.reports) != 32 {
		t.Fatal(len(a.reports))
	}
	if _, e := clients[8].Report(context.Background(), validReport()); status.Code(e) != codes.ResourceExhausted {
		t.Fatal("33rd global Report", e)
	}
	cancel()
	for i := 0; i < 32; i++ {
		if e := <-done; e == nil {
			t.Fatal("canceled report ACK")
		}
	}
	await(t, func() bool { return len(a.reports) == 0 })
	a.mu.Lock()
	charged := a.bytes
	a.mu.Unlock()
	if charged != 0 {
		t.Fatal("decoded charge leaked", charged)
	}
}
func TestEarlyOverloadExactExceptions(t *testing.T) {
	for _, literal := range []string{"overloaded", "ingest queue full"} {
		if !earlyOverload(status.Error(codes.ResourceExhausted, literal), literal) {
			t.Fatal(literal)
		}
		for _, e := range []error{status.Error(codes.Internal, literal), status.Error(codes.ResourceExhausted, literal+" "), status.Error(codes.ResourceExhausted, "received message larger than max"), wireError(codes.ResourceExhausted, literal), wireError(codes.ResourceExhausted, "result_limit"), report.Error(codes.ResourceExhausted, "overloaded", literal)} {
			if earlyOverload(e, literal) {
				t.Fatal("unsafe fallback", e)
			}
		}
	}
	if readError(status.Error(codes.ResourceExhausted, "overloaded")) != store.ErrQueryOverloaded {
		t.Fatal("read overload lost")
	}
	if readError(status.Error(codes.ResourceExhausted, "result_limit")) != store.ErrQueryResponse {
		t.Fatal("missing details accepted")
	}
	if status.Code(ReportError(status.Error(codes.ResourceExhausted, "ingest queue full"))) != codes.Unavailable {
		t.Fatal("fallback escaped verified-client boundary")
	}
	if status.Code(ReportError(report.Error(codes.Unavailable, "unavailable", "SQL secret"))) != codes.Unavailable || status.Convert(ReportError(report.Error(codes.Unavailable, "unavailable", "SQL secret"))).Message() == "SQL secret" {
		t.Fatal("untrusted diagnostic escaped")
	}
}

func TestHeadersOnlyTransportCancellationBeforeDecode(t *testing.T) {
	target, p, a := startCore(t, &fakeStore{})
	files := p.cert(t, "spiffe://pathwatch.local/frontend/stable", false, time.Now().Add(time.Hour))
	tr, e := LoadTrust(files, false)
	if e != nil {
		t.Fatal(e)
	}
	tr.config.NextProtos = []string{"h2"}
	for _, reset := range []bool{false, true} {
		c, e := tls.DialWithDialer(&net.Dialer{Timeout: time.Second}, "tcp", target, tr.config)
		if e != nil {
			t.Fatal(e)
		}
		c.SetDeadline(time.Now().Add(2 * time.Second))
		if _, e = c.Write([]byte(http2.ClientPreface)); e != nil {
			t.Fatal(e)
		}
		fr := http2.NewFramer(c, c)
		fr.WriteSettings()
		var buf bytes.Buffer
		enc := hpack.NewEncoder(&buf)
		for _, v := range [][2]string{{":method", "POST"}, {":scheme", "https"}, {":path", CorePrefix + "Report"}, {":authority", "core.test"}, {"content-type", "application/grpc"}, {"te", "trailers"}, {"pathwatch-core-major", "1"}, {"pathwatch-core-minor", "0"}} {
			enc.WriteField(hpack.HeaderField{Name: v[0], Value: v[1]})
		}
		if e = fr.WriteHeaders(http2.HeadersFrameParam{StreamID: 1, BlockFragment: buf.Bytes(), EndHeaders: true}); e != nil {
			t.Fatal(e)
		}
		await(t, func() bool { return len(a.reports) == 1 })
		a.mu.Lock()
		charged := a.bytes
		a.mu.Unlock()
		if charged != 0 {
			t.Fatal("HEADERS-only stream decoded", charged)
		}
		if reset {
			if e = fr.WriteRSTStream(1, http2.ErrCodeCancel); e != nil {
				t.Fatal(e)
			}
		} else {
			c.Close()
		}
		await(t, func() bool { return len(a.reports) == 0 })
		c.Close()
	}
}
