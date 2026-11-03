package queryapi

import (
	"errors"
	"net"
	"strconv"
	"sync"
)

// ValidateAddress permits only an explicit literal-loopback address and port.
func ValidateAddress(address string) error {
	h, p, e := net.SplitHostPort(address)
	n, x := strconv.Atoi(p)
	ip := net.ParseIP(h)
	if e != nil || x != nil || n < 1 || n > 65535 || ip == nil || !ip.IsLoopback() {
		return errors.New("query listener requires literal loopback IP and port 1..65535")
	}
	return nil
}

// LimitListener closes surplus connections; no unbounded goroutine waits for a slot.
func LimitListener(l net.Listener) net.Listener {
	return &limitedListener{Listener: l, slots: make(chan struct{}, 16)}
}

type limitedListener struct {
	net.Listener
	slots chan struct{}
}

func (l *limitedListener) Accept() (net.Conn, error) {
	for {
		c, e := l.Listener.Accept()
		if e != nil {
			return nil, e
		}
		select {
		case l.slots <- struct{}{}:
			return &limitedConn{Conn: c, slots: l.slots}, nil
		default:
			c.Close()
		}
	}
}

type limitedConn struct {
	net.Conn
	slots chan struct{}
	once  sync.Once
}

func (c *limitedConn) Close() error { e := c.Conn.Close(); c.once.Do(func() { <-c.slots }); return e }
