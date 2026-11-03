// Package testdns provides owned UDP/TCP loopback DNS fixtures for probe tests.
package testdns

import (
	"net"
	"testing"
	"time"

	"github.com/miekg/dns"
)

func Start(t *testing.T, respond func(network string, request *dns.Msg) *dns.Msg) string {
	t.Helper()
	// A free TCP port can still be taken for UDP by another process; retry.
	var tcp net.Listener
	var udp net.PacketConn
	var err error
	for attempt := 0; attempt < 20; attempt++ {
		if tcp, err = net.Listen("tcp4", "127.0.0.1:0"); err != nil {
			t.Fatal(err)
		}
		if udp, err = net.ListenPacket("udp4", tcp.Addr().String()); err == nil {
			break
		}
		tcp.Close()
	}
	if err != nil {
		t.Fatal(err)
	}
	for _, endpoint := range []struct {
		network  string
		listener net.Listener
		packet   net.PacketConn
	}{{"tcp", tcp, nil}, {"udp", nil, udp}} {
		ready, done := make(chan struct{}), make(chan error, 1)
		server := &dns.Server{Listener: endpoint.listener, PacketConn: endpoint.packet, NotifyStartedFunc: func() { close(ready) }}
		server.Handler = dns.HandlerFunc(func(w dns.ResponseWriter, q *dns.Msg) {
			if response := respond(endpoint.network, q); response != nil {
				_ = w.WriteMsg(response)
			}
		})
		go func() { done <- server.ActivateAndServe() }()
		select {
		case <-ready:
		case err := <-done:
			t.Fatal(err)
		case <-time.After(time.Second):
			t.Fatal("DNS fixture startup timed out")
		}
		t.Cleanup(func() {
			if err := server.Shutdown(); err != nil {
				t.Error(err)
			}
			if err := <-done; err != nil {
				t.Error(err)
			}
		})
	}
	return tcp.Addr().String()
}

func Answer(q *dns.Msg, addresses ...string) *dns.Msg {
	m := new(dns.Msg).SetReply(q)
	for _, address := range addresses {
		ip := net.ParseIP(address)
		h := dns.RR_Header{Name: q.Question[0].Name, Class: dns.ClassINET, Ttl: 60}
		if ip.To4() != nil && q.Question[0].Qtype == dns.TypeA {
			h.Rrtype = dns.TypeA
			m.Answer = append(m.Answer, &dns.A{Hdr: h, A: ip})
		}
		if ip.To4() == nil && q.Question[0].Qtype == dns.TypeAAAA {
			h.Rrtype = dns.TypeAAAA
			m.Answer = append(m.Answer, &dns.AAAA{Hdr: h, AAAA: ip})
		}
	}
	return m
}
