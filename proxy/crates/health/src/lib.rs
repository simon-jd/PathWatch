//! Independent active and passive eligibility gates, driven by monotonic time.
use tokio::time::{Duration, Instant};
pub struct Health {
    active_healthy: bool,
    failures: u32,
    successes: u32,
    passive_failures: u32,
    ejected_until: Option<Instant>,
    unhealthy_threshold: u32,
    healthy_threshold: u32,
}
impl Default for Health {
    fn default() -> Self {
        Self::new(3, 2)
    }
}
impl Health {
    pub fn new(unhealthy_threshold: u32, healthy_threshold: u32) -> Self {
        assert!(unhealthy_threshold > 0 && healthy_threshold > 0);
        Self {
            active_healthy: true,
            failures: 0,
            successes: 0,
            passive_failures: 0,
            ejected_until: None,
            unhealthy_threshold,
            healthy_threshold,
        }
    }
    /// A policy change resets active history only; passive ejection remains intact.
    pub fn reconfigure(&mut self, unhealthy_threshold: u32, healthy_threshold: u32) {
        assert!(unhealthy_threshold > 0 && healthy_threshold > 0);
        self.unhealthy_threshold = unhealthy_threshold;
        self.healthy_threshold = healthy_threshold;
        self.active_healthy = true;
        self.failures = 0;
        self.successes = 0;
    }
    pub fn passive(&mut self, status: u16, now: Instant) {
        if (500..600).contains(&status) {
            self.passive_failures = self.passive_failures.saturating_add(1);
            if self.passive_failures >= 5 {
                self.ejected_until = Some(now + Duration::from_secs(30));
                self.passive_failures = 0;
            }
        } else {
            self.passive_failures = 0;
        }
    }
    pub fn active(&mut self, success: bool) {
        if success {
            self.failures = 0;
            self.successes = self.successes.saturating_add(1);
            if self.successes >= self.healthy_threshold {
                self.active_healthy = true;
            }
        } else {
            self.successes = 0;
            self.failures = self.failures.saturating_add(1);
            if self.failures >= self.unhealthy_threshold {
                self.active_healthy = false;
            }
        }
    }
    pub fn available(&self, now: Instant) -> bool {
        self.active_healthy && self.ejected_until.is_none_or(|until| now >= until)
    }
}
