use lb::{Picker, least_request::LeastRequest, maglev::Maglev};
use std::sync::{Arc, atomic::AtomicUsize};
#[test]
fn least_request_prefers_idle() {
    let p = LeastRequest::new(&[
        (1, Arc::new(AtomicUsize::new(50))),
        (2, Arc::new(AtomicUsize::new(0))),
    ]);
    let idle = (0..1000).filter(|_| p.pick(None) == Some(2)).count();
    assert!(idle >= 950, "idle={idle}/1000");
}
#[test]
fn maglev_minimal_disruption() {
    // Declared before algorithm: all integer keys 0..1,000,000, IDs 1..=10; remove ID5.
    let a = Maglev::new(&(1..=10).collect::<Vec<_>>());
    let b = Maglev::new(&(1..=10).filter(|id| *id != 5).collect::<Vec<_>>());
    let changed = (0..1_000_000)
        .filter(|k| a.pick(Some(*k)) != b.pick(Some(*k)))
        .count();
    println!("fixed million-key removal churn={changed}/1000000");
    assert!(changed <= 120_000);
}
#[test]
fn empty_and_reordered_sets() {
    assert_eq!(Maglev::new(&[]).pick(None), None);
    assert_eq!(LeastRequest::new(&[]).pick(None), None);
    let a = Maglev::new(&[1, 2, 3]);
    let b = Maglev::new(&[3, 1, 2]);
    for k in 0..10000 {
        assert_eq!(a.pick(Some(k)), b.pick(Some(k)));
    }
}
