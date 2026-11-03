package store

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"reflect"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"
)

const QueryMaxBytes = 1 << 20
const QueryTimeout = 2 * time.Second

var (
	ErrQueryOverloaded  = errors.New("query admission full")
	ErrQueryInvalid     = errors.New("invalid query")
	ErrQueryUnavailable = errors.New("store unavailable")
	ErrQueryLimit       = errors.New("query result limit")
	ErrQueryResponse    = errors.New("invalid engine response")
)

type QueryBounds struct {
	PathKey  string
	From, To time.Time
	Limit    int
}

func (b QueryBounds) Validate(quantiles bool, now time.Time) error {
	if !utf8.ValidString(b.PathKey) || len(b.PathKey) < 1 || len(b.PathKey) > 512 || strings.ContainsRune(b.PathKey, 0) || b.Limit < 1 || b.Limit > 256 || b.From.Before(time.Unix(0, 0)) || b.To.After(time.Unix(4294967295-180*86400, 0)) || !b.To.After(b.From) || b.To.Sub(b.From) > 24*time.Hour {
		return ErrQueryInvalid
	}
	if quantiles && (b.From != b.From.Truncate(5*time.Minute) || b.To != b.To.Truncate(5*time.Minute) || b.To.After(now.UTC().Truncate(5*time.Minute))) {
		return ErrQueryInvalid
	}
	return nil
}

type RawResult struct {
	BatchKey     string            `json:"batch_key"`
	ResultIndex  uint32            `json:"result_index"`
	ProbeID      string            `json:"probe_id"`
	BatchID      string            `json:"batch_id"`
	TargetID     string            `json:"target_id"`
	Kind         string            `json:"kind"`
	Family       string            `json:"family"`
	Addr         string            `json:"addr"`
	PathKey      string            `json:"path_key"`
	StartedAt    string            `json:"started_at"`
	Phases       map[string]string `json:"phases_nanoseconds"`
	Err          string            `json:"err"`
	Protocol     string            `json:"protocol"`
	HopTTLs      []uint32          `json:"hop_ttls"`
	HopAddrs     []string          `json:"hop_addrs"`
	HopRTTs      []string          `json:"hop_rtt_nanoseconds"`
	TLSVersion   uint32            `json:"tls_version"`
	TLSResumed   bool              `json:"tls_resumed"`
	PhaseMS      *float64          `json:"phase_ms"`
	PhasePresent bool              `json:"phase_present"`
}
type Quantile struct {
	PathKey     string  `json:"path_key"`
	WindowStart string  `json:"window_start"`
	P50         float64 `json:"p50_ms"`
	P95         float64 `json:"p95_ms"`
	P99         float64 `json:"p99_ms"`
	Samples     string  `json:"samples"`
}
type Event struct {
	ID                string  `json:"event_id"`
	PathKey           string  `json:"path_key"`
	WindowStart       string  `json:"window_start"`
	P95               float64 `json:"p95_ms"`
	Baseline          float64 `json:"baseline_ms"`
	Sigma             float64 `json:"sigma_ms"`
	K                 float64 `json:"cusum_k"`
	H                 float64 `json:"cusum_h"`
	PathChanged       bool    `json:"path_changed"`
	FirstDivergentTTL uint16  `json:"first_divergent_ttl"`
	CorrelationReason string  `json:"correlation_reason"`
}
type MeasurementReader interface {
	ReadRaw(context.Context, QueryBounds) ([]RawResult, error)
	ReadQuantiles(context.Context, QueryBounds) ([]Quantile, error)
	ReadEvents(context.Context, QueryBounds) ([]Event, error)
}

const queryWhere = " WHERE path_key={path:String} AND %s >= fromUnixTimestamp64Nano({from_ns:Int64}) AND %s < fromUnixTimestamp64Nano({to_ns:Int64}) "
const rawSelect = "SELECT batch_key,result_index,probe_id,batch_id,target_id,kind,family,addr,path_key,toString(toUnixTimestamp64Nano(started_at)) AS started_at,phases_nanoseconds,err,protocol,hop_ttls,hop_addrs,hop_rtt_nanoseconds,tls_version,tls_resumed,if(phase_present=1,phase_ms,NULL) AS phase_ms,toBool(phase_present) AS phase_present FROM probe_results"
const quantileSelect = "SELECT path_key,toString(toUnixTimestamp(window_start)) AS window_start,quantilesMerge(0.5,0.95,0.99)(phase_ms)[1] AS p50_ms,quantilesMerge(0.5,0.95,0.99)(phase_ms)[2] AS p95_ms,quantilesMerge(0.5,0.95,0.99)(phase_ms)[3] AS p99_ms,toString(countMerge(samples)) AS samples FROM path_quantiles_5m"
const eventSelect = "SELECT event_id,path_key,toString(toUnixTimestamp(window_start)) AS window_start,p95_ms,baseline_ms,sigma_ms,cusum_k,cusum_h,path_changed,first_divergent_ttl,correlation_reason FROM path_events FINAL"

func (s *Store) ReadRaw(ctx context.Context, b QueryBounds) ([]RawResult, error) {
	return readMeasurement[RawResult](s, ctx, b, false, rawSelect+fmt.Sprintf(queryWhere, "probe_results.started_at", "probe_results.started_at")+"ORDER BY probe_results.started_at,batch_key,result_index")
}
func (s *Store) ReadQuantiles(ctx context.Context, b QueryBounds) ([]Quantile, error) {
	return readMeasurement[Quantile](s, ctx, b, true, quantileSelect+fmt.Sprintf(queryWhere, "path_quantiles_5m.window_start", "path_quantiles_5m.window_start")+"GROUP BY path_key,path_quantiles_5m.window_start ORDER BY path_quantiles_5m.window_start")
}
func (s *Store) ReadEvents(ctx context.Context, b QueryBounds) ([]Event, error) {
	return readMeasurement[Event](s, ctx, b, false, eventSelect+fmt.Sprintf(queryWhere, "path_events.window_start", "path_events.window_start")+"ORDER BY path_events.window_start,event_id")
}
func readMeasurement[T any](s *Store, ctx context.Context, b QueryBounds, quantiles bool, sql string) (out []T, err error) {
	if e := b.Validate(quantiles, time.Now()); e != nil {
		return nil, e
	}
	ctx, cancel := context.WithTimeout(ctx, QueryTimeout)
	defer cancel()
	entered := false
	err = s.anomalyStable(ctx, func(ctx context.Context) error {
		entered = true
		body, e := s.measurementRequest(ctx, sql+" LIMIT {fetch_limit:UInt32} FORMAT JSONEachRow", b)
		if e != nil {
			return e
		}
		rows, e := decodeMeasurement[T](body, b.Limit)
		if e != nil {
			return e
		}
		if e = s.bindSession(ctx); e != nil {
			return fmt.Errorf("%w: epoch check", ErrQueryUnavailable)
		}
		if e = ctx.Err(); e != nil {
			return e
		}
		out = rows
		return nil
	})
	if err != nil {
		out = nil
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
		if !entered {
			return nil, fmt.Errorf("%w: stable read rejected", ErrQueryUnavailable)
		}
	}
	return
}
func escapeParameter(s string) string {
	return strings.NewReplacer("\\", "\\\\", "\t", "\\t", "\n", "\\n", "\r", "\\r", "\x00", "\\0", "\b", "\\b", "\f", "\\f").Replace(s)
}
func (s *Store) measurementRequest(ctx context.Context, sql string, b QueryBounds) ([]byte, error) {
	u, _ := url.Parse(s.cfg.URL)
	v := u.Query()
	for k, x := range map[string]string{"database": s.cfg.Database, "session_id": s.session, "session_timeout": "300", "session_check": "1", "wait_end_of_query": "1", "max_execution_time": "2", "max_threads": "2", "max_memory_usage": "268435456", "max_rows_to_read": "1000000", "max_bytes_to_read": "134217728", "read_overflow_mode": "throw", "max_result_rows": "257", "max_result_bytes": "1048576", "result_overflow_mode": "throw", "output_format_json_quote_64bit_integers": "1", "param_path": escapeParameter(b.PathKey), "param_from_ns": strconv.FormatInt(b.From.UnixNano(), 10), "param_to_ns": strconv.FormatInt(b.To.UnixNano(), 10), "param_fetch_limit": strconv.Itoa(b.Limit + 1)} {
		v.Set(k, x)
	}
	u.RawQuery = v.Encode()
	r, e := http.NewRequestWithContext(ctx, "POST", u.String(), io.NopCloser(strings.NewReader(sql)))
	if e != nil {
		return nil, e
	}
	r.SetBasicAuth(s.cfg.Username, s.cfg.Password)
	resp, e := s.client.Do(r)
	if e != nil {
		return nil, fmt.Errorf("%w: transport", ErrQueryUnavailable)
	}
	defer resp.Body.Close()
	data, e := io.ReadAll(io.LimitReader(resp.Body, QueryMaxBytes+1))
	if e != nil {
		return nil, fmt.Errorf("%w: incomplete body", ErrQueryResponse)
	}
	if len(data) > QueryMaxBytes {
		return nil, ErrQueryLimit
	}
	if resp.StatusCode != 200 {
		// Only the engine's explicit limit exceptions are caller-visible resource errors.
		switch resp.Header.Get("X-ClickHouse-Exception-Code") {
		case "159":
			return nil, context.DeadlineExceeded
		case "158", "307", "396":
			return nil, ErrQueryLimit
		case "373", "372":
			return nil, ErrQueryUnavailable
		}
		return nil, ErrQueryResponse
	}
	return data, nil
}
func decodeMeasurement[T any](data []byte, limit int) ([]T, error) {
	if !utf8.Valid(data) {
		return nil, ErrQueryResponse
	}
	out := make([]T, 0)
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.DisallowUnknownFields()
	for dec.More() {
		var raw json.RawMessage
		if e := dec.Decode(&raw); e != nil {
			return nil, ErrQueryResponse
		}
		var fields map[string]json.RawMessage
		if json.Unmarshal(raw, &fields) != nil {
			return nil, ErrQueryResponse
		}
		var row T
		typ := reflect.TypeOf(row)
		if len(fields) != typ.NumField() {
			return nil, ErrQueryResponse
		}
		for i := 0; i < typ.NumField(); i++ {
			key := typ.Field(i).Tag.Get("json")
			v, ok := fields[key]
			if !ok || (string(v) == "null" && key != "phase_ms") {
				return nil, ErrQueryResponse
			}
		}
		d := json.NewDecoder(bytes.NewReader(raw))
		d.DisallowUnknownFields()
		if d.Decode(&row) != nil {
			return nil, ErrQueryResponse
		}
		if e := normalizeMeasurement(any(&row)); e != nil {
			return nil, e
		}
		out = append(out, row)
		if len(out) > limit {
			return nil, ErrQueryLimit
		}
	}
	var extra any
	if e := dec.Decode(&extra); e != io.EOF {
		return nil, ErrQueryResponse
	}
	return out, nil
}
func stamp(value string, nano bool) (string, error) {
	n, e := strconv.ParseInt(value, 10, 64)
	if e != nil || n < 0 {
		return "", ErrQueryResponse
	}
	t := time.Unix(n, 0)
	if nano {
		t = time.Unix(0, n)
	}
	return t.UTC().Format(time.RFC3339Nano), nil
}
func normalizeMeasurement(row any) error {
	var e error
	switch r := row.(type) {
	case *RawResult:
		r.StartedAt, e = stamp(r.StartedAt, true)
		if e != nil {
			return e
		}
		if len(r.Phases) > 16 || len(r.HopTTLs) > 255 || len(r.HopTTLs) != len(r.HopAddrs) || len(r.HopTTLs) != len(r.HopRTTs) || len(r.Err) > 4096 || r.PhasePresent != (r.PhaseMS != nil) {
			return ErrQueryResponse
		}
		for _, v := range r.Phases {
			if n, x := strconv.ParseInt(v, 10, 64); x != nil || n < 0 {
				return ErrQueryResponse
			}
		}
		for _, v := range r.HopRTTs {
			if n, x := strconv.ParseInt(v, 10, 64); x != nil || n < 0 {
				return ErrQueryResponse
			}
		}
	case *Quantile:
		r.WindowStart, e = stamp(r.WindowStart, false)
		if e != nil {
			return e
		}
		if n, x := strconv.ParseUint(r.Samples, 10, 64); x != nil || n == 0 {
			return ErrQueryResponse
		}
	case *Event:
		r.WindowStart, e = stamp(r.WindowStart, false)
	}
	return e
}
