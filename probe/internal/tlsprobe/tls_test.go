package tlsprobe

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"io"
	"log"
	"net"
	"net/http"
	"net/http/httptest"
	"strconv"
	"sync"
	"testing"
	"time"

	"github.com/miekg/dns"

	"pathwatch.local/pathwatch/probe/internal/resolve"
	"pathwatch.local/pathwatch/probe/internal/result"
	"pathwatch.local/pathwatch/probe/internal/testdns"
)

func fixture(t *testing.T) (*httptest.Server, Config, result.Target) {
	t.Helper()
	s := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(200) }))
	s.Config.ErrorLog = log.New(io.Discard, "", 0)
	s.StartTLS()
	t.Cleanup(s.Close)
	roots := x509.NewCertPool()
	roots.AddCert(s.Certificate())
	host, port, _ := net.SplitHostPort(s.Listener.Addr().String())
	n, _ := strconv.Atoi(port)
	return s, Config{ProbeID: "p", RootCAs: roots, Timeout: time.Second}, result.Target{ID: "t", Host: host, Port: n}
}
func TestTLSProbe_Handshake(t *testing.T) {
	s := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(200) }))
	defer s.Close()
	roots := x509.NewCertPool()
	roots.AddCert(s.Certificate())
	host, port, _ := net.SplitHostPort(s.Listener.Addr().String())
	n, _ := strconv.Atoi(port)
	cfg := Config{ProbeID: "p", RootCAs: roots, Timeout: time.Second}
	target := result.Target{ID: "t", Host: host, Port: n}
	p, err := New(cfg)
	if err != nil {
		t.Fatal(err)
	}
	got, err := p.Probe(context.Background(), target)
	if err != nil || len(got) != 1 || got[0].Err != "" || got[0].Phases["tls_handshake"] <= 0 || got[0].TLSVersion != tls.VersionTLS13 || got[0].ProbeID != "p" || got[0].Family != "v4" {
		t.Fatalf("got=%+v err=%v", got, err)
	}
	t.Logf("TLS1.3 handshake=%s resumed=%v", got[0].Phases["tls_handshake"], got[0].TLSResumed)
}
func TestTLSProbe_RealResumptionAndMonotonic(t *testing.T) {
	_, cfg, target := fixture(t)
	wall := time.Date(2026, 10, 23, 12, 0, 0, 0, time.UTC)
	calls := 0
	cfg.WallNow = func() time.Time { v := wall.Add(-time.Duration(calls) * time.Hour); calls++; return v }
	p, err := New(cfg)
	if err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 2; i++ {
		start := time.Now()
		got, err := p.Probe(context.Background(), target)
		elapsed := time.Since(start)
		if err != nil || len(got) != 1 || got[0].Err != "" || got[0].TLSResumed != (i == 1) || got[0].TLSVersion != tls.VersionTLS13 || got[0].StartedAt != wall.Add(-time.Duration(i)*time.Hour) || got[0].Phases["tls_handshake"] <= 0 || got[0].Phases["tls_handshake"] > elapsed {
			t.Fatalf("round=%d got=%+v err=%v", i, got, err)
		}
		t.Logf("round=%d wall=%s handshake=%s resumed=%v real-elapsed=%s", i, got[0].StartedAt, got[0].Phases["tls_handshake"], got[0].TLSResumed, elapsed)
	}
}
func TestTLSProbe_UntrustedAndTimeout(t *testing.T) {
	_, cfg, target := fixture(t)
	cfg.RootCAs = nil
	p, _ := New(cfg)
	got, err := p.Probe(context.Background(), target)
	if err != nil || len(got) != 1 || got[0].Err == "" || got[0].TLSVersion != 0 {
		t.Fatalf("untrusted got=%+v err=%v", got, err)
	}
	l, e := net.Listen("tcp4", "127.0.0.1:0")
	if e != nil {
		t.Fatal(e)
	}
	defer l.Close()
	accepted := make(chan net.Conn, 1)
	go func() {
		c, e := l.Accept()
		if e == nil {
			accepted <- c
		}
	}()
	target.Port = l.Addr().(*net.TCPAddr).Port
	cfg.Timeout = 40 * time.Millisecond
	p, _ = New(cfg)
	start := time.Now()
	got, err = p.Probe(context.Background(), target)
	c := <-accepted
	c.Close()
	if err != nil || len(got) != 1 || got[0].Err == "" || time.Since(start) > time.Second {
		t.Fatalf("timeout got=%+v err=%v", got, err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := p.Probe(ctx, target); !errors.Is(err, context.Canceled) {
		t.Fatalf("cancel=%v", err)
	}
}

func TestTLSProbe_ResolvedHostAndPartialFailure(t *testing.T) {
	_, cfg, target := fixture(t)
	queries := map[uint16]int{}
	var queryMu sync.Mutex
	cfg.Server = testdns.Start(t, func(_ string, q *dns.Msg) *dns.Msg {
		queryMu.Lock()
		queries[q.Question[0].Qtype]++
		queryMu.Unlock()
		return testdns.Answer(q, "127.0.0.1", "127.0.0.2", "::1")
	})
	p, _ := New(cfg)
	target.Host = "example.com"
	got, err := p.Probe(context.Background(), target)
	queryMu.Lock()
	defer queryMu.Unlock()
	if err != nil || len(got) != 3 || got[0].Err != "" || got[0].Addr != "127.0.0.1" || got[1].Err == "" || got[2].Family != "v6" || queries[dns.TypeA] != 1 || queries[dns.TypeAAAA] != 1 {
		t.Fatalf("resolved=%+v err=%v queries=%v", got, err, queries)
	}
	p.resolver = partialResolver{}
	got, err = p.Probe(context.Background(), target)
	if err != nil || len(got) != 2 || got[0].Err != "" || got[1].Err != context.DeadlineExceeded.Error() {
		t.Fatalf("partial=%+v err=%v", got, err)
	}
}

type partialResolver struct{}

func (partialResolver) Timeout() time.Duration { return time.Second }
func (partialResolver) Lookup(context.Context, string) ([]resolve.Answer, error) {
	return []resolve.Answer{{Family: "v4", Addr: "127.0.0.1"}}, context.DeadlineExceeded
}

func TestTLSProbe_InFlightCancel(t *testing.T) {
	l, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer l.Close()
	accepted := make(chan net.Conn, 1)
	go func() {
		c, err := l.Accept()
		if err == nil {
			accepted <- c
		}
	}()
	p, _ := New(Config{ProbeID: "p", Timeout: time.Second})
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() {
		_, err := p.Probe(ctx, result.Target{ID: "t", Host: "127.0.0.1", Port: l.Addr().(*net.TCPAddr).Port})
		done <- err
	}()
	c := <-accepted
	defer c.Close()
	cancel()
	select {
	case err := <-done:
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("cancel=%v", err)
		}
	case <-time.After(time.Second):
		t.Fatal("TLS cancellation did not interrupt handshake")
	}
}

func TestTLSProbe_IPv6Literal(t *testing.T) {
	s := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(200) }))
	s.Listener.Close()
	l, err := net.Listen("tcp6", "[::1]:0")
	if err != nil {
		t.Fatal(err)
	}
	s.Listener = l
	s.StartTLS()
	defer s.Close()
	roots := x509.NewCertPool()
	roots.AddCert(s.Certificate())
	p, _ := New(Config{ProbeID: "p", RootCAs: roots, Timeout: time.Second})
	got, err := p.Probe(context.Background(), result.Target{ID: "t", Host: "[::1]", Port: l.Addr().(*net.TCPAddr).Port})
	if err != nil || len(got) != 1 || got[0].Err != "" || got[0].Addr != "::1" || got[0].Family != "v6" {
		t.Fatalf("v6=%+v err=%v", got, err)
	}
}
