// Package resolve shares ordered A/AAAA resolution between DNS and TCP probes.
package resolve

import (
	"context"
	"fmt"
	"net"
	"net/netip"
	"strconv"
	"strings"
	"time"

	"github.com/miekg/dns"
)

const DefaultTimeout = 5 * time.Second

type Resolver struct {
	server    string
	timeout   time.Duration
	transport string
	doh       *dohClient
}

type Answer struct {
	Family, Addr string
	Duration     time.Duration
	Err          string
}

// PreserveFailure keeps an otherwise-unrepresented resolution error beside any
// partial answers. Its family is unknown and its duration covers the lookup.
func PreserveFailure(answers []Answer, err error, duration time.Duration) []Answer {
	if err == nil {
		return answers
	}
	for _, answer := range answers {
		if answer.Err == err.Error() {
			return answers
		}
	}
	return append(answers, Answer{Duration: duration, Err: err.Error()})
}

// New selects the first system nameserver if server is empty. An explicit
// server must be a literal IP:port; resolving the resolver would escape bounds.
func New(server string, timeout time.Duration) (*Resolver, error) {
	if timeout < 0 {
		return nil, fmt.Errorf("timeout must be nonnegative")
	}
	if timeout == 0 {
		timeout = DefaultTimeout
	}
	if server == "" {
		cfg, err := dns.ClientConfigFromFile("/etc/resolv.conf")
		if err != nil {
			return nil, fmt.Errorf("system DNS config: %w", err)
		}
		if len(cfg.Servers) == 0 {
			return nil, fmt.Errorf("system DNS config has no nameservers")
		}
		server = net.JoinHostPort(cfg.Servers[0], cfg.Port)
	}
	host, port, err := net.SplitHostPort(server)
	if err != nil {
		return nil, fmt.Errorf("DNS server must be literal IP:port: %w", err)
	}
	if _, err := netip.ParseAddr(host); err != nil {
		return nil, fmt.Errorf("invalid DNS server IP: %w", err)
	}
	n, err := strconv.Atoi(port)
	if err != nil || n < 1 || n > 65535 {
		return nil, fmt.Errorf("invalid DNS server port %q", port)
	}
	return &Resolver{server: server, timeout: timeout}, nil
}

func (r *Resolver) Timeout() time.Duration { return r.timeout }

// Host accepts an unbracketed hostname/IP or bracketed IPv6 literal, never an
// embedded port. Port belongs to Target.Port and is joined by net.JoinHostPort.
func Host(host string) (string, error) {
	if strings.HasPrefix(host, "[") && strings.HasSuffix(host, "]") {
		ip, err := netip.ParseAddr(host[1 : len(host)-1])
		if err == nil && ip.Is6() {
			return ip.String(), nil
		}
	}
	if ip, err := netip.ParseAddr(host); err == nil {
		return ip.Unmap().String(), nil
	}
	if host == "" || strings.ContainsAny(host, ":/\\[] \t\r\n") {
		return "", fmt.Errorf("invalid target host %q", host)
	}
	if _, ok := dns.IsDomainName(host); !ok {
		return "", fmt.Errorf("invalid DNS name %q", host)
	}
	return host, nil
}

// Lookup preserves answer order within v4, then v6. NODATA in one family is
// normal; transport/rcode failures remain observable in that family's result.
// Caller owns the round deadline. Cancellation returns partial answers + error.
func (r *Resolver) Lookup(ctx context.Context, host string) ([]Answer, error) {
	return r.lookup(ctx, host, r.exchange)
}

func (r *Resolver) lookup(ctx context.Context, host string, exchange func(context.Context, string, *dns.Msg) (*dns.Msg, error)) ([]Answer, error) {
	host, err := Host(host)
	if err != nil {
		return nil, err
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if ip, err := netip.ParseAddr(host); err == nil {
		family := "v6"
		if ip.Is4() {
			family = "v4"
		}
		return []Answer{{Family: family, Addr: ip.String()}}, nil
	}
	var answers []Answer
	lookupStart := time.Now()
	for _, query := range []struct {
		qtype  uint16
		family string
	}{{dns.TypeA, "v4"}, {dns.TypeAAAA, "v6"}} {
		if err := ctx.Err(); err != nil {
			// The previous family may have succeeded. Make the family we could
			// not start visible instead of silently reporting a complete lookup.
			answers = append(answers, Answer{Family: query.family, Duration: time.Since(lookupStart), Err: err.Error()})
			return answers, err
		}
		start := time.Now()
		q := new(dns.Msg).SetQuestion(dns.Fqdn(host), query.qtype)
		network := r.transport
		if network == "" {
			network = "udp"
		}
		response, err := exchange(ctx, network, q)
		if err == nil && response.Truncated && r.transport == "" {
			response, err = exchange(ctx, "tcp", q)
		}
		duration := time.Since(start)
		if err != nil {
			if ctxErr := ctx.Err(); ctxErr != nil {
				err = ctxErr
			}
			answers = append(answers, Answer{Family: query.family, Duration: duration, Err: err.Error()})
		} else if response.Truncated {
			answers = append(answers, Answer{Family: query.family, Duration: duration, Err: "truncated DNS response"})
		} else if response.Rcode != dns.RcodeSuccess {
			answers = append(answers, Answer{Family: query.family, Duration: duration, Err: fmt.Sprintf("DNS rcode %s", dns.RcodeToString[response.Rcode])})
		} else {
			for _, rr := range response.Answer {
				var address string
				switch rr := rr.(type) {
				case *dns.A:
					if query.qtype == dns.TypeA {
						address = rr.A.String()
					}
				case *dns.AAAA:
					if query.qtype == dns.TypeAAAA {
						address = rr.AAAA.String()
					}
				}
				if address != "" {
					answers = append(answers, Answer{Family: query.family, Addr: address, Duration: duration})
				}
			}
		}
	}
	if err := ctx.Err(); err != nil {
		return answers, err
	}
	if len(answers) == 0 {
		answers = append(answers, Answer{Duration: time.Since(lookupStart), Err: "DNS returned no A or AAAA addresses"})
	}
	return answers, nil
}

func (r *Resolver) exchange(ctx context.Context, network string, q *dns.Msg) (*dns.Msg, error) {
	if network == "doh" {
		return r.doh.exchange(ctx, q)
	}
	client := &dns.Client{Net: network, Timeout: r.timeout}
	conn, err := client.DialContext(ctx, r.server)
	if err != nil {
		return nil, err
	}
	defer conn.Close()
	// ExchangeWithConnContext observes deadlines but not cancellation without a
	// deadline. Closing the owned socket interrupts a pending read immediately.
	stop := context.AfterFunc(ctx, func() { _ = conn.Close() })
	defer stop()
	response, _, err := client.ExchangeWithConnContext(ctx, q, conn)
	return response, err
}
