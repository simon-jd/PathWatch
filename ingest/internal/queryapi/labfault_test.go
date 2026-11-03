package queryapi

import (
	"net/http/httptest"
	"strings"
	"testing"
)

// The canary drill (deploy/README.md) ships a frontend built with
// -tags pathwatch_lab_bad500. That build still reads through the core but
// answers every query with the fixed 500 envelope; the default build never does.
func TestLabBad500Build(t *testing.T) {
	h := New(reader{})
	for _, path := range []string{good, strings.Replace(good, "raw", "events", 1)} {
		w := httptest.NewRecorder()
		h.ServeHTTP(w, httptest.NewRequest("GET", path, nil))
		want := 200
		if labBad500 {
			want = 500
		}
		if w.Code != want {
			t.Fatalf("labBad500=%v %s: got %d %s", labBad500, path, w.Code, w.Body)
		}
		if labBad500 && !strings.Contains(w.Body.String(), `"internal_error"`) {
			t.Fatalf("bad build must use the fixed 500 envelope: %s", w.Body)
		}
	}
	m := httptest.NewRecorder()
	h.Metrics(m)
	metric := `pathwatch_query_requests_total{outcome="success"} 2`
	if labBad500 {
		metric = `pathwatch_query_requests_total{outcome="failure"} 2`
	}
	if !strings.Contains(m.Body.String(), metric) {
		t.Fatalf("labBad500=%v: want %s in\n%s", labBad500, metric, m.Body)
	}
}
