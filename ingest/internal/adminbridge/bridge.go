package adminbridge

import (
	"context"
	"crypto/tls"
	"errors"
	"io"
	"log"
	"net"
	"net/http"
	"pathwatch.local/pathwatch/ingest/internal/corerpc"
	"strconv"
	"strings"
	"sync"
	"time"
)

const bodyLimit = 64 << 10
const headerLimit = 8192
const requestLimit = 2 * time.Second

type Bridge struct {
	id                *identity
	upstream          string
	transport         *http.Transport
	client            *http.Client
	fetches           chan struct{}
	mu                sync.Mutex
	running, draining bool
	users             sync.WaitGroup
	// ErrorLog records standard HTTP/TLS handshake failures without request bodies or keys.
	ErrorLog *log.Logger
}

func canonicalEndpoint(address string, loopback bool) error {
	h, p, e := net.SplitHostPort(address)
	if e != nil {
		return errors.New("literal IP and canonical port required")
	}
	ip := net.ParseIP(h)
	n, e := strconv.Atoi(p)
	if e != nil || n < 1 || n > 65535 || p != strconv.Itoa(n) || ip == nil || h != ip.String() {
		return errors.New("literal canonical IP and port 1..65535 required")
	}
	if loopback && h != "127.0.0.1" && h != "::1" {
		return errors.New("upstream must be 127.0.0.1 or ::1")
	}
	return nil
}
func ValidateListen(address string) error { return canonicalEndpoint(address, false) }
func transport() *http.Transport {
	return &http.Transport{Proxy: nil, DialContext: observedDial, DisableCompression: true, DisableKeepAlives: true, MaxConnsPerHost: 2, ResponseHeaderTimeout: requestLimit, MaxResponseHeaderBytes: headerLimit, ReadBufferSize: 4096, WriteBufferSize: 4096}
}
func New(c Config) (*Bridge, error) {
	if e := canonicalEndpoint(c.Upstream, true); e != nil {
		return nil, e
	}
	id, e := loadIdentity(c)
	if e != nil {
		return nil, e
	}
	tr := transport()
	return &Bridge{id: id, upstream: c.Upstream, transport: tr, client: &http.Client{Transport: tr, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}, fetches: make(chan struct{}, 2)}, nil
}

// Serve owns l from entry, including failure. It is single-use. Cancellation closes
// connections, cancels fetches, and joins all admitted handlers before returning.
func (b *Bridge) Serve(ctx context.Context, l net.Listener) error {
	defer l.Close()
	b.mu.Lock()
	if b.running {
		b.mu.Unlock()
		return errors.New("bridge already served")
	}
	b.running = true
	b.mu.Unlock()
	base, cancel := context.WithCancel(ctx)
	defer cancel()
	defer b.transport.CloseIdleConnections()
	srv := &http.Server{Handler: b, ReadHeaderTimeout: requestLimit, ReadTimeout: requestLimit, WriteTimeout: requestLimit, IdleTimeout: 5 * time.Second, MaxHeaderBytes: headerLimit, ErrorLog: b.ErrorLog, BaseContext: func(net.Listener) context.Context { return base }, TLSNextProto: map[string]func(*http.Server, *tls.Conn, http.Handler){}}
	protocols := new(http.Protocols)
	protocols.SetHTTP1(true)
	srv.Protocols = protocols
	finished := make(chan error, 1)
	go func() { finished <- srv.Serve(tls.NewListener(corerpc.LimitListener(l, 4), b.id.config)) }()
	var result error
	serveReturned := false
	select {
	case result = <-finished:
		serveReturned = true
	case <-ctx.Done():
	}
	b.mu.Lock()
	b.draining = true
	b.mu.Unlock()
	cancel()
	srv.Close()
	if !serveReturned {
		result = <-finished
	}
	joined := make(chan struct{})
	go func() { b.users.Wait(); close(joined) }()
	select {
	case <-joined:
	case <-time.After(3 * time.Second):
		return errors.New("bridge shutdown users exceeded 3s")
	}
	if errors.Is(result, http.ErrServerClosed) || ctx.Err() != nil {
		return nil
	}
	return result
}

func reject(w http.ResponseWriter, r *http.Request, status int, message string) {
	w.Header().Set("Connection", "close")
	r.Close = true
	http.Error(w, message, status)
}
func headerSize(r *http.Request) int {
	n := len("Host") + len(r.Host) + 4
	for k, vs := range r.Header {
		for _, v := range vs {
			n += len(k) + len(v) + 4
		}
	}
	return n + 2
}
func (b *Bridge) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	b.mu.Lock()
	if b.draining {
		b.mu.Unlock()
		reject(w, r, 503, "draining")
		return
	}
	b.users.Add(1)
	b.mu.Unlock()
	defer b.users.Done()
	deadline := time.Now().Add(requestLimit)
	until, e := b.id.check(r.TLS)
	if e != nil {
		reject(w, r, 401, "invalid TLS identity")
		return
	}
	if until.Before(deadline) {
		deadline = until
	}
	ctl := http.NewResponseController(w)
	if e := ctl.SetWriteDeadline(deadline); e != nil {
		reject(w, r, 500, "write deadline unavailable")
		return
	}
	if r.ProtoMajor != 1 || r.ProtoMinor != 1 || r.Method != "GET" || r.RequestURI != "/metrics" || r.URL.Path != "/metrics" || r.URL.RawPath != "" || r.URL.RawQuery != "" || r.URL.ForceQuery || r.URL.Fragment != "" || r.URL.IsAbs() || r.ContentLength != 0 || len(r.TransferEncoding) != 0 || r.Header.Get("Upgrade") != "" || connectionUpgrade(r.Header) || r.Header.Get("Expect") != "" || len(r.Trailer) != 0 || r.Body != http.NoBody && r.Body != nil {
		reject(w, r, 400, "only bodyless GET /metrics permitted")
		return
	}
	if headerSize(r) > headerLimit {
		reject(w, r, 431, "headers too large")
		return
	}
	select {
	case b.fetches <- struct{}{}:
		defer func() { <-b.fetches }()
	default:
		reject(w, r, 503, "metrics busy")
		return
	}
	ctx, cancel := context.WithDeadline(r.Context(), deadline)
	defer cancel()
	response, body, e := fetch(ctx, b.client, b.upstream, "/metrics", bodyLimit)
	if e != nil {
		reject(w, r, 502, "invalid metrics response")
		return
	}
	if ctx.Err() != nil || !time.Now().Before(deadline) {
		reject(w, r, 504, "metrics deadline")
		return
	}
	if _, e = b.id.check(r.TLS); e != nil {
		reject(w, r, 401, "invalid TLS identity")
		return
	}
	if v := response.Header.Get("Content-Type"); v != "" {
		w.Header().Set("Content-Type", v)
	}
	w.Header().Set("Content-Length", strconv.Itoa(len(body)))
	w.WriteHeader(response.StatusCode)
	_, _ = w.Write(body)
}

func fetch(ctx context.Context, c *http.Client, address, path string, limit int) (*http.Response, []byte, error) {
	r, e := http.NewRequestWithContext(ctx, "GET", "http://"+address+path, nil)
	if e != nil {
		return nil, nil, e
	}
	r.Header.Set("Accept-Encoding", "identity")
	resp, e := c.Do(r)
	if e != nil {
		return nil, nil, e
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 && resp.StatusCode < 400 || resp.StatusCode == 101 || resp.Header.Get("Content-Encoding") != "" || resp.Uncompressed || resp.Header.Get("Upgrade") != "" || len(resp.Trailer) != 0 || resp.ContentLength > int64(limit) {
		return nil, nil, errors.New("unsupported upstream envelope")
	}
	if len(resp.TransferEncoding) > 1 || len(resp.TransferEncoding) == 1 && resp.TransferEncoding[0] != "chunked" {
		return nil, nil, errors.New("unsupported transfer encoding")
	}
	size := 2
	for k, vs := range resp.Header {
		for _, v := range vs {
			size += len(k) + len(v) + 4
		}
	}
	if size > headerLimit {
		return nil, nil, errors.New("upstream headers too large")
	}
	body, e := io.ReadAll(io.LimitReader(resp.Body, int64(limit)+1))
	if e != nil {
		return nil, nil, e
	}
	if len(body) > limit || resp.ContentLength >= 0 && int64(len(body)) != resp.ContentLength || len(resp.Trailer) != 0 {
		return nil, nil, errors.New("incomplete or excessive upstream body")
	}
	return resp, body, nil
}

// Header parsing is performed by net/http. MaxHeaderBytes8192 has4096 bytes of
// parser allowance in the pinned Go runtime; headerSize adds an explicit bound
// on normalized names/values, separators and Host before any forwarding.

func connectionUpgrade(h http.Header) bool {
	for _, v := range h.Values("Connection") {
		for _, token := range strings.Split(v, ",") {
			if strings.EqualFold(strings.TrimSpace(token), "upgrade") {
				return true
			}
		}
	}
	return false
}
