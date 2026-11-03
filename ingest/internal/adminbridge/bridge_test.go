package adminbridge

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
	"fmt"
	"io"
	"log"
	"math/big"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"pathwatch.local/pathwatch/ingest/internal/frontend"
	"pathwatch.local/pathwatch/ingest/internal/queryapi"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

type authority struct {
	cert   *x509.Certificate
	key    *ecdsa.PrivateKey
	der    []byte
	parent *authority
}

func newAuthority(t *testing.T, parent *authority, before, until time.Time) *authority {
	t.Helper()
	k, e := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if e != nil {
		t.Fatal(e)
	}
	c := &x509.Certificate{SerialNumber: big.NewInt(time.Now().UnixNano()), Subject: pkix.Name{CommonName: "test-ca"}, NotBefore: before, NotAfter: until, IsCA: true, BasicConstraintsValid: true, KeyUsage: x509.KeyUsageCertSign}
	issuer, ik := c, k
	if parent != nil {
		issuer, ik = parent.cert, parent.key
	}
	der, e := x509.CreateCertificate(rand.Reader, c, issuer, &k.PublicKey, ik)
	if e != nil {
		t.Fatal(e)
	}
	c, e = x509.ParseCertificate(der)
	if e != nil {
		t.Fatal(e)
	}
	return &authority{c, k, der, parent}
}
func leaf(t *testing.T, a *authority, dns, uri string, usage x509.ExtKeyUsage, before, until time.Time) tls.Certificate {
	t.Helper()
	k, e := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if e != nil {
		t.Fatal(e)
	}
	u, e := url.Parse(uri)
	if e != nil {
		t.Fatal(e)
	}
	c := &x509.Certificate{SerialNumber: big.NewInt(time.Now().UnixNano()), DNSNames: []string{dns}, URIs: []*url.URL{u}, NotBefore: before, NotAfter: until, KeyUsage: x509.KeyUsageDigitalSignature, ExtKeyUsage: []x509.ExtKeyUsage{usage}}
	der, e := x509.CreateCertificate(rand.Reader, c, a.cert, &k.PublicKey, a.key)
	if e != nil {
		t.Fatal(e)
	}
	chain := [][]byte{der}
	for p := a; p != nil && p.parent != nil; p = p.parent {
		chain = append(chain, p.der)
	}
	return tls.Certificate{Certificate: chain, PrivateKey: k}
}
func configFiles(t *testing.T, a *authority, cert tls.Certificate) Config {
	t.Helper()
	dir := t.TempDir()
	var b bytes.Buffer
	for _, der := range cert.Certificate {
		pem.Encode(&b, &pem.Block{Type: "CERTIFICATE", Bytes: der})
	}
	k, e := x509.MarshalECPrivateKey(cert.PrivateKey.(*ecdsa.PrivateKey))
	if e != nil {
		t.Fatal(e)
	}
	for n, v := range map[string][]byte{"ca": pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: a.der}), "cert": b.Bytes(), "key": pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: k})} {
		if e := os.WriteFile(filepath.Join(dir, n), v, 0600); e != nil {
			t.Fatal(e)
		}
	}
	return Config{CA: filepath.Join(dir, "ca"), Cert: filepath.Join(dir, "cert"), Key: filepath.Join(dir, "key"), ServerName: "metrics.pathwatch.local", ServerURI: "spiffe://pathwatch.local/metrics", ClientURI: "spiffe://pathwatch.local/prometheus"}
}
func clientConfig(a *authority, cert tls.Certificate) *tls.Config {
	roots := x509.NewCertPool()
	roots.AddCert(a.cert)
	return &tls.Config{MinVersion: tls.VersionTLS13, MaxVersion: tls.VersionTLS13, RootCAs: roots, ServerName: "metrics.pathwatch.local", Certificates: []tls.Certificate{cert}, NextProtos: []string{"http/1.1"}}
}
func request(t *testing.T, addr string, c *tls.Config, path string) (int, []byte, error) {
	t.Helper()
	tr := &http.Transport{TLSClientConfig: c, Proxy: nil, DisableKeepAlives: true}
	defer tr.CloseIdleConnections()
	cl := &http.Client{Transport: tr, Timeout: 3 * time.Second}
	r, e := cl.Get("https://" + addr + path)
	if e != nil {
		return 0, nil, e
	}
	defer r.Body.Close()
	b, e := io.ReadAll(r.Body)
	return r.StatusCode, b, e
}
func serve(t *testing.T, cfg Config, up http.Handler) (string, *Bridge, context.CancelFunc) {
	t.Helper()
	u := httptest.NewServer(up)
	t.Cleanup(u.Close)
	cfg.Upstream = u.Listener.Addr().String()
	b, e := New(cfg)
	if e != nil {
		t.Fatal(e)
	}
	b.ErrorLog = log.New(&lockedLog{}, "", 0)
	l, e := net.Listen("tcp", "127.0.0.1:0")
	if e != nil {
		t.Fatal(e)
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- b.Serve(ctx, l) }()
	t.Cleanup(func() {
		cancel()
		select {
		case e := <-done:
			if e != nil {
				t.Errorf("serve cleanup: %v", e)
			}
		case <-time.After(3 * time.Second):
			t.Error("serve leaked")
		}
		c, e := net.DialTimeout("tcp", l.Addr().String(), time.Second)
		if e == nil {
			c.Close()
			t.Error("listener survives")
		}
	})
	return l.Addr().String(), b, cancel
}
func standard(t *testing.T) (*authority, Config, *tls.Config) {
	t.Helper()
	now := time.Now()
	a := newAuthority(t, nil, now.Add(-time.Hour), now.Add(time.Hour))
	s := leaf(t, a, "metrics.pathwatch.local", "spiffe://pathwatch.local/metrics", x509.ExtKeyUsageServerAuth, now.Add(-time.Hour), now.Add(time.Hour))
	c := leaf(t, a, "", "spiffe://pathwatch.local/prometheus", x509.ExtKeyUsageClientAuth, now.Add(-time.Hour), now.Add(time.Hour))
	return a, configFiles(t, a, s), clientConfig(a, c)
}

func TestMetricsRealHandlerAndBoundary(t *testing.T) {
	_, cfg, c := standard(t)
	actual := frontend.New(nil, time.Now)
	query := queryapi.New(nil)
	handler := func(w http.ResponseWriter, r *http.Request) { actual.Metrics(w, r); query.Metrics(w) }
	ref := httptest.NewRecorder()
	handler(ref, httptest.NewRequest("GET", "/metrics", nil))
	addr, _, _ := serve(t, cfg, http.HandlerFunc(handler))
	status, b, e := request(t, addr, c, "/metrics")
	if e != nil || status != 200 || !bytes.Equal(b, ref.Body.Bytes()) {
		t.Fatalf("metrics status=%d bytes=%q err=%v", status, b, e)
	}
	if bytes.Count(b, []byte("pathwatch_frontend_report_requests_total{")) != 2 {
		t.Fatal("series changed")
	}
	for _, n := range []int{65535, 65536, 65537} {
		t.Run(fmt.Sprint(n), func(t *testing.T) {
			addr, _, _ := serve(t, cfg, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.Write(bytes.Repeat([]byte{'m'}, n)) }))
			s, b, e := request(t, addr, c, "/metrics")
			if n <= 65536 {
				if e != nil || s != 200 || len(b) != n {
					t.Fatalf("inclusive boundary %d %d %v", s, len(b), e)
				}
			} else if e == nil && s == 200 {
				t.Fatal("oversize success")
			}
		})
	}
}
func TestRejectWrongRoleWithPositiveControls(t *testing.T) {
	a, cfg, c := standard(t)
	var calls atomic.Int32
	addr, bridge, _ := serve(t, cfg, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { calls.Add(1); w.Write([]byte("metric 1\n")) }))
	for _, phase := range []string{"before", "wrong-role", "after"} {
		cc := c.Clone()
		if phase == "wrong-role" {
			cc.Certificates = []tls.Certificate{leaf(t, a, "", "spiffe://pathwatch.local/not-prometheus", x509.ExtKeyUsageClientAuth, time.Now().Add(-time.Hour), time.Now().Add(time.Hour))}
		}
		start := calls.Load()
		s, _, e := request(t, addr, cc, "/metrics")
		delta := calls.Load() - start
		t.Logf("phase=%s status=%d upstream_delta=%d err=%v", phase, s, delta, e)
		if phase == "wrong-role" {
			if e == nil && s == 200 || delta != 0 {
				t.Errorf("unauthorized identity reached upstream: status=%d delta=%d err=%v", s, delta, e)
			} else {
				cause := bridge.ErrorLog.Writer().(*lockedLog).String()
				if !strings.Contains(cause, "unauthorized metrics client identity") {
					t.Errorf("missing exact-role auth origin: %s", cause)
				}
				t.Logf("role_auth_origin=%q", cause)
			}
		} else if e != nil || s != 200 || delta != 1 {
			t.Fatalf("positive failed %s %d %d %v", phase, s, delta, e)
		}
	}
}
func TestPathsAndBodiesNeverForward(t *testing.T) {
	_, cfg, c := standard(t)
	var calls atomic.Int32
	addr, _, _ := serve(t, cfg, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { calls.Add(1); w.Write([]byte("m 1\n")) }))
	cases := []string{"GET /metrics HTTP/1.0\r\nHost: lab\r\n\r\n", "GET /metrics? HTTP/1.1\r\nHost: lab\r\n\r\n", "GET /metrics?x=1 HTTP/1.1\r\nHost: lab\r\n\r\n", "GET /%6det rics HTTP/1.1\r\nHost: lab\r\n\r\n", "GET /%6detrics HTTP/1.1\r\nHost: lab\r\n\r\n", "HEAD /metrics HTTP/1.1\r\nHost: lab\r\n\r\n", "POST /metrics HTTP/1.1\r\nHost: lab\r\nContent-Length: 1\r\n\r\nx", "GET /readyz HTTP/1.1\r\nHost: lab\r\n\r\n", "GET /metrics HTTP/1.1\r\nHost: lab\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n", "GET /metrics HTTP/1.1\r\nHost: lab\r\nConnection: upgrade\r\nUpgrade: h2c\r\n\r\n"}
	for i, wire := range cases {
		t.Run(fmt.Sprint(i), func(t *testing.T) {
			conn, e := tls.Dial("tcp", addr, c)
			if e != nil {
				t.Fatal(e)
			}
			defer conn.Close()
			conn.SetDeadline(time.Now().Add(3 * time.Second))
			conn.Write([]byte(wire))
			b := make([]byte, 2048)
			n, _ := conn.Read(b)
			if strings.Contains(string(b[:n]), " 200 ") {
				t.Fatalf("bad request succeeds: %q", b[:n])
			}
		})
	}
	if calls.Load() != 0 {
		t.Fatalf("forwarded %d invalid requests", calls.Load())
	}
}
func TestReadyFrontendExact(t *testing.T) {
	for _, tc := range []struct {
		name, body string
		code       int
		ok         bool
	}{{"good", "ready\n", 200, true}, {"wrong", "ready", 200, false}, {"extra", "ready\ntrailing", 200, false}, {"notready", "ready\n", 503, false}, {"redirect", "ready\n", 302, false}} {
		t.Run(tc.name, func(t *testing.T) {
			u := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.RequestURI() != "/readyz" {
					t.Error("wrong upstream path")
				}
				w.WriteHeader(tc.code)
				w.Write([]byte(tc.body))
			}))
			defer u.Close()
			e := CheckReady(context.Background(), u.Listener.Addr().String(), "frontend")
			if (e == nil) != tc.ok {
				t.Fatalf("ready err=%v", e)
			}
		})
	}
}
