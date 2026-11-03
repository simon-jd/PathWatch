package anomaly

import (
	"net/netip"
	"time"
)

type Hop struct {
	TTL  int
	Addr string
	At   time.Time
}
type PathChange struct {
	Changed           bool
	FirstDivergentTTL int
	Valid             bool
	Reason            string
}

// Correlate compares known routers at matching TTLs. Before must precede the
// alarm window; after must be in that five-minute window. Missing replies and
// truncated tails are not evidence of a router change. Sparse/duplicate TTLs,
// inconsistent snapshot timestamps and address families invalidate correlation.
func Correlate(alarmAt time.Time, before, after []Hop) PathChange {
	invalid := PathChange{Reason: "missing or invalid trace snapshot"}
	maps := []map[int]netip.Addr{}
	family := 0
	for i, hops := range [][]Hop{before, after} {
		if len(hops) == 0 {
			return invalid
		}
		m := map[int]netip.Addr{}
		at := hops[0].At
		if at.IsZero() || (i == 0 && !at.Before(alarmAt)) || (i == 1 && (at.Before(alarmAt) || !at.Before(alarmAt.Add(5*time.Minute)))) {
			return invalid
		}
		for _, h := range hops {
			if h.TTL < 1 || h.TTL > 255 || !h.At.Equal(at) {
				return invalid
			}
			if _, ok := m[h.TTL]; ok {
				return invalid
			}
			var addr netip.Addr
			if h.Addr != "" && h.Addr != "*" {
				var e error
				addr, e = netip.ParseAddr(h.Addr)
				if e != nil {
					return invalid
				}
				addr = addr.Unmap()
				f := addr.BitLen()
				if family != 0 && family != f {
					return invalid
				}
				family = f
			}
			m[h.TTL] = addr
		}
		for ttl := 1; ttl <= len(m); ttl++ {
			if _, ok := m[ttl]; !ok {
				return invalid
			}
		}
		maps = append(maps, m)
	}
	result := PathChange{Valid: true}
	for ttl := 1; ttl <= len(maps[0]) && ttl <= len(maps[1]); ttl++ {
		b, a := maps[0][ttl], maps[1][ttl]
		if b.IsValid() && a.IsValid() && b != a {
			result.Changed = true
			result.FirstDivergentTTL = ttl
			return result
		}
	}
	return result
}
