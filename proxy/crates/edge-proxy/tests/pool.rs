mod common;
use common::*;
#[tokio::test]
async fn pool_reuses_upstream_connections() {
    let upstream = Upstream::start().await;
    let proxy = Proxy::start(config(upstream.addr)).await;
    let (mut client, driver) = h1(proxy.addr).await;
    for _ in 0..1000 {
        check(client.send_request(request("/echo")).await.unwrap()).await;
    }
    assert!(
        upstream.count() > 0 && upstream.count() <= 4,
        "actual accepts {}",
        upstream.count()
    );
    println!(
        "1000 serial requests, {} upstream TCP accepts",
        upstream.count()
    );
    drop(client);
    driver.await.unwrap();
    proxy.close().await;
    upstream.close().await;
}
#[tokio::test]
async fn empty_responses_reuse_physical_socket() {
    let upstream = Upstream::start().await;
    let proxy = Proxy::start(config(upstream.addr)).await;
    let (mut client, driver) = h1(proxy.addr).await;
    for _ in 0..10 {
        let req = hyper::Request::builder()
            .uri("/")
            .header("host", "test.local")
            .body(http_body_util::Full::new(bytes::Bytes::new()))
            .unwrap();
        let r = client.send_request(req).await.unwrap();
        assert_eq!(r.status(), 201);
        http_body_util::BodyExt::collect(r.into_body())
            .await
            .unwrap();
    }
    assert_eq!(
        upstream.count(),
        1,
        "empty bodies must return a reusable socket"
    );
    drop(client);
    driver.await.unwrap();
    proxy.close().await;
    upstream.close().await;
}
#[tokio::test]
async fn a_global_slot_can_switch_endpoints_without_deadlock() {
    let a = Upstream::start().await;
    let b = Upstream::start().await;
    let mut cfg = config(a.addr);
    cfg.pool.max_connections = 1;
    let mut second = cfg.clusters[0].clone();
    second.name = "second".into();
    second.endpoints = vec![format!("http://{}", b.addr)];
    cfg.clusters.push(second);
    cfg.routes.push(config::Route {
        host: "other.test".into(),
        path_prefix: "/".into(),
        cluster: "second".into(),
    });
    let proxy = Proxy::start(cfg).await;
    let (mut client, driver) = h1(proxy.addr).await;
    check(client.send_request(request("/")).await.unwrap()).await;
    let mut req = request("/");
    req.headers_mut()
        .insert("host", "other.test".parse().unwrap());
    check(client.send_request(req).await.unwrap()).await;
    assert_eq!(a.count(), 1);
    assert_eq!(b.count(), 1);
    drop(client);
    driver.await.unwrap();
    proxy.close().await;
    a.close().await;
    b.close().await;
}
#[tokio::test]
async fn streaming_body_holds_slot_and_graceful_shutdown_drains() {
    use http_body::{Body, Frame};
    use http_body_util::BodyExt;
    use std::{
        convert::Infallible,
        pin::Pin,
        sync::{
            Arc,
            atomic::{AtomicUsize, Ordering},
        },
        task::{Context, Poll},
        time::Duration,
    };
    struct Stream(tokio::sync::mpsc::Receiver<bytes::Bytes>);
    impl Body for Stream {
        type Data = bytes::Bytes;
        type Error = Infallible;
        fn poll_frame(
            mut self: Pin<&mut Self>,
            cx: &mut Context<'_>,
        ) -> Poll<Option<Result<Frame<Self::Data>, Infallible>>> {
            self.0.poll_recv(cx).map(|o| o.map(|b| Ok(Frame::data(b))))
        }
    }
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let gate = Arc::new(tokio::sync::Semaphore::new(0));
    let count = Arc::new(AtomicUsize::new(0));
    let seen = Arc::new(AtomicUsize::new(0));
    let (up_gate, up_count, up_seen) = (gate.clone(), count.clone(), seen.clone());
    let stop = edge_proxy::Shutdown::new();
    let up_stop = stop.clone();
    let task = tokio::spawn(async move {
        let mut connections = tokio::task::JoinSet::new();
        loop {
            tokio::select! {_=up_stop.cancelled()=>break, accepted=listener.accept()=>{
             let(s,_)=accepted.unwrap(); up_count.fetch_add(1,Ordering::SeqCst); let gate=up_gate.clone(); let seen=up_seen.clone();
             connections.spawn(async move {let service=hyper::service::service_fn(move |req:hyper::Request<hyper::body::Incoming>| {let gate=gate.clone(); let seen=seen.clone(); async move {
              req.into_body().collect().await.unwrap(); seen.fetch_add(1,Ordering::SeqCst); let(tx,rx)=tokio::sync::mpsc::channel(1);
              tokio::spawn(async move {let _=tx.send(bytes::Bytes::from_static(b"abc")).await; let permit=gate.acquire().await.unwrap(); permit.forget(); let _=tx.send(bytes::Bytes::from_static(b"def")).await;});
              Ok::<_,Infallible>(hyper::Response::builder().header("content-length","6").body(Stream(rx)).unwrap())
             }}); let _=hyper_util::server::conn::auto::Builder::new(hyper_util::rt::TokioExecutor::new()).serve_connection(hyper_util::rt::TokioIo::new(s),service).await;});
            }}
        }
        connections.abort_all();
        while connections.join_next().await.is_some() {}
    });
    let mut cfg = config(addr);
    cfg.pool.max_connections = 1;
    let proxy = Proxy::start(cfg).await;
    let (client, driver) = h2(proxy.addr).await;
    let mut first_client = client.clone();
    let mut first = first_client
        .send_request(request("http://test.local/first"))
        .await
        .unwrap();
    assert_eq!(
        first
            .body_mut()
            .frame()
            .await
            .unwrap()
            .unwrap()
            .into_data()
            .unwrap(),
        "abc"
    );
    let mut second_client = client.clone();
    let second = tokio::spawn(async move {
        second_client
            .send_request(request("http://test.local/second"))
            .await
            .unwrap()
            .into_body()
            .collect()
            .await
            .unwrap()
            .to_bytes()
    });
    tokio::time::sleep(Duration::from_millis(50)).await;
    assert_eq!(
        seen.load(Ordering::SeqCst),
        1,
        "second request cannot use active streaming socket"
    );
    assert_eq!(
        count.load(Ordering::SeqCst),
        1,
        "no extra physical socket while first body streams"
    );
    proxy.stop.cancel();
    tokio::time::sleep(Duration::from_millis(20)).await;
    assert!(
        !proxy.task.is_finished(),
        "shutdown must drain active bodies"
    );
    gate.add_permits(2);
    assert_eq!(first.into_body().collect().await.unwrap().to_bytes(), "def");
    assert_eq!(second.await.unwrap(), "abcdef");
    drop(first_client);
    drop(client);
    driver.await.unwrap();
    proxy.close().await;
    stop.cancel();
    task.await.unwrap();
}
#[tokio::test]
async fn stalled_downstream_cannot_hold_pool_slot_past_deadline() {
    stalled_protocol("http").await;
}
#[tokio::test]
async fn h2c_stalled_response_reclaims_transport_before_body_drop() {
    stalled_protocol("h2c").await;
}
async fn stalled_protocol(scheme: &str) {
    let upstream = Upstream::start().await;
    let mut cfg = config(upstream.addr);
    cfg.pool.max_connections = 1;
    cfg.pool.request_timeout_ms = 150;
    cfg.clusters[0].endpoints[0] = format!("{scheme}://{}", upstream.addr);
    let proxy = Proxy::start(cfg).await;
    let (mut client, driver) = h2(proxy.addr).await;
    // Generate a large response from a small upload: isolate response flow control.
    let req = request("http://test.local/large-response");
    let first = client.send_request(req).await.unwrap();
    assert_eq!(first.status(), 201);
    tokio::time::sleep(std::time::Duration::from_millis(250)).await;
    let second = client
        .send_request(request("http://test.local/next"))
        .await
        .unwrap();
    assert_eq!(
        second.status(),
        201,
        "stalled body must release lease on deadline"
    );
    check(second).await;
    tokio::time::timeout(std::time::Duration::from_secs(1), async {
        while upstream.closed.load(std::sync::atomic::Ordering::SeqCst) < 1 {
            tokio::task::yield_now().await;
        }
    })
    .await
    .expect("old physical socket must close while first Incoming is retained");
    assert_eq!(upstream.count(), 2, "exactly one replacement socket");
    drop(first);
    drop(client);
    driver.await.unwrap();
    proxy.close().await;
    upstream.close().await;
}

#[tokio::test]
async fn h2c_response_drop_cancels_transport_and_replacement_reuses() {
    use std::{sync::atomic::Ordering, time::Duration};
    let upstream = Upstream::start().await;
    let mut cfg = config(upstream.addr);
    cfg.clusters[0].endpoints[0] = format!("h2c://{}", upstream.addr);
    cfg.pool.max_connections = 1;
    // Cancellation must reclaim the socket well before this five-second deadline.
    let proxy = Proxy::start(cfg).await;
    let (mut client, driver) = h2(proxy.addr).await;
    let response = client
        .send_request(request("http://test.local/large-response"))
        .await
        .unwrap();
    assert_eq!(response.status(), 201);
    drop(response);
    tokio::time::timeout(Duration::from_secs(1), async {
        while upstream.closed.load(Ordering::SeqCst) != 1 {
            tokio::task::yield_now().await;
        }
    })
    .await
    .expect("downstream cancellation must close the actual h2c socket promptly");
    for _ in 0..10 {
        check(
            client
                .send_request(request("http://test.local/reuse"))
                .await
                .unwrap(),
        )
        .await;
    }
    assert_eq!(
        upstream.count(),
        2,
        "normal complete exchanges reuse the replacement socket"
    );
    let retained = client
        .send_request(request("http://test.local/large-response"))
        .await
        .unwrap();
    assert_eq!(retained.status(), 201);
    proxy.close().await;
    tokio::time::timeout(Duration::from_secs(1), async {
        while upstream.closed.load(Ordering::SeqCst) != 2 {
            tokio::task::yield_now().await;
        }
    })
    .await
    .expect("shutdown must close actual transport with a retained response body");
    drop(retained);
    drop(client);
    driver.await.unwrap();
    upstream.close().await;
}
