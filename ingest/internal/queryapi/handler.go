// Package queryapi exposes only bounded measurement reads. It never opens a Store.
package queryapi

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"pathwatch.local/pathwatch/ingest/internal/store"
	"regexp"
	"strconv"
	"strings"
	"sync/atomic"
	"time"
)

type Handler struct {
	reader store.MeasurementReader
	slots  chan struct{}
	counts [4]atomic.Uint64
}

func New(r store.MeasurementReader) *Handler {
	return &Handler{reader: r, slots: make(chan struct{}, 4)}
}

var outcomes = []string{"success", "failure", "rejected", "canceled"}

func (h *Handler) Metrics(w http.ResponseWriter) {
	fmt.Fprintln(w, "# TYPE pathwatch_query_requests_total counter")
	for i, n := range outcomes {
		fmt.Fprintf(w, "pathwatch_query_requests_total{outcome=%q} %d\n", n, h.counts[i].Load())
	}
}

var utcStamp = regexp.MustCompile(`^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,9})?Z$`)

func parse(r *http.Request) (store.QueryBounds, error) {
	b := store.QueryBounds{Limit: 100}
	v, e := url.ParseQuery(r.URL.RawQuery)
	if e != nil {
		return b, store.ErrQueryInvalid
	}
	for k, x := range v {
		if len(x) != 1 || (k != "path_key" && k != "from" && k != "to" && k != "limit") {
			return b, store.ErrQueryInvalid
		}
	}
	if len(v["path_key"]) != 1 || len(v["from"]) != 1 || len(v["to"]) != 1 {
		return b, store.ErrQueryInvalid
	}
	b.PathKey = v.Get("path_key")
	for key, dst := range map[string]*time.Time{"from": &b.From, "to": &b.To} {
		s := v.Get(key)
		if !utcStamp.MatchString(s) {
			return b, store.ErrQueryInvalid
		}
		*dst, e = time.Parse(time.RFC3339Nano, s)
		if e != nil {
			return b, store.ErrQueryInvalid
		}
	}
	if a, ok := v["limit"]; ok {
		if a[0] == "" || strings.Trim(a[0], "0123456789") != "" {
			return b, store.ErrQueryInvalid
		}
		b.Limit, e = strconv.Atoi(a[0])
		if e != nil {
			return b, store.ErrQueryInvalid
		}
	}
	return b, b.Validate(r.URL.Path == "/v1/measurements/quantiles", time.Now())
}

var errLabBad500 = errors.New("lab canary drill: injected query failure")

var errorsByStatus = map[int][2]string{
	400: {"invalid_request", "invalid query parameters or body"}, 404: {"not_found", "unknown query route"}, 405: {"method_not_allowed", "only GET is allowed"}, 413: {"result_limit", "narrow the requested interval or result"}, 414: {"uri_too_long", "request URI exceeds limit"}, 429: {"overloaded", "query admission is full"}, 503: {"store_unavailable", "durable store unavailable"}, 504: {"query_timeout", "query deadline exceeded"}, 502: {"invalid_upstream_response", "invalid engine response"}, 500: {"internal_error", "internal query error"},
}

func (h *Handler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	outcome := 2
	defer func() {
		if r.Context().Err() != nil {
			outcome = 3
		}
		h.counts[outcome].Add(1)
	}()
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	fail := func(status int) {
		if status >= 500 {
			outcome = 1
		}
		x := errorsByStatus[status]
		body, _ := json.Marshal(struct {
			Version string `json:"api_version"`
			Error   struct {
				Code    string `json:"code"`
				Message string `json:"message"`
			} `json:"error"`
		}{"v1", struct {
			Code    string `json:"code"`
			Message string `json:"message"`
		}{x[0], x[1]}})
		w.WriteHeader(status)
		if _, e := w.Write(body); e != nil {
			outcome = 3
		}
	}
	if len(r.RequestURI) > 4096 {
		fail(414)
		return
	}
	if r.URL.RawPath != "" || (r.URL.Path != "/v1/measurements/raw" && r.URL.Path != "/v1/measurements/quantiles" && r.URL.Path != "/v1/measurements/events") {
		fail(404)
		return
	}
	if r.Method != "GET" {
		w.Header().Set("Allow", "GET")
		fail(405)
		return
	}
	if r.ContentLength != 0 || len(r.TransferEncoding) != 0 {
		fail(400)
		return
	}
	b, e := parse(r)
	if e != nil {
		fail(400)
		return
	}
	select {
	case h.slots <- struct{}{}:
		defer func() { <-h.slots }()
	default:
		w.Header().Set("Retry-After", "1")
		fail(429)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), store.QueryTimeout)
	defer cancel()
	var data any
	var count int
	switch r.URL.Path {
	case "/v1/measurements/raw":
		var rows []store.RawResult
		rows, e = h.reader.ReadRaw(ctx, b)
		if rows == nil {
			rows = []store.RawResult{}
		}
		data = rows
		count = len(rows)
	case "/v1/measurements/quantiles":
		var rows []store.Quantile
		rows, e = h.reader.ReadQuantiles(ctx, b)
		if rows == nil {
			rows = []store.Quantile{}
		}
		data = rows
		count = len(rows)
	case "/v1/measurements/events":
		var rows []store.Event
		rows, e = h.reader.ReadEvents(ctx, b)
		if rows == nil {
			rows = []store.Event{}
		}
		data = rows
		count = len(rows)
	}
	if e == nil {
		e = ctx.Err()
	}
	if e == nil && labBad500 {
		e = errLabBad500
	}
	if e != nil {
		switch {
		case errors.Is(e, context.Canceled):
			fail(503)
			outcome = 3
			return
		case errors.Is(e, context.DeadlineExceeded):
			fail(504)
		case errors.Is(e, store.ErrQueryOverloaded):
			w.Header().Set("Retry-After", "1")
			fail(429)
		case errors.Is(e, store.ErrQueryInvalid):
			fail(400)
		case errors.Is(e, store.ErrQueryLimit):
			fail(413)
		case errors.Is(e, store.ErrQueryUnavailable):
			fail(503)
		case errors.Is(e, store.ErrQueryResponse):
			fail(502)
		default:
			fail(500)
		}
		return
	}
	if count > b.Limit {
		fail(413)
		return
	}
	result := struct {
		Version string `json:"api_version"`
		Data    any    `json:"data"`
		Count   int    `json:"count"`
		Range   struct {
			From string `json:"from"`
			To   string `json:"to"`
		} `json:"range"`
		Limit int `json:"limit"`
	}{"v1", data, count, struct {
		From string `json:"from"`
		To   string `json:"to"`
	}{b.From.UTC().Format(time.RFC3339Nano), b.To.UTC().Format(time.RFC3339Nano)}, b.Limit}
	var body boundedBuffer
	if e = json.NewEncoder(&body).Encode(result); e != nil {
		if errors.Is(e, store.ErrQueryLimit) {
			fail(413)
		} else {
			fail(500)
		}
		return
	}
	if r.Context().Err() != nil {
		fail(503)
		outcome = 3
		return
	}
	w.WriteHeader(200)
	outcome = 0
	if _, e = w.Write(body.Bytes()); e != nil {
		outcome = 3
	}
}

type boundedBuffer struct{ bytes.Buffer }

func (b *boundedBuffer) Write(p []byte) (int, error) {
	if b.Len()+len(p) > store.QueryMaxBytes {
		return 0, store.ErrQueryLimit
	}
	return b.Buffer.Write(p)
}
