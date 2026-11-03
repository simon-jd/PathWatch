// This executable belongs only to the finite owned Task14b fixture.
// It is never linked into ingest or deployed as a service.
package main

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/json"
	"encoding/pem"
	"flag"
	"fmt"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
	"math/big"
	"net"
	"net/url"
	"os"
	"os/signal"
	"path/filepath"
	"pathwatch.local/pathwatch/ingest/internal/corerpc"
	"pathwatch.local/pathwatch/ingest/internal/report"
	cp "pathwatch.local/pathwatch/proto/pathwatch/core/v1"
	pb "pathwatch.local/pathwatch/proto/pathwatch/v1"
	"strings"
	"sync"
	"syscall"
	"time"
)

func main() {
	if e := run(); e != nil {
		fmt.Fprintln(os.Stderr, e)
		os.Exit(1)
	}
}
func emit(v any) {
	if e := json.NewEncoder(os.Stdout).Encode(v); e != nil {
		panic(e)
	}
}
func run() error {
	if len(os.Args) < 2 {
		return fmt.Errorf("command required")
	}
	f := flag.NewFlagSet("fixture", flag.ContinueOnError)
	dir := f.String("dir", "", "owned directory")
	target := f.String("target", "", "endpoint(s)")
	batch := f.String("batch", "batch", "batch ID")
	ms := f.Int64("ms", 10, "connect milliseconds")
	sent := f.String("sent-at", "", "fixed original Report timestamp for retry proof")
	at := f.Int64("at", time.Now().Unix(), "measurement seconds")
	listen := f.String("listen", "", "fault relay listener")
	marker := f.String("marker", "", "owned ACK-loss marker")
	if e := f.Parse(os.Args[2:]); e != nil {
		return e
	}
	if *dir == "" {
		return fmt.Errorf("owned directory required")
	}
	switch os.Args[1] {
	case "pki":
		return makePKI(*dir)
	case "report":
		c, close, e := public(*target, *dir)
		if e != nil {
			return e
		}
		defer close()
		r := request(*batch, *ms, *at)
		if *sent != "" {
			v, e := time.Parse(time.RFC3339Nano, *sent)
			if e != nil {
				return e
			}
			r.SentAt = timestamppb.New(v)
		}
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		a, e := c.Report(ctx, r)
		emit(map[string]any{"response": a, "code": status.Code(e).String(), "error": fmt.Sprint(e), "request": r})
		return nil
	case "exercise":
		return exercise(strings.Split(*target, ","), *dir, *at)
	case "ch-relay":
		return chRelay(*target, *listen, *marker)
	case "pending":
		return pending(*dir)
	case "relay":
		return relay(*target, *listen, *dir, *marker)
	default:
		return fmt.Errorf("unknown fixture command")
	}
}
func makePKI(dir string) error {
	if e := os.MkdirAll(dir, 0700); e != nil {
		return e
	}
	key, e := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if e != nil {
		return e
	}
	ca := &x509.Certificate{SerialNumber: big.NewInt(time.Now().UnixNano()), Subject: pkix.Name{CommonName: "task14b-owned-ca"}, NotBefore: time.Now().Add(-time.Minute), NotAfter: time.Now().Add(time.Hour), IsCA: true, BasicConstraintsValid: true, KeyUsage: x509.KeyUsageCertSign | x509.KeyUsageDigitalSignature}
	der, e := x509.CreateCertificate(rand.Reader, ca, ca, &key.PublicKey, key)
	if e != nil {
		return e
	}
	if e = os.WriteFile(filepath.Join(dir, "ca.pem"), pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}), 0600); e != nil {
		return e
	}
	ca, e = x509.ParseCertificate(der)
	if e != nil {
		return e
	}
	for _, name := range []string{"core", "frontend", "query", "stable", "candidate"} {
		k, e := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
		if e != nil {
			return e
		}
		c := &x509.Certificate{SerialNumber: big.NewInt(time.Now().UnixNano()), NotBefore: ca.NotBefore, NotAfter: ca.NotAfter, KeyUsage: x509.KeyUsageDigitalSignature}
		if name == "stable" || name == "candidate" {
			u, _ := url.Parse("spiffe://pathwatch.local/frontend/" + name)
			c.URIs = []*url.URL{u}
			c.ExtKeyUsage = []x509.ExtKeyUsage{x509.ExtKeyUsageClientAuth}
		} else {
			c.DNSNames = []string{name + ".test"}
			c.ExtKeyUsage = []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}
		}
		der, e := x509.CreateCertificate(rand.Reader, c, ca, &k.PublicKey, key)
		if e != nil {
			return e
		}
		b, e := x509.MarshalECPrivateKey(k)
		if e != nil {
			return e
		}
		if e = os.WriteFile(filepath.Join(dir, name+".pem"), pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}), 0600); e != nil {
			return e
		}
		if e = os.WriteFile(filepath.Join(dir, name+".key"), pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: b}), 0600); e != nil {
			return e
		}
	}
	emit(map[string]any{"pki": dir, "expires": ca.NotAfter})
	return nil
}
func trust(dir, role string, server bool) (*corerpc.Trust, error) {
	return corerpc.LoadTrust(corerpc.TLSFiles{CA: filepath.Join(dir, "ca.pem"), Cert: filepath.Join(dir, role+".pem"), Key: filepath.Join(dir, role+".key"), ServerName: "core.test", Frontends: []string{"spiffe://pathwatch.local/frontend/stable", "spiffe://pathwatch.local/frontend/candidate"}}, server)
}
func public(target, dir string) (pb.IngestClient, func(), error) {
	b, e := os.ReadFile(filepath.Join(dir, "ca.pem"))
	if e != nil {
		return nil, nil, e
	}
	roots := x509.NewCertPool()
	if !roots.AppendCertsFromPEM(b) {
		return nil, nil, fmt.Errorf("CA")
	}
	c, e := grpc.NewClient("passthrough:///"+target, grpc.WithTransportCredentials(credentials.NewTLS(&tls.Config{MinVersion: tls.VersionTLS13, RootCAs: roots, ServerName: "frontend.test"})), grpc.WithDisableRetry(), grpc.WithNoProxy())
	if e != nil {
		return nil, nil, e
	}
	return pb.NewIngestClient(c), func() { c.Close() }, nil
}
func request(batch string, ms, at int64) *pb.ReportRequest {
	return &pb.ReportRequest{ProbeId: "home", BatchId: batch, SentAt: timestamppb.Now(), Results: []*pb.ProbeResult{{TargetId: "target", ProbeId: "home", Kind: "tcp", Family: "v4", Addr: "127.0.0.1", StartedAt: timestamppb.New(time.Unix(at, 123)), PhasesNanoseconds: map[string]int64{"connect": ms * 1000000}}}}
}
func exercise(targets []string, dir string, at int64) error {
	if len(targets) != 3 {
		return fmt.Errorf("two frontend targets and core target required")
	}
	var cs []pb.IngestClient
	for _, target := range targets[:2] {
		c, close, e := public(target, dir)
		if e != nil {
			return e
		}
		defer close()
		cs = append(cs, c)
	}
	tr, e := trust(dir, "stable", false)
	if e != nil {
		return e
	}
	core, e := corerpc.Dial(context.Background(), targets[2], tr)
	if e != nil {
		return e
	}
	defer core.Close()
	r := request("concurrent", 10, at)
	r.ProtoReflect().SetUnknown([]byte{0xa0, 6, 1})
	var wg sync.WaitGroup
	acks := make([]*pb.ReportResponse, 2)
	errs := make([]error, 2)
	for i, c := range cs {
		wg.Add(1)
		go func(i int, c pb.IngestClient) {
			defer wg.Done()
			ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer cancel()
			acks[i], errs[i] = c.Report(ctx, r)
		}(i, c)
	}
	wg.Wait()
	counts := map[pb.AckStatus]int{}
	for i, a := range acks {
		if errs[i] != nil || a == nil {
			return fmt.Errorf("concurrent: %v", errs)
		}
		counts[a.Status]++
	}
	if counts[pb.AckStatus_ACK_STATUS_ACCEPTED] != 1 || counts[pb.AckStatus_ACK_STATUS_DUPLICATE] != 1 {
		return fmt.Errorf("dedupe: %v", acks)
	}
	withoutUnknown := proto.Clone(r).(*pb.ReportRequest)
	withoutUnknown.ProtoReflect().SetUnknown(nil)
	if _, e := core.Report(context.Background(), withoutUnknown); status.Code(e) != codes.AlreadyExists {
		return fmt.Errorf("unknown fields did not reach durable content identity: %v", e)
	}
	changed := proto.Clone(r).(*pb.ReportRequest)
	changed.Results[0].PhasesNanoseconds["connect"] = 20000000
	for _, c := range cs {
		if _, e := c.Report(context.Background(), changed); status.Code(e) != codes.AlreadyExists {
			return fmt.Errorf("conflict: %v", e)
		}
	}
	cases := []*pb.ReportRequest{{}, request("!", 10, at), request("skew", 10, at)}
	cases[2].SentAt = timestamppb.New(time.Now().Add(-10 * time.Minute))
	for _, r := range cases {
		_, want := core.Report(context.Background(), r)
		for _, c := range cs {
			_, got := c.Report(context.Background(), r)
			if status.Code(got) != status.Code(want) || status.Convert(got).Message() != status.Convert(want).Message() || !proto.Equal(status.Convert(got).Proto(), status.Convert(want).Proto()) {
				return fmt.Errorf("direct/forwarded parity got=%v want=%v", got, want)
			}
		}
	}
	matrix, e := wireValidation(core, cs)
	if e != nil {
		return e
	}
	emit(map[string]any{"concurrent_acks": acks, "conflicts": 2, "validation_parity_cases": len(cases) * 2, "validation_matrix": matrix, "unknown_fields_forwarded": true})
	return nil
}

type dropReporter struct {
	core   *corerpc.Client
	marker string
	once   sync.Once
}

func (d *dropReporter) Report(ctx context.Context, r *pb.ReportRequest) (*pb.ReportResponse, error) {
	a, e := d.core.Report(ctx, r)
	if e != nil {
		return nil, e
	}
	drop := false
	if r.BatchId == "lost-ack" {
		d.once.Do(func() {
			drop = true
			os.WriteFile(d.marker, []byte("durable core ACK observed then deliberately withheld by owned test relay\n"), 0600)
		})
	}
	if drop {
		return nil, report.Unavailable()
	}
	return a, nil
}
func relay(target, listen, dir, marker string) error {
	tr, e := trust(dir, "stable", false)
	if e != nil {
		return e
	}
	c, e := corerpc.Dial(context.Background(), target, tr)
	if e != nil {
		return e
	}
	defer c.Close()
	st, e := trust(dir, "core", true)
	if e != nil {
		return e
	}
	a := corerpc.NewAdmission(true, st.Authenticate, nil, nil)
	g := grpc.NewServer(a.Options(st.Credentials())...)
	corerpc.Register(g, &cp.Core_ServiceDesc, &corerpc.Service{Reporter: &dropReporter{core: c, marker: marker}, Reader: c, Ready: c.Ready})
	l, e := net.Listen("tcp", listen)
	if e != nil {
		return e
	}
	defer l.Close()
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, os.Interrupt)
	defer stop()
	done := make(chan error, 1)
	go func() { done <- g.Serve(corerpc.LimitListener(l, 10)) }()
	select {
	case <-ctx.Done():
		a.Stop()
		g.Stop()
		<-done
		a.Wait()
		return nil
	case e := <-done:
		return e
	}
}
