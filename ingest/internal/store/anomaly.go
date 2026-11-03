package store

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

type TraceHop struct {
	TTL  int
	Addr string
	At   time.Time
}
type AnomalyWindow struct {
	PathKey           string
	Start             time.Time
	P95               float64
	Samples           uint64
	Before, After     []TraceHop
	CorrelationReason string
}
type AnomalyEvent struct {
	ID, PathKey            string
	Window                 time.Time
	P95, Mean, Sigma, K, H float64
	PathChanged            bool
	FirstDivergentTTL      int
	CorrelationReason      string
}

const MaxAnomalyStateBytes = 4 << 20

// anomalyStable excludes Commit/recovery and rejects every uncertain raw/MV
// state. Network calls are bounded; notification HTTP never holds this gate.
func (s *Store) anomalyStable(ctx context.Context, fn func(context.Context) error) error {
	ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-s.gate:
	}
	defer func() { s.gate <- struct{}{} }()
	if s.closed {
		return errors.New("store closed")
	}
	if s.poison != nil {
		return s.poison
	}
	for _, r := range s.records {
		if !r.Committed {
			return errors.New("pending batch excludes anomaly reads")
		}
	}
	if e := s.bindSession(ctx); e != nil {
		return e
	}
	return fn(ctx)
}

// ReadAnomalyWindows merges every batch state for actual completed windows.
// The composite cursor is chronological. Windows arriving after their cursor
// has passed are intentionally not revisited; no synthetic gap samples exist.
func (s *Store) ReadAnomalyWindows(ctx context.Context, after time.Time, key string, closed time.Time, limit int) (out []AnomalyWindow, err error) {
	if limit < 1 || limit > 256 {
		return nil, errors.New("anomaly window limit must be 1..256")
	}
	closed = closed.UTC().Truncate(5 * time.Minute)
	err = s.anomalyStable(ctx, func(ctx context.Context) error {
		where := "window_start < toDateTime(" + strconv.FormatInt(closed.Unix(), 10) + ")"
		if !after.IsZero() {
			where += " AND (window_start,path_key) > (toDateTime(" + strconv.FormatInt(after.Unix(), 10) + ")," + quote(key) + ")"
		}
		q := "SELECT path_key,toUnixTimestamp(window_start) AS start,quantilesMerge(0.5,0.95,0.99)(phase_ms)[2] AS p95,toString(countMerge(samples)) AS samples FROM path_quantiles_5m WHERE " + where + " GROUP BY path_key,window_start ORDER BY window_start,path_key LIMIT " + strconv.Itoa(limit) + " FORMAT JSONEachRow"
		b, e := s.query(ctx, q)
		if e != nil {
			return e
		}
		if b == "" {
			return nil
		}
		for _, line := range strings.Split(b, "\n") {
			var row struct {
				PathKey string  `json:"path_key"`
				Start   int64   `json:"start"`
				P95     float64 `json:"p95"`
				Samples string  `json:"samples"`
			}
			if e = json.Unmarshal([]byte(line), &row); e != nil {
				return e
			}
			n, e := strconv.ParseUint(row.Samples, 10, 64)
			if e != nil {
				return e
			}
			w := AnomalyWindow{PathKey: row.PathKey, Start: time.Unix(row.Start, 0).UTC(), P95: row.P95, Samples: n}
			if e = s.windowTraces(ctx, &w); e != nil {
				return e
			}
			out = append(out, w)
		}
		return nil
	})
	return
}
func date64(t time.Time) string {
	return "toDateTime64(" + quote(t.UTC().Format("2006-01-02 15:04:05.000000000")) + ",9,'UTC')"
}
func (s *Store) windowTraces(ctx context.Context, w *AnomalyWindow) error {
	b, e := s.query(ctx, "SELECT probe_id,target_id,kind,family,groupUniqArray(2)(addr) AS addresses FROM probe_results WHERE path_key="+quote(w.PathKey)+" AND started_at >= "+date64(w.Start)+" AND started_at < "+date64(w.Start.Add(5*time.Minute))+" AND phase_present=1 GROUP BY probe_id,target_id,kind,family LIMIT 2 FORMAT JSONEachRow")
	if e != nil {
		return e
	}
	var identity struct {
		Probe     string   `json:"probe_id"`
		Target    string   `json:"target_id"`
		Kind      string   `json:"kind"`
		Family    string   `json:"family"`
		Addresses []string `json:"addresses"`
	}
	if b == "" || strings.Contains(b, "\n") {
		w.CorrelationReason = "ambiguous path identity"
		return nil
	}
	if e = json.Unmarshal([]byte(b), &identity); e != nil {
		return e
	}
	if identity.Kind == "trace" {
		w.CorrelationReason = "trace latency has no independent latency path"
		return nil
	}
	if len(identity.Addresses) != 1 || identity.Addresses[0] == "" {
		w.CorrelationReason = "multiple or unknown destination addresses"
		return nil
	}
	where := "probe_id=" + quote(identity.Probe) + " AND target_id=" + quote(identity.Target) + " AND family=" + quote(identity.Family) + " AND addr=" + quote(identity.Addresses[0]) + " AND kind='trace' AND err=''"
	for i, bounds := range []string{"started_at >= " + date64(w.Start.Add(-time.Hour)) + " AND started_at < " + date64(w.Start), "started_at >= " + date64(w.Start) + " AND started_at < " + date64(w.Start.Add(5*time.Minute))} {
		b, e = s.query(ctx, "SELECT toString(toUnixTimestamp64Nano(started_at)) AS at,hop_ttls AS ttls,hop_addrs AS addrs FROM probe_results WHERE "+where+" AND "+bounds+" ORDER BY started_at DESC,batch_key DESC,result_index DESC LIMIT 1 FORMAT JSONEachRow")
		if e != nil {
			return e
		}
		if b == "" {
			w.CorrelationReason = "missing same-destination trace snapshot"
			continue
		}
		var row struct {
			At    string   `json:"at"`
			TTLs  []int    `json:"ttls"`
			Addrs []string `json:"addrs"`
		}
		if e = json.Unmarshal([]byte(b), &row); e != nil {
			return e
		}
		ns, e := strconv.ParseInt(row.At, 10, 64)
		if e != nil {
			return e
		}
		if len(row.TTLs) != len(row.Addrs) {
			return errors.New("inconsistent trace arrays")
		}
		hops := []TraceHop{}
		for k, ttl := range row.TTLs {
			hops = append(hops, TraceHop{ttl, row.Addrs[k], time.Unix(0, ns).UTC()})
		}
		if i == 0 {
			w.Before = hops
		} else {
			w.After = hops
		}
	}
	return nil
}

// Repeated identical inserts can create physical rows; consumers MUST use
// path_events FINAL (or GROUP BY event_id). The deterministic ID is immutable.
func (s *Store) WriteAnomalyEvent(ctx context.Context, e AnomalyEvent) error {
	return s.anomalyStable(ctx, func(ctx context.Context) error {
		row := map[string]any{"event_id": e.ID, "path_key": e.PathKey, "window_start": e.Window.UTC().Format("2006-01-02 15:04:05"), "p95_ms": e.P95, "baseline_ms": e.Mean, "sigma_ms": e.Sigma, "cusum_k": e.K, "cusum_h": e.H, "path_changed": e.PathChanged, "first_divergent_ttl": e.FirstDivergentTTL, "correlation_reason": e.CorrelationReason}
		b, err := json.Marshal(row)
		if err != nil {
			return err
		}
		_, err = s.query(ctx, "INSERT INTO path_events FORMAT JSONEachRow\n"+string(b))
		return err
	})
}
func (s *Store) LoadAnomalyState(ctx context.Context) (data []byte, err error) {
	err = s.anomalyStable(ctx, func(context.Context) error {
		var e error
		data, e = readFile(filepath.Join(s.cfg.JournalDir, "anomaly", "state.json"), MaxAnomalyStateBytes)
		if errors.Is(e, os.ErrNotExist) {
			return nil
		}
		if e == nil && len(data) == 0 {
			return errors.New("corrupt empty anomaly state")
		}
		return e
	})
	return
}
func (s *Store) SaveAnomalyState(ctx context.Context, data []byte) error {
	if len(data) > MaxAnomalyStateBytes {
		return fmt.Errorf("anomaly state exceeds %d bytes", MaxAnomalyStateBytes)
	}
	return s.anomalyStable(ctx, func(context.Context) error {
		dir := filepath.Join(s.cfg.JournalDir, "anomaly")
		if e := os.Mkdir(dir, 0700); e != nil && !errors.Is(e, os.ErrExist) {
			return e
		}
		if e := syncDirectory(s.cfg.JournalDir); e != nil {
			return e
		}
		st, e := os.Lstat(dir)
		if e != nil {
			return e
		}
		if !st.IsDir() {
			return errors.New("anomaly state requires real directory")
		}
		return atomicWrite(dir, "state.json", data)
	})
}
