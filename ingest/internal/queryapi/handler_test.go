package queryapi

import (
	"context"
	"errors"
	"net"
	"net/http/httptest"
	"pathwatch.local/pathwatch/ingest/internal/store"
	"strings"
	"testing"
	"time"
)

type reader struct{ block chan struct{} }

func (r reader) ReadRaw(c context.Context, b store.QueryBounds) ([]store.RawResult, error) {
	if r.block != nil {
		select {
		case <-r.block:
		case <-c.Done():
			return nil, c.Err()
		}
	}
	return []store.RawResult{}, nil
}
func (r reader) ReadQuantiles(context.Context, store.QueryBounds) ([]store.Quantile, error) {
	return []store.Quantile{}, nil
}
func (r reader) ReadEvents(context.Context, store.QueryBounds) ([]store.Event, error) {
	return []store.Event{}, nil
}

const good = "/v1/measurements/raw?path_key=p&from=2026-01-01T00:00:00Z&to=2026-01-01T00:05:00Z"

func TestContract(t *testing.T) {
	h := New(reader{})
	for _, c := range []struct {
		method, url string
		code        int
	}{{"GET", good, 200}, {"HEAD", good, 405}, {"GET", good + "&sql=SELECT", 400}, {"GET", good + "&limit=257", 400}, {"GET", good + "&path_key=other", 400}, {"GET", "/v1/measurements/raw/", 404}, {"GET", "/v1/measurements/%72aw", 404}, {"GET", good + "%zz", 400}} {
		w := httptest.NewRecorder()
		h.ServeHTTP(w, httptest.NewRequest(c.method, c.url, nil))
		if w.Code != c.code {
			t.Fatalf("%s got %d %s", c.url, w.Code, w.Body)
		}
	}
}
func TestAdmissionCancellation(t *testing.T) {
	block := make(chan struct{})
	h := New(reader{block})
	done := make(chan struct{}, 4)
	for i := 0; i < 4; i++ {
		go func() { h.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest("GET", good, nil)); done <- struct{}{} }()
	}
	deadline := time.Now().Add(time.Second)
	for len(h.slots) < 4 {
		if time.Now().After(deadline) {
			t.Fatal("not admitted")
		}
		time.Sleep(time.Millisecond)
	}
	w := httptest.NewRecorder()
	h.ServeHTTP(w, httptest.NewRequest("GET", good, nil))
	if w.Code != 429 {
		t.Fatal(w.Code)
	}
	close(block)
	for i := 0; i < 4; i++ {
		<-done
	}
}

func TestListenerValidation(t *testing.T) {
	for _, address := range []string{"localhost:9000", "0.0.0.0:9000", "127.0.0.1:0", "[::]:9000", "127.0.0.1:65536"} {
		if ValidateAddress(address) == nil {
			t.Fatal(address)
		}
	}
	for _, address := range []string{"127.0.0.1:9000", "[::1]:9000"} {
		if e := ValidateAddress(address); e != nil {
			t.Fatal(e)
		}
	}
}
func TestCancellationAndMetricBudget(t *testing.T) {
	h := New(reader{})
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	canceled := httptest.NewRecorder()
	h.ServeHTTP(canceled, httptest.NewRequest("GET", good, nil).WithContext(ctx))
	if canceled.Code != 503 {
		t.Fatal("cancellation must not emit empty success", canceled.Code)
	}
	w := httptest.NewRecorder()
	h.Metrics(w)
	if strings.Count(w.Body.String(), "outcome=") != 4 || !strings.Contains(w.Body.String(), `outcome="canceled"} 1`) {
		t.Fatal(w.Body)
	}
}
func TestBoundedJSON(t *testing.T) {
	var b boundedBuffer
	if _, e := b.Write(make([]byte, store.QueryMaxBytes)); e != nil {
		t.Fatal(e)
	}
	if _, e := b.Write([]byte{1}); !errors.Is(e, store.ErrQueryLimit) {
		t.Fatal(e)
	}
}
func TestBodyAndTimeBoundaries(t *testing.T) {
	h := New(reader{})
	r := httptest.NewRequest("GET", good, strings.NewReader("x"))
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	if w.Code != 400 {
		t.Fatal(w.Code)
	}
	for _, s := range []string{strings.Replace(good, "00:05:00Z", "00:05:00+00:00", 1), strings.Replace(good, "00:05:00Z", "00:05:00,1Z", 1), good + "&limit=+2", strings.Replace(good, "path_key=p", "path_key=%00", 1)} {
		w = httptest.NewRecorder()
		h.ServeHTTP(w, httptest.NewRequest("GET", s, nil))
		if w.Code != 400 {
			t.Fatalf("%s %d", s, w.Code)
		}
	}
}
func TestConnectionCap(t *testing.T) {
	l, e := net.Listen("tcp", "127.0.0.1:0")
	if e != nil {
		t.Fatal(e)
	}
	limited := LimitListener(l)
	defer limited.Close()
	accepted := make(chan net.Conn, 17)
	go func() {
		for {
			c, e := limited.Accept()
			if e != nil {
				return
			}
			accepted <- c
		}
	}()
	clients := []net.Conn{}
	servers := []net.Conn{}
	defer func() {
		for _, c := range clients {
			c.Close()
		}
		for _, c := range servers {
			c.Close()
		}
	}()
	for i := 0; i < 16; i++ {
		c, e := net.Dial("tcp", l.Addr().String())
		if e != nil {
			t.Fatal(e)
		}
		clients = append(clients, c)
		servers = append(servers, <-accepted)
	}
	c, e := net.Dial("tcp", l.Addr().String())
	if e != nil {
		t.Fatal(e)
	}
	defer c.Close()
	c.SetReadDeadline(time.Now().Add(time.Second))
	if n, err := c.Read(make([]byte, 1)); n != 0 || err == nil {
		t.Fatal("overflow connection not closed")
	} else if e, ok := err.(net.Error); ok && e.Timeout() {
		t.Fatal("connection timed out instead of closing")
	}
	servers[0].Close()
	servers[0].Close()
	c2, e := net.Dial("tcp", l.Addr().String())
	if e != nil {
		t.Fatal(e)
	}
	clients = append(clients, c2)
	select {
	case c := <-accepted:
		servers = append(servers, c)
	case <-time.After(time.Second):
		t.Fatal("permit not released")
	}
}

type failingReader struct {
	reader
	err error
}

func (r failingReader) ReadRaw(context.Context, store.QueryBounds) ([]store.RawResult, error) {
	return nil, r.err
}
func TestErrorContract(t *testing.T) {
	for _, c := range []struct {
		err    error
		status int
		code   string
	}{{store.ErrQueryUnavailable, 503, "store_unavailable"}, {context.DeadlineExceeded, 504, "query_timeout"}, {store.ErrQueryResponse, 502, "invalid_upstream_response"}, {store.ErrQueryLimit, 413, "result_limit"}, {errors.New("secret SQL password"), 500, "internal_error"}} {
		w := httptest.NewRecorder()
		New(failingReader{err: c.err}).ServeHTTP(w, httptest.NewRequest("GET", good, nil))
		if w.Code != c.status || !strings.Contains(w.Body.String(), c.code) || strings.Contains(w.Body.String(), "secret") {
			t.Fatalf("%d %s", w.Code, w.Body)
		}
	}
}
func TestTimeAndPathEdges(t *testing.T) {
	h := New(reader{})
	for _, u := range []string{strings.Replace(good, "00:05:00Z", "00:05:00.1234567890Z", 1), strings.Replace(good, "from=2026-01-01T00:00:00Z", "from=2026-01-01T00:05:00Z", 1), strings.Replace(good, "to=2026-01-01T00:05:00Z", "to=2026-01-02T00:00:00.000000001Z", 1), strings.Replace(good, "/raw?", "/quantiles?", 1) + "&limit=0", strings.Replace(good, "path_key=p", "path_key=%FF", 1)} {
		w := httptest.NewRecorder()
		h.ServeHTTP(w, httptest.NewRequest("GET", u, nil))
		if w.Code != 400 {
			t.Fatalf("%s ->%d", u, w.Code)
		}
	}
}

type largeReader struct{ reader }

func (largeReader) ReadRaw(context.Context, store.QueryBounds) ([]store.RawResult, error) {
	rows := make([]store.RawResult, 128)
	for i := range rows {
		rows[i].Err = strings.Repeat("<", 2000)
	}
	return rows, nil
}
func TestFinalJSONExpansionBound(t *testing.T) {
	w := httptest.NewRecorder()
	New(largeReader{}).ServeHTTP(w, httptest.NewRequest("GET", good+"&limit=256", nil))
	if w.Code != 413 || strings.Contains(w.Body.String(), "data") {
		t.Fatalf("%d %s", w.Code, w.Body)
	}
}

func TestRemoteOverloadPreservesRetryContract(t *testing.T) {
	w := httptest.NewRecorder()
	New(failingReader{err: store.ErrQueryOverloaded}).ServeHTTP(w, httptest.NewRequest("GET", good, nil))
	if w.Code != 429 || w.Header().Get("Retry-After") != "1" || !strings.Contains(w.Body.String(), `"code":"overloaded"`) {
		t.Fatal(w.Code, w.Header(), w.Body.String())
	}
}
