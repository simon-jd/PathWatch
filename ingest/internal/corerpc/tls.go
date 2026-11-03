package corerpc

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"fmt"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/keepalive"
	"google.golang.org/grpc/peer"
	"google.golang.org/grpc/status"
	"net"
	"net/url"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"
)

type TLSFiles struct {
	CA, Cert, Key, ServerName string
	Frontends                 []string
}
type Trust struct {
	config      *tls.Config
	leaf        *x509.Certificate
	localChains [][]*x509.Certificate
	allowed     map[string]bool
	server      bool
}

func LoadTrust(f TLSFiles, server bool) (*Trust, error) {
	if f.CA == "" || f.Cert == "" || f.Key == "" || f.ServerName == "" {
		return nil, fmt.Errorf("explicit CA, certificate, key and server SAN required")
	}
	if net.ParseIP(f.ServerName) == nil && strings.ContainsAny(f.ServerName, "/*:@ ") {
		return nil, fmt.Errorf("server name must be exact DNS/IP SAN")
	}
	cert, e := tls.LoadX509KeyPair(f.Cert, f.Key)
	if e != nil {
		return nil, e
	}
	leaf, e := x509.ParseCertificate(cert.Certificate[0])
	if e != nil {
		return nil, e
	}
	cert.Leaf = leaf
	ca, e := os.ReadFile(f.CA)
	if e != nil {
		return nil, e
	}
	roots := x509.NewCertPool()
	if !roots.AppendCertsFromPEM(ca) {
		return nil, fmt.Errorf("invalid CA")
	}
	t := &Trust{leaf: leaf, server: server, allowed: map[string]bool{}}
	usage := x509.ExtKeyUsageClientAuth
	if server {
		usage = x509.ExtKeyUsageServerAuth
		if e := exactSAN(leaf, f.ServerName); e != nil {
			return nil, e
		}
	}
	if !hasUsage(leaf, usage) || !validNow(leaf) {
		return nil, fmt.Errorf("invalid local certificate purpose/time")
	}
	intermediates := x509.NewCertPool()
	for _, der := range cert.Certificate[1:] {
		x, err := x509.ParseCertificate(der)
		if err != nil {
			return nil, err
		}
		intermediates.AddCert(x)
	}
	chains, err := leaf.Verify(x509.VerifyOptions{Roots: roots, Intermediates: intermediates, KeyUsages: []x509.ExtKeyUsage{usage}})
	if err != nil {
		return nil, fmt.Errorf("local certificate chain: %w", err)
	}
	t.localChains = chains
	for _, id := range f.Frontends {
		u, e := url.Parse(id)
		if e != nil || u.Scheme != "spiffe" || u.Host != "pathwatch.local" || u.RawQuery != "" || u.Fragment != "" || u.User != nil || u.RawPath != "" || !strings.HasPrefix(u.Path, "/frontend/") || len(strings.TrimPrefix(u.Path, "/frontend/")) == 0 {
			return nil, fmt.Errorf("exact frontend URI role required")
		}
		t.allowed[id] = true
	}
	if server && len(t.allowed) == 0 {
		return nil, fmt.Errorf("frontend allowlist required")
	}
	c := &tls.Config{MinVersion: tls.VersionTLS13, Certificates: []tls.Certificate{cert}, RootCAs: roots, ServerName: f.ServerName, SessionTicketsDisabled: true}
	if server {
		c.ClientCAs = roots
		c.ClientAuth = tls.RequireAndVerifyClientCert
	}
	c.VerifyConnection = func(s tls.ConnectionState) error { _, e := t.check(s); return e }
	t.config = c
	return t, nil
}
func hasUsage(c *x509.Certificate, u x509.ExtKeyUsage) bool {
	for _, x := range c.ExtKeyUsage {
		if x == u {
			return true
		}
	}
	return false
}
func validNow(c *x509.Certificate) bool {
	n := time.Now()
	return !n.Before(c.NotBefore) && n.Before(c.NotAfter)
}
func (t *Trust) check(s tls.ConnectionState) (time.Time, error) {
	if len(s.VerifiedChains) == 0 || len(s.PeerCertificates) == 0 || !validNow(t.leaf) {
		return time.Time{}, fmt.Errorf("unverified/expired identity")
	}
	p := s.PeerCertificates[0]
	usage := x509.ExtKeyUsageServerAuth
	if t.server {
		usage = x509.ExtKeyUsageClientAuth
	}
	if !validNow(p) || !hasUsage(p, usage) {
		return time.Time{}, fmt.Errorf("peer certificate purpose/time")
	}
	if t.server {
		ok := false
		for _, u := range p.URIs {
			if t.allowed[u.String()] {
				ok = true
			}
		}
		if !ok {
			return time.Time{}, fmt.Errorf("unauthorized frontend identity")
		}
	} else if e := exactSAN(p, t.config.ServerName); e != nil {
		return time.Time{}, e
	}
	localUntil, localOK := chainUntil(t.localChains)
	peerUntil, peerOK := chainUntil(s.VerifiedChains)
	if !localOK || !peerOK {
		return time.Time{}, fmt.Errorf("expired certificate chain")
	}
	if localUntil.Before(peerUntil) {
		peerUntil = localUntil
	}
	return peerUntil, nil
}
func (t *Trust) Authenticate(ctx context.Context) (time.Time, error) {
	p, ok := peer.FromContext(ctx)
	if !ok {
		return time.Time{}, status.Error(codes.Unauthenticated, "missing TLS peer")
	}
	i, ok := p.AuthInfo.(credentials.TLSInfo)
	if !ok {
		return time.Time{}, status.Error(codes.Unauthenticated, "missing TLS identity")
	}
	until, e := t.check(i.State)
	if e != nil {
		return time.Time{}, status.Error(codes.Unauthenticated, "invalid TLS identity")
	}
	return until, nil
}
func (t *Trust) Credentials() credentials.TransportCredentials {
	return credentials.NewTLS(t.config.Clone())
}
func (a *Admission) Options(cred credentials.TransportCredentials) []grpc.ServerOption {
	streams := uint32(2)
	if a.private {
		streams = 4
	}
	o := []grpc.ServerOption{grpc.MaxRecvMsgSize(16 << 20), grpc.MaxSendMsgSize(MaxReply), grpc.MaxHeaderListSize(16 << 10), grpc.MaxConcurrentStreams(streams), grpc.ConnectionTimeout(5 * time.Second), grpc.InitialWindowSize(64 << 10), grpc.InitialConnWindowSize(1 << 20), grpc.InTapHandle(a.Tap), grpc.StatsHandler(a), grpc.ForceServerCodec(codec{}), grpc.KeepaliveParams(keepalive.ServerParameters{MaxConnectionIdle: 15 * time.Second, MaxConnectionAge: 5 * time.Minute, MaxConnectionAgeGrace: 30 * time.Second})}
	if cred != nil {
		o = append(o, grpc.Creds(cred))
	}
	return o
}

type cappedListener struct {
	net.Listener
	slots chan struct{}
}
type cappedConn struct {
	net.Conn
	once    sync.Once
	release func()
}

func (c *cappedConn) Close() error { e := c.Conn.Close(); c.once.Do(c.release); return e }
func LimitListener(l net.Listener, n int) net.Listener {
	return &cappedListener{l, make(chan struct{}, n)}
}
func (l *cappedListener) Accept() (net.Conn, error) {
	for {
		c, e := l.Listener.Accept()
		if e != nil {
			return nil, e
		}
		select {
		case l.slots <- struct{}{}:
			return &cappedConn{Conn: c, release: func() { <-l.slots }}, nil
		default:
			c.Close()
		}
	}
}

func exactSAN(c *x509.Certificate, name string) error {
	if ip := net.ParseIP(name); ip != nil {
		for _, x := range c.IPAddresses {
			if x.Equal(ip) {
				return nil
			}
		}
	} else {
		for _, s := range c.DNSNames {
			if s == name {
				return nil
			}
		}
	}
	return fmt.Errorf("exact server SAN missing")
}
func ValidateEndpoint(address string) error {
	h, p, e := net.SplitHostPort(address)
	if e != nil || h == "" {
		return fmt.Errorf("explicit host and port required")
	}
	n, e := strconv.Atoi(p)
	if e != nil || n < 1 || n > 65535 {
		return fmt.Errorf("port must be1..65535")
	}
	return nil
}

// A chain verified at handshake may expire while its connection remains open.
// At least one complete local and peer chain must still be within validity.
func chainUntil(chains [][]*x509.Certificate) (time.Time, bool) {
	var latest time.Time
	for _, chain := range chains {
		if len(chain) == 0 {
			continue
		}
		until := chain[0].NotAfter
		valid := true
		for _, c := range chain {
			if !validNow(c) {
				valid = false
				break
			}
			if c.NotAfter.Before(until) {
				until = c.NotAfter
			}
		}
		if valid && until.After(latest) {
			latest = until
		}
	}
	return latest, !latest.IsZero()
}

// ExactCertificateSAN requires an exact DNS or IP SAN, without wildcard or CN fallback.
func ExactCertificateSAN(c *x509.Certificate, name string) error { return exactSAN(c, name) }

// CertificateHasUsage requires the explicit extended key usage on the leaf.
func CertificateHasUsage(c *x509.Certificate, usage x509.ExtKeyUsage) bool { return hasUsage(c, usage) }

// ValidChainDeadline returns the latest deadline among complete currently valid chains.
// Callers must supply chains previously verified against their explicit trust roots.
func ValidChainDeadline(chains [][]*x509.Certificate) (time.Time, bool) { return chainUntil(chains) }
