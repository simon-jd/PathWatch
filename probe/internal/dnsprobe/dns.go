// Package dnsprobe measures ordered A/AAAA queries over configured DNS transports.
package dnsprobe

import (
	"context"
	"fmt"
	"net/netip"
	"regexp"
	"strings"
	"time"

	"pathwatch.local/pathwatch/probe/internal/resolve"
	"pathwatch.local/pathwatch/probe/internal/result"
)

type Config struct {
	ProbeID, Server string
	Timeout         time.Duration
	WallNow         func() time.Time
	Targets         []result.Target
}

type Probe struct {
	resolver interface {
		Timeout() time.Duration
		Lookup(context.Context, string) ([]resolve.Answer, error)
	}
	configured map[string]configuredResolver
	probeID    string
	wallNow    func() time.Time
}

type configuredResolver struct {
	settings result.DNSConfig
	resolver *resolve.Resolver
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
	p := &Probe{resolver: r, probeID: cfg.ProbeID, wallNow: cfg.WallNow, configured: make(map[string]configuredResolver)}
	for _, target := range cfg.Targets {
		if err := validateTarget(target); err != nil {
			return nil, err
		}
		if target.DNS == nil {
			continue
		}
		if _, exists := p.configured[target.ID]; exists {
			return nil, fmt.Errorf("duplicate DNS target %q", target.ID)
		}
		settings := *target.DNS
		server := settings.Server
		if server == "" && settings.Transport != "doh" {
			server = cfg.Server
		}
		resolver, err := resolve.NewTransport(resolve.TransportConfig{Transport: settings.Transport, Server: server, DoHURL: settings.DoHURL, CAFile: settings.CAFile, Timeout: cfg.Timeout})
		if err != nil {
			return nil, fmt.Errorf("DNS target %s: %w", target.ID, err)
		}
		p.configured[target.ID] = configuredResolver{settings: settings, resolver: resolver}
	}
	return p, nil
}

// Probe reports remote failures in Result.Err. Invalid input and caller
// cancellation return a top-level error; the latter may include partial results.
func (p *Probe) Probe(ctx context.Context, target result.Target) ([]result.Result, error) {
	if err := validateTarget(target); err != nil {
		return nil, err
	}
	resolver := p.resolver
	targetID := target.ID
	if target.DNS != nil {
		configured, ok := p.configured[target.ID]
		if !ok || configured.settings != *target.DNS {
			return nil, fmt.Errorf("DNS target %q was not configured at startup", target.ID)
		}
		resolver = configured.resolver
		targetID += ":dns:" + configured.settings.Transport
	}
	host, err := resolve.Host(target.Host)
	if err != nil {
		return nil, err
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	round, cancel := context.WithTimeout(ctx, resolver.Timeout())
	defer cancel()
	startedAt := p.wallNow()
	resolveStart := time.Now()
	answers, resolveErr := resolver.Lookup(round, host)
	answers = resolve.PreserveFailure(answers, resolveErr, time.Since(resolveStart))
	results := make([]result.Result, 0, len(answers))
	for _, answer := range answers {
		results = append(results, result.Result{TargetID: targetID, ProbeID: p.probeID, Kind: result.DNS, Family: answer.Family, Addr: answer.Addr, StartedAt: startedAt, Phases: map[string]time.Duration{"resolve": answer.Duration}, Err: answer.Err})
	}
	if err := ctx.Err(); err != nil {
		return results, err
	}
	return results, nil
}

var explicitTargetID = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}$`)

// Reserve the generated identity namespace even for legacy targets so a caller
// cannot accidentally pool an explicit transport with an unsuffixed target.
func validateTarget(target result.Target) error {
	if strings.TrimSpace(target.ID) == "" || strings.Contains(target.ID, ":dns:") {
		return fmt.Errorf("target ID is empty or contains reserved :dns: marker")
	}
	host, err := resolve.Host(target.Host)
	if err != nil {
		return err
	}
	if target.DNS != nil {
		if !explicitTargetID.MatchString(target.ID) {
			return fmt.Errorf("explicit DNS target ID must use ingest identity characters and be at most 120 bytes")
		}
		if _, err := netip.ParseAddr(host); err == nil {
			return fmt.Errorf("explicit DNS measurement requires a hostname, not an IP literal")
		}
	}
	return nil
}
