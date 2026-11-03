package anomaly

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"net"
	"net/http"
	"net/url"
	"sync"
	"sync/atomic"
	"time"

	"pathwatch.local/pathwatch/ingest/internal/store"
)

// Config uses a fixed initial baseline of Warmup actual five-minute p95s.
// SigmaFloor (milliseconds) handles a zero or tiny observed sample variance.
// K,H are standardized sigma units; the ingest binary defaults to K=0.5, H=12.
type Config struct {
	Warmup, MaxPaths, MaxWindows, MaxPending int
	SigmaFloor, K, H                         float64
	AlertmanagerURL                          string
	Timeout, RetryMin, RetryMax              time.Duration
}
type detector struct {
	N             int
	Mean, M2, Sum float64
	// omitempty keeps the canonical encoding (and checksum) of state saved
	// before the latch existed.
	Latched bool `json:",omitempty"`
}
type pending struct {
	Event    store.AnomalyEvent
	Written  bool
	Attempts uint32
	Next     time.Time
}
type jobState struct {
	Owner         string
	Checksum      string
	Version       int
	Configuration string
	Cursor        time.Time
	Key           string
	Paths         map[string]detector
	Pending       []pending
}
type Job struct {
	mu                         sync.Mutex
	store                      *store.Store
	config                     Config
	state                      jobState
	client                     *http.Client
	endpoint                   string
	poison                     error
	ticks, failures, delivered atomic.Uint64
}

func New(s *store.Store, c Config) (*Job, error) {
	if s == nil || c.Warmup < 2 || c.Warmup > 1000 || c.MaxPaths < 1 || c.MaxPaths > 4096 || c.MaxWindows < 1 || c.MaxWindows > 256 || c.MaxPending < 1 || c.MaxPending > 256 || !finite(c.SigmaFloor) || c.SigmaFloor <= 0 || !finite(c.K) || c.K < 0 || !finite(c.H) || c.H <= 0 || c.Timeout <= 0 || c.Timeout > 30*time.Second || c.RetryMin <= 0 || c.RetryMax < c.RetryMin || c.RetryMax > time.Hour {
		return nil, errors.New("invalid anomaly configuration")
	}
	u, e := url.Parse(c.AlertmanagerURL)
	if e != nil || u.Scheme != "http" || u.User != nil || u.RawQuery != "" || u.Fragment != "" || (u.Path != "" && u.Path != "/") || net.ParseIP(u.Hostname()) == nil || !net.ParseIP(u.Hostname()).IsLoopback() || u.Port() == "" {
		return nil, errors.New("Alertmanager requires literal loopback http://IP:port base URL")
	}
	u.Path = "/api/v2/alerts"
	j := &Job{store: s, config: c, endpoint: u.String(), client: &http.Client{Timeout: c.Timeout, Transport: &http.Transport{Proxy: nil, DisableKeepAlives: true}, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}}
	// Bind state to all behavioral configuration, including delivery destination.
	configBytes, _ := json.Marshal(c)
	fingerprint := hash(configBytes)
	j.state = jobState{Owner: s.OwnerIdentity(), Version: 1, Configuration: fingerprint, Paths: map[string]detector{}}
	if len(j.state.Owner) != 64 {
		return nil, errors.New("anomaly requires persistent store owner")
	}
	data, e := s.LoadAnomalyState(context.Background())
	if e != nil {
		return nil, e
	}
	if len(data) > 0 {
		dec := json.NewDecoder(bytes.NewReader(data))
		dec.DisallowUnknownFields()
		if e = dec.Decode(&j.state); e != nil {
			return nil, fmt.Errorf("corrupt anomaly state: %w", e)
		}
		if e = dec.Decode(new(any)); e != io.EOF {
			return nil, errors.New("trailing anomaly state data")
		}
		if j.state.Owner != s.OwnerIdentity() {
			return nil, errors.New("anomaly state belongs to another ingest owner")
		}
		if j.state.Version != 1 || j.state.Configuration != fingerprint || j.state.Paths == nil || len(j.state.Paths) > c.MaxPaths || len(j.state.Pending) > c.MaxPending {
			return nil, errors.New("anomaly state configuration/capacity mismatch")
		}
		checksum := j.state.Checksum
		j.state.Checksum = ""
		canonical, _ := json.Marshal(j.state)
		j.state.Checksum = checksum
		if checksum != hash(canonical) {
			return nil, errors.New("corrupt anomaly state checksum")
		}
		if (len(j.state.Paths) > 0 && (j.state.Cursor.IsZero() || j.state.Key == "")) || (!j.state.Cursor.IsZero() && !j.state.Cursor.Equal(j.state.Cursor.Truncate(5*time.Minute))) {
			return nil, errors.New("corrupt anomaly cursor")
		}
		if !j.state.Cursor.IsZero() {
			if _, ok := j.state.Paths[j.state.Key]; !ok {
				return nil, errors.New("anomaly cursor has no detector")
			}
		}
		for _, d := range j.state.Paths {
			if d.N < 0 || d.N > c.Warmup || !finite(d.Mean) || !finite(d.M2) || d.M2 < 0 || !finite(d.Sum) || d.Sum < 0 || d.Sum > c.H {
				return nil, errors.New("corrupt anomaly detector")
			}
		}
		for _, p := range j.state.Pending {
			if len(p.Event.ID) != 64 || p.Event.PathKey == "" || p.Event.Window.IsZero() {
				return nil, errors.New("corrupt pending anomaly event")
			}
		}
	}
	return j, nil
}
func finite(x float64) bool { return !math.IsNaN(x) && !math.IsInf(x, 0) }
func hash(b []byte) string  { h := sha256.Sum256(b); return hex.EncodeToString(h[:]) }
func (j *Job) save(ctx context.Context) error {
	j.state.Checksum = ""
	canonical, e := json.Marshal(j.state)
	if e != nil {
		return e
	}
	j.state.Checksum = hash(canonical)
	b, e := json.Marshal(j.state)
	if e == nil {
		e = j.store.SaveAnomalyState(ctx, b)
	}
	if e != nil {
		j.poison = fmt.Errorf("anomaly persistence failed; reopen job before retry: %w", e)
	}
	return e
}

// Tick first drains durable pending events, then processes a bounded page of
// real closed windows in chronological order. No late-window rewind or gap fill.
// Once a persistence operation is uncertain this instance stops advancing.
func (j *Job) Tick(ctx context.Context, now time.Time) (err error) {
	j.mu.Lock()
	defer j.mu.Unlock()
	j.ticks.Add(1)
	defer func() {
		if err != nil {
			j.failures.Add(1)
		}
	}()
	if j.poison != nil {
		return j.poison
	}
	if err = j.deliver(ctx, now); err != nil {
		return err
	}
	if len(j.state.Pending) > 0 {
		return nil
	} // durable backoff still in progress
	windows, err := j.store.ReadAnomalyWindows(ctx, j.state.Cursor, j.state.Key, now, j.config.MaxWindows)
	if err != nil {
		return err
	}
	for _, w := range windows {
		if w.Samples == 0 || !finite(w.P95) || w.P95 < 0 {
			return errors.New("invalid quantile window")
		}
		d, exists := j.state.Paths[w.PathKey]
		if !exists && len(j.state.Paths) >= j.config.MaxPaths {
			return errors.New("anomaly path capacity reached; cursor retained")
		}
		if d.N < j.config.Warmup {
			d.N++
			delta := w.P95 - d.Mean
			d.Mean += delta / float64(d.N)
			d.M2 += delta * (w.P95 - d.Mean)
		} else {
			sigma := math.Max(j.config.SigmaFloor, math.Sqrt(d.M2/float64(d.N-1)))
			c := CUSUM{K: j.config.K, H: j.config.H, Sum: d.Sum, Latched: d.Latched}
			if c.Update((w.P95 - d.Mean) / sigma) {
				if len(j.state.Pending) >= j.config.MaxPending {
					return errors.New("anomaly pending capacity reached; cursor retained")
				}
				before, after := []Hop{}, []Hop{}
				for _, h := range w.Before {
					before = append(before, Hop{h.TTL, h.Addr, h.At})
				}
				for _, h := range w.After {
					after = append(after, Hop{h.TTL, h.Addr, h.At})
				}
				change := Correlate(w.Start, before, after)
				reason := w.CorrelationReason
				if reason != "" {
					change = PathChange{Reason: reason}
				} else if !change.Valid {
					reason = change.Reason
				}
				id := hash([]byte(j.state.Owner + "\x00" + j.state.Configuration + "\x00" + w.PathKey + "\x00" + w.Start.UTC().Format(time.RFC3339Nano)))
				evt := store.AnomalyEvent{ID: id, PathKey: w.PathKey, Window: w.Start, P95: w.P95, Mean: d.Mean, Sigma: sigma, K: c.K, H: c.H, PathChanged: change.Changed, FirstDivergentTTL: change.FirstDivergentTTL, CorrelationReason: reason}
				j.state.Pending = append(j.state.Pending, pending{Event: evt})
			}
			d.Sum, d.Latched = c.Sum, c.Latched
		}
		j.state.Paths[w.PathKey] = d
		j.state.Cursor = w.Start
		j.state.Key = w.PathKey
		if err = j.save(ctx); err != nil {
			return err
		}
	}
	return j.deliver(ctx, now)
}
func (j *Job) deliver(ctx context.Context, now time.Time) error {
	for len(j.state.Pending) > 0 {
		p := &j.state.Pending[0]
		if now.Before(p.Next) {
			return nil
		}
		var err error
		if !p.Written {
			err = j.store.WriteAnomalyEvent(ctx, p.Event)
			if err == nil {
				p.Written = true
				if err = j.save(ctx); err != nil {
					return err
				}
			}
		}
		if err == nil {
			err = j.notify(ctx, p.Event)
		}
		if err != nil {
			delay := j.config.RetryMin
			// Saturating exponential backoff, including after process restart.
			for n := uint32(0); n < p.Attempts && delay < j.config.RetryMax; n++ {
				if delay > j.config.RetryMax/2 {
					delay = j.config.RetryMax
				} else {
					delay *= 2
				}
			}
			if delay > j.config.RetryMax {
				delay = j.config.RetryMax
			}
			if p.Attempts < 63 {
				p.Attempts++
			}
			p.Next = now.Add(delay)
			if e := j.save(ctx); e != nil {
				return errors.Join(err, e)
			}
			return err
		}
		j.state.Pending = j.state.Pending[1:]
		if err = j.save(ctx); err != nil {
			return err
		}
		j.delivered.Add(1)
	}
	return nil
}
func (j *Job) notify(ctx context.Context, e store.AnomalyEvent) error {
	// Fixed labels, original startsAt and observation annotations make retries
	// the same event. Recovery time is unknown: omit endsAt so delayed delivery
	// receives Alertmanager's now+resolve_timeout. This is a one-shot event,
	// not a continually refreshed firing condition or a recovery detector.
	payload := []any{map[string]any{"labels": map[string]string{"alertname": "PathLatencyCUSUM", "event_id": e.ID, "severity": "warning"}, "annotations": map[string]string{"path_key": e.PathKey, "observed_window_start": e.Window.UTC().Format(time.RFC3339Nano), "observed_window_end": e.Window.Add(5 * time.Minute).UTC().Format(time.RFC3339Nano), "correlation_reason": e.CorrelationReason, "summary": fmt.Sprintf("p95 %.3fms; baseline %.3fms; sigma %.3fms; K=%g H=%g; first divergent TTL=%d", e.P95, e.Mean, e.Sigma, e.K, e.H, e.FirstDivergentTTL)}, "startsAt": e.Window.Add(5 * time.Minute).UTC().Format(time.RFC3339Nano)}}
	b, _ := json.Marshal(payload)
	ctx, cancel := context.WithTimeout(ctx, j.config.Timeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, "POST", j.endpoint, bytes.NewReader(b))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := j.client.Do(req)
	if err != nil {
		return fmt.Errorf("Alertmanager delivery: %w", err)
	}
	defer resp.Body.Close()
	_, err = io.Copy(io.Discard, io.LimitReader(resp.Body, 4096))
	if err != nil {
		return err
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fmt.Errorf("Alertmanager status %d", resp.StatusCode)
	}
	return nil
}

// Run is owned by ingest's lifecycle. Errors stay visible while pending state
// remains durable; the next bounded tick retries. Cancellation stops new work.
func (j *Job) Run(ctx context.Context, report func(error)) {
	tick := time.NewTicker(5 * time.Minute)
	defer tick.Stop()
	for {
		if err := j.Tick(ctx, time.Now().UTC()); err != nil && ctx.Err() == nil {
			report(err)
		}
		select {
		case <-ctx.Done():
			return
		case <-tick.C:
		}
	}
}
func (j *Job) Metrics(w http.ResponseWriter) {
	fmt.Fprintf(w, "pathwatch_anomaly_ticks_total %d\npathwatch_anomaly_failures_total %d\npathwatch_anomaly_delivered_total %d\n", j.ticks.Load(), j.failures.Load(), j.delivered.Load())
}
