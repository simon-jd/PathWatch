// Package traceprobe measures private IPv4 UDP paths with matched ICMP quotes.
package traceprobe

import (
	"context"
	"encoding/binary"
	"fmt"
	"net"
	"strings"
	"time"

	"golang.org/x/net/icmp"
	"golang.org/x/net/ipv4"

	"pathwatch.local/pathwatch/probe/internal/resolve"
	"pathwatch.local/pathwatch/probe/internal/result"
)

// PacketConn owns both sending and ICMP receiving resources. Send sets the TTL
// and returns the actual UDP source port used in the quoted-packet match.
// Close must be safe concurrently with Send/ReadFrom and unblock pending IO,
// matching net.PacketConn semantics.
type PacketConn interface {
	Send(ttl int, dest *net.UDPAddr) (sourcePort int, err error)
	ReadFrom([]byte) (int, net.Addr, error)
	SetReadDeadline(time.Time) error
	Close() error
}

// Both modes send UDP probes with rising TTLs; they differ in how the ICMP
// errors come back. ModeICMP reads a raw ICMP socket (needs CAP_NET_RAW);
// ModeUDP reads the probe socket's IP_RECVERR error queue (Linux, unprivileged,
// tracepath style).
const (
	ModeICMP = "icmp"
	ModeUDP  = "udp"
)

type Config struct {
	ProbeID, Server     string
	Timeout, HopTimeout time.Duration
	MaxTTL              int
	Mode                string // ModeICMP (default) or ModeUDP
	WallNow             func() time.Time
	Open                func() (PacketConn, error)
}

// ErrReport is one IP_RECVERR error queue entry: the ICMP type/code, the
// router that sent it, and the destination of the datagram that caused it.
type ErrReport struct {
	Origin, Type, Code uint8
	Offender           net.IP
	Dest               *net.UDPAddr
}

// errQueue is implemented by connections that receive errors through the
// socket error queue instead of ReadFrom on an ICMP socket.
type errQueue interface {
	ReadErr() (ErrReport, error)
}
type Probe struct {
	resolver interface {
		Timeout() time.Duration
		Lookup(context.Context, string) ([]resolve.Answer, error)
	}
	cfg Config
}

var _ result.Prober = (*Probe)(nil)

func New(cfg Config) (*Probe, error) {
	if strings.TrimSpace(cfg.ProbeID) == "" {
		return nil, fmt.Errorf("probe ID is required")
	}
	if cfg.MaxTTL == 0 {
		cfg.MaxTTL = 30
	}
	if cfg.MaxTTL < 1 || cfg.MaxTTL > 64 {
		return nil, fmt.Errorf("max TTL must be in 1..64")
	}
	if cfg.HopTimeout == 0 {
		cfg.HopTimeout = 250 * time.Millisecond
	}
	if cfg.HopTimeout < 0 {
		return nil, fmt.Errorf("hop timeout must be nonnegative")
	}
	r, err := resolve.New(cfg.Server, cfg.Timeout)
	if err != nil {
		return nil, err
	}
	if cfg.WallNow == nil {
		cfg.WallNow = time.Now
	}
	switch cfg.Mode {
	case "", ModeICMP:
		if cfg.Open == nil {
			cfg.Open = openPackets
		}
	case ModeUDP:
		if cfg.Open == nil {
			cfg.Open = openRecvErr
		}
	default:
		return nil, fmt.Errorf("trace mode must be %q or %q", ModeICMP, ModeUDP)
	}
	return &Probe{resolver: r, cfg: cfg}, nil
}
func (p *Probe) Probe(ctx context.Context, target result.Target) ([]result.Result, error) {
	if target.Public {
		return nil, fmt.Errorf("UDP traceroute is restricted to owned private targets")
	}
	if strings.TrimSpace(target.ID) == "" {
		return nil, fmt.Errorf("target ID is required")
	}
	port := target.Port
	if port == 0 {
		port = 33434
	}
	if port < 1 || port > 65535 || port+p.cfg.MaxTTL-1 > 65535 {
		return nil, fmt.Errorf("trace port range exceeds 1..65535")
	}
	host, err := resolve.Host(target.Host)
	if err != nil {
		return nil, err
	}
	if err = contextFailure(ctx); err != nil {
		return nil, err
	}
	round, cancel := context.WithTimeout(ctx, p.resolver.Timeout())
	defer cancel()
	wall := p.cfg.WallNow()
	start := time.Now()
	answers, err := p.resolver.Lookup(round, host)
	answers = resolve.PreserveFailure(answers, err, time.Since(start))
	results := make([]result.Result, 0, len(answers))
	for _, a := range answers {
		if err = contextFailure(ctx); err != nil {
			return results, err
		}
		r := result.Result{TargetID: target.ID, ProbeID: p.cfg.ProbeID, Kind: result.Trace, Family: a.Family, Addr: a.Addr, StartedAt: wall, Phases: map[string]time.Duration{}, Err: a.Err}
		measurement := time.Now()
		if a.Err == "" {
			ip := net.ParseIP(a.Addr)
			if ip == nil || (!ip.IsPrivate() && !ip.IsLoopback()) {
				r.Err = "UDP traceroute destination is not private or loopback"
			} else if a.Family != "v4" {
				r.Err = "IPv6 UDP traceroute is unsupported"
			} else {
				p.measure(round, ip, port, &r)
			}
		}
		r.Phases["total"] = time.Since(measurement)
		results = append(results, r)
	}
	return results, contextFailure(ctx)
}
func (p *Probe) measure(ctx context.Context, dest net.IP, port int, r *result.Result) {
	if err := contextFailure(ctx); err != nil {
		r.Err = err.Error()
		return
	}
	conn, err := p.cfg.Open()
	if err != nil {
		r.Err = err.Error()
		return
	}
	defer conn.Close()
	stop := context.AfterFunc(ctx, func() { conn.Close() })
	defer stop()
	var buf [1500]byte
	for ttl := 1; ttl <= p.cfg.MaxTTL; ttl++ {
		if err := contextFailure(ctx); err != nil {
			r.Err = err.Error()
			return
		}
		dport := port + ttl - 1
		start := time.Now()
		source, err := conn.Send(ttl, &net.UDPAddr{IP: dest, Port: dport})
		if err != nil {
			r.Err = err.Error()
			return
		}
		deadline := start.Add(p.cfg.HopTimeout)
		if d, ok := ctx.Deadline(); ok && d.Before(deadline) {
			deadline = d
		}
		if err = conn.SetReadDeadline(deadline); err != nil {
			r.Err = err.Error()
			return
		}
		hop := result.Hop{TTL: ttl}
		terminal := false
		// Time and packet bounds apply even when unrelated ICMP floods the socket.
		for packets := 0; packets < 256 && time.Now().Before(deadline); packets++ {
			if err := contextFailure(ctx); err != nil {
				r.Err = err.Error()
				return
			}
			addr, last, ok, readErr := next(conn, buf[:], dest, source, dport)
			if readErr != nil {
				if err := contextFailure(ctx); err != nil {
					r.Err = err.Error()
					return
				}
				if e, ok := readErr.(net.Error); ok && e.Timeout() {
					break
				}
				r.Err = readErr.Error()
				return
			}
			if !ok {
				continue
			}
			hop.Addr = addr
			hop.RTT = time.Since(start)
			terminal = last
			break
		}
		// Socket deadlines and context timers are scheduled independently. In
		// particular, the final TTL must not turn an expired round deadline
		// into a successful unknown hop merely because ctx.Err is still nil.
		if err := contextFailure(ctx); err != nil {
			r.Err = err.Error()
			return
		}
		r.Hops = append(r.Hops, hop)
		if terminal {
			return
		}
	}
}

// contextFailure also checks the real monotonic deadline, which may precede
// delivery of Done/Err. Applied to the round context it classifies library
// timeout per result; applied to the caller context it preserves the top-level
// cancellation/deadline contract.
func contextFailure(ctx context.Context) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	if deadline, ok := ctx.Deadline(); ok && !time.Now().Before(deadline) {
		return context.DeadlineExceeded
	}
	return nil
}

func next(conn PacketConn, buf []byte, dest net.IP, sport, dport int) (string, bool, bool, error) {
	if q, ok := conn.(errQueue); ok {
		r, err := q.ReadErr()
		if err != nil {
			return "", false, false, err
		}
		addr, last, matched := matchedReport(r, dest, dport)
		return addr, last, matched, nil
	}
	n, from, err := conn.ReadFrom(buf)
	if err != nil {
		return "", false, false, err
	}
	addr, last, matched := matchedHop(buf[:n], from, dest, sport, dport)
	return addr, last, matched, nil
}

// matchedReport applies matchedHop's rules to an error queue entry. The kernel
// already matched the quoted datagram to this socket (hence its source port).
func matchedReport(r ErrReport, dest net.IP, dport int) (string, bool, bool) {
	const originICMP = 2 // SO_EE_ORIGIN_ICMP
	if r.Origin != originICMP || r.Dest == nil || !r.Dest.IP.Equal(dest) || r.Dest.Port != dport || r.Offender.To4() == nil {
		return "", false, false
	}
	switch {
	case r.Type == uint8(ipv4.ICMPTypeTimeExceeded) && r.Code == 0:
		return r.Offender.String(), false, true
	case r.Type == uint8(ipv4.ICMPTypeDestinationUnreachable) && r.Code == 3 && r.Offender.Equal(dest):
		return r.Offender.String(), true, true
	}
	return "", false, false
}

func matchedHop(packet []byte, from net.Addr, dest net.IP, sport, dport int) (string, bool, bool) {
	message, err := icmp.ParseMessage(1, packet)
	if err != nil {
		return "", false, false
	}
	var quote []byte
	terminal := false
	switch body := message.Body.(type) {
	case *icmp.TimeExceeded:
		if message.Type != ipv4.ICMPTypeTimeExceeded || message.Code != 0 {
			return "", false, false
		}
		quote = body.Data
	case *icmp.DstUnreach:
		if message.Type != ipv4.ICMPTypeDestinationUnreachable || message.Code != 3 {
			return "", false, false
		}
		quote = body.Data
		terminal = true
	default:
		return "", false, false
	}
	if len(quote) < 28 || quote[0]>>4 != 4 {
		return "", false, false
	}
	header := int(quote[0]&15) * 4
	if header < 20 || len(quote) < header+8 || quote[9] != 17 || binary.BigEndian.Uint16(quote[6:8])&0x1fff != 0 || !net.IP(quote[16:20]).Equal(dest) {
		return "", false, false
	}
	total := int(binary.BigEndian.Uint16(quote[2:4]))
	udpLength := int(binary.BigEndian.Uint16(quote[header+4 : header+6]))
	if total < header+8 || udpLength < 8 || udpLength > total-header {
		return "", false, false
	}
	if int(binary.BigEndian.Uint16(quote[header:header+2])) != sport || int(binary.BigEndian.Uint16(quote[header+2:header+4])) != dport {
		return "", false, false
	}
	ipAddr, ok := from.(*net.IPAddr)
	if !ok || ipAddr.IP.To4() == nil {
		return "", false, false
	}
	if terminal && !ipAddr.IP.Equal(dest) {
		return "", false, false
	}
	return ipAddr.IP.String(), terminal, true
}

type packets struct {
	udp  *net.UDPConn
	icmp net.PacketConn
	ipv4 *ipv4.PacketConn
}

func openPackets() (PacketConn, error) {
	raw, err := net.ListenPacket("ip4:icmp", "0.0.0.0")
	if err != nil {
		return nil, err
	}
	udp, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4zero})
	if err != nil {
		raw.Close()
		return nil, err
	}
	return &packets{udp: udp, icmp: raw, ipv4: ipv4.NewPacketConn(udp)}, nil
}
func (p *packets) Send(ttl int, dest *net.UDPAddr) (int, error) {
	if err := p.ipv4.SetTTL(ttl); err != nil {
		return 0, err
	}
	_, err := p.udp.WriteToUDP([]byte("pathwatch"), dest)
	return p.udp.LocalAddr().(*net.UDPAddr).Port, err
}
func (p *packets) ReadFrom(b []byte) (int, net.Addr, error) { return p.icmp.ReadFrom(b) }
func (p *packets) SetReadDeadline(t time.Time) error        { return p.icmp.SetReadDeadline(t) }
func (p *packets) Close() error {
	err := p.udp.Close()
	other := p.icmp.Close()
	if err != nil {
		return err
	}
	return other
}
