use crate::{EndpointId, Picker, maglev::mix};
use std::sync::{
    Arc,
    atomic::{AtomicU64, AtomicUsize, Ordering},
};
pub struct LeastRequest {
    endpoints: Vec<(EndpointId, Arc<AtomicUsize>)>,
    sequence: AtomicU64,
}
impl LeastRequest {
    pub fn new(endpoints: &[(EndpointId, Arc<AtomicUsize>)]) -> Self {
        Self {
            endpoints: endpoints.to_vec(),
            sequence: AtomicU64::new(0),
        }
    }
}
impl Picker for LeastRequest {
    fn pick(&self, key: Option<u64>) -> Option<EndpointId> {
        let n = self.endpoints.len();
        if n == 0 {
            return None;
        }
        let r = mix(key
            .unwrap_or_else(|| self.sequence.fetch_add(1, Ordering::Relaxed))
            .wrapping_add(0x9e3779b97f4a7c15));
        let a = r as usize % n;
        if n == 1 {
            return Some(self.endpoints[a].0);
        }
        // Distinct choices: never compare a busy endpoint to itself.
        let b = (a + 1 + (mix(r) as usize % (n - 1))) % n;
        let chosen = if self.endpoints[a].1.load(Ordering::Acquire)
            <= self.endpoints[b].1.load(Ordering::Acquire)
        {
            a
        } else {
            b
        };
        Some(self.endpoints[chosen].0)
    }
}
