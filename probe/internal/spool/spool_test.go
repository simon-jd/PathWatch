package spool

import (
	"bufio"
	"bytes"
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"pathwatch.local/pathwatch/probe/internal/result"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"
)

func fixture() []result.Result {
	return []result.Result{
		{TargetID: "t1", ProbeID: "p", Kind: result.HTTP3, Family: "v6", Addr: "::1", StartedAt: time.Date(2026, 10, 23, 1, 2, 3, 456, time.UTC), Protocol: "h3", TLSVersion: 0x304, TLSResumed: true, Phases: map[string]time.Duration{"dns": 3, "connect": 5, "tls_handshake": 7, "ttfb": 11, "total": 13}, Err: "remote failure", Hops: []result.Hop{{TTL: 1, Addr: "127.0.0.1", RTT: 17}, {TTL: 2}}},
		{TargetID: "t2", ProbeID: "p", Kind: result.TCP, Family: "v4", Addr: "127.0.0.1", StartedAt: time.Date(2026, 10, 23, 1, 2, 4, 0, time.UTC), Phases: map[string]time.Duration{"resolve": 19, "connect": 23}},
	}
}
func mustOpen(t *testing.T, dir string, max int64) *Spool {
	t.Helper()
	s, e := Open(dir, max)
	if e != nil {
		t.Fatal(e)
	}
	t.Cleanup(func() { s.Close() })
	return s
}
func TestSpool_SurvivesRestart(t *testing.T) {
	dir := t.TempDir()
	s := mustOpen(t, dir, 1<<20)
	for i := 0; i < 3; i++ {
		rs := fixture()
		rs[0].TargetID = fmt.Sprintf("t%d", i+1)
		if e := s.Append(rs); e != nil {
			t.Fatal(e)
		}
	}
	// Read a filesystem snapshot through the public FIFO API, preserving the
	// original three committed batches for a separate Close/Open comparison.
	copyDir := t.TempDir()
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	for _, e := range entries {
		if e.Name() == ".lock" {
			continue
		}
		b, e2 := os.ReadFile(filepath.Join(dir, e.Name()))
		if e2 != nil {
			t.Fatal(e2)
		}
		if e2 = os.WriteFile(filepath.Join(copyDir, e.Name()), b, 0600); e2 != nil {
			t.Fatal(e2)
		}
	}
	snapshot := mustOpen(t, copyDir, 1<<20)
	var ids []string
	for i := 0; i < 3; i++ {
		id, _, ack, e := snapshot.Next()
		if e != nil {
			t.Fatal(e)
		}
		ids = append(ids, id)
		if e = ack(); e != nil {
			t.Fatal(e)
		}
	}
	snapshot.Close()
	if err := s.Close(); err != nil {
		t.Fatal(err)
	}
	s = mustOpen(t, dir, 1<<20)
	seen := map[string]bool{}
	for i := 0; i < 3; i++ {
		id, rs, _, e := s.Next()
		want := fixture()
		want[0].TargetID = fmt.Sprintf("t%d", i+1)
		if e != nil || id == "" || seen[id] || !reflect.DeepEqual(rs, want) {
			t.Fatalf("id=%s results=%+v err=%v", id, rs, e)
		}
		if id != ids[i] {
			t.Fatal("ID changed on restart")
		}
		seen[id] = true
		if err := s.Close(); err != nil {
			t.Fatal(err)
		}
		s = mustOpen(t, dir, 1<<20)
		again, _, ack2, e := s.Next()
		if e != nil || again != id {
			t.Fatal("retry changed ID")
		}
		if e = ack2(); e != nil {
			t.Fatal(e)
		}
	}
	_, _, _, err = s.Next()
	if !errors.Is(err, ErrEmpty) {
		t.Fatalf("got %v", err)
	}
}
func TestSpool_DropsOldestWhenFull(t *testing.T) {
	dir := t.TempDir()
	s := mustOpen(t, dir, 1<<20)
	if e := s.Append(fixture()); e != nil {
		t.Fatal(e)
	}
	size := s.Stats().Bytes
	first, _, stale, e := s.Next()
	if e != nil {
		t.Fatal(e)
	}
	s.Close()
	s = mustOpen(t, dir, 3*size)
	for i := 0; i < 3; i++ {
		if e := s.Append(fixture()); e != nil {
			t.Fatal(e)
		}
	}
	id, _, _, e := s.Next()
	if e != nil || id == first || s.Stats().Dropped != 1 || s.Stats().Batches != 3 {
		t.Fatalf("%s %+v %v", id, s.Stats(), e)
	}
	// The old instance is closed: stale closure must not mutate reopened owner.
	if e = stale(); !errors.Is(e, ErrClosed) {
		t.Fatalf("closed ack: %v", e)
	}
	_, _, stale, e = s.Next()
	if e != nil {
		t.Fatal(e)
	}
	if e = s.Append(fixture()); e != nil {
		t.Fatal(e)
	}
	newID, _, _, _ := s.Next()
	if e = stale(); e != nil {
		t.Fatal(e)
	}
	after, _, _, _ := s.Next()
	if newID != after {
		t.Fatal("stale ACK deleted newer head")
	}
	before := s.Stats()
	huge := fixture()
	huge[0].Err = strings.Repeat("x", int(3*size))
	if e = s.Append(huge); e == nil {
		t.Fatal("oversized accepted")
	}
	if got := s.Stats(); got != before {
		t.Fatalf("oversized changed queue: %+v %+v", before, got)
	}
	s.Close()
	s = mustOpen(t, dir, 3*size)
	if s.Stats().Dropped != 2 {
		t.Fatal("drop counter reset")
	}
}
func TestSpool_OwnershipRecoveryAndCorruption(t *testing.T) {
	dir := t.TempDir()
	s := mustOpen(t, dir, 1<<20)
	if _, e := Open(dir, 1<<20); e == nil {
		t.Fatal("second owner accepted")
	}
	s.Append(fixture())
	id, _, _, _ := s.Next()
	s.Close()
	os.WriteFile(filepath.Join(dir, ".pw-tmp-interrupted"), []byte("partial"), 0600)
	os.WriteFile(filepath.Join(dir, strings.Repeat("a", 32)+".batch"), []byte("orphan"), 0600)
	s = mustOpen(t, dir, 1<<20)
	if _, e := os.Stat(filepath.Join(dir, ".pw-tmp-interrupted")); !os.IsNotExist(e) {
		t.Fatal("temp not recovered")
	}
	s.Close()
	os.WriteFile(filepath.Join(dir, id+".batch"), []byte(`{"ID":"corrupt"}`), 0600)
	if _, e := Open(dir, 1<<20); e == nil {
		t.Fatal("corrupt committed batch silently skipped")
	}
	for _, max := range []int64{0, -1, (1 << 30) + 1} {
		if _, e := Open(t.TempDir(), max); e == nil {
			t.Fatal("invalid capacity accepted")
		}
	}
}
func TestSpool_FailedAppendPreservesQueue(t *testing.T) {
	dir := t.TempDir()
	s := mustOpen(t, dir, 1<<20)
	s.Append(fixture())
	id, _, _, _ := s.Next()
	before := s.Stats()
	// A real filesystem rename failure after writing the candidate batch.
	manifest, err := os.ReadFile(filepath.Join(dir, "manifest.json"))
	if err != nil {
		t.Fatal(err)
	}
	if err = os.Remove(filepath.Join(dir, "manifest.json")); err != nil {
		t.Fatal(err)
	}
	os.Mkdir(filepath.Join(dir, "manifest.json"), 0700)
	if err = s.Append(fixture()); err == nil {
		t.Fatal("rename failure hidden")
	}
	after, rs, _, err := s.Next()
	if err != nil || after != id || !reflect.DeepEqual(rs, fixture()) || s.Stats() != before {
		t.Fatal("failed append damaged committed queue")
	}
	os.Remove(filepath.Join(dir, "manifest.json"))
	os.WriteFile(filepath.Join(dir, "manifest.json"), manifest, 0600)
	s.Close()
	s = mustOpen(t, dir, 1<<20)
	if s.Stats() != before {
		t.Fatal("failed candidate became committed")
	}
}
func TestSpool_ConcurrentAppendNextAck(t *testing.T) {
	s := mustOpen(t, t.TempDir(), 1<<20)
	var wg sync.WaitGroup
	for i := 0; i < 4; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for j := 0; j < 20; j++ {
				if e := s.Append(fixture()); e != nil {
					t.Error(e)
				}
				_, _, ack, e := s.Next()
				if e == nil {
					if e = ack(); e != nil {
						t.Error(e)
					}
				} else if !errors.Is(e, ErrEmpty) {
					t.Error(e)
				}
			}
		}()
	}
	wg.Wait()
}

func TestSpool_CrashRecovery(t *testing.T) {
	if dir := os.Getenv("PATHWATCH_TEST_CRASH_DIR"); dir != "" {
		s, e := Open(dir, 8<<20)
		if e != nil {
			panic(e)
		}
		if e = s.Append(fixture()); e != nil {
			panic(e)
		}
		fmt.Println("durable")
		for {
			rs := fixture()
			rs[0].Err = strings.Repeat("x", 1<<20)
			if e = s.Append(rs); e != nil {
				panic(e)
			}
		}
	}
	dir := t.TempDir()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, os.Args[0], "-test.run=^TestSpool_CrashRecovery$")
	cmd.Env = append(os.Environ(), "PATHWATCH_TEST_CRASH_DIR="+dir)
	stdout, e := cmd.StdoutPipe()
	if e != nil {
		t.Fatal(e)
	}
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	if e = cmd.Start(); e != nil {
		t.Fatal(e)
	}
	line, e := bufio.NewReader(stdout).ReadString('\n')
	if e != nil || line != "durable\n" {
		cmd.Process.Kill()
		cmd.Wait()
		t.Fatalf("child not ready: %q %v %s", line, e, stderr.String())
	}
	// Kill during repeated candidate writes, bypassing Close and deferred cleanup.
	time.Sleep(time.Millisecond)
	if e = cmd.Process.Kill(); e != nil {
		t.Fatal(e)
	}
	cmd.Wait()
	s := mustOpen(t, dir, 8<<20)
	if s.Stats().Batches < 1 {
		t.Fatal("durably committed prefix lost")
	}
	for {
		_, rs, ack, e := s.Next()
		if errors.Is(e, ErrEmpty) {
			break
		}
		if e != nil || len(rs) != 2 {
			t.Fatalf("partial committed batch: %v", e)
		}
		if e = ack(); e != nil {
			t.Fatal(e)
		}
	}
}
