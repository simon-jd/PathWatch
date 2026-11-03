// Package result defines the common contract consumed by probers and reporting.
package result

import (
	"context"
	"time"
)

type Kind string

const (
	DNS   Kind = "dns"
	TCP   Kind = "tcp"
	TLS   Kind = "tls"
	HTTP1 Kind = "http1"
	HTTP2 Kind = "http2"
	HTTP3 Kind = "http3"
	Trace Kind = "trace"
)

// DNSConfig selects an explicit DNS-only measurement transport. An absent
// config keeps the historical UDP-with-TCP-fallback resolver.
type DNSConfig struct {
	Transport string `json:"transport"`
	Server    string `json:"server,omitempty"`
	DoHURL    string `json:"doh_url,omitempty"`
	CAFile    string `json:"ca_file,omitempty"`
}

type Target struct {
	DNS      *DNSConfig `json:"DNS,omitempty"`
	ID, Host string
	Port     int
	Public   bool
	Kinds    []Kind
}

// Hop identifies a responding TTL; an unknown hop has empty Addr and zero RTT.
type Hop struct {
	TTL  int
	Addr string
	RTT  time.Duration
}

type Result struct {
	// Protocol is the actual HTTP response protocol: h1, h2 or h3.
	Protocol string
	Hops     []Hop
	// TLS metadata is populated only after a successful TLS probe handshake.
	TLSVersion        uint16
	TLSResumed        bool
	TargetID, ProbeID string
	Kind              Kind
	Family            string
	Addr              string
	StartedAt         time.Time
	Phases            map[string]time.Duration
	Err               string
}

type Prober interface {
	Probe(context.Context, Target) ([]Result, error)
}
