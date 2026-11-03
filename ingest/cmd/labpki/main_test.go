package main

import (
	"crypto/tls"
	"crypto/x509"
	"os"
	"path/filepath"
	"testing"
	"time"

	"pathwatch.local/pathwatch/ingest/internal/adminbridge"
	"pathwatch.local/pathwatch/ingest/internal/corerpc"
)

// The generated identities must pass the same checks the pods run at startup.
func TestGeneratedIdentitiesSatisfyServiceTrust(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "pki")
	if err := generate(dir, 24*time.Hour); err != nil {
		t.Fatal(err)
	}
	f := func(n string) string { return filepath.Join(dir, n) }
	if _, err := corerpc.LoadTrust(corerpc.TLSFiles{CA: f("ca.pem"), Cert: f("core.pem"), Key: f("core-key.pem"), ServerName: CoreServerName, Frontends: []string{FrontendURI}}, true); err != nil {
		t.Fatalf("core server: %v", err)
	}
	if _, err := corerpc.LoadTrust(corerpc.TLSFiles{CA: f("ca.pem"), Cert: f("frontend.pem"), Key: f("frontend-key.pem"), ServerName: CoreServerName}, false); err != nil {
		t.Fatalf("frontend client: %v", err)
	}
	if _, err := adminbridge.New(adminbridge.Config{Upstream: "127.0.0.1:9091", CA: f("ca.pem"), Cert: f("metrics.pem"), Key: f("metrics-key.pem"),
		ServerName: "metrics.pathwatch.local", ServerURI: "spiffe://pathwatch.local/metrics", ClientURI: "spiffe://pathwatch.local/prometheus"}); err != nil {
		t.Fatalf("metrics bridge: %v", err)
	}
	prom, err := tls.LoadX509KeyPair(f("prometheus.pem"), f("prometheus-key.pem"))
	if err != nil {
		t.Fatal(err)
	}
	leaf, _ := x509.ParseCertificate(prom.Certificate[0])
	if len(leaf.URIs) != 1 || leaf.URIs[0].String() != "spiffe://pathwatch.local/prometheus" || !corerpc.CertificateHasUsage(leaf, x509.ExtKeyUsageClientAuth) {
		t.Fatalf("prometheus client identity: %v %v", leaf.URIs, leaf.ExtKeyUsage)
	}
	edge, err := tls.LoadX509KeyPair(f("edge.pem"), f("edge-key.pem"))
	if err != nil {
		t.Fatal(err)
	}
	leaf, _ = x509.ParseCertificate(edge.Certificate[0])
	if corerpc.ExactCertificateSAN(leaf, EdgeServerName) != nil || !corerpc.CertificateHasUsage(leaf, x509.ExtKeyUsageServerAuth) {
		t.Fatalf("edge server identity: %v", leaf.DNSNames)
	}
	for _, n := range []string{"ca-key.pem", "core-key.pem", "edge-key.pem"} {
		st, err := os.Stat(f(n))
		if err != nil || st.Mode().Perm() != 0o600 {
			t.Fatalf("%s mode %v %v", n, st.Mode(), err)
		}
	}
	if st, _ := os.Stat(dir); st.Mode().Perm() != 0o700 {
		t.Fatalf("pki dir mode %v", st.Mode())
	}
	if err := generate(dir, time.Hour); err == nil {
		t.Fatal("must refuse to overwrite an existing PKI directory")
	}
}

func TestExtraServerLeaves(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "pki")
	if err := generate(dir, time.Hour, "krill"); err != nil {
		t.Fatal(err)
	}
	pair, err := tls.LoadX509KeyPair(filepath.Join(dir, "krill.pem"), filepath.Join(dir, "krill-key.pem"))
	if err != nil {
		t.Fatal(err)
	}
	leaf, _ := x509.ParseCertificate(pair.Certificate[0])
	pem, _ := os.ReadFile(filepath.Join(dir, "ca.pem"))
	roots := x509.NewCertPool()
	roots.AppendCertsFromPEM(pem)
	if _, err := leaf.Verify(x509.VerifyOptions{Roots: roots, DNSName: "krill", KeyUsages: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}}); err != nil {
		t.Fatal(err)
	}
	if err := generate(filepath.Join(t.TempDir(), "x"), time.Hour, "../escape"); err == nil {
		t.Fatal("extra names must be plain DNS labels")
	}
}
