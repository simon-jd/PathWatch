// Package store implements a bounded single-writer journal and ClickHouse sink.
// Ambiguous writes require a verified ClickHouse restart before recovery.
package store

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"

	"golang.org/x/sys/unix"
	"google.golang.org/protobuf/proto"
	"pathwatch.local/pathwatch/ingest/schema"
	pb "pathwatch.local/pathwatch/proto/pathwatch/v1"
)

var ErrConflict = errors.New("batch key has different ordered content")
var ErrCapacity = errors.New("journal capacity reached")
var identifier = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_]{0,63}$`)

const maxRecord = 24 << 20
const maxRecords = 65536

type Config struct {
	URL, Database, Username, Password, JournalDir string
	MaxJournalBytes                               int64
	MaxRecords                                    int
}
type owner struct {
	Version                                           int
	URL, Database, DatabaseUUID, JournalID, Directory string
}
type record struct {
	Key, Hash, Epoch string
	Payload          []byte
	Committed        bool
}
type Store struct {
	ownerIdentity string
	cfg           Config
	lock          *os.File
	gate          chan struct{}
	client        *http.Client
	epoch         string
	session       string
	records       map[string]record
	bytes         int64
	closed        bool
	poison        error
	recoveries    uint64
}

func Open(ctx context.Context, cfg Config) (s *Store, err error) {
	if cfg.MaxRecords == 0 {
		cfg.MaxRecords = maxRecords
	}
	if cfg.MaxRecords < 1 || cfg.MaxRecords > 262144 {
		return nil, errors.New("journal record capacity must be in 1..262144")
	}
	u, e := url.Parse(cfg.URL)
	if e != nil || u.Scheme != "http" || u.User != nil || u.RawQuery != "" || u.Fragment != "" || (u.Path != "" && u.Path != "/") || net.ParseIP(u.Hostname()) == nil || !net.ParseIP(u.Hostname()).IsLoopback() || u.Port() == "" {
		return nil, errors.New("ClickHouse URL must be literal loopback http://IP:port")
	}
	if !identifier.MatchString(cfg.Database) || cfg.JournalDir == "" || cfg.MaxJournalBytes < 1024 || cfg.MaxJournalBytes > 1<<30 {
		return nil, errors.New("database, journal directory and capacity [1KiB,1GiB] required")
	}
	if err = os.MkdirAll(cfg.JournalDir, 0700); err != nil {
		return nil, err
	}
	cfg.JournalDir, err = filepath.EvalSymlinks(cfg.JournalDir)
	if err != nil {
		return nil, err
	}
	cfg.JournalDir, err = filepath.Abs(cfg.JournalDir)
	if err != nil {
		return nil, err
	}
	lock, err := regular(filepath.Join(cfg.JournalDir, ".lock"), unix.O_CREAT|unix.O_RDWR)
	if err != nil {
		return nil, err
	}
	if err = unix.Flock(int(lock.Fd()), unix.LOCK_EX|unix.LOCK_NB); err != nil {
		lock.Close()
		return nil, fmt.Errorf("journal already owned: %w", err)
	}
	s = &Store{cfg: cfg, lock: lock, gate: make(chan struct{}, 1), records: map[string]record{}, client: &http.Client{Timeout: 30 * time.Second, Transport: &http.Transport{Proxy: nil, DisableKeepAlives: true, MaxConnsPerHost: 1}, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}}
	s.gate <- struct{}{}
	owned := s
	defer func() {
		if err != nil {
			owned.Close()
			s = nil
		}
	}()
	ctx, cancel := context.WithTimeout(ctx, 60*time.Second)
	defer cancel()
	dbUUID, err := s.query(ctx, "SELECT toString(uuid) FROM system.databases WHERE name="+quote(cfg.Database))
	if err != nil {
		return nil, err
	}
	if dbUUID == "" || dbUUID == "00000000-0000-0000-0000-000000000000" {
		return nil, errors.New("dedicated Atomic database with stable UUID required")
	}
	meta := owner{Version: 1, URL: cfg.URL, Database: cfg.Database, DatabaseUUID: dbUUID, Directory: cfg.JournalDir}
	b, err := readFile(filepath.Join(cfg.JournalDir, "owner.json"), 4096)
	if errors.Is(err, os.ErrNotExist) {
		names, e := os.ReadDir(cfg.JournalDir)
		if e != nil {
			return nil, e
		}
		for _, n := range names {
			if strings.HasSuffix(n.Name(), ".json") {
				return nil, errors.New("journal records without owner")
			}
		}
		meta.JournalID = randomID()
		if err = atomicJSON(cfg.JournalDir, "owner.json", meta); err != nil {
			return nil, err
		}
	} else if err != nil {
		return nil, err
	} else {
		var old owner
		if json.Unmarshal(b, &old) != nil || old.Version != 1 || old.JournalID == "" {
			return nil, errors.New("corrupt owner")
		}
		meta.JournalID = old.JournalID
		if old != meta {
			return nil, errors.New("journal endpoint/database UUID/owner mismatch")
		}
	}
	binding, _ := json.Marshal(meta)
	s.ownerIdentity = digest(binding)
	exists, err := s.query(ctx, "SELECT count() FROM system.tables WHERE database=currentDatabase() AND name='ingest_owner'")
	if err != nil {
		return nil, err
	}
	if exists == "0" {
		n, e := s.query(ctx, "SELECT count() FROM system.tables WHERE database=currentDatabase()")
		if e != nil {
			return nil, e
		}
		if n != "0" {
			return nil, errors.New("refuse to claim nonempty unowned database")
		}
	}
	if _, err = s.query(ctx, "CREATE TABLE IF NOT EXISTS ingest_owner (dummy UInt8) ENGINE=MergeTree ORDER BY tuple() COMMENT "+quote(string(binding))); err != nil {
		return nil, err
	}
	got, err := s.query(ctx, "SELECT comment FROM system.tables WHERE database=currentDatabase() AND name='ingest_owner'")
	if err != nil {
		return nil, err
	}
	if got != string(binding) {
		return nil, errors.New("database belongs to another journal")
	}
	if _, err = s.query(ctx, "CREATE TABLE IF NOT EXISTS ingest_epoch (epoch String) ENGINE=Memory"); err != nil {
		return nil, err
	}
	s.epoch, err = s.query(ctx, "SELECT epoch FROM ingest_epoch")
	if err != nil {
		return nil, err
	}
	if s.epoch == "" {
		s.epoch = randomID()
		if _, err = s.query(ctx, "INSERT INTO ingest_epoch VALUES ("+quote(s.epoch)+")"); err != nil {
			return nil, err
		}
	}
	if len(s.epoch) != 32 {
		return nil, errors.New("invalid server epoch; refuse writes")
	}
	s.session = randomID()
	if err = s.bindSession(ctx); err != nil {
		return nil, err
	}
	for _, q := range strings.Split(schema.Initial, ";") {
		if strings.TrimSpace(q) != "" {
			if _, err = s.query(ctx, q); err != nil {
				return nil, err
			}
		}
	}
	names, err := os.ReadDir(cfg.JournalDir)
	if err != nil {
		return nil, err
	}
	if len(names) > cfg.MaxRecords+8 {
		return nil, ErrCapacity
	}
	for _, n := range names {
		if n.Name() == "owner.json" || !strings.HasSuffix(n.Name(), ".json") {
			continue
		}
		b, e := readFile(filepath.Join(cfg.JournalDir, n.Name()), maxRecord)
		if e != nil {
			return nil, e
		}
		var r record
		if json.Unmarshal(b, &r) != nil || n.Name() != r.Key+".json" || len(r.Key) != 64 || r.Hash != digest(r.Payload) || len(r.Epoch) != 32 {
			return nil, errors.New("corrupt journal record")
		}
		var req pb.ReportRequest
		if proto.Unmarshal(r.Payload, &req) != nil || len(req.Results) == 0 || r.Key != key(&req) {
			return nil, errors.New("corrupt journal payload")
		}
		if len(s.records) >= cfg.MaxRecords {
			return nil, ErrCapacity
		}
		s.records[r.Key] = r
		s.bytes += int64(len(b))
		if s.bytes > cfg.MaxJournalBytes {
			return nil, ErrCapacity
		}
	}
	for _, n := range names {
		if strings.HasPrefix(n.Name(), ".pw-tmp-") {
			if err = os.Remove(filepath.Join(cfg.JournalDir, n.Name())); err != nil {
				return nil, err
			}
		}
	}
	if err = syncDirectory(cfg.JournalDir); err != nil {
		return nil, err
	}
	for _, r := range s.records {
		if r.Committed {
			continue
		}
		if r.Epoch == s.epoch {
			return nil, errors.New("pending write: restart dedicated ClickHouse before recovery")
		}
		r.Epoch = s.epoch
		if err = s.persist(r); err != nil {
			return nil, err
		}
		// A previous interrupted mutation can outlive its SQL connection. Wait for
		// all owned-table mutations to finish before any new insert.
		for {
			n, e := s.query(ctx, "SELECT count() FROM system.mutations WHERE database=currentDatabase() AND table IN ('probe_results','path_quantiles_5m') AND NOT is_done")
			if e != nil {
				return nil, e
			}
			if n == "0" {
				break
			}
			select {
			case <-ctx.Done():
				return nil, ctx.Err()
			case <-time.After(100 * time.Millisecond):
			}
		}
		for _, table := range []string{"probe_results", "path_quantiles_5m"} {
			if _, err = s.query(ctx, "ALTER TABLE "+table+" DELETE WHERE batch_key="+quote(r.Key)+" SETTINGS mutations_sync=2"); err != nil {
				return nil, err
			}
		}
		if err = s.insert(ctx, r); err != nil {
			return nil, err
		}
		r.Committed = true
		if err = s.persist(r); err != nil {
			return nil, err
		}
		s.recoveries++
	}
	return s, nil
}
func (s *Store) Close() error {
	<-s.gate
	defer func() { s.gate <- struct{}{} }()
	if s.closed {
		return nil
	}
	s.closed = true
	s.client.CloseIdleConnections()
	return s.lock.Close()
}
func (s *Store) Recoveries() uint64 { return s.recoveries }

// OwnerIdentity fingerprints the existing durable endpoint/database UUID/journal
// UUID/canonical-directory binding. It is immutable and excludes epoch/session.
func (s *Store) OwnerIdentity() string { return s.ownerIdentity }

// Commit receives a validated request. The entire batch is flushed immediately,
// with no timer or partial ACK. A pending record is durable before any SQL write.
func (s *Store) Commit(ctx context.Context, req *pb.ReportRequest) (duplicate bool, err error) {
	select {
	case <-ctx.Done():
		return false, ctx.Err()
	case <-s.gate:
	}
	defer func() { s.gate <- struct{}{} }()
	if s.closed {
		return false, errors.New("store closed")
	}
	if s.poison != nil {
		return false, s.poison
	}
	clone := proto.Clone(req).(*pb.ReportRequest)
	clone.SentAt = nil
	data, e := proto.MarshalOptions{Deterministic: true}.Marshal(clone)
	if e != nil {
		return false, e
	}
	k := key(clone)
	hash := digest(data)
	if old, ok := s.records[k]; ok {
		if old.Hash != hash {
			return false, ErrConflict
		}
		if !old.Committed {
			return false, errors.New("pending batch requires restart recovery")
		}
		return true, nil
	}
	if err = s.bindSession(ctx); err != nil {
		s.poison = err
		return false, err
	}
	r := record{Key: k, Hash: hash, Epoch: s.epoch, Payload: data}
	if err = s.persist(r); err != nil {
		if !errors.Is(err, ErrCapacity) {
			s.poison = err
		}
		return false, err
	}
	ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	if err = s.insert(ctx, r); err == nil {
		r.Committed = true
		err = s.persist(r)
	}
	if err != nil {
		s.poison = fmt.Errorf("uncertain batch; writer frozen, close and restart dedicated ClickHouse: %w", err)
		return false, s.poison
	}
	return false, nil
}
func (s *Store) persist(r record) error {
	b, e := json.Marshal(r)
	if e != nil {
		return e
	}
	old := s.records[r.Key]
	oldBytes, _ := json.Marshal(old)
	delta := int64(len(b))
	if old.Key != "" {
		delta -= int64(len(oldBytes))
	}
	if len(b) > maxRecord || s.bytes+delta > s.cfg.MaxJournalBytes || (old.Key == "" && len(s.records) >= s.recordLimit()) {
		return ErrCapacity
	}
	if e = atomicWrite(s.cfg.JournalDir, r.Key+".json", b); e != nil {
		return e
	}
	s.records[r.Key] = r
	s.bytes += delta
	return nil
}
func (s *Store) bindSession(ctx context.Context) error {
	got, err := s.request(ctx, "SELECT epoch FROM ingest_epoch", false)
	if err != nil {
		return err
	}
	if got != s.epoch {
		return errors.New("ClickHouse restarted; reopen writer before new batches")
	}
	return nil
}
func (s *Store) query(ctx context.Context, q string) (string, error) { return s.request(ctx, q, true) }
func (s *Store) request(ctx context.Context, q string, check bool) (string, error) {
	u, _ := url.Parse(s.cfg.URL)
	v := u.Query()
	v.Set("database", s.cfg.Database)
	if s.session != "" {
		v.Set("session_id", s.session)
		v.Set("session_timeout", "300")
		if check {
			v.Set("session_check", "1")
		}
	}
	v.Set("wait_end_of_query", "1")
	v.Set("async_insert", "0")
	v.Set("insert_deduplicate", "0")
	v.Set("materialized_views_ignore_errors", "0")
	v.Set("max_execution_time", "30")
	v.Set("max_threads", "2")
	v.Set("max_memory_usage", "268435456")
	u.RawQuery = v.Encode()
	// POST with a non-rewindable body and no idempotency headers: no HTTP retry.
	req, e := http.NewRequestWithContext(ctx, "POST", u.String(), io.NopCloser(strings.NewReader(q)))
	if e != nil {
		return "", e
	}
	req.SetBasicAuth(s.cfg.Username, s.cfg.Password)
	resp, e := s.client.Do(req)
	if e != nil {
		return "", e
	}
	defer resp.Body.Close()
	b, e := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if e != nil {
		return "", e
	}
	if resp.StatusCode != 200 {
		return "", fmt.Errorf("ClickHouse status %d: %.2000s", resp.StatusCode, b)
	}
	if len(b) >= 1<<20 {
		return "", errors.New("ClickHouse response too large")
	}
	return strings.TrimSpace(string(b)), nil
}
func key(r *pb.ReportRequest) string { return digest([]byte(r.ProbeId + "\x00" + r.BatchId)) }
func digest(b []byte) string         { h := sha256.Sum256(b); return hex.EncodeToString(h[:]) }
func randomID() string {
	var b [16]byte
	if _, e := rand.Read(b[:]); e != nil {
		panic(e)
	}
	return hex.EncodeToString(b[:])
}
func quote(s string) string {
	return "'" + strings.ReplaceAll(strings.ReplaceAll(s, `\`, `\\`), "'", `\'`) + "'"
}

func (s *Store) recordLimit() int {
	if s.cfg.MaxRecords == 0 {
		return maxRecords
	}
	return s.cfg.MaxRecords
}
