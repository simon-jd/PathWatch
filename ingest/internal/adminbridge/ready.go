package adminbridge

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"strconv"
	"unicode/utf8"
)

type readiness struct {
	Windows        *map[string][]string `json:"windows"`
	JournalRecords *int                 `json:"journal_records"`
	JournalBytes   *int64               `json:"journal_bytes"`
}

func validCoreReady(body []byte) bool {
	var r readiness
	d := json.NewDecoder(bytes.NewReader(body))
	d.DisallowUnknownFields()
	if d.Decode(&r) != nil || r.Windows == nil || r.JournalRecords == nil || r.JournalBytes == nil || *r.JournalRecords < 0 || *r.JournalBytes < 0 {
		return false
	}
	for window, ids := range *r.Windows {
		n, e := strconv.ParseInt(window, 10, 64)
		if e != nil || strconv.FormatInt(n, 10) != window || n%300 != 0 || len(ids) == 0 {
			return false
		}
		seen := map[string]bool{}
		for _, id := range ids {
			if id == "" || !utf8.ValidString(id) || seen[id] {
				return false
			}
			seen[id] = true
		}
	}
	var canonical bytes.Buffer
	if json.NewEncoder(&canonical).Encode(r) != nil {
		return false
	}
	return bytes.Equal(canonical.Bytes(), body)
}

// CheckReady recognizes only the explicitly chosen, complete readiness contract.
func CheckReady(ctx context.Context, address, format string) error {
	if e := canonicalEndpoint(address, true); e != nil {
		return e
	}
	limit := len("ready\n")
	switch format {
	case "frontend":
	case "core":
		limit = 1 << 20
	default:
		return errors.New("unsupported readiness format")
	}
	ctx, cancel := context.WithTimeout(ctx, requestLimit)
	defer cancel()
	tr := transport()
	defer tr.CloseIdleConnections()
	c := &http.Client{Transport: tr, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	r, body, e := fetch(ctx, c, address, "/readyz", limit)
	if e != nil {
		return e
	}
	if ctx.Err() != nil {
		return ctx.Err()
	}
	if r.StatusCode != 200 {
		return errors.New("not ready: unexpected status")
	}
	if format == "frontend" && string(body) != "ready\n" || format == "core" && !validCoreReady(body) {
		return errors.New("not ready: unexpected body")
	}
	return nil
}
