package traceprobe

import (
	"context"
	"encoding/binary"
	"errors"
	"net"
	"testing"
	"time"

	"github.com/miekg/dns"
	"golang.org/x/net/icmp"
	"golang.org/x/net/ipv4"

	"pathwatch.local/pathwatch/probe/internal/result"
	"pathwatch.local/pathwatch/probe/internal/testdns"
)

type packet struct {
	data []byte
	from net.Addr
}
type fakeConn struct {
	packets   []packet
	dest      net.IP
	port, ttl int
	closed    bool
	sends     int
	foreign   bool
}

func quote(dest net.IP, sport, dport int) []byte {
	b := make([]byte, 28)
	b[0] = 0x45
	b[9] = 17
	binary.BigEndian.PutUint16(b[2:4], 28)
	copy(b[12:16], net.ParseIP("127.0.0.1").To4())
	copy(b[16:20], dest.To4())
	binary.BigEndian.PutUint16(b[20:22], uint16(sport))
	binary.BigEndian.PutUint16(b[22:24], uint16(dport))
	binary.BigEndian.PutUint16(b[24:26], 8)
	return b
}
func wire(t ipv4.ICMPType, code int, q []byte) []byte {
	m := icmp.Message{Type: t, Code: code}
	if t == ipv4.ICMPTypeTimeExceeded {
		m.Body = &icmp.TimeExceeded{Data: q}
	} else {
		m.Body = &icmp.DstUnreach{Data: q}
	}
	b, _ := m.Marshal(nil)
	return b
}
func (f *fakeConn) Send(ttl int, dest *net.UDPAddr) (int, error) {
	f.ttl = ttl
	f.sends++
	f.dest = dest.IP
	f.port = dest.Port
	if f.foreign {
		f.packets = []packet{{[]byte{1, 2, 3}, &net.IPAddr{IP: dest.IP}}, {wire(ipv4.ICMPTypeDestinationUnreachable, 3, quote(dest.IP, 42001, dest.Port)), &net.IPAddr{IP: dest.IP}}, {wire(ipv4.ICMPTypeDestinationUnreachable, 3, quote(net.ParseIP("127.0.0.2"), 42000, dest.Port)), &net.IPAddr{IP: dest.IP}}, {wire(ipv4.ICMPTypeDestinationUnreachable, 3, quote(dest.IP, 42000, dest.Port)), &net.IPAddr{IP: net.ParseIP("127.0.0.2")}}}
		return 42000, nil
	}
	typ := ipv4.ICMPTypeTimeExceeded
	code := 0
	from := net.ParseIP("10.0.0." + string(rune('0'+ttl)))
	if ttl == 4 {
		typ = ipv4.ICMPTypeDestinationUnreachable
		code = 3
		from = dest.IP
	}
	f.packets = []packet{{wire(typ, code, quote(dest.IP, 42000, dest.Port)), &net.IPAddr{IP: from}}}
	return 42000, nil
}
func (f *fakeConn) ReadFrom(b []byte) (int, net.Addr, error) {
	if len(f.packets) == 0 {
		return 0, nil, &net.OpError{Op: "read", Err: timeoutError{}}
	}
	p := f.packets[0]
	f.packets = f.packets[1:]
	return copy(b, p.data), p.from, nil
}
func (f *fakeConn) SetReadDeadline(time.Time) error { return nil }
func (f *fakeConn) Close() error                    { f.closed = true; return nil }

type timeoutError struct{}

func (timeoutError) Error() string   { return "timeout" }
func (timeoutError) Timeout() bool   { return true }
func (timeoutError) Temporary() bool { return true }
func TestTraceProbe_ParsesHops(t *testing.T) {
	f := &fakeConn{}
	p, err := New(Config{ProbeID: "p", MaxTTL: 8, Open: func() (PacketConn, error) { return f, nil }})
	if err != nil {
		t.Fatal(err)
	}
	got, err := p.Probe(context.Background(), result.Target{ID: "t", Host: "127.0.0.1", Port: 33434})
	if err != nil || len(got) != 1 || got[0].Err != "" || len(got[0].Hops) != 4 || got[0].Phases["total"] <= 0 || len(got[0].Phases) != 1 || !f.closed {
		t.Fatalf("got=%+v err=%v closed=%v", got, err, f.closed)
	}
	for i, h := range got[0].Hops {
		if h.TTL != i+1 || h.Addr == "" || h.RTT <= 0 {
			t.Fatalf("hop=%+v", h)
		}
	}
	t.Logf("hops=%+v", got[0].Hops)
}
func TestTraceProbe_RejectsForeignAndBounds(t *testing.T) {
	f := &fakeConn{foreign: true}
	p, _ := New(Config{ProbeID: "p", MaxTTL: 3, Open: func() (PacketConn, error) { return f, nil }})
	got, err := p.Probe(context.Background(), result.Target{ID: "t", Host: "127.0.0.1", Port: 33434})
	if err != nil || len(got) != 1 || len(got[0].Hops) != 3 || f.sends != 3 || !f.closed {
		t.Fatalf("got=%+v err=%v", got, err)
	}
	for _, h := range got[0].Hops {
		if h.Addr != "" || h.RTT != 0 {
			t.Fatalf("foreign accepted %+v", h)
		}
	}
}
func TestTraceProbe_PublicAndIPv6(t *testing.T) {
	opened := 0
	p, _ := New(Config{ProbeID: "p", Open: func() (PacketConn, error) { opened++; return &fakeConn{}, nil }})
	for _, target := range []result.Target{{ID: "t", Host: "127.0.0.1", Public: true}, {ID: "t", Host: "8.8.8.8"}} {
		got, err := p.Probe(context.Background(), target)
		if err == nil && (len(got) == 0 || got[0].Err == "") {
			t.Fatalf("public accepted=%+v %v", got, err)
		}
	}
	got, err := p.Probe(context.Background(), result.Target{ID: "t", Host: "::1"})
	if err != nil || len(got) != 1 || got[0].Err == "" || opened != 0 {
		t.Fatalf("IPv6=%+v %v opened=%d", got, err, opened)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err = p.Probe(ctx, result.Target{ID: "t", Host: "127.0.0.1"}); !errors.Is(err, context.Canceled) {
		t.Fatalf("cancel=%v", err)
	}
}

type blockingConn struct {
	net.PacketConn
	sent chan struct{}
}

func (b *blockingConn) Send(int, *net.UDPAddr) (int, error) { close(b.sent); return 42000, nil }
func TestTraceProbe_ContextInterruptsICMPRead(t *testing.T) {
	socket, err := net.ListenPacket("udp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	b := &blockingConn{PacketConn: socket, sent: make(chan struct{})}
	defer b.Close()
	p, _ := New(Config{ProbeID: "p", Timeout: time.Second, HopTimeout: time.Second, Open: func() (PacketConn, error) { return b, nil }})
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() { _, err := p.Probe(ctx, result.Target{ID: "t", Host: "127.0.0.1"}); done <- err }()
	<-b.sent
	cancel()
	select {
	case err := <-done:
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("cancel=%v", err)
		}
	case <-time.After(time.Second):
		t.Fatal("ICMP read did not stop on cancel")
	}
}
func TestTraceProbe_ResolvedGlobalRejected(t *testing.T) {
	server := testdns.Start(t, func(_ string, q *dns.Msg) *dns.Msg { return testdns.Answer(q, "8.8.8.8") })
	opened := false
	p, _ := New(Config{ProbeID: "p", Server: server, Open: func() (PacketConn, error) { opened = true; return &fakeConn{}, nil }})
	got, err := p.Probe(context.Background(), result.Target{ID: "t", Host: "owned.test"})
	if err != nil || len(got) != 1 || got[0].Err == "" || opened {
		t.Fatalf("global resolved result=%+v err=%v opened=%v", got, err, opened)
	}
}
func TestTraceProbe_MalformedInnerHeader(t *testing.T) {
	dest := net.ParseIP("127.0.0.1")
	q := quote(dest, 42000, 33434)
	q[24] = 0
	q[25] = 0
	if _, _, ok := matchedHop(wire(ipv4.ICMPTypeDestinationUnreachable, 3, q), &net.IPAddr{IP: dest}, dest, 42000, 33434); ok {
		t.Fatal("malformed quoted UDP length was accepted")
	}
}

func TestTraceProbe_InvalidPortAndRealReadDeadline(t *testing.T) {
	p, _ := New(Config{ProbeID: "p", Open: func() (PacketConn, error) { return &fakeConn{}, nil }})
	if _, err := p.Probe(context.Background(), result.Target{ID: "t", Host: "127.0.0.1", Port: int(^uint(0) >> 1)}); err == nil {
		t.Fatal("overflowing destination port accepted")
	}
	socket, err := net.ListenPacket("udp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer socket.Close()
	b := &blockingConn{PacketConn: socket, sent: make(chan struct{})}
	p, _ = New(Config{ProbeID: "p", MaxTTL: 1, HopTimeout: 20 * time.Millisecond, Timeout: time.Second, Open: func() (PacketConn, error) { return b, nil }})
	start := time.Now()
	got, err := p.Probe(context.Background(), result.Target{ID: "t", Host: "127.0.0.1"})
	elapsed := time.Since(start)
	if err != nil || len(got) != 1 || got[0].Err != "" || len(got[0].Hops) != 1 || got[0].Hops[0].Addr != "" || got[0].Hops[0].RTT != 0 || elapsed < 20*time.Millisecond || elapsed > time.Second {
		t.Fatalf("deadline got=%+v err=%v elapsed=%s", got, err, elapsed)
	}
}

func TestTraceProbe_LastTTLReportsRoundDeadline(t *testing.T) {
	const rounds = 100
	lost := 0
	for i := 0; i < rounds; i++ {
		socket, err := net.ListenPacket("udp4", "127.0.0.1:0")
		if err != nil {
			t.Fatal(err)
		}
		b := &blockingConn{PacketConn: socket, sent: make(chan struct{})}
		p, err := New(Config{ProbeID: "p", MaxTTL: 1, Timeout: time.Millisecond, HopTimeout: time.Second, Open: func() (PacketConn, error) { return b, nil }})
		if err != nil {
			socket.Close()
			t.Fatal(err)
		}
		start := time.Now()
		got, err := p.Probe(context.Background(), result.Target{ID: "t", Host: "127.0.0.1"})
		elapsed := time.Since(start)
		socket.Close()
		if err != nil || len(got) != 1 || elapsed > time.Second {
			t.Fatalf("round=%d result=%+v err=%v elapsed=%s", i, got, err, elapsed)
		}
		if got[0].Err != context.DeadlineExceeded.Error() {
			if lost == 0 {
				t.Logf("first lost deadline: round=%d elapsed=%s result=%+v", i, elapsed, got[0])
			}
			lost++
		}
	}
	if lost != 0 {
		t.Fatalf("library round deadline lost in %d/%d rounds", lost, rounds)
	}
	t.Logf("last-TTL total deadline reported as Result.Err in %d/%d real socket rounds; top-level error nil", rounds, rounds)
}

func TestTraceProbe_CallerDeadlineIsTopLevel(t *testing.T) {
	socket, err := net.ListenPacket("udp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer socket.Close()
	b := &blockingConn{PacketConn: socket, sent: make(chan struct{})}
	p, err := New(Config{ProbeID: "p", MaxTTL: 1, Timeout: time.Second, HopTimeout: time.Second, Open: func() (PacketConn, error) { return b, nil }})
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 40*time.Millisecond)
	defer cancel()
	got, err := p.Probe(ctx, result.Target{ID: "t", Host: "127.0.0.1"})
	if !errors.Is(err, context.DeadlineExceeded) || len(got) != 1 || got[0].Err != context.DeadlineExceeded.Error() {
		t.Fatalf("caller deadline result=%+v err=%v", got, err)
	}
}
