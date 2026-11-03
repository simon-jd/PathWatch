package server_test

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"math/big"
	"net"
	"os"
	"path/filepath"
	"testing"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/status"
	"pathwatch.local/pathwatch/ingest/internal/server"
	pb "pathwatch.local/pathwatch/proto/pathwatch/v1"
)

func TestTransport_SecureDefault(t *testing.T) {
	for _, c := range []server.TransportConfig{{Listen: "127.0.0.1:9443"}, {Listen: "0.0.0.0:9443", DevelopmentCleartext: true}, {Listen: "localhost:9443", DevelopmentCleartext: true}} {
		if _, e := server.Options(c); e == nil {
			t.Fatalf("unsafe config accepted: %+v", c)
		}
	}
	if _, e := server.Options(server.TransportConfig{Listen: "127.0.0.1:9443", DevelopmentCleartext: true}); e != nil {
		t.Fatal(e)
	}
}
func TestTransport_RealTLSReportAndMessageBound(t *testing.T) {
	cfg, _, s := fixture(t)
	dir := t.TempDir()
	key, e := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if e != nil {
		t.Fatal(e)
	}
	cert := &x509.Certificate{SerialNumber: big.NewInt(1), Subject: pkix.Name{CommonName: "ingest-test"}, NotBefore: time.Now().Add(-time.Hour), NotAfter: time.Now().Add(time.Hour), IPAddresses: []net.IP{net.ParseIP("127.0.0.1")}, ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}, KeyUsage: x509.KeyUsageDigitalSignature, IsCA: true, BasicConstraintsValid: true}
	der, e := x509.CreateCertificate(rand.Reader, cert, cert, &key.PublicKey, key)
	if e != nil {
		t.Fatal(e)
	}
	kb, _ := x509.MarshalPKCS8PrivateKey(key)
	cf := filepath.Join(dir, "cert.pem")
	kf := filepath.Join(dir, "key.pem")
	os.WriteFile(cf, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}), 0600)
	os.WriteFile(kf, pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: kb}), 0600)
	l, e := net.Listen("tcp", "127.0.0.1:0")
	if e != nil {
		t.Fatal(e)
	}
	opts, e := server.Options(server.TransportConfig{Listen: l.Addr().String(), CertFile: cf, KeyFile: kf})
	if e != nil {
		t.Fatal(e)
	}
	g := grpc.NewServer(opts...)
	pb.RegisterIngestServer(g, s)
	go g.Serve(l)
	defer g.Stop()
	pool := x509.NewCertPool()
	parsed, _ := x509.ParseCertificate(der)
	pool.AddCert(parsed)
	c, e := grpc.NewClient(l.Addr().String(), grpc.WithTransportCredentials(credentials.NewTLS(&tls.Config{RootCAs: pool, MinVersion: tls.VersionTLS12})))
	if e != nil {
		t.Fatal(e)
	}
	defer c.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	rpc := pb.NewIngestClient(c)
	if _, e = rpc.Report(ctx, batch("tls", 2)); e != nil {
		t.Fatal(e)
	}
	r := batch("oversized", 1)
	r.Results[0].Err = string(make([]byte, server.MaxMessageBytes+1))
	if _, e = rpc.Report(ctx, r, grpc.MaxCallSendMsgSize(20<<20)); status.Code(e) != codes.ResourceExhausted {
		t.Fatalf("message bound=%v", e)
	}
	counts(t, cfg.Database, 2)
}
