// Package adminbridge exposes only an authenticated, bounded metrics transport.
package adminbridge

import (
	"crypto/tls"
	"crypto/x509"
	"errors"
	"fmt"
	"net/url"
	"os"
	"pathwatch.local/pathwatch/ingest/internal/corerpc"
	"strings"
	"time"
)

type Config struct {
	Upstream                         string
	CA, Cert, Key                    string
	ServerName, ServerURI, ClientURI string
}

type identity struct {
	config    *tls.Config
	local     [][]*x509.Certificate
	clientURI string
}

func exactRole(c *x509.Certificate, role string) bool {
	return len(c.URIs) == 1 && c.URIs[0].String() == role
}
func validateRole(role string) error {
	u, e := url.Parse(role)
	if e != nil || u.Scheme != "spiffe" || u.Host == "" || u.Port() != "" || u.Path == "" || u.User != nil || u.RawQuery != "" || u.ForceQuery || u.Fragment != "" || u.RawPath != "" || strings.ContainsAny(role, "* \t\r\n") {
		return errors.New("one exact SPIFFE URI required")
	}
	return nil
}
func loadIdentity(c Config) (*identity, error) {
	if c.CA == "" || c.Cert == "" || c.Key == "" || c.ServerName == "" {
		return nil, errors.New("explicit CA, certificate, key and server DNS required")
	}
	for _, label := range strings.Split(c.ServerName, ".") {
		if label == "" || strings.HasPrefix(label, "-") || strings.HasSuffix(label, "-") {
			return nil, errors.New("exact server DNS required")
		}
		for _, ch := range label {
			if !(ch >= 'a' && ch <= 'z' || ch >= 'A' && ch <= 'Z' || ch >= '0' && ch <= '9' || ch == '-') {
				return nil, errors.New("exact server DNS required")
			}
		}
	}
	if e := validateRole(c.ServerURI); e != nil {
		return nil, e
	}
	if e := validateRole(c.ClientURI); e != nil {
		return nil, e
	}
	if c.ServerURI == c.ClientURI {
		return nil, errors.New("server and client roles must differ")
	}
	cert, e := tls.LoadX509KeyPair(c.Cert, c.Key)
	if e != nil {
		return nil, e
	}
	leaf, e := x509.ParseCertificate(cert.Certificate[0])
	if e != nil {
		return nil, e
	}
	cert.Leaf = leaf
	if e := corerpc.ExactCertificateSAN(leaf, c.ServerName); e != nil {
		return nil, e
	}
	if !corerpc.CertificateHasUsage(leaf, x509.ExtKeyUsageServerAuth) || !exactRole(leaf, c.ServerURI) {
		return nil, errors.New("invalid local server identity")
	}
	ca, e := os.ReadFile(c.CA)
	if e != nil {
		return nil, e
	}
	roots := x509.NewCertPool()
	if !roots.AppendCertsFromPEM(ca) {
		return nil, errors.New("invalid explicit CA")
	}
	inter := x509.NewCertPool()
	for _, der := range cert.Certificate[1:] {
		v, e := x509.ParseCertificate(der)
		if e != nil {
			return nil, e
		}
		inter.AddCert(v)
	}
	chains, e := leaf.Verify(x509.VerifyOptions{Roots: roots, Intermediates: inter, KeyUsages: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}})
	if e != nil {
		return nil, fmt.Errorf("local certificate chain: %w", e)
	}
	id := &identity{local: chains, clientURI: c.ClientURI}
	tc := &tls.Config{MinVersion: tls.VersionTLS13, MaxVersion: tls.VersionTLS13, ClientAuth: tls.RequireAndVerifyClientCert, ClientCAs: roots, Certificates: []tls.Certificate{cert}, NextProtos: []string{"http/1.1"}, SessionTicketsDisabled: true}
	tc.VerifyConnection = func(s tls.ConnectionState) error { _, e := id.check(&s); return e }
	id.config = tc
	return id, nil
}
func (i *identity) check(s *tls.ConnectionState) (time.Time, error) {
	if s == nil || s.Version != tls.VersionTLS13 || len(s.PeerCertificates) == 0 || len(s.VerifiedChains) == 0 {
		return time.Time{}, errors.New("authenticated TLS1.3 peer required")
	}
	peer := s.PeerCertificates[0]
	if !corerpc.CertificateHasUsage(peer, x509.ExtKeyUsageClientAuth) || !exactRole(peer, i.clientURI) {
		return time.Time{}, errors.New("unauthorized metrics client identity")
	}
	local, ok := corerpc.ValidChainDeadline(i.local)
	until, peerOK := corerpc.ValidChainDeadline(s.VerifiedChains)
	if !ok || !peerOK {
		return time.Time{}, errors.New("invalid certificate chain time")
	}
	if local.Before(until) {
		until = local
	}
	return until, nil
}
