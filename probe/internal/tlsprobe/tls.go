// Package tlsprobe measures verified TLS handshakes on fresh TCP connections.
package tlsprobe

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"fmt"
	"net"
	"strconv"
	"strings"
	"time"

	"pathwatch.local/pathwatch/probe/internal/resolve"
	"pathwatch.local/pathwatch/probe/internal/result"
)

type Config struct {
	ProbeID, Server string
	Timeout         time.Duration
	RootCAs         *x509.CertPool
	WallNow         func() time.Time
}

type Probe struct {
	resolver interface {
		Timeout() time.Duration
		Lookup(context.Context, string) ([]resolve.Answer, error)
	}
	probeID  string
	roots    *x509.CertPool
	wallNow  func() time.Time
	sessions tls.ClientSessionCache
}

var _ result.Prober = (*Probe)(nil)

func New(cfg Config) (*Probe, error) {
	if strings.TrimSpace(cfg.ProbeID) == "" {
		return nil, fmt.Errorf("probe ID is required")
	}
	r, err := resolve.New(cfg.Server, cfg.Timeout)
	if err != nil {
		return nil, err
	}
	if cfg.WallNow == nil {
		cfg.WallNow = time.Now
	}
	if cfg.RootCAs != nil {
		cfg.RootCAs = cfg.RootCAs.Clone()
	}
	return &Probe{resolver: r, probeID: cfg.ProbeID, roots: cfg.RootCAs, wallNow: cfg.WallNow, sessions: tls.NewLRUClientSessionCache(64)}, nil
}

func (p *Probe) Probe(ctx context.Context, target result.Target) ([]result.Result, error) {
	if strings.TrimSpace(target.ID) == "" {
		return nil, fmt.Errorf("target ID is required")
	}
	if target.Port < 1 || target.Port > 65535 {
		return nil, fmt.Errorf("target port must be in 1..65535")
	}
	host, err := resolve.Host(target.Host)
	if err != nil {
		return nil, err
	}
	if err = ctx.Err(); err != nil {
		return nil, err
	}
	round, cancel := context.WithTimeout(ctx, p.resolver.Timeout())
	defer cancel()
	wall := p.wallNow()
	start := time.Now()
	answers, err := p.resolver.Lookup(round, host)
	answers = resolve.PreserveFailure(answers, err, time.Since(start))
	results := make([]result.Result, 0, len(answers))
	for _, a := range answers {
		if err = ctx.Err(); err != nil {
			return results, err
		}
		r := result.Result{TargetID: target.ID, ProbeID: p.probeID, Kind: result.TLS, Family: a.Family, Addr: a.Addr, StartedAt: wall, Phases: map[string]time.Duration{"dns": a.Duration}, Err: a.Err}
		if a.Err == "" {
			p.measure(round, host, target.Port, &r)
		}
		results = append(results, r)
	}
	return results, ctx.Err()
}

func (p *Probe) measure(ctx context.Context, host string, port int, r *result.Result) {
	network := "tcp4"
	if r.Family == "v6" {
		network = "tcp6"
	}
	start := time.Now()
	raw, err := (&net.Dialer{}).DialContext(ctx, network, net.JoinHostPort(r.Addr, strconv.Itoa(port)))
	r.Phases["connect"] = time.Since(start)
	if err != nil {
		r.Err = err.Error()
		return
	}
	defer raw.Close()
	stop := context.AfterFunc(ctx, func() { raw.Close() })
	defer stop()
	if deadline, ok := ctx.Deadline(); ok {
		_ = raw.SetDeadline(deadline)
	}
	// Per-authority cache keys include the port. SNI/certificate verification always
	// use the original host, while the TCP socket uses the measured literal IP.
	conn := tls.Client(raw, &tls.Config{ServerName: strings.TrimSuffix(host, "."), RootCAs: p.roots, MinVersion: tls.VersionTLS13, ClientSessionCache: authorityCache{p.sessions, net.JoinHostPort(host, strconv.Itoa(port))}})
	start = time.Now()
	err = conn.HandshakeContext(ctx)
	r.Phases["tls_handshake"] = time.Since(start)
	if err != nil {
		r.Err = err.Error()
		return
	}
	state := conn.ConnectionState()
	r.TLSVersion = state.Version
	r.TLSResumed = state.DidResume
	// TLS 1.3 tickets arrive after the handshake. Read processes control records
	// without sending an application request; cap this optional read at 50ms and
	// one application byte. A peer need not issue a ticket or application data.
	deadline := time.Now().Add(50 * time.Millisecond)
	if d, ok := ctx.Deadline(); ok && d.Before(deadline) {
		deadline = d
	}
	_ = conn.SetReadDeadline(deadline)
	var b [1]byte
	_, _ = conn.Read(b[:])
}

type authorityCache struct {
	cache     tls.ClientSessionCache
	authority string
}

func (c authorityCache) Get(key string) (*tls.ClientSessionState, bool) {
	return c.cache.Get(c.authority + "|" + key)
}
func (c authorityCache) Put(key string, state *tls.ClientSessionState) {
	c.cache.Put(c.authority+"|"+key, state)
}
