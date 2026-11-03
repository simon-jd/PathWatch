package store

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestConfiguredRecordCapacityRetainsUpdates(t *testing.T) {
	var cfg Config
	if err := json.Unmarshal([]byte(`{"MaxRecords":1}`), &cfg); err != nil {
		t.Fatal(err)
	}
	cfg.JournalDir = t.TempDir()
	cfg.MaxJournalBytes = 1 << 20
	st := &Store{cfg: cfg, records: map[string]record{}}
	first := record{Key: strings.Repeat("a", 64), Hash: strings.Repeat("b", 64), Epoch: strings.Repeat("c", 32), Payload: []byte("complete payload")}
	if err := st.persist(first); err != nil {
		t.Fatal(err)
	}
	first.Committed = true
	if err := st.persist(first); err != nil {
		t.Fatal("capacity blocked commit update", err)
	}
	second := first
	second.Key = strings.Repeat("d", 64)
	if err := st.persist(second); !errors.Is(err, ErrCapacity) {
		t.Fatalf("new record accepted beyond explicit cap: %v", err)
	}
	raw, err := os.ReadFile(filepath.Join(cfg.JournalDir, first.Key+".json"))
	if err != nil {
		t.Fatal(err)
	}
	var got record
	if json.Unmarshal(raw, &got) != nil || !got.Committed || string(got.Payload) != "complete payload" {
		t.Fatal("old payload/commit state changed")
	}
}
func TestJournalEncodingOverheadBound(t *testing.T) {
	r := record{Key: strings.Repeat("a", 64), Hash: strings.Repeat("b", 64), Epoch: strings.Repeat("c", 32), Payload: make([]byte, 10001)}
	raw, err := json.Marshal(r)
	if err != nil {
		t.Fatal(err)
	}
	base64Bytes := 4 * ((len(r.Payload) + 2) / 3)
	if len(raw)-base64Bytes > 256 {
		t.Fatalf("record overhead %d exceeds model256", len(raw)-base64Bytes)
	}
	t.Logf("actual_record_overhead=%d payload=%d json=%d", len(raw)-base64Bytes, len(r.Payload), len(raw))
}

func TestRecordCapacityDefaultAndRaisedBoundary(t *testing.T) {
	for _, cap := range []int{0, 2, 131072} {
		cfg := Config{JournalDir: t.TempDir(), MaxJournalBytes: 1 << 30, MaxRecords: cap}
		st := &Store{cfg: cfg, records: map[string]record{}}
		limit := cap
		if limit == 0 {
			limit = 65536
		}
		for i := 0; i < limit; i++ {
			st.records[fmt.Sprint(i)] = record{}
		}
		if err := st.persist(record{Key: strings.Repeat("z", 64)}); !errors.Is(err, ErrCapacity) {
			t.Fatalf("limit=%d: %v", limit, err)
		}
	}
	for _, limit := range []int{-1, 262145} {
		if st, err := Open(context.Background(), Config{MaxRecords: limit}); err == nil {
			st.Close()
			t.Fatalf("invalid cap accepted: %d", limit)
		}
	}
}
