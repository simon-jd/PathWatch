mod common;
#[path = "support/retry.rs"]
mod fixture;
use common::{Body, Proxy, address, config, h1, h2};
use fixture::{Backend, Observed};
use http_body_util::{BodyExt, Full};
use hyper::{Request, Response, body::Incoming};
use std::{
    future::Future,
    net::SocketAddr,
    sync::{Arc, atomic::Ordering},
    time::Duration,
};

fn get(path: &str) -> Request<Body> {
    Request::builder()
        .uri(path)
        .header("host", "test.local")
        .body(Full::new(bytes::Bytes::new()))
        .unwrap()
}
async fn bounded<T>(future: impl Future<Output = T>) -> T {
    tokio::select! {
        result = future => result,
        _ = async {
            let start = std::time::Instant::now();
            while start.elapsed() < Duration::from_secs(5) { tokio::task::yield_now().await; }
        } => panic!("fixture exceeded five wall-clock seconds"),
    }
}
async fn scrape(addr: SocketAddr) -> String {
    bounded(async {
        let (mut tx, driver) = h1(addr).await;
        let response = tx.send_request(get("/metrics")).await.unwrap();
        assert_eq!(response.status(), 200);
        let text = response.into_body().collect().await.unwrap().to_bytes();
        drop(tx);
        driver.abort();
        let _ = driver.await;
        String::from_utf8(text.to_vec()).unwrap()
    })
    .await
}
fn value(text: &str, name: &str) -> usize {
    let label = format!("pathwatch_proxy_{name}{{cluster=\"echo\"}} ");
    text.lines()
        .find_map(|line| line.strip_prefix(&label))
        .unwrap()
        .parse()
        .unwrap()
}
async fn gauges(addr: SocketAddr, active: usize, pending: usize) -> String {
    bounded(async {
        loop {
            let text = scrape(addr).await;
            if value(&text, "active_requests") == active
                && value(&text, "pending_requests") == pending
            {
                return text;
            }
            tokio::task::yield_now().await;
        }
    })
    .await
}
async fn complete(response: Response<Incoming>, status: u16) {
    assert_eq!(response.status(), status);
    bounded(response.into_body().collect()).await.unwrap();
}
async fn closed(observed: &Observed, count: usize) {
    bounded(async {
        while observed.closed.load(Ordering::SeqCst) < count {
            tokio::task::yield_now().await;
        }
    })
    .await;
}
fn settings(upstream: SocketAddr, management: SocketAddr, active: usize) -> config::Config {
    let mut cfg = config(upstream);
    cfg.management = Some(config::Management { addr: management });
    cfg.pool.max_connections = 4;
    cfg.pool.request_timeout_ms = 60_000;
    cfg.clusters[0].circuit.max_requests = active;
    cfg.clusters[0].circuit.max_pending = 1;
    cfg
}

#[tokio::test]
async fn retry_bodies_survive_rollover_and_cancel_releases_real_capacity() {
    let observed = Arc::new(Observed::default());
    let backend = Backend::start(observed.clone()).await;
    let management = address();
    let proxy = Proxy::start(settings(backend.addr, management, 4)).await;
    let (mut tx, driver) = h2(proxy.addr).await;
    // Network fixture is ready before pausing. bounded() stays runnable while
    // awaiting network IO, so Tokio cannot auto-advance to the 60-second deadline.
    tokio::time::pause();
    let start = tokio::time::Instant::now();
    for _ in 0..100 {
        complete(bounded(tx.send_request(get("/ok"))).await.unwrap(), 200).await;
    }
    let mut held = Vec::new();
    for i in 0..3 {
        let response = bounded(tx.send_request(get(&format!("/hold{i}"))))
            .await
            .unwrap();
        assert_eq!(response.status(), 200);
        held.push(response);
    }
    let text = gauges(management, 3, 0).await;
    assert_eq!(value(&text, "requests_total"), 103);
    assert_eq!(value(&text, "retries_total"), 3);
    assert_eq!(observed.attempts(), 106);
    assert_eq!(
        tokio::time::Instant::now(),
        start,
        "no implicit clock advance"
    );
    tokio::time::advance(Duration::from_millis(1001)).await;

    // Old bucket had >20 retry credits. New bucket has only its three floor
    // reservations; old held leases still occupy all three concurrency slots.
    complete(
        bounded(tx.send_request(get("/blocked"))).await.unwrap(),
        503,
    )
    .await;
    assert_eq!(
        observed.calls("/blocked"),
        1,
        "rollover must not reset held concurrency"
    );
    assert_eq!(value(&gauges(management, 3, 0).await, "retries_total"), 3);

    drop(held.remove(0)); // Actual h2 downstream response drop sends RST_STREAM.
    let text = gauges(management, 2, 0).await;
    assert_eq!(
        value(&text, "requests_total"),
        104,
        "cancel adds no original credit"
    );
    closed(&observed, 1).await;
    let replacement = bounded(tx.send_request(get("/hold-replacement")))
        .await
        .unwrap();
    assert_eq!(replacement.status(), 200);
    assert_eq!(
        observed.calls("/hold-replacement"),
        2,
        "exactly one freed retry lease is reusable"
    );
    assert_eq!(value(&gauges(management, 3, 0).await, "retries_total"), 4);
    drop(held);
    gauges(management, 1, 0).await;
    closed(&observed, 3).await;
    for path in ["/short1", "/short2"] {
        complete(bounded(tx.send_request(get(path))).await.unwrap(), 200).await;
        assert_eq!(observed.calls(path), 2);
    }
    complete(bounded(tx.send_request(get("/short3"))).await.unwrap(), 503).await;
    assert_eq!(
        observed.calls("/short3"),
        1,
        "old original credit must not carry into new bucket"
    );
    let text = gauges(management, 1, 0).await;
    assert_eq!(value(&text, "requests_total"), 108);
    assert_eq!(value(&text, "retries_total"), 6);
    assert_eq!(observed.attempts(), 114);
    assert_eq!(
        tokio::time::Instant::now() - start,
        Duration::from_millis(1001)
    );
    drop(replacement);
    gauges(management, 0, 0).await;
    closed(&observed, 4).await;
    assert!(observed.peak.load(Ordering::SeqCst) <= 4);
    println!(
        "real rollover: originals=108 attempts=114 retries=6; old held concurrency blocks fourth; new bucket permits exactly3; downstream drops close4 held sockets; physical peak={}",
        observed.peak.load(Ordering::SeqCst)
    );
    tokio::time::resume();
    drop(tx);
    driver.abort();
    let _ = driver.await;
    proxy.close().await;
    closed(&observed, observed.accepted.load(Ordering::SeqCst)).await;
    assert_eq!(observed.live.load(Ordering::SeqCst), 0);
    backend.close().await;
}

#[tokio::test]
async fn downstream_pending_and_retry_cancellation_release_all_real_leases() {
    let observed = Arc::new(Observed::default());
    let a = Backend::start(observed.clone()).await;
    let b = Backend::start(observed.clone()).await;
    let management = address();
    let mut cfg = settings(a.addr, management, 2);
    cfg.clusters[0].endpoints.push(format!("http://{}", b.addr));
    let proxy = Proxy::start(cfg).await;
    let (mut tx, driver) = h2(proxy.addr).await;
    tokio::time::pause();
    let first = bounded(tx.send_request(get("/hold-first"))).await.unwrap();
    let second = bounded(tx.send_request(get("/hold-second"))).await.unwrap();
    assert_eq!(first.status(), 200);
    assert_eq!(second.status(), 200);
    let freed = first.headers()["x-backend"].clone();
    assert_ne!(
        freed,
        second.headers()["x-backend"],
        "P2C holds one retry per endpoint"
    );
    assert_eq!(value(&gauges(management, 2, 0).await, "retries_total"), 2);
    for originals in [3, 4] {
        let mut waiting = tx.clone();
        let pending = tokio::spawn(async move { waiting.send_request(get("/pending")).await });
        let text = gauges(management, 2, 1).await;
        assert_eq!(value(&text, "requests_total"), originals);
        pending.abort();
        let _ = pending.await; // Drop actual h2 response future.
        let text = gauges(management, 2, 0).await;
        assert_eq!(value(&text, "requests_total"), originals);
        assert_eq!(value(&text, "retries_total"), 2);
        assert_eq!(
            observed.calls("/pending"),
            0,
            "canceled pending request must never reach upstream"
        );
    }
    drop(first);
    gauges(management, 1, 0).await;
    closed(&observed, 1).await;
    // Ten real selections must see the released endpoint as idle while the other
    // response is held. This observes endpoint RAII, not a private counter.
    for _ in 0..10 {
        let response = bounded(tx.send_request(get("/ok-after-cancel")))
            .await
            .unwrap();
        assert_eq!(
            response.headers()["x-backend"],
            freed,
            "canceled retry endpoint must be idle"
        );
        complete(response, 200).await;
    }
    let replacement = bounded(tx.send_request(get("/hold-new"))).await.unwrap();
    assert_eq!(replacement.status(), 200);
    assert_eq!(replacement.headers()["x-backend"], freed);
    assert_eq!(observed.calls("/hold-new"), 2);
    let text = gauges(management, 2, 0).await;
    assert_eq!(value(&text, "requests_total"), 15);
    assert_eq!(value(&text, "retries_total"), 3);
    assert_eq!(observed.attempts(), 16);
    drop(second);
    drop(replacement);
    gauges(management, 0, 0).await;
    closed(&observed, 3).await;
    println!(
        "real cancellation:2 accepted pending canceled,0 forwarded; originals=15 attempts=16 retries=3; P2C10/10 selects released endpoint; all gauges zero"
    );
    tokio::time::resume();
    drop(tx);
    driver.abort();
    let _ = driver.await;
    proxy.close().await;
    closed(&observed, observed.accepted.load(Ordering::SeqCst)).await;
    assert_eq!(observed.live.load(Ordering::SeqCst), 0);
    assert!(observed.peak.load(Ordering::SeqCst) <= 4);
    a.close().await;
    b.close().await;
}
