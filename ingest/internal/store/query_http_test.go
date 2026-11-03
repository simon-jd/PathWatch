package store_test

import (
	"context"
	"google.golang.org/protobuf/types/known/timestamppb"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"pathwatch.local/pathwatch/ingest/internal/queryapi"
	"pathwatch.local/pathwatch/ingest/internal/store"
	pb "pathwatch.local/pathwatch/proto/pathwatch/v1"
	"strings"
	"testing"
	"time"
)

type slowResponse struct {
	*httptest.ResponseRecorder
	writing, release chan struct{}
}

func (w slowResponse) Write(b []byte) (int, error) {
	close(w.writing)
	<-w.release
	return w.ResponseRecorder.Write(b)
}
func TestQueryHTTPSlowClient(t *testing.T) {
	endpoint := os.Getenv("PATHWATCH_QUERY_TEST_URL")
	if endpoint == "" {
		t.Skip("owned engine fixture not requested")
	}
	label, e := exec.Command("docker", "inspect", "--format", `{{index .Config.Labels "purpose"}}`, os.Getenv("PATHWATCH_QUERY_TEST_CONTAINER")).Output()
	if e != nil || strings.TrimSpace(string(label)) != "task14-query-api" {
		t.Fatal("requires owned container")
	}
	client := &http.Client{Transport: &http.Transport{Proxy: nil}, Timeout: 5 * time.Second}
	resp, e := client.Post(endpoint, "text/plain", strings.NewReader("CREATE DATABASE queryslow ENGINE=Atomic"))
	if e != nil {
		t.Fatal(e)
	}
	body, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if resp.StatusCode != 200 {
		t.Fatal(string(body))
	}
	st, e := store.Open(context.Background(), store.Config{URL: endpoint, Database: "queryslow", Username: "default", JournalDir: filepath.Join(os.Getenv("PATHWATCH_QUERY_TEST_WORK"), "slow-journal"), MaxJournalBytes: 1 << 20})
	if e != nil {
		t.Fatal(e)
	}
	defer st.Close()
	at := time.Now().UTC().Truncate(5 * time.Minute).Add(-10 * time.Minute)
	v := url.Values{"path_key": {"p→t/tcp/v4"}, "from": {at.Format(time.RFC3339Nano)}, "to": {at.Add(5 * time.Minute).Format(time.RFC3339Nano)}}
	w := slowResponse{httptest.NewRecorder(), make(chan struct{}), make(chan struct{})}
	done := make(chan struct{})
	go func() {
		queryapi.New(st).ServeHTTP(w, httptest.NewRequest("GET", "/v1/measurements/raw?"+v.Encode(), nil))
		close(done)
	}()
	defer func() { close(w.release); <-done }()
	select {
	case <-w.writing:
	case <-time.After(3 * time.Second):
		t.Fatal("response did not reach slow writer")
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	_, e = st.Commit(ctx, &pb.ReportRequest{ProbeId: "p", BatchId: "during-response", SentAt: timestamppb.Now(), Results: []*pb.ProbeResult{{ProbeId: "p", TargetId: "t", Kind: "tcp", Family: "v4", Addr: "127.0.0.1", StartedAt: timestamppb.New(at), PhasesNanoseconds: map[string]int64{"connect": 1000}}}})
	if e != nil {
		t.Fatalf("slow response blocked same Store commit: %v", e)
	}
	t.Log("actual Store Commit completed while HTTP response writer remained blocked")
}
