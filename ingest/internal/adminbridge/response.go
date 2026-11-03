package adminbridge

import (
	"bufio"
	"bytes"
	"context"
	"errors"
	"io"
	"net"
	"net/textproto"
	"strconv"
	"strings"
)

// observedConn examines one bounded raw response header before Transport can
// normalize ambiguous framing. DisableKeepAlives guarantees one exchange per
// connection. All original bytes, including bounded read-ahead, are replayed.
// The unchanged net/http parser remains responsible for HTTP/body framing.
type observedConn struct {
	net.Conn
	pending []byte
	checked bool
}

func observedDial(ctx context.Context, network, address string) (net.Conn, error) {
	c, e := (&net.Dialer{Timeout: requestLimit}).DialContext(ctx, network, address)
	if e != nil {
		return nil, e
	}
	return &observedConn{Conn: c}, nil
}
func (c *observedConn) Read(p []byte) (int, error) {
	if len(p) == 0 {
		return 0, nil
	}
	if !c.checked {
		data := make([]byte, 0, headerLimit)
		for {
			end := bytes.Index(data, []byte("\r\n\r\n"))
			if end >= 0 {
				if e := checkResponseHeader(data[:end+4]); e != nil {
					return 0, e
				}
				c.checked = true
				c.pending = data
				break
			}
			if len(data) == cap(data) {
				return 0, errors.New("upstream header exceeds raw bound")
			}
			old := len(data)
			data = data[:cap(data)]
			n, e := c.Conn.Read(data[old:])
			data = data[:old+n]
			if e != nil {
				if n == 0 {
					return 0, e
				}
				if bytes.Index(data, []byte("\r\n\r\n")) < 0 {
					return 0, io.ErrUnexpectedEOF
				}
			}
		}
	}
	if len(c.pending) > 0 {
		n := copy(p, c.pending)
		c.pending = c.pending[n:]
		if len(c.pending) == 0 {
			c.pending = nil
		}
		return n, nil
	}
	return c.Conn.Read(p)
}
func checkResponseHeader(b []byte) error {
	r := textproto.NewReader(bufio.NewReader(bytes.NewReader(b)))
	status, e := r.ReadLine()
	if e != nil {
		return e
	}
	parts := strings.SplitN(status, " ", 3)
	if len(parts) < 2 {
		return errors.New("invalid upstream status")
	}
	code, e := strconv.Atoi(parts[1])
	if e != nil || code < 200 || code > 599 {
		return errors.New("unsupported informational/status response")
	}
	h, e := r.ReadMIMEHeader()
	if e != nil {
		return e
	}
	cl, te := h.Values("Content-Length"), h.Values("Transfer-Encoding")
	if len(cl) > 1 || len(te) > 1 || len(cl) > 0 && len(te) > 0 {
		return errors.New("ambiguous upstream framing")
	}
	if len(te) > 0 && strings.ToLower(strings.TrimSpace(te[0])) != "chunked" {
		return errors.New("unsupported transfer encoding")
	}
	for _, k := range []string{"Content-Encoding", "Upgrade", "Trailer"} {
		if _, ok := h[k]; ok {
			return errors.New("unsupported response envelope")
		}
	}
	for _, v := range h.Values("Connection") {
		for _, token := range strings.Split(v, ",") {
			if strings.EqualFold(strings.TrimSpace(token), "upgrade") {
				return errors.New("upstream upgrade rejected")
			}
		}
	}
	return nil
}
