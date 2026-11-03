package store

// Explicit opt-in synthetic fixture. Compiled before tool retirement and run by
// capacity_fixture.py in a fresh dedicated database/journal, never a campaign.
import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
	"math/rand"
	"os"
	"path/filepath"
	pb "pathwatch.local/pathwatch/proto/pathwatch/v1"
	"runtime"
	"strconv"
	"strings"
	"testing"
	"time"
)

func TestCapacityNativeFixture(t *testing.T) {
	base := os.Getenv("PW_C3_DIRECTORY")
	if base == "" {
		t.Skip("explicit owned synthetic fixture only")
	}
	if !strings.HasPrefix(filepath.Base(base), "task10c3-") {
		t.Fatal("dedicated task10c3 directory required")
	}
	rows, records := 1500, 192
	if os.Getenv("PW_C3_VOLUME") == "medium" {
		rows, records = 150000, 19200
	}
	if os.Getenv("PW_C3_VOLUME") == "full" {
		rows, records = 864525, 110505
	}
	cfg := Config{URL: os.Getenv("PW_C3_URL"), Database: "task10c3", Username: "pathwatch_ingest", Password: os.Getenv("PATHWATCH_CLICKHOUSE_PASSWORD"), JournalDir: filepath.Join(base, "journal"), MaxJournalBytes: 1 << 30, MaxRecords: 131072}
	ctx := context.Background()
	out := map[string]any{"stress_fields": "maximum-size diagnostic and phase/TLS fields, not a replay of real measurements", "load_method": "synthetic O_EXCL0600 canonical JSON; directory sync; production pending/recovery unchanged", "filesystem_cache": "warm; not a cold-disk or power-loss claim", "synthetic": true, "rows": rows, "records": records, "seed": int64(1032026), "stage": os.Getenv("PW_C3_STAGE")}
	save := func() {
		b, _ := json.MarshalIndent(out, "", "  ")
		if e := os.WriteFile(filepath.Join(base, "fixture-"+os.Getenv("PW_C3_STAGE")+".json"), b, 0600); e != nil {
			t.Fatal(e)
		}
	}
	settle := func(st *Store, afterRows int) {
		began := time.Now()
		polls := 0
		for {
			value, err := st.query(ctx, "SELECT (SELECT count() FROM system.parts WHERE database='task10c3' AND NOT active)+(SELECT count() FROM system.merges WHERE database='task10c3')")
			if err != nil {
				t.Fatal(err)
			}
			polls++
			if value == "0" {
				break
			}
			if time.Since(began) > 600*time.Second {
				t.Fatal("original480s part GC did not settle within600s")
			}
			time.Sleep(2 * time.Second)
		}
		events, _ := out["settle"].([]map[string]any)
		out["settle"] = append(events, map[string]any{"after_rows": afterRows, "seconds": time.Since(began).Seconds(), "polls": polls, "old_parts_lifetime_seconds": 480})
		t.Logf("settled original part GC after_rows=%d seconds=%.3f polls=%d", afterRows, time.Since(began).Seconds(), polls)
	}
	if os.Getenv("PW_C3_STAGE") == "seed" {
		started := time.Now()
		st, e := Open(ctx, cfg)
		if e != nil {
			t.Fatal(e)
		}
		rng := rand.New(rand.NewSource(1032026))
		rowIndex := 0
		siteRows := []int{rows * 35 / 72, rows * 35 / 72, rows - 2*(rows*35/72)}
		siteRecords := []int{records * 35 / 72, records * 35 / 72, records - 2*(records*35/72)}
		siteIndex, siteBatch, siteRow := 0, 0, 0
		out["site_rows"] = siteRows
		out["site_records"] = siteRecords
		out["timestamp_method"] = "108s synthetic rounds across35d/35d/48h; conservative extra restart rows at final second"
		var sql strings.Builder
		sql.WriteString("INSERT INTO probe_results FORMAT JSONEachRow\n")
		buffered := 0
		sqlHash := sha256.New()
		journalHash := sha256.New()
		insertCount := 0
		nextSettle := 150000
		flush := func() {
			if buffered == 0 {
				return
			}
			sqlHash.Write([]byte(sql.String()))
			insertCount++
			if _, e := st.query(ctx, sql.String()); e != nil {
				t.Fatal(e)
			}
			sql.Reset()
			sql.WriteString("INSERT INTO probe_results FORMAT JSONEachRow\n")
			buffered = 0
		}
		kinds := []string{"tcp", "tls", "http1", "http2", "http3", "trace", "dns", "dns", "dns", "dns", "dns", "dns", "dns", "dns", "dns"}
		for batch := 0; batch < records; batch++ {
			if siteBatch == siteRecords[siteIndex] {
				siteIndex++
				siteBatch = 0
				siteRow = 0
			}
			req := &pb.ReportRequest{ProbeId: fmt.Sprintf("observer-%07d", siteIndex), BatchId: fmt.Sprintf("%064d", batch)}
			count := siteRows[siteIndex] / siteRecords[siteIndex]
			if siteBatch < siteRows[siteIndex]%siteRecords[siteIndex] {
				count++
			}
			for j := 0; j < count; j++ {
				diagnostic := make([]byte, 256)
				for k := range diagnostic {
					diagnostic[k] = byte(33 + rng.Intn(94))
				}
				position := siteRow % 15
				kind := kinds[position]
				duration := 35 * 86400
				if siteIndex == 2 {
					duration = 2 * 86400
				}
				elapsed := (siteRow / 15) * 108
				if elapsed >= duration {
					elapsed = duration - 1
				}
				at := time.Date(2026, 10, 24, 0, 0, 0, 999999999, time.UTC).Add(time.Duration(elapsed) * time.Second)
				phases := map[string]int64{"resolve": rng.Int63(), "dns": rng.Int63(), "connect": rng.Int63(), "tls_handshake": rng.Int63(), "ttfb": rng.Int63(), "total": rng.Int63()}
				r := &pb.ProbeResult{ProbeId: req.ProbeId, TargetId: strings.Repeat("t", 16), Kind: kind, Family: "v4", Addr: "10.255.255.254", StartedAt: timestamppb.New(at), Err: string(diagnostic), Protocol: "h3", TlsVersion: 65535, TlsResumed: true, PhasesNanoseconds: phases}
				if kind == "dns" {
					mode := []string{"udp", "tcp", "doh"}[(position-6)/3]
					r.TargetId = strings.Repeat("d", 13) + mode + ":dns:" + mode
					r.Family = []string{"v4", "v6", ""}[(position-6)%3]
					if r.Family == "v6" {
						r.Addr = "fdff:ffff:ffff:ffff:ffff:ffff:ffff:ffff"
					}
					if r.Family == "" {
						r.Addr = ""
					}
				}
				if kind == "trace" {
					r.Family = "v4"
					r.Addr = "10.255.255.254"
					r.Protocol = ""
					r.TlsVersion = 0
					r.TlsResumed = false
					r.PhasesNanoseconds = map[string]int64{"total": rng.Int63()}
					for ttl := 1; ttl <= 30; ttl++ {
						r.Hops = append(r.Hops, &pb.Hop{Ttl: uint32(ttl), Addr: fmt.Sprintf("10.%d.%d.%d", rng.Intn(256), rng.Intn(256), rng.Intn(256)), RttNanoseconds: rng.Int63()})
					}
				}
				if proto.Size(r)+3 > 512 && kind != "trace" {
					t.Fatalf("normal encoding exceeds bound: %d", proto.Size(r)+3)
				}
				if kind == "trace" && proto.Size(r)+3 > 1472 {
					t.Fatal("trace encoding exceeds bound")
				}
				req.Results = append(req.Results, r)
				rowIndex++
				siteRow++
			}
			payload, e := proto.MarshalOptions{Deterministic: true}.Marshal(req)
			if e != nil {
				t.Fatal(e)
			}
			rec := record{Key: key(req), Hash: digest(payload), Epoch: st.epoch, Payload: payload, Committed: true}
			encoded, e := json.Marshal(rec)
			if e != nil {
				t.Fatal(e)
			}
			journalHash.Write([]byte(rec.Key))
			journalHash.Write(encoded)
			file, e := os.OpenFile(filepath.Join(cfg.JournalDir, rec.Key+".json"), os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
			if e != nil {
				t.Fatal(e)
			}
			n, e := file.Write(encoded)
			closeErr := file.Close()
			if e != nil || n != len(encoded) || closeErr != nil {
				t.Fatalf("synthetic seed write: %d %v %v", n, e, closeErr)
			}
			for index, r := range req.Results {
				ttls := []uint32{}
				addresses := []string{}
				rtts := []int64{}
				for _, h := range r.Hops {
					ttls = append(ttls, h.Ttl)
					addresses = append(addresses, h.Addr)
					rtts = append(rtts, h.RttNanoseconds)
				}
				n, present := PrimaryPhase(r)
				p := 0
				if present {
					p = 1
				}
				row := map[string]any{"batch_key": rec.Key, "result_index": index, "probe_id": r.ProbeId, "batch_id": req.BatchId, "target_id": r.TargetId, "kind": r.Kind, "family": r.Family, "addr": r.Addr, "path_key": r.ProbeId + "→" + r.TargetId + "/" + r.Kind + "/" + r.Family, "started_at": r.StartedAt.AsTime().Format("2006-01-02 15:04:05.000000000"), "phases_nanoseconds": r.PhasesNanoseconds, "err": r.Err, "protocol": r.Protocol, "hop_ttls": ttls, "hop_addrs": addresses, "hop_rtt_nanoseconds": rtts, "tls_version": r.TlsVersion, "tls_resumed": r.TlsResumed, "phase_ms": float64(n) / 1e6, "phase_present": p}
				b, e := json.Marshal(row)
				if e != nil {
					t.Fatal(e)
				}
				sql.Write(b)
				sql.WriteByte('\n')
				buffered++
			}
			if buffered >= 1024 {
				flush()
			}
			if os.Getenv("PW_C3_VOLUME") == "full" && rowIndex >= nextSettle && rowIndex < rows {
				flush()
				settle(st, rowIndex)
				nextSettle += 150000
			}
			siteBatch++
			if batch%10000 == 0 {
				t.Logf("seeded records=%d rows=%d", batch, rowIndex)
			}
		}
		flush()
		if e = syncDirectory(cfg.JournalDir); e != nil {
			t.Fatal(e)
		}
		out["insert_queries"] = insertCount
		out["insert_query_stream_sha256"] = hex.EncodeToString(sqlHash.Sum(nil))
		out["canonical_journal_stream_sha256"] = hex.EncodeToString(journalHash.Sum(nil))
		if e = st.Close(); e != nil {
			t.Fatal(e)
		}
		st = nil
		runtime.GC()
		if records > 65536 {
			smaller := cfg
			smaller.MaxRecords = 0
			if bad, e := Open(ctx, smaller); e != ErrCapacity {
				if bad != nil {
					bad.Close()
				}
				t.Fatalf("legacy record limit must reject larger journal: %v", e)
			}
		}
		startedOpen := time.Now()
		st, e = Open(ctx, cfg)
		out["open_seconds"] = time.Since(startedOpen).Seconds()
		if e != nil {
			t.Fatal(e)
		}
		if time.Since(startedOpen) > 60*time.Second {
			t.Fatal("Store.Open exceeded 60s")
		}
		firstKey := key(&pb.ReportRequest{ProbeId: "observer-0000000", BatchId: fmt.Sprintf("%064d", 0)})
		first := st.records[firstKey]
		var req pb.ReportRequest
		if e = proto.Unmarshal(first.Payload, &req); e != nil {
			t.Fatal(e)
		}
		duplicate, e := st.Commit(ctx, &req)
		if e != nil || !duplicate {
			t.Fatal("duplicate replay failed", e)
		}
		out["duplicate"] = true
		req.Results[0].Err = "different"
		if _, e = st.Commit(ctx, &req); e != ErrConflict {
			t.Fatal("conflicting replay accepted", e)
		}
		first.Committed = false
		if e = st.persist(first); e != nil {
			t.Fatal(e)
		}
		st.Close()
		st = nil
		runtime.GC()
		if bad, e := Open(ctx, cfg); e == nil {
			bad.Close()
			t.Fatal("same epoch pending record accepted")
		}
		out["requires_restart"] = true
		out["seed_seconds"] = time.Since(started).Seconds()
		save()
		return
	}
	if os.Getenv("PW_C3_STAGE") != "verify" {
		t.Fatal("seed or verify required")
	}
	started := time.Now()
	st, e := Open(ctx, cfg)
	if e != nil {
		t.Fatal(e)
	}
	defer st.Close()
	out["recovery_open_seconds"] = time.Since(started).Seconds()
	if time.Since(started) > 60*time.Second {
		t.Fatal("recovery Open exceeded60s")
	}
	if st.Recoveries() != 1 || len(st.records) != records {
		t.Fatalf("recovery=%d records=%d", st.Recoveries(), len(st.records))
	}
	for label, q := range map[string]string{"raw": "SELECT count() FROM probe_results", "unique": "SELECT uniqExact(tuple(batch_key,result_index)) FROM probe_results", "mv": "SELECT countMerge(samples) FROM path_quantiles_5m"} {
		value, e := st.query(ctx, q)
		if e != nil {
			t.Fatal(e)
		}
		n, e := strconv.Atoi(value)
		if e != nil || n != rows {
			t.Fatalf("%s=%s want%d: %v", label, value, rows, e)
		}
		out[label] = n
	}
	for label, query := range map[string]string{"paths": "SELECT uniqExact(path_key) FROM probe_results", "probes": "SELECT uniqExact(probe_id) FROM probe_results"} {
		value, err := st.query(ctx, query)
		want := 45
		if label == "probes" {
			want = 3
		}
		n, parseErr := strconv.Atoi(value)
		if err != nil || parseErr != nil || n != want {
			t.Fatalf("%s=%s want%d: %v %v", label, value, want, err, parseErr)
		}
		out[label] = n
	}
	first := st.records[key(&pb.ReportRequest{ProbeId: "observer-0000000", BatchId: fmt.Sprintf("%064d", 0)})]
	var req pb.ReportRequest
	proto.Unmarshal(first.Payload, &req)
	if dup, e := st.Commit(ctx, &req); e != nil || !dup {
		t.Fatal("post-recovery duplicate failed", e)
	}
	if os.Getenv("PW_C3_VOLUME") == "full" {
		settle(st, rows)
	}
	for _, table := range []string{"probe_results", "path_quantiles_5m"} {
		if _, e := st.query(ctx, "OPTIMIZE TABLE "+table+" FINAL"); e != nil {
			t.Fatal(e)
		}
	}
	out["recovered"] = true
	out["duplicate"] = true
	save()
}
