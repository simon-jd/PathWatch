//go:build linux

package traceprobe

import (
	"encoding/binary"
	"errors"
	"net"
	"syscall"
	"time"

	"golang.org/x/net/ipv4"
	"golang.org/x/sys/unix"
)

// recverrConn is the unprivileged mode (like tracepath): plain UDP probes with
// IP_RECVERR, so the kernel queues each matching ICMP error on this socket's
// error queue. No raw socket and no CAP_NET_RAW are needed.
type recverrConn struct {
	udp  *net.UDPConn
	raw  syscall.RawConn
	ipv4 *ipv4.PacketConn
}

func openRecvErr() (PacketConn, error) {
	udp, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4zero})
	if err != nil {
		return nil, err
	}
	raw, err := udp.SyscallConn()
	if err == nil {
		var set error
		if err = raw.Control(func(fd uintptr) { set = unix.SetsockoptInt(int(fd), unix.SOL_IP, unix.IP_RECVERR, 1) }); err == nil {
			err = set
		}
	}
	if err != nil {
		udp.Close()
		return nil, err
	}
	return &recverrConn{udp: udp, raw: raw, ipv4: ipv4.NewPacketConn(udp)}, nil
}

func (c *recverrConn) Send(ttl int, dest *net.UDPAddr) (int, error) {
	if err := c.ipv4.SetTTL(ttl); err != nil {
		return 0, err
	}
	_, err := c.udp.WriteToUDP([]byte("pathwatch"), dest)
	return c.udp.LocalAddr().(*net.UDPAddr).Port, err
}

func (c *recverrConn) ReadFrom([]byte) (int, net.Addr, error) {
	return 0, nil, errors.New("UDP trace mode reads the socket error queue")
}
func (c *recverrConn) SetReadDeadline(t time.Time) error { return c.udp.SetReadDeadline(t) }
func (c *recverrConn) Close() error                      { return c.udp.Close() }

// ReadErr waits (bounded by the read deadline) for one error queue entry.
// An ICMP error raises EPOLLERR, which Go's poller reports as readable.
func (c *recverrConn) ReadErr() (ErrReport, error) {
	var report ErrReport
	var failure error
	var buf [64]byte
	var oob [512]byte
	err := c.raw.Read(func(fd uintptr) bool {
		_, oobn, _, from, e := unix.Recvmsg(int(fd), buf[:], oob[:], unix.MSG_ERRQUEUE)
		if e == unix.EAGAIN || e == unix.EWOULDBLOCK {
			return false
		}
		if e != nil {
			failure = e
		} else {
			report, failure = parseErrQueue(oob[:oobn], from)
		}
		return true
	})
	if err != nil {
		return report, err
	}
	return report, failure
}

// parseErrQueue reads struct sock_extended_err and the SO_EE_OFFENDER
// sockaddr_in after it; msg_name is the original datagram's destination.
func parseErrQueue(oob []byte, from unix.Sockaddr) (ErrReport, error) {
	var r ErrReport
	messages, err := unix.ParseSocketControlMessage(oob)
	if err != nil {
		return r, err
	}
	found := false
	for _, m := range messages {
		if m.Header.Level != unix.SOL_IP || m.Header.Type != unix.IP_RECVERR || len(m.Data) < 16 {
			continue
		}
		found = true
		r.Origin, r.Type, r.Code = m.Data[4], m.Data[5], m.Data[6]
		if len(m.Data) >= 24 && binary.NativeEndian.Uint16(m.Data[16:18]) == unix.AF_INET {
			r.Offender = net.IPv4(m.Data[20], m.Data[21], m.Data[22], m.Data[23]).To4()
		}
	}
	if !found {
		return r, errors.New("error queue message without IP_RECVERR data")
	}
	if sa, ok := from.(*unix.SockaddrInet4); ok {
		r.Dest = &net.UDPAddr{IP: net.IP(sa.Addr[:]).To4(), Port: sa.Port}
	}
	return r, nil
}
