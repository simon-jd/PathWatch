package resolve

import (
	"context"
	"encoding/pem"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/miekg/dns"
	"pathwatch.local/pathwatch/probe/internal/testdns"
)

func TestDoHDoesNotUseAmbientProxy(t *testing.T) {
	var proxyCalls atomic.Int32
	proxy := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { proxyCalls.Add(1); w.WriteHeader(502) }))
	defer proxy.Close()
	t.Setenv("HTTPS_PROXY", proxy.URL)
	t.Setenv("HTTP_PROXY", proxy.URL)
	t.Setenv("NO_PROXY", "")
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		q := new(dns.Msg)
		if err := q.Unpack(raw); err != nil {
			t.Error(err)
			return
		}
		raw, _ = testdns.Answer(q, "127.0.0.1", "::1").Pack()
		w.Header().Set("Content-Type", "application/dns-message")
		_, _ = w.Write(raw)
	}))
	defer server.Close()
	ca := filepath.Join(t.TempDir(), "ca.pem")
	if err := os.WriteFile(ca, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: server.Certificate().Raw}), 0600); err != nil {
		t.Fatal(err)
	}
	// Use a certificate DNS name (not localhost/IP: those bypass even ambient
	// proxies), mapping only that owned name to the loopback fixture at dial time.
	name := server.Certificate().DNSNames[0]
	r, err := NewTransport(TransportConfig{Transport: "doh", DoHURL: "https://" + name + "/dns-query", CAFile: ca, Timeout: time.Second})
	if err != nil {
		t.Fatal(err)
	}
	transport := r.doh.client.Transport.(*http.Transport)
	transport.DialContext = func(ctx context.Context, network, addr string) (net.Conn, error) {
		if addr != net.JoinHostPort(name, "443") {
			return nil, fmt.Errorf("unconfigured proxy dial %s", addr)
		}
		return (&net.Dialer{}).DialContext(ctx, network, strings.TrimPrefix(server.URL, "https://"))
	}
	answers, err := r.Lookup(context.Background(), "owned.test")
	if err != nil || len(answers) != 2 || answers[0].Addr != "127.0.0.1" || answers[1].Addr != "::1" || proxyCalls.Load() != 0 {
		t.Fatalf("ambient proxy escaped configuration: %+v err=%v proxyCalls=%d", answers, err, proxyCalls.Load())
	}
}
