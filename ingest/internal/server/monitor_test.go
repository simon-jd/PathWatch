package server_test

import (
	"context"
	"encoding/json"
	"google.golang.org/protobuf/types/known/timestamppb"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
	"time"
)

func TestMonitorCommittedCurrentWindowOnly(t *testing.T) {
	_, st, s := fixture(t)
	r := batch("monitor-current", 1)
	if _, err := s.Report(context.Background(), r); err != nil {
		t.Fatal(err)
	}
	w := httptest.NewRecorder()
	s.Ready(w, httptest.NewRequest("GET", "/readyz", nil))
	var v struct {
		Windows        map[string][]string `json:"windows"`
		JournalRecords int                 `json:"journal_records"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &v); err != nil {
		t.Fatal(err)
	}
	if w.Code != 200 || v.JournalRecords != 1 || len(v.Windows) != 1 {
		t.Fatalf("readiness: %d %s", w.Code, w.Body)
	}
	late := batch("monitor-late", 1)
	late.ProbeId = "late"
	late.Results[0].ProbeId = "late"
	late.Results[0].StartedAt = timestamppb.New(now.Add(-10 * time.Minute))
	if _, err := s.Report(context.Background(), late); err != nil {
		t.Fatal(err)
	}
	w = httptest.NewRecorder()
	s.Ready(w, httptest.NewRequest("GET", "/readyz", nil))
	json.Unmarshal(w.Body.Bytes(), &v)
	for _, ids := range v.Windows {
		if len(ids) != 1 || ids[0] != "home" {
			t.Fatalf("late data counted: %v", v.Windows)
		}
	}
	st.Close()
	w = httptest.NewRecorder()
	s.Ready(w, httptest.NewRequest("GET", "/readyz", nil))
	if w.Code != 503 {
		t.Fatalf("closed store reports healthy: %d", w.Code)
	}
}

func TestMonitorUncertainCommitFailsClosed(t *testing.T) {
	cfg, _, s := fixture(t)
	sql(t, cfg.Database, "DROP TABLE path_quantiles_5m")
	if _, err := s.Report(context.Background(), batch("monitor-poison", 1)); err == nil {
		t.Fatal("expected failed durable commit")
	}
	w := httptest.NewRecorder()
	s.Ready(w, httptest.NewRequest("GET", "/readyz", nil))
	if w.Code != 503 {
		t.Fatalf("uncertain writer healthy: %d %s", w.Code, w.Body)
	}
}
func TestMonitorRealDynamicProbeCheck(t *testing.T) {
	cfg, _, s := fixture(t)
	if _, err := s.Report(context.Background(), batch("monitor-check", 2)); err != nil {
		t.Fatal(err)
	}
	roster := filepath.Join(t.TempDir(), "roster.json")
	if err := os.WriteFile(roster, []byte(`{"home":"home-ubuntu"}`), 0600); err != nil {
		t.Fatal(err)
	}
	cmd := exec.Command("python3", "../../../drills/check_probes.py", "--roster", roster, "--url", cfg.URL, "--database", cfg.Database)
	cmd.Env = append(os.Environ(), "PATHWATCH_CLICKHOUSE_USER=default", "PATHWATCH_CLICKHOUSE_PASSWORD=task08-local-test")
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("real CH probe check: %v %s", err, out)
	}
}
