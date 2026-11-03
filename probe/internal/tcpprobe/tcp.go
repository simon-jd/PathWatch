// Package tcpprobe measures a fresh TCP connection to every resolved address.
package tcpprobe

import (
	"context"
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
	WallNow         func() time.Time
}

type Probe struct {
	resolver interface {
		Timeout() time.Duration
		Lookup(context.Context, string) ([]resolve.Answer, error)
	}
	probeID string
	wallNow func() time.Time
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
	return &Probe{resolver: r, probeID: cfg.ProbeID, wallNow: cfg.WallNow}, nil
}

// Probe uses wall time only for StartedAt. Both resolution and connection phase
// durations use time.Since on real time.Now values retaining monotonic readings.
// Remote failures are per result; invalid input/caller cancellation are errors.
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
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	round, cancel := context.WithTimeout(ctx, p.resolver.Timeout())
	defer cancel()
	startedAt := p.wallNow()
	resolveStart := time.Now()
	answers, resolveErr := p.resolver.Lookup(round, host)
	answers = resolve.PreserveFailure(answers, resolveErr, time.Since(resolveStart))
	results := make([]result.Result, 0, len(answers))
	for _, answer := range answers {
		if err := ctx.Err(); err != nil {
			return results, err
		}
		r := result.Result{TargetID: target.ID, ProbeID: p.probeID, Kind: result.TCP, Family: answer.Family, Addr: answer.Addr, StartedAt: startedAt, Phases: map[string]time.Duration{"resolve": answer.Duration}, Err: answer.Err}
		if answer.Err == "" {
			network := "tcp4"
			if answer.Family == "v6" {
				network = "tcp6"
			}
			start := time.Now()
			conn, err := (&net.Dialer{}).DialContext(round, network, net.JoinHostPort(answer.Addr, strconv.Itoa(target.Port)))
			r.Phases["connect"] = time.Since(start)
			if err != nil {
				r.Err = err.Error()
			} else {
				_ = conn.Close()
			}
		}
		results = append(results, r)
	}
	if err := ctx.Err(); err != nil {
		return results, err
	}
	return results, nil
}
