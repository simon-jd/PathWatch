package resolve

import (
	"bytes"
	"context"
	"crypto/tls"
	"crypto/x509"
	"fmt"
	"io"
	"mime"
	"net"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/miekg/dns"
)

// TransportConfig is only used by explicit DNS measurements. New retains the
// legacy resolver contract for TCP, TLS, HTTP and trace probes.
type TransportConfig struct {
	Transport, Server, DoHURL, CAFile string
	Timeout                           time.Duration
}

func NewTransport(cfg TransportConfig) (*Resolver, error) {
	switch cfg.Transport {
	case "udp", "tcp":
		if cfg.DoHURL != "" || cfg.CAFile != "" {
			return nil, fmt.Errorf("DoH URL/CA require doh transport")
		}
		r, err := New(cfg.Server, cfg.Timeout)
		if err != nil {
			return nil, err
		}
		r.transport = cfg.Transport
		return r, nil
	case "doh":
		if cfg.Server != "" {
			return nil, fmt.Errorf("doh transport cannot use a DNS server address")
		}
	default:
		return nil, fmt.Errorf("DNS transport must be udp, tcp or doh")
	}
	if cfg.Timeout < 0 {
		return nil, fmt.Errorf("timeout must be nonnegative")
	}
	if cfg.Timeout == 0 {
		cfg.Timeout = DefaultTimeout
	}
	u, err := url.Parse(cfg.DoHURL)
	if err != nil || u.Scheme != "https" || u.Hostname() == "" || u.User != nil || u.Fragment != "" || u.Opaque != "" {
		return nil, fmt.Errorf("DoH URL must be absolute HTTPS without userinfo or fragment")
	}
	if port := u.Port(); port != "" {
		n, err := strconv.Atoi(port)
		if err != nil || n < 1 || n > 65535 {
			return nil, fmt.Errorf("invalid DoH port")
		}
	}
	var roots *x509.CertPool
	if cfg.CAFile != "" {
		raw, err := os.ReadFile(cfg.CAFile)
		if err != nil {
			return nil, fmt.Errorf("DoH CA: %w", err)
		}
		roots = x509.NewCertPool()
		if !roots.AppendCertsFromPEM(raw) {
			return nil, fmt.Errorf("DoH CA contains no certificates")
		}
	}
	transport := &http.Transport{
		Proxy:                  nil,
		DialContext:            (&net.Dialer{Timeout: cfg.Timeout}).DialContext,
		TLSClientConfig:        &tls.Config{RootCAs: roots, MinVersion: tls.VersionTLS12},
		TLSHandshakeTimeout:    cfg.Timeout,
		ResponseHeaderTimeout:  cfg.Timeout,
		MaxResponseHeaderBytes: 16 << 10,
		DisableCompression:     true,
		DisableKeepAlives:      true,
		ForceAttemptHTTP2:      true,
	}
	client := &http.Client{Transport: transport, Timeout: cfg.Timeout, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	return &Resolver{timeout: cfg.Timeout, transport: "doh", doh: &dohClient{url: u.String(), client: client}}, nil
}

type dohClient struct {
	url    string
	client *http.Client
}

func (d *dohClient) exchange(ctx context.Context, q *dns.Msg) (*dns.Msg, error) {
	// RFC 8484 correlates using HTTP and recommends ID zero for cacheability.
	q = q.Copy()
	q.Id = 0
	wire, err := q.Pack()
	if err != nil {
		return nil, err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, d.url, bytes.NewReader(wire))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/dns-message")
	req.Header.Set("Accept", "application/dns-message")
	response, err := d.client.Do(req)
	if err != nil {
		return nil, err
	}
	defer response.Body.Close()
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return nil, fmt.Errorf("DoH HTTP status %d", response.StatusCode)
	}
	media, _, err := mime.ParseMediaType(response.Header.Get("Content-Type"))
	if err != nil || media != "application/dns-message" {
		return nil, fmt.Errorf("invalid DoH content type")
	}
	// RFC 8484's wire media type is bounded by the DNS 65535-byte maximum.
	wire, err = io.ReadAll(io.LimitReader(response.Body, 65536))
	if err != nil {
		return nil, err
	}
	if len(wire) > 65535 {
		return nil, fmt.Errorf("DoH response exceeds 65535 bytes")
	}
	m := new(dns.Msg)
	if err = m.Unpack(wire); err != nil {
		return nil, fmt.Errorf("malformed DoH DNS response: %w", err)
	}
	if !m.Response || m.Opcode != q.Opcode || m.Id != q.Id || len(m.Question) != 1 || m.Question[0].Qtype != q.Question[0].Qtype || m.Question[0].Qclass != q.Question[0].Qclass || !strings.EqualFold(m.Question[0].Name, q.Question[0].Name) {
		return nil, fmt.Errorf("mismatched DoH DNS response")
	}
	return m, nil
}
