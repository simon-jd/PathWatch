package store

import "context"

// MonitoringSnapshot uses the same writer/epoch gate as anomaly reads. It never
// observes pending raw/MV rows and never creates an independent writer session.
func (s *Store) MonitoringSnapshot(ctx context.Context) (records int, bytes int64, err error) {
	err = s.anomalyStable(ctx, func(context.Context) error {
		records, bytes = len(s.records), s.bytes
		return nil
	})
	return
}
