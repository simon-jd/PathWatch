package dnsprobe

import (
	"context"
	"encoding/json"
	"github.com/miekg/dns"
	"pathwatch.local/pathwatch/probe/internal/result"
	"pathwatch.local/pathwatch/probe/internal/testdns"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

// The JSON boundary lets the pre-feature implementation demonstrate an actual
// wrong transport/result, rather than failing to compile on the additive API.
func configuredProbe(t *testing.T, server, settings string) (*Probe, result.Target) {
	t.Helper()
	return configuredProbeTimeout(t, server, settings, 0)
}

func configuredProbeTimeout(t *testing.T, server, settings string, timeout time.Duration) (*Probe, result.Target) {
	t.Helper()
	var target result.Target
	if err := json.Unmarshal([]byte(`{"ID":"owned","Host":"owned.test","Kinds":["dns"],"DNS":`+settings+`}`), &target); err != nil {
		t.Fatal(err)
	}
	raw, err := json.Marshal(map[string]any{"ProbeID": "p", "Server": server, "Timeout": timeout, "Targets": []result.Target{target}})
	if err != nil {
		t.Fatal(err)
	}
	var cfg Config
	if err := json.Unmarshal(raw, &cfg); err != nil {
		t.Fatal(err)
	}
	p, err := New(cfg)
	if err != nil {
		t.Fatal(err)
	}
	return p, target
}

func TestDNSExplicitTCPUsesTCPAndSeparateIdentity(t *testing.T) {
	var udp, tcp atomic.Int32
	server := testdns.Start(t, func(network string, q *dns.Msg) *dns.Msg {
		if network == "udp" {
			udp.Add(1)
		} else {
			tcp.Add(1)
		}
		return testdns.Answer(q, "127.0.0.1", "::1")
	})
	p, target := configuredProbe(t, server, `{"transport":"tcp"}`)
	got, err := p.Probe(context.Background(), target)
	if err != nil || len(got) != 2 {
		t.Fatalf("results=%+v err=%v", got, err)
	}
	if tcp.Load() != 2 || udp.Load() != 0 || got[0].TargetID != "owned:dns:tcp" || got[0].Protocol != "" {
		t.Fatalf("transport/identity wrong: UDP=%d TCP=%d result=%+v", udp.Load(), tcp.Load(), got[0])
	}
}

func TestDNSExplicitUDPDoesNotFallback(t *testing.T) {
	var tcp atomic.Int32
	server := testdns.Start(t, func(network string, q *dns.Msg) *dns.Msg {
		if network == "tcp" {
			tcp.Add(1)
			return testdns.Answer(q, "127.0.0.1", "::1")
		}
		m := new(dns.Msg).SetReply(q)
		m.Truncated = true
		return m
	})
	p, target := configuredProbe(t, server, `{"transport":"udp"}`)
	got, err := p.Probe(context.Background(), target)
	if err != nil || len(got) != 2 || got[0].Err == "" || got[1].Err == "" || tcp.Load() != 0 || got[0].TargetID != "owned:dns:udp" {
		t.Fatalf("UDP-only leaked fallback: %+v %v TCP=%d", got, err, tcp.Load())
	}
}

func TestDNSConfigurationRejectedAtStartup(t *testing.T) {
	for _, settings := range []string{`{}`, `{"transport":"dot"}`, `{"transport":"doh"}`, `{"transport":"doh","doh_url":"http://127.0.0.1/dns-query"}`, `{"transport":"doh","doh_url":"https://user:pass@localhost/dns-query"}`, `{"transport":"doh","doh_url":"https://localhost/dns-query#fragment"}`, `{"transport":"doh","doh_url":"https://localhost/dns-query","server":"127.0.0.1:53"}`, `{"transport":"tcp","doh_url":"https://localhost/dns-query"}`, `{"transport":"udp","ca_file":"unused"}`, `{"transport":"tcp","server":"invalid"}`, `{"transport":"doh","doh_url":"https://localhost/dns-query","ca_file":"/nonexistent/pathwatch.pem"}`} {
		var cfg Config
		err := json.Unmarshal([]byte(`{"ProbeID":"p","Server":"127.0.0.1:53","Targets":[{"ID":"owned","Host":"owned.test","DNS":`+settings+`}]}`), &cfg)
		if err != nil {
			t.Fatal(err)
		}
		if _, err = New(cfg); err == nil {
			t.Errorf("accepted invalid DNS config %s", settings)
		}
	}
	var cfg Config
	if err := json.Unmarshal([]byte(`{"ProbeID":"p","Server":"127.0.0.1:53","Targets":[{"ID":"owned:dns:tcp","Host":"owned.test"}]}`), &cfg); err != nil {
		t.Fatal(err)
	}
	if _, err := New(cfg); err == nil {
		t.Error("accepted reserved target ID")
	}
}

func TestDNSExplicitIdentityAndLiteralValidation(t *testing.T) {
	for _, target := range []result.Target{
		{ID: "owned", Host: "127.0.0.1", DNS: &result.DNSConfig{Transport: "udp"}},
		{ID: "owned", Host: "[::1]", DNS: &result.DNSConfig{Transport: "doh", DoHURL: "https://127.0.0.1/dns-query"}},
		{ID: strings.Repeat("a", 121), Host: "owned.test", DNS: &result.DNSConfig{Transport: "tcp"}},
		{ID: "bad/id", Host: "owned.test", DNS: &result.DNSConfig{Transport: "tcp"}},
	} {
		if _, err := New(Config{ProbeID: "p", Server: "127.0.0.1:53", Targets: []result.Target{target}}); err == nil {
			t.Errorf("accepted explicit target %+v", target)
		}
	}
	p, err := New(Config{ProbeID: "p", Server: "127.0.0.1:53"})
	if err != nil {
		t.Fatal(err)
	}
	for _, target := range []result.Target{{ID: "owned:dns:udp", Host: "owned.test"}, {ID: "owned", Host: "owned.test", DNS: &result.DNSConfig{Transport: "tcp"}}} {
		if _, err := p.Probe(context.Background(), target); err == nil {
			t.Errorf("direct library accepted unconfigured/reserved target %+v", target)
		}
	}
}

func TestDNSExplicitUDPAndTCPWire(t *testing.T) {
	for _, mode := range []string{"udp", "tcp"} {
		t.Run(mode, func(t *testing.T) {
			var correct, wrong atomic.Int32
			server := testdns.Start(t, func(network string, q *dns.Msg) *dns.Msg {
				if network == mode {
					correct.Add(1)
				} else {
					wrong.Add(1)
				}
				return testdns.Answer(q, "127.0.0.1", "::1")
			})
			p, target := configuredProbe(t, server, `{"transport":"`+mode+`"}`)
			got, err := p.Probe(context.Background(), target)
			if err != nil || len(got) != 2 || got[0].Addr != "127.0.0.1" || got[1].Addr != "::1" || got[0].Err != "" || got[1].Err != "" || correct.Load() != 2 || wrong.Load() != 0 {
				t.Fatalf("wrong transport or answer: %+v %v correct=%d wrong=%d", got, err, correct.Load(), wrong.Load())
			}
		})
	}
}
