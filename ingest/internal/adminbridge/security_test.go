package adminbridge

import (
	"bytes"
	"crypto/tls"
	"crypto/x509"
	"encoding/pem"
	"io"
	"net"
	"net/http"
	"net/http/httptrace"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

type lockedLog struct {
	mu sync.Mutex
	b  bytes.Buffer
}

func (l *lockedLog) Write(p []byte) (int, error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.b.Write(p)
}
func (l *lockedLog) String() string { l.mu.Lock(); defer l.mu.Unlock(); return l.b.String() }
func TestAuthenticationMatrix(t *testing.T) {
	a, cfg, c := standard(t)
	var calls atomic.Int32
	addr, b, _ := serve(t, cfg, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { calls.Add(1); w.Write([]byte("m 1\n")) }))
	now := time.Now()
	other := newAuthority(t, nil, now.Add(-time.Hour), now.Add(time.Hour))
	tests := []struct {
		name   string
		modify func(*tls.Config)
	}{
		{"missing", func(x *tls.Config) { x.Certificates = nil }},
		{"wrong-ca", func(x *tls.Config) {
			x.Certificates = []tls.Certificate{leaf(t, other, "", "spiffe://pathwatch.local/prometheus", x509.ExtKeyUsageClientAuth, now.Add(-time.Hour), now.Add(time.Hour))}
		}},
		{"wrong-dns", func(x *tls.Config) { x.ServerName = "wrong.pathwatch.local" }},
		{"wrong-eku", func(x *tls.Config) {
			x.Certificates = []tls.Certificate{leaf(t, a, "", "spiffe://pathwatch.local/prometheus", x509.ExtKeyUsageServerAuth, now.Add(-time.Hour), now.Add(time.Hour))}
		}},
		{"expired-leaf", func(x *tls.Config) {
			x.Certificates = []tls.Certificate{leaf(t, a, "", "spiffe://pathwatch.local/prometheus", x509.ExtKeyUsageClientAuth, now.Add(-time.Hour), now.Add(-time.Minute))}
		}},
		{"not-yet-leaf", func(x *tls.Config) {
			x.Certificates = []tls.Certificate{leaf(t, a, "", "spiffe://pathwatch.local/prometheus", x509.ExtKeyUsageClientAuth, now.Add(time.Minute), now.Add(time.Hour))}
		}},
		{"tls12", func(x *tls.Config) { x.MinVersion = tls.VersionTLS12; x.MaxVersion = tls.VersionTLS12 }},
		{"h2-only", func(x *tls.Config) { x.NextProtos = []string{"h2"} }},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			s, _, e := request(t, addr, c, "/metrics")
			if e != nil || s != 200 {
				t.Fatalf("before %d %v", s, e)
			}
			start := calls.Load()
			x := c.Clone()
			tc.modify(x)
			if len(x.Certificates) > 0 {
				chosen := x.Certificates[0]
				x.GetClientCertificate = func(*tls.CertificateRequestInfo) (*tls.Certificate, error) { return &chosen, nil }
			}
			logStart := len(b.ErrorLog.Writer().(*lockedLog).String())
			s, _, e = request(t, addr, x, "/metrics")
			delta := calls.Load() - start
			if e == nil || delta != 0 {
				t.Fatalf("auth negative status=%d delta=%d err=%v", s, delta, e)
			}
			cause := ""
			deadline := time.Now().Add(time.Second)
			for time.Now().Before(deadline) {
				cause = b.ErrorLog.Writer().(*lockedLog).String()[logStart:]
				if strings.Contains(cause, "TLS handshake error") {
					break
				}
				time.Sleep(time.Millisecond)
			}
			if tc.name == "wrong-dns" {
				if !strings.Contains(e.Error(), "certificate") {
					t.Fatalf("not DNS auth origin: %v", e)
				}
			} else {
				expected := map[string]string{"missing": "certificate", "wrong-ca": "certificate", "wrong-eku": "certificate", "expired-leaf": "certificate", "not-yet-leaf": "certificate", "tls12": "versions", "h2-only": "application protocol"}[tc.name]
				if !strings.Contains(cause, expected) {
					t.Fatalf("missing authenticated failure origin: %s client=%v", cause, e)
				}
			}
			t.Logf("auth=%s delta=%d client_error=%v server_cause=%q", tc.name, delta, e, cause)
			s, _, e = request(t, addr, c, "/metrics")
			if e != nil || s != 200 {
				t.Fatalf("after %d %v", s, e)
			}
		})
	}
	start := calls.Load()
	conn, e := net.Dial("tcp", addr)
	if e != nil {
		t.Fatal(e)
	}
	conn.SetDeadline(time.Now().Add(time.Second))
	conn.Write([]byte("GET /metrics HTTP/1.1\r\nHost: test\r\n\r\n"))
	reply, _ := io.ReadAll(conn)
	conn.Close()
	if bytes.Contains(reply, []byte("200 OK")) || calls.Load() != start {
		t.Fatal("plaintext forwarded")
	}
	t.Logf("server-auth-causes=%s", b.ErrorLog.Writer().(*lockedLog).String())
}
func TestChainAndOwnIdentity(t *testing.T) {
	now := time.Now()
	for _, kind := range []string{"expired-intermediate", "not-yet-intermediate", "expired-root", "not-yet-root"} {
		t.Run(kind, func(t *testing.T) {
			a, cfg, c := standard(t)
			before, until := now.Add(-time.Hour), now.Add(-time.Minute)
			if strings.HasPrefix(kind, "not-yet") {
				before, until = now.Add(time.Minute), now.Add(time.Hour)
			}
			issuer := newAuthority(t, a, before, until)
			if strings.HasSuffix(kind, "root") {
				issuer = newAuthority(t, nil, before, until)
				f, e := os.OpenFile(cfg.CA, os.O_APPEND|os.O_WRONLY, 0600)
				if e != nil {
					t.Fatal(e)
				}
				pem.Encode(f, &pem.Block{Type: "CERTIFICATE", Bytes: issuer.der})
				f.Close()
			}
			var calls atomic.Int32
			addr, bridge, _ := serve(t, cfg, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { calls.Add(1); w.Write([]byte("ok")) }))
			s, _, e := request(t, addr, c, "/metrics")
			if e != nil || s != 200 {
				t.Fatal("valid before", e)
			}
			x := c.Clone()
			x.Certificates = []tls.Certificate{leaf(t, issuer, "", "spiffe://pathwatch.local/prometheus", x509.ExtKeyUsageClientAuth, now.Add(-time.Hour), now.Add(time.Hour))}
			start := calls.Load()
			s, _, e = request(t, addr, x, "/metrics")
			if e == nil || calls.Load() != start {
				t.Fatalf("chain allowed %d %v", s, e)
			}
			cause := bridge.ErrorLog.Writer().(*lockedLog).String()
			if !strings.Contains(cause, "certificate has expired or is not yet valid") {
				t.Fatalf("missing chain auth origin: %s", cause)
			}
			t.Logf("chain=%s auth_error=%v server_cause=%q", kind, e, cause)
			s, _, e = request(t, addr, c, "/metrics")
			if e != nil || s != 200 {
				t.Fatal("valid after", e)
			}
		})
	}
	for _, kind := range []string{"dns", "uri", "eku", "expired", "notyet", "expired-root", "expired-intermediate", "not-yet-root", "not-yet-intermediate"} {
		t.Run("own-"+kind, func(t *testing.T) {
			a := newAuthority(t, nil, now.Add(-time.Hour), now.Add(time.Hour))
			issuer := a
			dns, uri, usage := "metrics.pathwatch.local", "spiffe://pathwatch.local/metrics", x509.ExtKeyUsageServerAuth
			before, until := now.Add(-time.Hour), now.Add(time.Hour)
			switch kind {
			case "dns":
				dns = "wrong.local"
			case "uri":
				uri = "spiffe://pathwatch.local/wrong"
			case "eku":
				usage = x509.ExtKeyUsageClientAuth
			case "expired":
				until = now.Add(-time.Minute)
			case "notyet":
				before = now.Add(time.Minute)
			case "expired-root":
				a = newAuthority(t, nil, now.Add(-time.Hour), now.Add(-time.Minute))
				issuer = a
			case "not-yet-root":
				a = newAuthority(t, nil, now.Add(time.Minute), now.Add(time.Hour))
				issuer = a
			case "not-yet-intermediate":
				issuer = newAuthority(t, a, now.Add(time.Minute), now.Add(time.Hour))
			case "expired-intermediate":
				issuer = newAuthority(t, a, now.Add(-time.Hour), now.Add(-time.Minute))
			}
			cfg := configFiles(t, a, leaf(t, issuer, dns, uri, usage, before, until))
			cfg.Upstream = "127.0.0.1:9091"
			if _, e := New(cfg); e == nil {
				t.Fatal("bad own identity accepted")
			} else {
				t.Logf("own=%s error=%v", kind, e)
			}
		})
	}
}
func TestKeepaliveAndInflightExpiry(t *testing.T) {
	for _, kind := range []string{"peer-keepalive", "server-keepalive", "peer-inflight", "server-inflight"} {
		t.Run(kind, func(t *testing.T) {
			a, cfg, c := standard(t)
			until := time.Now().Truncate(time.Second).Add(2 * time.Second)
			if strings.HasPrefix(kind, "peer") {
				c.Certificates = []tls.Certificate{leaf(t, a, "", "spiffe://pathwatch.local/prometheus", x509.ExtKeyUsageClientAuth, time.Now().Add(-time.Hour), until)}
			} else {
				cfg = configFiles(t, a, leaf(t, a, "metrics.pathwatch.local", "spiffe://pathwatch.local/metrics", x509.ExtKeyUsageServerAuth, time.Now().Add(-time.Hour), until))
			}
			var calls atomic.Int32
			entered := make(chan struct{}, 1)
			addr, _, _ := serve(t, cfg, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls.Add(1)
				if strings.HasSuffix(kind, "inflight") {
					entered <- struct{}{}
					<-r.Context().Done()
					return
				}
				w.Write([]byte("ok"))
			}))
			tr := &http.Transport{TLSClientConfig: c, Proxy: nil}
			defer tr.CloseIdleConnections()
			cl := &http.Client{Transport: tr, Timeout: 3 * time.Second}
			if strings.HasSuffix(kind, "keepalive") {
				r, e := cl.Get("https://" + addr + "/metrics")
				if e != nil {
					t.Fatal(e)
				}
				io.Copy(io.Discard, r.Body)
				r.Body.Close()
				if r.StatusCode != 200 {
					t.Fatal("before")
				}
				time.Sleep(time.Until(until) + 30*time.Millisecond)
				var reused bool
				req, _ := http.NewRequest("GET", "https://"+addr+"/metrics", nil)
				req = req.WithContext(httptrace.WithClientTrace(req.Context(), &httptrace.ClientTrace{GotConn: func(i httptrace.GotConnInfo) { reused = i.Reused }}))
				r, e = cl.Do(req)
				if !reused {
					t.Fatal("expiry not tested on reused connection")
				}
				if e == nil {
					io.Copy(io.Discard, r.Body)
					r.Body.Close()
					if r.StatusCode == 200 {
						t.Fatal("expired success")
					}
				}
				if calls.Load() != 1 {
					t.Fatal("expired entry forwarded")
				}
			} else {
				r, e := cl.Get("https://" + addr + "/metrics")
				if e == nil {
					body, re := io.ReadAll(r.Body)
					r.Body.Close()
					if r.StatusCode == 200 && re == nil {
						t.Fatalf("expiry succeeded %q", body)
					}
				}
				select {
				case <-entered:
				default:
					t.Fatal("not actually inflight")
				}
				if calls.Load() != 1 {
					t.Fatal("wrong call count")
				}
			}
			t.Logf("expiry=%s calls=%d", kind, calls.Load())
		})
	}
}
