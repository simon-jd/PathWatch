use health::Health;
use tokio::time::{Duration, Instant};
#[tokio::test(start_paused = true)]
async fn outlier_ejection_and_recovery() {
    let mut h = Health::default();
    for _ in 0..4 {
        h.passive(503, Instant::now());
    }
    assert!(h.available(Instant::now()));
    h.passive(503, Instant::now());
    assert!(!h.available(Instant::now()));
    tokio::time::advance(Duration::from_millis(29999)).await;
    assert!(!h.available(Instant::now()));
    tokio::time::advance(Duration::from_millis(1)).await;
    assert!(h.available(Instant::now()));
}
#[test]
fn active_thresholds_and_independent_passive_deadline() {
    let mut h = Health::default();
    let now = Instant::now();
    h.active(false);
    h.active(false);
    assert!(h.available(now));
    h.active(false);
    assert!(!h.available(now));
    h.active(true);
    assert!(!h.available(now));
    h.active(true);
    assert!(h.available(now));
    for _ in 0..5 {
        h.passive(500, now);
    }
    h.active(true);
    h.active(true);
    assert!(!h.available(now));
}
#[test]
fn success_resets_consecutive_failures() {
    let mut h = Health::default();
    let now = Instant::now();
    for _ in 0..4 {
        h.passive(500, now);
    }
    h.passive(200, now);
    h.passive(500, now);
    assert!(h.available(now));
}
#[test]
fn policy_change_preserves_passive_ejection() {
    let mut h = Health::default();
    let now = Instant::now();
    for _ in 0..5 {
        h.passive(503, now);
    }
    h.reconfigure(4, 3);
    for _ in 0..3 {
        h.active(true);
    }
    assert!(!h.available(now + Duration::from_secs(29)));
    assert!(h.available(now + Duration::from_secs(30)));
}
