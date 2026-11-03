mod common;
mod support;
use bytes::Bytes;
use common::*;
use http_body_util::{BodyExt, Full};
use hyper::Request;
use std::{sync::atomic::Ordering, time::Duration};
use support::*;
fn get() -> Request<Body> {
    Request::builder()
        .uri("/")
        .header("host", "test.local")
        .body(Full::new(Bytes::new()))
        .unwrap()
}
#[tokio::test]
async fn healthy_endpoints_are_selected_in_real_datapath() {
    let a = Backend::start(Duration::ZERO, 200).await;
    let b = Backend::start(Duration::ZERO, 200).await;
    let mut cfg = config(a.addr);
    cfg.clusters[0].endpoints.push(format!("http://{}", b.addr));
    let p = Proxy::start(cfg).await;
    for _ in 0..40 {
        let (mut tx, t) = h1(p.addr).await;
        let r = tx.send_request(get()).await.unwrap();
        assert_eq!(r.status(), 200);
        r.into_body().collect().await.unwrap();
        drop(tx);
        t.abort();
        let _ = t.await;
    }
    assert!(a.calls.load(Ordering::SeqCst) > 0 && b.calls.load(Ordering::SeqCst) > 0);
    p.close().await;
    a.close().await;
    b.close().await;
}
#[tokio::test]
async fn five_5xx_eject_endpoint_in_real_datapath() {
    let a = Backend::start(Duration::ZERO, 500).await;
    let p = Proxy::start(config(a.addr)).await;
    for _ in 0..6 {
        let (mut tx, t) = h1(p.addr).await;
        tx.send_request(request("/"))
            .await
            .unwrap()
            .into_body()
            .collect()
            .await
            .unwrap();
        drop(tx);
        t.abort();
        let _ = t.await;
    }
    assert_eq!(a.calls.load(Ordering::SeqCst), 5);
    p.close().await;
    a.close().await;
}
#[tokio::test]
async fn empty_get_transport_failure_is_retried_but_post_is_not() {
    // Owned listener drops each accepted socket, counting real physical attempts.
    let l = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = l.local_addr().unwrap();
    let count = std::sync::Arc::new(std::sync::atomic::AtomicUsize::new(0));
    let c = count.clone();
    let server = tokio::spawn(async move {
        loop {
            let (s, _) = l.accept().await.unwrap();
            c.fetch_add(1, Ordering::SeqCst);
            drop(s);
        }
    });
    let p = Proxy::start(config(addr)).await;
    let (mut tx, t) = h1(p.addr).await;
    assert_eq!(tx.send_request(get()).await.unwrap().status(), 502);
    assert_eq!(count.load(Ordering::SeqCst), 2);
    assert_eq!(tx.send_request(request("/")).await.unwrap().status(), 502);
    assert_eq!(count.load(Ordering::SeqCst), 3);
    drop(tx);
    t.abort();
    let _ = t.await;
    p.close().await;
    server.abort();
    let _ = server.await;
}
async fn metrics(addr: std::net::SocketAddr) -> String {
    let (mut tx, t) = h1(addr).await;
    let r = tx
        .send_request(
            Request::builder()
                .uri("/metrics")
                .body(Full::new(Bytes::new()))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(r.status(), 200);
    let text =
        String::from_utf8(r.into_body().collect().await.unwrap().to_bytes().to_vec()).unwrap();
    drop(tx);
    t.abort();
    let _ = t.await;
    text
}
#[tokio::test]
async fn active_probe_thresholds_control_real_selection() {
    let a = Backend::start(Duration::ZERO, 200).await;
    a.health_ok.store(false, Ordering::SeqCst);
    let mut cfg = config(a.addr);
    cfg.clusters[0].health = Some(config::Health {
        interval_ms: 30,
        timeout_ms: 20,
        ..Default::default()
    });
    let p = Proxy::start(cfg).await;
    until(|| a.probes.load(Ordering::SeqCst) >= 3).await;
    tokio::time::sleep(Duration::from_millis(5)).await;
    let (mut tx, t) = h1(p.addr).await;
    assert_eq!(tx.send_request(get()).await.unwrap().status(), 503);
    assert_eq!(a.calls.load(Ordering::SeqCst), 0);
    let probes = a.probes.load(Ordering::SeqCst);
    a.health_ok.store(true, Ordering::SeqCst);
    until(|| a.probes.load(Ordering::SeqCst) >= probes + 2).await;
    tokio::time::sleep(Duration::from_millis(5)).await;
    assert_eq!(tx.send_request(get()).await.unwrap().status(), 200);
    drop(tx);
    t.abort();
    let _ = t.await;
    p.close().await;
    a.close().await;
}
#[tokio::test]
async fn circuit_bounds_pending_and_original_credit_and_timeout() {
    let a = Backend::start(Duration::from_millis(500), 200).await;
    let mut cfg = config(a.addr);
    cfg.clusters[0].circuit = config::Circuit {
        max_requests: 1,
        max_pending: 1,
        request_timeout_ms: Some(150),
    };
    let m = address();
    cfg.management = Some(config::Management { addr: m });
    let p = Proxy::start(cfg).await;
    let (tx, t) = h2(p.addr).await;
    let mut first = tx.clone();
    let one = tokio::spawn(async move { first.send_request(get()).await.unwrap().status() });
    until(|| a.active.load(Ordering::SeqCst) == 1).await;
    let mut second = tx.clone();
    let two = tokio::spawn(async move { second.send_request(get()).await.unwrap().status() });
    tokio::time::sleep(Duration::from_millis(10)).await;
    let mut third = tx.clone();
    assert_eq!(third.send_request(get()).await.unwrap().status(), 503);
    let text = metrics(m).await;
    assert!(text.contains("active_requests{cluster=\"echo\"} 1"));
    assert!(text.contains("pending_requests{cluster=\"echo\"} 1"));
    assert!(text.contains("requests_total{cluster=\"echo\"} 2"));
    assert_eq!(one.await.unwrap(), 504);
    assert_eq!(two.await.unwrap(), 504);
    let text = metrics(m).await;
    assert!(text.contains("active_requests{cluster=\"echo\"} 0"));
    assert!(text.contains("pending_requests{cluster=\"echo\"} 0"));
    drop(third);
    drop(tx);
    t.abort();
    let _ = t.await;
    p.close().await;
    a.close().await;
}
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn retry_budget_caps_real_all_fail_attempts() {
    let l = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = l.local_addr().unwrap();
    let count = std::sync::Arc::new(std::sync::atomic::AtomicUsize::new(0));
    let c = count.clone();
    let server = tokio::spawn(async move {
        loop {
            let (s, _) = l.accept().await.unwrap();
            c.fetch_add(1, Ordering::SeqCst);
            drop(s);
        }
    });
    let mut cfg = config(addr);
    cfg.pool.max_connections = 32;
    cfg.clusters[0].circuit.max_requests = 300;
    cfg.clusters[0].circuit.max_pending = 0;
    let m = address();
    cfg.management = Some(config::Management { addr: m });
    let p = Proxy::start(cfg).await;
    let start = std::time::Instant::now();
    let mut tasks = tokio::task::JoinSet::new();
    // Three h2 connections, 100 streams each; originals arrive while prior attempts fail.
    let mut drivers = Vec::new();
    for _ in 0..3 {
        let (tx, t) = h2(p.addr).await;
        drivers.push(t);
        for _ in 0..100 {
            let mut tx = tx.clone();
            tasks.spawn(async move {
                let r = tx.send_request(get()).await.unwrap();
                assert_eq!(r.status(), 502);
                r.into_body().collect().await.unwrap();
            });
        }
    }
    while let Some(t) = tasks.join_next().await {
        t.unwrap();
    }
    let attempts = count.load(Ordering::SeqCst);
    let elapsed = start.elapsed();
    assert!(
        elapsed < Duration::from_millis(900),
        "all-fail bucket fixture took {elapsed:?}"
    );
    assert!(
        attempts > 300 && attempts <= 360,
        "attempts={attempts},originals=300"
    );
    let text = metrics(m).await;
    assert!(text.contains("requests_total{cluster=\"echo\"} 300"));
    assert!(text.contains(&format!(
        "retries_total{{cluster=\"echo\"}} {}",
        attempts - 300
    )));
    println!(
        "actual all-fail originals=300 attempts={attempts} retry_ratio={} elapsed={elapsed:?}",
        (attempts - 300) as f64 / 300.0
    );
    for t in drivers {
        t.abort();
        let _ = t.await;
    }
    p.close().await;
    server.abort();
    let _ = server.await;
}
#[tokio::test]
async fn response_body_reserves_endpoint_and_circuit_until_deadline() {
    let a = Upstream::start().await;
    let b = Upstream::start().await;
    let mut cfg = config(a.addr);
    cfg.clusters[0].endpoints.push(format!("http://{}", b.addr));
    cfg.clusters[0].circuit.max_requests = 2;
    cfg.clusters[0].circuit.max_pending = 0;
    cfg.pool.request_timeout_ms = 200;
    let m = address();
    cfg.management = Some(config::Management { addr: m });
    let p = Proxy::start(cfg).await;
    let (mut tx, t) = h2(p.addr).await;
    let held = tx.send_request(request("/large-response")).await.unwrap();
    let chosen = held.headers()["x-host"].clone();
    let second = tx.send_request(request("/")).await.unwrap();
    assert_ne!(
        second.headers()["x-host"],
        chosen,
        "P2C must see body-held request"
    );
    check(second).await;
    assert!(
        metrics(m)
            .await
            .contains("active_requests{cluster=\"echo\"} 1")
    );
    tokio::time::sleep(Duration::from_millis(250)).await;
    assert!(
        metrics(m)
            .await
            .contains("active_requests{cluster=\"echo\"} 0")
    );
    assert!(
        metrics(m)
            .await
            .contains("errors_total{cluster=\"echo\"} 1"),
        "body deadline must be observable"
    );
    check(tx.send_request(request("/")).await.unwrap()).await;
    drop(held);
    drop(tx);
    t.abort();
    let _ = t.await;
    p.close().await;
    a.close().await;
    b.close().await;
}
#[tokio::test]
async fn maglev_routes_identical_keys_to_same_actual_endpoint() {
    let a = Backend::start(Duration::ZERO, 200).await;
    let b = Backend::start(Duration::ZERO, 200).await;
    let mut cfg = config(a.addr);
    cfg.clusters[0].endpoints.push(format!("http://{}", b.addr));
    cfg.clusters[0].lb = config::LoadBalance::Maglev;
    let p = Proxy::start(cfg).await;
    let (mut tx, t) = h1(p.addr).await;
    let mut previous = None;
    for _ in 0..20 {
        let r = tx.send_request(get()).await.unwrap();
        let id = r.headers()["x-backend"].clone();
        if let Some(old) = &previous {
            assert_eq!(&id, old);
        }
        previous = Some(id);
        r.into_body().collect().await.unwrap();
    }
    drop(tx);
    t.abort();
    let _ = t.await;
    p.close().await;
    a.close().await;
    b.close().await;
}
#[tokio::test]
async fn retryable_status_does_not_replay_get_body_or_post() {
    let a = Backend::start(Duration::ZERO, 503).await;
    let p = Proxy::start(config(a.addr)).await;
    let (mut tx, t) = h1(p.addr).await;
    assert_eq!(tx.send_request(get()).await.unwrap().status(), 503);
    assert_eq!(a.calls.load(Ordering::SeqCst), 2);
    let mut with_body = request("/");
    *with_body.method_mut() = http::Method::GET;
    assert_eq!(tx.send_request(with_body).await.unwrap().status(), 503);
    assert_eq!(a.calls.load(Ordering::SeqCst), 3);
    assert_eq!(tx.send_request(request("/")).await.unwrap().status(), 503);
    assert_eq!(a.calls.load(Ordering::SeqCst), 4);
    drop(tx);
    t.abort();
    let _ = t.await;
    p.close().await;
    a.close().await;
}
