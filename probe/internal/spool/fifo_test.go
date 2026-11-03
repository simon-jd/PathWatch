package spool

import (
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	"golang.org/x/sys/unix"
)

// A FIFO substituted for a committed file must fail before waiting for a
// writer. On RED, connect both ends only to release the blocked open syscall;
// always join the operation so no diagnostic goroutine remains behind.
func assertFIFORejectedPromptly(t *testing.T, path string, operation func() error) {
	t.Helper()
	done := make(chan error, 1)
	go func() { done <- operation() }()
	var err error
	select {
	case err = <-done:
	case <-time.After(250 * time.Millisecond):
		unblock, e := os.OpenFile(path, os.O_RDWR|unix.O_NONBLOCK, 0)
		if e != nil {
			t.Fatal(e)
		}
		defer unblock.Close()
		select {
		case err = <-done:
		case <-time.After(time.Second):
			t.Fatal("FIFO operation stayed blocked after connecting a writer")
		}
		t.Errorf("FIFO operation blocked until a writer connected: %v", err)
	}
	if err == nil || !strings.Contains(err.Error(), "not a regular file") {
		t.Fatalf("FIFO must be explicitly rejected, got %v", err)
	}
}
func substituteFIFO(t *testing.T, path string) func() {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if err = os.Remove(path); err != nil {
		t.Fatal(err)
	}
	if err = unix.Mkfifo(path, 0600); err != nil {
		t.Fatal(err)
	}
	return func() {
		t.Helper()
		if err := os.Remove(path); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, data, 0600); err != nil {
			t.Fatal(err)
		}
	}
}
func TestSpool_FIFORecoveryRejectsWithoutBlocking(t *testing.T) {
	for _, kind := range []string{"manifest", "committed-batch"} {
		t.Run(kind, func(t *testing.T) {
			dir := t.TempDir()
			s := mustOpen(t, dir, 1<<20)
			if err := s.Append(fixture()); err != nil {
				t.Fatal(err)
			}
			id, _, _, err := s.Next()
			if err != nil {
				t.Fatal(err)
			}
			if err = s.Close(); err != nil {
				t.Fatal(err)
			}
			path := filepath.Join(dir, "manifest.json")
			if kind == "committed-batch" {
				path = filepath.Join(dir, id+".batch")
			}
			restore := substituteFIFO(t, path)
			assertFIFORejectedPromptly(t, path, func() error {
				got, e := Open(dir, 1<<20)
				if got != nil {
					got.Close()
				}
				return e
			})
			restore()
			// The failed Open must have closed its lock/file descriptors. Repaired data
			// remains readable by a new owner with the original ID and ordered contents.
			recovered := mustOpen(t, dir, 1<<20)
			again, rs, _, e := recovered.Next()
			if e != nil || again != id || !reflect.DeepEqual(rs, fixture()) {
				t.Fatalf("failed recovery leaked ownership or damaged contents: %s %v", again, e)
			}
		})
	}
}
func TestSpool_FIFONextReleasesMutexAndOwnership(t *testing.T) {
	dir := t.TempDir()
	s := mustOpen(t, dir, 1<<20)
	if e := s.Append(fixture()); e != nil {
		t.Fatal(e)
	}
	id, _, _, e := s.Next()
	if e != nil {
		t.Fatal(e)
	}
	path := filepath.Join(dir, id+".batch")
	restore := substituteFIFO(t, path)
	assertFIFORejectedPromptly(t, path, func() error { _, _, _, e := s.Next(); return e })
	done := make(chan error, 1)
	go func() {
		if s.Stats().Batches != 1 {
			done <- os.ErrInvalid
			return
		}
		done <- s.Close()
	}()
	select {
	case e := <-done:
		if e != nil {
			t.Fatal(e)
		}
	case <-time.After(time.Second):
		t.Fatal("FIFO rejection retained spool mutex or blocked Close")
	}
	restore()
	recovered := mustOpen(t, dir, 1<<20)
	again, rs, _, e := recovered.Next()
	if e != nil || again != id || !reflect.DeepEqual(rs, fixture()) {
		t.Fatalf("FIFO Next damaged retry snapshot: %s %v", again, e)
	}
}
