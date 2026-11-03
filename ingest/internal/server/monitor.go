package server

import (
	"context"
	"encoding/json"
	"net/http"
	pb "pathwatch.local/pathwatch/proto/pathwatch/v1"
	"strconv"
	"time"
)

// observeCommit runs only after a new durable commit, never on duplicate ACK.
// Volatile history deliberately starts empty after restart: no historical
// journal mtime or late backfill can manufacture an on-time observation.
func (s *Server) observeCommit(r *pb.ReportRequest) {
	at := s.now().UTC()
	window := at.Truncate(5 * time.Minute).Unix()
	s.observationMu.Lock()
	defer s.observationMu.Unlock()
	for k := range s.windows {
		if k < window-300 {
			delete(s.windows, k)
		}
	}
	for _, result := range r.Results {
		started := result.StartedAt.AsTime()
		if started.After(at) || started.UTC().Truncate(5*time.Minute).Unix() != window {
			continue
		}
		if s.windows[window] == nil {
			s.windows[window] = map[string]bool{}
		}
		// Endpoint cardinality is bounded independently of untrusted probe IDs.
		if len(s.windows[window]) < 128 {
			s.windows[window][r.ProbeId] = true
		}
		break
	}
}

// Ready is an owned loopback administrative API. 200 means the durable writer
// and ClickHouse epoch are readable now, not that arbitrary Report calls work.
func (s *Server) Ready(w http.ResponseWriter, r *http.Request) {
	if r.Method != "GET" {
		w.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 2*time.Second)
	defer cancel()
	records, bytes, err := s.store.MonitoringSnapshot(ctx)
	if err != nil {
		http.Error(w, "durable writer unavailable", http.StatusServiceUnavailable)
		return
	}
	windows := map[string][]string{}
	s.observationMu.Lock()
	for key, ids := range s.windows {
		for id := range ids {
			windows[strconv.FormatInt(key, 10)] = append(windows[strconv.FormatInt(key, 10)], id)
		}
	}
	s.observationMu.Unlock()
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(struct {
		Windows        map[string][]string `json:"windows"`
		JournalRecords int                 `json:"journal_records"`
		JournalBytes   int64               `json:"journal_bytes"`
	}{windows, records, bytes})
}
