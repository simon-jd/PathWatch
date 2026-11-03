//! Shared per-name quotas survive routing/TLS generations. No historical strong owners.
use crate::cluster::Node;
use std::{
    collections::HashMap,
    sync::{
        Arc, Mutex, Weak,
        atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering},
    },
};
use tokio::{
    sync::Notify,
    time::{Duration, Instant},
};
#[derive(Default)]
pub struct Registry(Mutex<HashMap<String, Weak<State>>>);
impl Registry {
    pub fn get(&self, name: &str, limits: &config::Circuit) -> Arc<State> {
        let existing = {
            let mut map = self.0.lock().unwrap();
            map.retain(|_, v| v.strong_count() > 0);
            map.get(name).and_then(Weak::upgrade)
        };
        if let Some(state) = existing {
            state.limits(limits);
            return state;
        }
        let state = State::new(limits);
        self.0
            .lock()
            .unwrap()
            .insert(name.to_owned(), Arc::downgrade(&state));
        state
    }
    pub fn snapshot(&self) -> Vec<(String, Arc<State>)> {
        let mut map = self.0.lock().unwrap();
        map.retain(|_, v| v.strong_count() > 0);
        let mut states: Vec<_> = map
            .iter()
            .filter_map(|(k, v)| v.upgrade().map(|s| (k.clone(), s)))
            .collect();
        states.sort_by(|a, b| a.0.cmp(&b.0));
        states
    }
}
struct Budget {
    start: Instant,
    originals: u64,
    retries: u64,
    concurrent: usize,
}
impl Budget {
    fn new() -> Self {
        Self {
            start: Instant::now(),
            originals: 0,
            retries: 0,
            concurrent: 0,
        }
    }
    fn roll(&mut self) {
        let seconds = Instant::now().duration_since(self.start).as_secs();
        if seconds > 0 {
            self.start += Duration::from_secs(seconds);
            self.originals = 0;
            self.retries = 0;
        }
    }
    fn original(&mut self) {
        self.roll();
        self.originals += 1;
    }
    fn retry(&mut self, inflight: usize) -> bool {
        self.roll();
        if self.retries >= 3.max(self.originals / 5) || self.concurrent >= 3.max(inflight / 5) {
            return false;
        }
        self.retries += 1;
        self.concurrent += 1;
        true
    }
}
struct Limits {
    active: usize,
    pending: usize,
}
pub struct State {
    limits: Mutex<Limits>,
    changed: Notify,
    budget: Mutex<Budget>,
    pub active_count: AtomicUsize,
    pub pending_count: AtomicUsize,
    pub originals: AtomicU64,
    pub errors: AtomicU64,
    pub retries: AtomicU64,
    pub rejected: AtomicU64,
    pub nodes: Mutex<HashMap<lb::EndpointId, Weak<Node>>>,
}
impl State {
    fn new(c: &config::Circuit) -> Arc<Self> {
        Arc::new(Self {
            limits: Mutex::new(Limits {
                active: c.max_requests,
                pending: c.max_pending,
            }),
            changed: Notify::new(),
            budget: Mutex::new(Budget::new()),
            active_count: AtomicUsize::new(0),
            pending_count: AtomicUsize::new(0),
            originals: AtomicU64::new(0),
            errors: AtomicU64::new(0),
            retries: AtomicU64::new(0),
            rejected: AtomicU64::new(0),
            nodes: Mutex::new(HashMap::new()),
        })
    }
    fn limits(&self, c: &config::Circuit) {
        *self.limits.lock().unwrap() = Limits {
            active: c.max_requests,
            pending: c.max_pending,
        };
        self.changed.notify_waiters();
    }
    fn original(&self) {
        self.originals.fetch_add(1, Ordering::Relaxed);
        self.budget.lock().unwrap().original();
    }
    fn active(self: &Arc<Self>) -> Arc<Original> {
        self.active_count.fetch_add(1, Ordering::SeqCst);
        Arc::new(Original {
            state: self.clone(),
            failed: AtomicBool::new(false),
        })
    }
    pub async fn admit(self: &Arc<Self>) -> Option<Arc<Original>> {
        let mut pending: Option<Pending> = None;
        loop {
            // Register before checking the gate, preventing a lost release notification.
            let notified = self.changed.notified();
            tokio::pin!(notified);
            notified.as_mut().enable();
            {
                let limits = self.limits.lock().unwrap();
                if self.active_count.load(Ordering::SeqCst) < limits.active {
                    if let Some(mut p) = pending.take() {
                        self.pending_count.fetch_sub(1, Ordering::SeqCst);
                        p.counted = false;
                    } else {
                        self.original();
                    }
                    return Some(self.active());
                }
                if pending.is_none() {
                    if self.pending_count.load(Ordering::SeqCst) >= limits.pending {
                        return None;
                    }
                    self.pending_count.fetch_add(1, Ordering::SeqCst);
                    self.original();
                    pending = Some(Pending {
                        state: self.clone(),
                        counted: true,
                    });
                }
            }
            notified.await;
        }
    }
    pub fn retry(self: &Arc<Self>) -> Option<Retry> {
        let _gate = self.limits.lock().unwrap();
        let inflight =
            self.active_count.load(Ordering::SeqCst) + self.pending_count.load(Ordering::SeqCst);
        self.budget
            .lock()
            .unwrap()
            .retry(inflight)
            .then(|| Retry(self.clone()))
    }
    pub fn metrics(&self, name: &str, out: &mut String) {
        use std::fmt::Write;
        for (metric, value) in [
            ("requests_total", self.originals.load(Ordering::Relaxed)),
            ("errors_total", self.errors.load(Ordering::Relaxed)),
            ("retries_total", self.retries.load(Ordering::Relaxed)),
            ("rejected_total", self.rejected.load(Ordering::Relaxed)),
            (
                "active_requests",
                self.active_count.load(Ordering::Relaxed) as u64,
            ),
            (
                "pending_requests",
                self.pending_count.load(Ordering::Relaxed) as u64,
            ),
        ] {
            let _ = writeln!(
                out,
                "pathwatch_proxy_{metric}{{cluster=\"{name}\"}} {value}"
            );
        }
    }
}
struct Pending {
    state: Arc<State>,
    counted: bool,
}
impl Drop for Pending {
    fn drop(&mut self) {
        if self.counted {
            let _gate = self.state.limits.lock().unwrap();
            self.state.pending_count.fetch_sub(1, Ordering::SeqCst);
        }
    }
}
pub struct Original {
    state: Arc<State>,
    failed: AtomicBool,
}
impl Original {
    pub fn error(&self) {
        if !self.failed.swap(true, Ordering::Relaxed) {
            self.state.errors.fetch_add(1, Ordering::Relaxed);
        }
    }
}
impl Drop for Original {
    fn drop(&mut self) {
        let _gate = self.state.limits.lock().unwrap();
        self.state.active_count.fetch_sub(1, Ordering::SeqCst);
        self.state.changed.notify_waiters();
    }
}
pub struct Retry(Arc<State>);
impl Drop for Retry {
    fn drop(&mut self) {
        self.0.budget.lock().unwrap().concurrent -= 1;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test(start_paused = true)]
    async fn retry_budget_caps_retries() {
        let mut b = Budget::new();
        for _ in 0..1000 {
            b.original();
        }
        let retries = (0..1000)
            .filter(|_| {
                let ok = b.retry(1000);
                if ok {
                    b.concurrent -= 1;
                }
                ok
            })
            .count();
        assert!(retries > 0 && retries <= 200, "retries={retries}/1000");
        assert_eq!(b.originals, 1000);
    }
    #[tokio::test(start_paused = true)]
    async fn retry_floor_rollover_and_staggered_arrivals() {
        let mut b = Budget::new();
        for _ in 0..3 {
            b.original();
            assert!(b.retry(1));
            b.concurrent -= 1;
        }
        b.original();
        assert!(!b.retry(1));
        tokio::time::advance(std::time::Duration::from_secs(1)).await;
        b.original();
        assert!(b.retry(1));
        b.concurrent -= 1;
        assert_eq!(b.originals, 1);
        for _ in 0..29 {
            b.original();
            let ok = b.retry(3);
            if ok {
                b.concurrent -= 1;
            }
        }
        assert!(b.retries <= 6);
        // Outstanding retry ownership survives bucket reset and still gates concurrency.
        let mut b = Budget::new();
        for _ in 0..100 {
            b.original();
        }
        for _ in 0..3 {
            assert!(b.retry(3));
        }
        assert!(!b.retry(3));
        tokio::time::advance(std::time::Duration::from_secs(1)).await;
        b.original();
        assert!(!b.retry(1));
        b.concurrent -= 1;
        assert!(b.retry(1));
    }
    #[tokio::test]
    async fn cancelled_queue_and_retry_owners_release_without_minting_credit() {
        let cfg = config::Config::parse(
            r#"listeners=[{addr="127.0.0.1:8000"}]
routes=[]
clusters=[{name="test",endpoints=["http://127.0.0.1:9000"],circuit={max_requests=1,max_pending=1}}]
"#,
        )
        .unwrap();
        let c = State::new(&cfg.clusters[0].circuit);
        let active = c.admit().await.unwrap();
        let waiting = c.clone();
        let waiting = tokio::spawn(async move { waiting.admit().await });
        tokio::task::yield_now().await;
        assert_eq!(c.pending_count.load(Ordering::SeqCst), 1);
        assert!(c.admit().await.is_none());
        assert_eq!(c.originals.load(Ordering::Relaxed), 2);
        waiting.abort();
        let _ = waiting.await;
        assert_eq!(c.pending_count.load(Ordering::SeqCst), 0);
        let retry = c.retry().unwrap();
        assert_eq!(c.budget.lock().unwrap().concurrent, 1);
        drop(retry);
        assert_eq!(c.budget.lock().unwrap().concurrent, 0);
        assert_eq!(c.originals.load(Ordering::Relaxed), 2);
        drop(active);
        assert_eq!(c.active_count.load(Ordering::SeqCst), 0);
        let next = c.admit().await.unwrap();
        assert_eq!(c.originals.load(Ordering::Relaxed), 3);
        drop(next);
    }
    #[tokio::test]
    async fn registry_tracks_live_exchanges_and_prunes_retired_names() {
        let registry = Registry::default();
        let limits = config::Circuit::default();
        let state = registry.get("removed", &limits);
        let exchange = state.admit().await.unwrap();
        drop(state);
        assert_eq!(registry.snapshot().len(), 1);
        drop(exchange);
        assert!(registry.snapshot().is_empty());
        for i in 0..100 {
            drop(registry.get(&format!("retired{i}"), &limits));
        }
        assert!(registry.snapshot().is_empty());
    }
}
