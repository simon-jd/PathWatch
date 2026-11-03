//go:build linux

package traceprobe

import (
	"context"
	"encoding/binary"
	"net"
	"testing"
	"time"
	"unsafe"

	"golang.org/x/sys/unix"

	"pathwatch.local/pathwatch/probe/internal/result"
)

func TestMatchedReport(t *testing.T) {
	dest := net.ParseIP("10.0.0.9")
	router := net.ParseIP("10.0.1.1")
	base := ErrReport{Origin: unix.SO_EE_ORIGIN_ICMP, Type: 11, Code: 0, Offender: router, Dest: &net.UDPAddr{IP: dest, Port: 33435}}
	cases := []struct {
		name           string
		edit           func(*ErrReport)
		addr           string
		terminal, okay bool
	}{
		{"time exceeded", func(*ErrReport) {}, "10.0.1.1", false, true},
		{"port unreachable from destination", func(r *ErrReport) { r.Type, r.Code, r.Offender = 3, 3, dest }, "10.0.0.9", true, true},
		{"port unreachable from another router", func(r *ErrReport) { r.Type, r.Code = 3, 3 }, "", false, false},
		{"host unreachable", func(r *ErrReport) { r.Type, r.Code = 3, 1 }, "", false, false},
		{"local error", func(r *ErrReport) { r.Origin = unix.SO_EE_ORIGIN_LOCAL }, "", false, false},
		{"other destination port", func(r *ErrReport) { r.Dest = &net.UDPAddr{IP: dest, Port: 33436} }, "", false, false},
		{"other destination", func(r *ErrReport) { r.Dest = &net.UDPAddr{IP: router, Port: 33435} }, "", false, false},
		{"no offender", func(r *ErrReport) { r.Offender = nil }, "", false, false},
	}
	for _, c := range cases {
		r := base
		c.edit(&r)
		addr, terminal, ok := matchedReport(r, dest, 33435)
		if addr != c.addr || terminal != c.terminal || ok != c.okay {
			t.Errorf("%s: got %q %v %v", c.name, addr, terminal, ok)
		}
	}
}

func TestParseErrQueueMessage(t *testing.T) {
	// struct sock_extended_err followed by the offender's sockaddr_in.
	data := make([]byte, 32)
	binary.NativeEndian.PutUint32(data[0:4], uint32(unix.EHOSTUNREACH))
	data[4], data[5], data[6] = unix.SO_EE_ORIGIN_ICMP, 11, 0
	binary.NativeEndian.PutUint16(data[16:18], unix.AF_INET)
	copy(data[20:24], net.ParseIP("10.0.1.1").To4())
	oob := cmsg(unix.SOL_IP, unix.IP_RECVERR, data)
	r, err := parseErrQueue(oob, &unix.SockaddrInet4{Port: 33434, Addr: [4]byte{10, 0, 0, 9}})
	if err != nil {
		t.Fatal(err)
	}
	if r.Origin != unix.SO_EE_ORIGIN_ICMP || r.Type != 11 || r.Code != 0 || r.Offender.String() != "10.0.1.1" || r.Dest.String() != "10.0.0.9:33434" {
		t.Fatalf("report=%+v", r)
	}
	if _, err = parseErrQueue(nil, nil); err == nil {
		t.Fatal("empty error queue message accepted")
	}
}

func cmsg(level, typ int32, data []byte) []byte {
	b := make([]byte, unix.CmsgSpace(len(data)))
	h := (*unix.Cmsghdr)(unsafe.Pointer(&b[0]))
	h.Level, h.Type = level, typ
	h.SetLen(unix.CmsgLen(len(data)))
	copy(b[unix.CmsgLen(0):], data)
	return b
}

// A real unprivileged round on loopback: the closed port answers with an ICMP
// port unreachable, which the kernel queues on the probe's own UDP socket.
func TestRecvErr_LoopbackPortUnreachable(t *testing.T) {
	socket, err := net.ListenPacket("udp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	port := socket.LocalAddr().(*net.UDPAddr).Port
	socket.Close()
	if port+7 > 65535 {
		t.Skip("ephemeral port too high for an 8-port range")
	}
	p, err := New(Config{ProbeID: "p", Mode: ModeUDP, MaxTTL: 8, Timeout: 2 * time.Second, HopTimeout: 500 * time.Millisecond})
	if err != nil {
		t.Fatal(err)
	}
	got, err := p.Probe(context.Background(), result.Target{ID: "t", Host: "127.0.0.1", Port: port})
	if err != nil || len(got) != 1 || got[0].Err != "" || len(got[0].Hops) != 1 {
		t.Fatalf("got=%+v err=%v", got, err)
	}
	if h := got[0].Hops[0]; h.TTL != 1 || h.Addr != "127.0.0.1" || h.RTT <= 0 {
		t.Fatalf("hop=%+v", h)
	}
}

func TestModeValidation(t *testing.T) {
	if _, err := New(Config{ProbeID: "p", Mode: "tcp"}); err == nil {
		t.Fatal("unknown trace mode accepted")
	}
	for _, m := range []string{"", ModeICMP, ModeUDP} {
		if _, err := New(Config{ProbeID: "p", Mode: m}); err != nil {
			t.Fatalf("mode %q: %v", m, err)
		}
	}
}
