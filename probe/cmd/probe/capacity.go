package main

import (
	"context"
	"crypto/sha256"
	"errors"
	"fmt"
	"net/netip"
	"pathwatch.local/pathwatch/probe/internal/result"
	"regexp"
	"strings"
	"time"
	"unicode/utf8"
)

const capacityProfile = "five-week-v1"

var capacityID = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.:-]{0,15}$`)

// This is a deliberately small, opt-in deployment profile, not a replacement
// for the general probe configuration. All existing defaults remain unchanged.
func (c *Config) validateCapacity(every, timeout, rpc time.Duration) error {
	if c.CapacityProfile == "" {
		if len(c.CapacityDNSAddresses) != 0 || c.CapacityStartUTC != "" || c.CapacityDeadlineUTC != "" {
			return fmt.Errorf("capacity DNS contract requires profile")
		}
		return nil
	}
	bad := func() error { return fmt.Errorf("unsupported five-week-v1 configuration") }
	start, e1 := time.Parse(time.RFC3339, c.CapacityStartUTC)
	end, e2 := time.Parse(time.RFC3339, c.CapacityDeadlineUTC)
	if e1 != nil || e2 != nil || start.UTC().Format(time.RFC3339) != c.CapacityStartUTC || end.UTC().Format(time.RFC3339) != c.CapacityDeadlineUTC || start.Unix() < 0 || end.Unix() > 4294967295-180*86400 || (end.Sub(start) != 35*24*time.Hour && end.Sub(start) != 48*time.Hour) {
		return bad()
	}
	if c.CapacityProfile != capacityProfile || !capacityID.MatchString(c.ProbeID) || every != 120*time.Second || timeout > 5*time.Second || rpc > 10*time.Second || c.BatchSize != 128 || c.SpoolMaxBytes != 64<<20 || len(c.Targets) != 4 || len(c.CapacityDNSAddresses) != 2 {
		return bad()
	}
	v4, e := netip.ParseAddr(c.CapacityDNSAddresses[0])
	if e != nil || !v4.Is4() || !v4.IsPrivate() {
		return bad()
	}
	v6, e := netip.ParseAddr(c.CapacityDNSAddresses[1])
	if e != nil || !v6.Is6() || v6.Is4In6() || !v6.IsPrivate() {
		return bad()
	}
	c.CapacityDNSAddresses = []string{v4.String(), v6.String()}
	kinds := map[result.Kind]bool{result.TCP: true, result.TLS: true, result.HTTP1: true, result.HTTP2: true, result.HTTP3: true, result.Trace: true}
	modes := map[string]bool{}
	private := 0
	hostname := ""
	ids := map[string]bool{}
	for _, t := range c.Targets {
		if !capacityID.MatchString(t.ID) || ids[t.ID] || t.Public {
			return bad()
		}
		ids[t.ID] = true
		if t.DNS == nil {
			if t.Host != v4.String() || t.Port < 1 || t.Port > 65472 || len(t.Kinds) != 6 {
				return bad()
			}
			seen := map[result.Kind]bool{}
			for _, k := range t.Kinds {
				if !kinds[k] || seen[k] {
					return bad()
				}
				seen[k] = true
			}
			private++
		} else {
			if len(t.Kinds) != 1 || t.Kinds[0] != result.DNS || len(t.Host) > 253 {
				return bad()
			}
			if _, e := netip.ParseAddr(t.Host); e == nil {
				return bad()
			}
			if hostname != "" && hostname != t.Host {
				return bad()
			}
			hostname = t.Host
			m := t.DNS.Transport
			if (m != "udp" && m != "tcp" && m != "doh") || modes[m] {
				return bad()
			}
			modes[m] = true
		}
	}
	if private != 1 || len(modes) != 3 {
		return bad()
	}
	return nil
}

func (c Config) flushInterval() time.Duration {
	if c.CapacityProfile == capacityProfile {
		return 60 * time.Second
	}
	return time.Second
}

// Normalization occurs before results leave the prober, before collection and
// measurement counting. The original long text is NOT recoverable from its hash.
func boundedDiagnostic(raw string) string {
	if len(raw) <= 256 && utf8.ValidString(raw) {
		return raw
	}
	suffix := fmt.Sprintf(" [truncated original_bytes=%d sha256=%x]", len(raw), sha256.Sum256([]byte(raw)))
	prefix := strings.ToValidUTF8(raw, "�")
	if len(prefix) > 256-len(suffix) {
		prefix = prefix[:256-len(suffix)]
	}
	for !utf8.ValidString(prefix) {
		prefix = prefix[:len(prefix)-1]
	}
	return prefix + suffix
}

type capacityProber struct {
	inner     result.Prober
	addresses []string
}

func (p capacityProber) Probe(ctx context.Context, t result.Target) ([]result.Result, error) {
	rs, err := p.inner.Probe(ctx, t)
	limit := 1
	if t.DNS != nil {
		limit = 3
	}
	violation := len(rs) > limit
	seen := map[string]bool{}
	for i := range rs {
		r := &rs[i]
		r.Err = boundedDiagnostic(r.Err)
		if t.DNS != nil && r.Addr != "" {
			want := ""
			if r.Family == "v4" {
				want = p.addresses[0]
			}
			if r.Family == "v6" {
				want = p.addresses[1]
			}
			if r.Addr != want || seen[r.Family] {
				violation = true
			}
			seen[r.Family] = true
		}
	}
	if violation {
		err = errors.Join(err, fmt.Errorf("capacity profile response exceeded; all produced results retained; operator review required"))
	}
	return rs, err
}

// Anchor the UTC remaining budget to Go's monotonic timer once. Wall-clock
// rollback can never extend this run; a >5s discontinuity stops it early.
func (c Config) capacityWindow(parent context.Context) (context.Context, context.CancelFunc, error) {
	if c.CapacityProfile == "" {
		ctx, cancel := context.WithCancel(parent)
		return ctx, cancel, nil
	}
	start, err := time.Parse(time.RFC3339Nano, c.CapacityStartUTC)
	if err != nil {
		return nil, nil, err
	}
	end, err := time.Parse(time.RFC3339Nano, c.CapacityDeadlineUTC)
	if err != nil {
		return nil, nil, err
	}
	anchor := time.Now()
	if !end.After(anchor) {
		return nil, nil, fmt.Errorf("capacity campaign deadline expired")
	}
	ctx, cancel := context.WithTimeout(parent, end.Sub(anchor))
	go func() {
		ticker := time.NewTicker(time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case now := <-ticker.C:
				drift := time.Duration(now.UnixNano()-anchor.UnixNano()) - now.Sub(anchor)
				if drift > 5*time.Second || drift < -5*time.Second || !now.Before(end) {
					cancel()
					return
				}
			}
		}
	}()
	if start.After(anchor) {
		timer := time.NewTimer(start.Sub(anchor))
		defer timer.Stop()
		select {
		case <-ctx.Done():
			cancel()
			return nil, nil, ctx.Err()
		case <-timer.C:
		}
	}
	return ctx, cancel, nil
}
