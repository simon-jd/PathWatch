// Package spool persists a single-owner FIFO with atomic manifest transactions.
package spool

import (
	"bytes"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"sync"

	"golang.org/x/sys/unix"
	"pathwatch.local/pathwatch/probe/internal/result"
)

var (
	ErrEmpty  = errors.New("spool is empty")
	ErrClosed = errors.New("spool is closed")
	// ErrUncertain means the manifest rename occurred but durability could not be
	// confirmed. No old files are removed; stop and reopen to recover the outcome.
	ErrUncertain = errors.New("spool transaction outcome uncertain; close and reopen")
)

const MaxBytes int64 = 1 << 30
const maxBatchBytes int64 = 16 << 20
const maxManifestBytes int64 = 16 << 20
const maxBatches = 65536

type entry struct {
	ID     string
	Size   int64
	SHA256 string
}
type manifest struct {
	Version int
	Dropped uint64
	Entries []entry
}
type batch struct {
	ID      string
	Results []result.Result
}
type Stats struct {
	Bytes   int64
	Batches int
	Dropped uint64
}
type Spool struct {
	mu     sync.Mutex
	dir    string
	max    int64
	lock   *os.File
	state  manifest
	bytes  int64
	closed bool
	poison error
}

// Open takes a nonblocking OS file lock, validates every committed batch and
// recovers only this format's temporary/unreferenced files. maxBytes counts
// serialized committed batch bytes; metadata/block overhead and one atomic
// candidate are additional. See README for bounds and crash semantics.
func Open(dir string, maxBytes int64) (s *Spool, err error) {
	if maxBytes <= 0 || maxBytes > MaxBytes {
		return nil, fmt.Errorf("spool capacity must be in 1..1GiB")
	}
	if err = os.MkdirAll(dir, 0700); err != nil {
		return nil, err
	}
	lock, err := openRegular(filepath.Join(dir, ".lock"), unix.O_RDWR|unix.O_CREAT)
	if err != nil {
		return nil, err
	}
	if err = unix.Flock(int(lock.Fd()), unix.LOCK_EX|unix.LOCK_NB); err != nil {
		lock.Close()
		return nil, fmt.Errorf("spool already owned: %w", err)
	}
	s = &Spool{dir: dir, max: maxBytes, lock: lock, state: manifest{Version: 1}}
	owned := s
	defer func() {
		if err != nil {
			owned.Close()
			s = nil
		}
	}()
	raw, err := readLimited(filepath.Join(dir, "manifest.json"), maxManifestBytes)
	if errors.Is(err, os.ErrNotExist) {
		// A missing manifest with batch files may be data loss, not a new spool.
		if err = walk(dir, func(name string) error {
			if validBatchName(name) {
				return fmt.Errorf("batch exists without manifest: %s", name)
			}
			return nil
		}); err != nil {
			return nil, err
		}
		data, _ := json.Marshal(s.state)
		if _, err = s.atomicWrite("manifest.json", data); err != nil {
			return nil, err
		}
	} else if err != nil {
		return nil, err
	} else {
		if err = json.Unmarshal(raw, &s.state); err != nil {
			return nil, fmt.Errorf("corrupt manifest: %w", err)
		}
	}
	if s.state.Version != 1 || len(s.state.Entries) > maxBatches {
		return nil, fmt.Errorf("invalid spool manifest version or size")
	}
	seen := map[string]bool{}
	for _, e := range s.state.Entries {
		if !validID(e.ID) || seen[e.ID] || e.Size <= 0 || e.Size > maxBatchBytes || len(e.SHA256) != 64 {
			return nil, fmt.Errorf("invalid committed batch metadata")
		}
		seen[e.ID] = true
		s.bytes += e.Size
		if s.bytes > maxBytes {
			return nil, fmt.Errorf("committed spool exceeds configured capacity")
		}
		if _, err = s.load(e); err != nil {
			return nil, err
		}
	}
	if err = s.cleanup(); err != nil {
		return nil, err
	}
	return s, nil
}
func (s *Spool) check() error {
	if s.closed {
		return ErrClosed
	}
	return s.poison
}
func (s *Spool) Stats() Stats {
	s.mu.Lock()
	defer s.mu.Unlock()
	return Stats{s.bytes, len(s.state.Entries), s.state.Dropped}
}
func (s *Spool) Close() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.closed {
		return nil
	}
	s.closed = true
	return s.lock.Close()
}

func (s *Spool) Append(rs []result.Result) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := s.check(); err != nil {
		return err
	}
	if len(rs) == 0 || len(rs) > 10000 {
		return fmt.Errorf("batch requires 1..10000 results")
	}
	var idBytes [16]byte
	if _, err := rand.Read(idBytes[:]); err != nil {
		return err
	}
	id := hex.EncodeToString(idBytes[:])
	limit := min(s.max, maxBatchBytes)
	buf := boundedBuffer{limit: limit}
	if err := json.NewEncoder(&buf).Encode(batch{ID: id, Results: rs}); err != nil {
		return fmt.Errorf("serialize batch (limit %d): %w", limit, err)
	}
	data := buf.Bytes()
	sum := sha256.Sum256(data)
	e := entry{id, int64(len(data)), hex.EncodeToString(sum[:])}
	next := manifest{Version: 1, Dropped: s.state.Dropped, Entries: append(append([]entry(nil), s.state.Entries...), e)}
	total := s.bytes + e.Size
	for total > s.max || len(next.Entries) > maxBatches {
		total -= next.Entries[0].Size
		next.Entries = next.Entries[1:]
		next.Dropped++
	}
	// No eviction is committed until both the candidate and manifest are durable.
	renamed, err := s.atomicWrite(id+".batch", data)
	if err != nil {
		if renamed {
			_ = os.Remove(filepath.Join(s.dir, id+".batch"))
		}
		return err
	}
	if err = s.commit(next, total); err != nil {
		if !errors.Is(err, ErrUncertain) {
			_ = os.Remove(filepath.Join(s.dir, id+".batch"))
		}
		return err
	}
	return s.afterCommit()
}

// Next returns an isolated, ordered snapshot. ACK is idempotent and bound to
// this exact ID; eviction/repeated/stale ACK cannot remove another batch.
func (s *Spool) Next() (string, []result.Result, func() error, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := s.check(); err != nil {
		return "", nil, nil, err
	}
	if len(s.state.Entries) == 0 {
		return "", nil, nil, ErrEmpty
	}
	e := s.state.Entries[0]
	rs, err := s.load(e)
	if err != nil {
		return "", nil, nil, err
	}
	return e.ID, rs, func() error { return s.ack(e.ID) }, nil
}
func (s *Spool) ack(id string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := s.check(); err != nil {
		return err
	}
	if len(s.state.Entries) == 0 || s.state.Entries[0].ID != id {
		return nil
	}
	next := manifest{Version: 1, Dropped: s.state.Dropped, Entries: append([]entry(nil), s.state.Entries[1:]...)}
	if err := s.commit(next, s.bytes-s.state.Entries[0].Size); err != nil {
		return err
	}
	return s.afterCommit()
}
func (s *Spool) commit(next manifest, total int64) error {
	data, err := json.Marshal(next)
	if err != nil {
		return err
	}
	if len(data) > int(maxManifestBytes) {
		return fmt.Errorf("manifest size limit exceeded")
	}
	renamed, err := s.atomicWrite("manifest.json", data)
	if err != nil {
		if renamed {
			s.poison = fmt.Errorf("%w: %v", ErrUncertain, err)
			return s.poison
		}
		return err
	}
	s.state = next
	s.bytes = total
	return nil
}
func (s *Spool) afterCommit() error {
	if err := s.cleanup(); err != nil {
		s.poison = fmt.Errorf("transaction committed; cleanup failed, close and reopen: %w", err)
		return s.poison
	}
	return nil
}
func (s *Spool) load(e entry) ([]result.Result, error) {
	data, err := readLimited(filepath.Join(s.dir, e.ID+".batch"), min(s.max, maxBatchBytes))
	if err != nil {
		return nil, fmt.Errorf("read committed batch %s: %w", e.ID, err)
	}
	sum := sha256.Sum256(data)
	if int64(len(data)) != e.Size || hex.EncodeToString(sum[:]) != e.SHA256 {
		return nil, fmt.Errorf("corrupt committed batch %s", e.ID)
	}
	var b batch
	if err = json.Unmarshal(data, &b); err != nil {
		return nil, fmt.Errorf("corrupt committed batch %s: %w", e.ID, err)
	}
	if b.ID != e.ID || len(b.Results) == 0 || len(b.Results) > 10000 {
		return nil, fmt.Errorf("invalid committed batch %s", e.ID)
	}
	return b.Results, nil
}
func (s *Spool) cleanup() error {
	keep := map[string]bool{}
	for _, e := range s.state.Entries {
		keep[e.ID+".batch"] = true
	}
	removed := false
	err := walk(s.dir, func(name string) error {
		if strings.HasPrefix(name, ".pw-tmp-") || (validBatchName(name) && !keep[name]) {
			if err := os.Remove(filepath.Join(s.dir, name)); err != nil {
				return err
			}
			removed = true
		}
		return nil
	})
	if err != nil {
		return err
	}
	if removed {
		return syncDir(s.dir)
	}
	return nil
}

// atomicWrite never removes the previous destination on a pre-rename failure.
// renamed distinguishes uncertain post-rename durability from a failed write.
func (s *Spool) atomicWrite(name string, data []byte) (renamed bool, err error) {
	f, err := os.CreateTemp(s.dir, ".pw-tmp-")
	if err != nil {
		return false, err
	}
	tmp := f.Name()
	defer func() {
		f.Close()
		if !renamed {
			_ = os.Remove(tmp)
		}
	}()
	if _, err = f.Write(data); err != nil {
		return false, err
	}
	if err = f.Sync(); err != nil {
		return false, err
	}
	if err = f.Close(); err != nil {
		return false, err
	}
	if err = os.Rename(tmp, filepath.Join(s.dir, name)); err != nil {
		return false, err
	}
	return true, syncDir(s.dir)
}
func syncDir(dir string) error {
	f, e := os.Open(dir)
	if e != nil {
		return e
	}
	defer f.Close()
	return f.Sync()
}
func openRegular(path string, flags int) (*os.File, error) {
	fd, err := unix.Open(path, flags|unix.O_NOFOLLOW|unix.O_CLOEXEC|unix.O_NONBLOCK, 0600)
	if err != nil {
		return nil, err
	}
	f := os.NewFile(uintptr(fd), path)
	info, err := f.Stat()
	if err != nil || !info.Mode().IsRegular() {
		f.Close()
		if err == nil {
			err = fmt.Errorf("not a regular file: %s", path)
		}
		return nil, err
	}
	return f, nil
}
func readLimited(path string, limit int64) ([]byte, error) {
	f, err := openRegular(path, unix.O_RDONLY)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil {
		return nil, err
	}
	if info.Size() > limit {
		return nil, fmt.Errorf("spool file exceeds limit %d", limit)
	}
	b, err := io.ReadAll(io.LimitReader(f, limit+1))
	if int64(len(b)) > limit {
		return nil, fmt.Errorf("spool file exceeds limit %d", limit)
	}
	return b, err
}
func walk(dir string, visit func(string) error) error {
	f, err := os.Open(dir)
	if err != nil {
		return err
	}
	defer f.Close()
	for {
		names, err := f.Readdirnames(128)
		for _, n := range names {
			if e := visit(n); e != nil {
				return e
			}
		}
		if err == io.EOF {
			return nil
		}
		if err != nil {
			return err
		}
	}
}
func validID(s string) bool {
	if len(s) != 32 {
		return false
	}
	_, e := hex.DecodeString(s)
	return e == nil && strings.ToLower(s) == s
}
func validBatchName(s string) bool {
	return strings.HasSuffix(s, ".batch") && validID(strings.TrimSuffix(s, ".batch"))
}

type boundedBuffer struct {
	bytes.Buffer
	limit int64
}

func (b *boundedBuffer) Write(p []byte) (int, error) {
	if int64(b.Len())+int64(len(p)) > b.limit {
		return 0, fmt.Errorf("batch exceeds capacity")
	}
	return b.Buffer.Write(p)
}
