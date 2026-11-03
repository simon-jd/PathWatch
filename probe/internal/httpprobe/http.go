// Package httpprobe measures a verified HEAD / request on each resolved IP.
package httpprobe

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"fmt"
	"net"
	"net/http"
	"net/http/httptrace"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/quic-go/quic-go"
	"github.com/quic-go/quic-go/http3"

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
	proto, probeID string
	roots          *x509.CertPool
	wallNow        func() time.Time
}

var _ result.Prober = (*Probe)(nil)

// New supplies a usable local probe identity; deployments should set ProbeID.
func New(proto string) (*Probe, error) { return NewWithConfig(proto, Config{ProbeID: "local"}) }
func NewWithConfig(proto string, cfg Config) (*Probe, error) {
	if proto != "h1" && proto != "h2" && proto != "h3" {
		return nil, fmt.Errorf("protocol must be h1, h2 or h3")
	}
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
	return &Probe{resolver: r, proto: proto, probeID: cfg.ProbeID, roots: cfg.RootCAs, wallNow: cfg.WallNow}, nil
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
	kind := map[string]result.Kind{"h1": result.HTTP1, "h2": result.HTTP2, "h3": result.HTTP3}[p.proto]
	for _, a := range answers {
		if err = ctx.Err(); err != nil {
			return results, err
		}
		r := result.Result{TargetID: target.ID, ProbeID: p.probeID, Kind: kind, Family: a.Family, Addr: a.Addr, StartedAt: wall, Phases: map[string]time.Duration{"dns": a.Duration}, Err: a.Err}
		if a.Err == "" {
			p.measure(round, host, target.Port, &r)
		}
		results = append(results, r)
	}
	return results, ctx.Err()
}
func (p *Probe) measure(ctx context.Context, host string, port int, r *result.Result) {
	authority := net.JoinHostPort(host, strconv.Itoa(port))
	dest := net.JoinHostPort(r.Addr, strconv.Itoa(port))
	tlsCfg := &tls.Config{RootCAs: p.roots, ServerName: strings.TrimSuffix(host, "."), MinVersion: tls.VersionTLS12}
	var transport http.RoundTripper
	if p.proto == "h3" {
		tr := &http3.Transport{TLSClientConfig: tlsCfg, MaxResponseHeaderBytes: 64 << 10, DisableCompression: true, Dial: func(ctx context.Context, _ string, cfg *tls.Config, qcfg *quic.Config) (*quic.Conn, error) {
			return quic.DialAddrEarly(ctx, dest, cfg, qcfg)
		}}
		transport = tr
		defer tr.Close()
	} else {
		tr := &http.Transport{TLSClientConfig: tlsCfg, Proxy: nil, ForceAttemptHTTP2: p.proto == "h2", DisableKeepAlives: true, DisableCompression: true, MaxResponseHeaderBytes: 64 << 10, DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
			network := "tcp4"
			if r.Family == "v6" {
				network = "tcp6"
			}
			return (&net.Dialer{}).DialContext(ctx, network, dest)
		}}
		if p.proto == "h1" {
			tr.TLSNextProto = map[string]func(string, *tls.Conn) http.RoundTripper{}
		}
		transport = tr
		defer tr.CloseIdleConnections()
	}
	start := time.Now()
	var mu sync.Mutex
	var first time.Duration
	trace := &httptrace.ClientTrace{GotFirstResponseByte: func() {
		mu.Lock()
		if first == 0 {
			first = time.Since(start)
		}
		mu.Unlock()
	}}
	req, err := http.NewRequestWithContext(httptrace.WithClientTrace(ctx, trace), http.MethodHead, "https://"+authority+"/", nil)
	if err != nil {
		r.Err = err.Error()
		return
	}
	// RoundTrip itself never follows a redirect. Each transport has a single
	// measured destination; no proxy, alt-svc discovery, retry loop or body read.
	response, err := transport.RoundTrip(req)
	if response != nil {
		response.Body.Close()
	}
	r.Phases["total"] = time.Since(start)
	mu.Lock()
	if first > 0 {
		r.Phases["ttfb"] = first
	}
	mu.Unlock()
	if err != nil {
		r.Err = err.Error()
		return
	}
	r.Protocol = map[int]string{1: "h1", 2: "h2", 3: "h3"}[response.ProtoMajor]
	if r.Protocol != p.proto {
		r.Err = fmt.Sprintf("requested %s, negotiated %s", p.proto, r.Protocol)
	}
}
