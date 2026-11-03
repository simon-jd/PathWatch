mod common;
use common::*;
#[tokio::test]
async fn h2_multiplexed_streams() {
    for scheme in ["http", "h2c"] {
        let upstream = Upstream::with_delay(std::time::Duration::from_millis(5)).await;
        let mut cfg = config(upstream.addr);
        cfg.clusters[0].endpoints[0] = format!("{scheme}://{}", upstream.addr);
        let proxy = Proxy::start(cfg).await;
        let (client, driver) = h2(proxy.addr).await; // Exactly one TCP connection, 100 cloned h2 stream senders.
        let mut tasks = tokio::task::JoinSet::new();
        for _ in 0..100 {
            let mut client = client.clone();
            tasks.spawn(async move {
                check(
                    client
                        .send_request(request("http://test.local/echo"))
                        .await
                        .unwrap(),
                )
                .await;
            });
        }
        tokio::time::timeout(std::time::Duration::from_secs(10), async {
            while let Some(result) = tasks.join_next().await {
                result.unwrap();
            }
        })
        .await
        .unwrap();
        assert!(
            upstream.count() > 0 && upstream.count() <= 4,
            "actual TCP accepts {}",
            upstream.count()
        );
        println!(
            "100 concurrent streams, 1 downstream h2 TCP connection, {} {scheme} upstream accepts, cap 4",
            upstream.count()
        );
        drop(client);
        driver.await.unwrap();
        proxy.close().await;
        upstream.close().await;
    }
}
// Task 13 slow path: small H2 requests, several streams in flight on one
// downstream connection (h2load keeps up to 16). Loopback round trips are
// ~0.1 ms; with Nagle on the proxy's downstream socket a second response
// written while the first is unacknowledged waits for the client's (delayed) ACK.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn h2_small_request_latency() {
    use hyper_util::rt::{TokioExecutor, TokioIo};
    use std::time::{Duration, Instant};
    let upstream = Upstream::start().await;
    let proxy = Proxy::start(config(upstream.addr)).await;
    let socket = tokio::net::TcpStream::connect(proxy.addr).await.unwrap();
    socket.set_nodelay(true).unwrap();
    let (client, connection) =
        hyper::client::conn::http2::handshake(TokioExecutor::new(), TokioIo::new(socket))
            .await
            .unwrap();
    let driver = tokio::spawn(async move {
        let _ = connection.await;
    });
    let mut samples = Vec::new();
    for round in 0..120 {
        let mut streams = tokio::task::JoinSet::new();
        for _ in 0..4 {
            let mut client = client.clone();
            streams.spawn(async move {
                let start = Instant::now();
                check(
                    client
                        .send_request(request("http://test.local/latency"))
                        .await
                        .unwrap(),
                )
                .await;
                start.elapsed()
            });
        }
        while let Some(elapsed) = streams.join_next().await {
            if round >= 20 {
                samples.push(elapsed.unwrap());
            }
        }
    }
    samples.sort();
    let p50 = samples[samples.len() / 2];
    let p90 = samples[samples.len() * 9 / 10];
    println!(
        "h2 4-stream round trip p50={p50:?} p90={p90:?} n={}",
        samples.len()
    );
    // A Nagle stall waits for Linux's 40 ms delayed ACK; a loaded debug build
    // alone stays well under 20 ms.
    assert!(
        p90 < Duration::from_millis(20),
        "h2 p90 {p90:?} (p50 {p50:?}) over a loopback proxy"
    );
    drop(client);
    driver.await.unwrap();
    proxy.close().await;
    upstream.close().await;
}

#[tokio::test]
async fn upstream_h2c_roundtrip() {
    let upstream = Upstream::start().await;
    let mut cfg = config(upstream.addr);
    cfg.clusters[0].endpoints[0] = format!("h2c://{}", upstream.addr);
    let proxy = Proxy::start(cfg).await;
    let (mut client, driver) = h1(proxy.addr).await;
    let response = client.send_request(request("/h2-upstream")).await.unwrap();
    assert_eq!(response.headers()["x-upstream-version"], "HTTP/2.0");
    check(response).await;
    drop(client);
    driver.await.unwrap();
    proxy.close().await;
    upstream.close().await;
}

#[tokio::test]
async fn h2c_early_response_cancels_unfinished_upload_and_socket() {
    use bytes::Bytes;
    use http_body::{Body, Frame};
    use http_body_util::{BodyExt, Full};
    use hyper::{Request, Response, body::Incoming};
    use hyper_util::rt::{TokioExecutor, TokioIo};
    use std::{
        convert::Infallible,
        pin::Pin,
        sync::{
            Arc,
            atomic::{AtomicBool, AtomicUsize, Ordering},
        },
        task::{Context, Poll},
        time::Duration,
    };
    struct GatedUpload {
        first: bool,
        dropped: Arc<AtomicBool>,
    }
    impl Body for GatedUpload {
        type Data = Bytes;
        type Error = Infallible;
        fn poll_frame(
            mut self: Pin<&mut Self>,
            _: &mut Context<'_>,
        ) -> Poll<Option<Result<Frame<Bytes>, Infallible>>> {
            if self.first {
                self.first = false;
                Poll::Ready(Some(Ok(Frame::data(Bytes::from_static(b"partial upload")))))
            } else {
                Poll::Pending
            }
        }
    }
    impl Drop for GatedUpload {
        fn drop(&mut self) {
            self.dropped.store(true, Ordering::SeqCst);
        }
    }
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let closed = Arc::new(AtomicUsize::new(0));
    let (held_tx, mut held_rx) = tokio::sync::mpsc::channel::<Incoming>(1);
    let stop = edge_proxy::Shutdown::new();
    let (server_stop, server_closed) = (stop.clone(), closed.clone());
    let server = tokio::spawn(async move {
        let mut tasks = tokio::task::JoinSet::new();
        loop {
            tokio::select! {
                _ = server_stop.cancelled() => break,
                accepted = listener.accept() => {
                    let (socket, _) = accepted.unwrap();
                    let held_tx = held_tx.clone();
                    let closed = server_closed.clone();
                    tasks.spawn(async move {
                        let service = hyper::service::service_fn(move |req: Request<Incoming>| {
                            let held_tx = held_tx.clone();
                            async move {
                                let response = if req.uri().path() == "/early" {
                                    held_tx.send(req.into_body()).await.unwrap();
                                    Response::builder().status(204).body(Full::new(Bytes::new())).unwrap()
                                } else {
                                    let body = req.into_body().collect().await.unwrap().to_bytes();
                                    Response::builder().status(201).body(Full::new(body)).unwrap()
                                };
                                Ok::<_, Infallible>(response)
                            }
                        });
                        let _ = hyper::server::conn::http2::Builder::new(TokioExecutor::new())
                            .serve_connection(TokioIo::new(socket), service).await;
                        closed.fetch_add(1, Ordering::SeqCst);
                    });
                }
            }
        }
        tasks.abort_all();
        while tasks.join_next().await.is_some() {}
    });
    let mut cfg = config(addr);
    cfg.clusters[0].endpoints[0] = format!("h2c://{addr}");
    cfg.pool.max_connections = 1;
    cfg.pool.request_timeout_ms = 150;
    let proxy = Proxy::start(cfg).await;
    let socket = tokio::net::TcpStream::connect(proxy.addr).await.unwrap();
    let (mut client, connection) =
        hyper::client::conn::http2::handshake(TokioExecutor::new(), TokioIo::new(socket))
            .await
            .unwrap();
    let driver = tokio::spawn(async move {
        let _ = connection.await;
    });
    let dropped = Arc::new(AtomicBool::new(false));
    let req = Request::builder()
        .method("POST")
        .uri("http://test.local/early")
        .body(GatedUpload {
            first: true,
            dropped: dropped.clone(),
        })
        .unwrap();
    let response = client.send_request(req).await.unwrap();
    assert_eq!(response.status(), 204);
    response.into_body().collect().await.unwrap();
    let mut held = held_rx.recv().await.unwrap();
    tokio::time::timeout(Duration::from_millis(400), async {
        while !dropped.load(Ordering::SeqCst) || closed.load(Ordering::SeqCst) == 0 {
            tokio::task::yield_now().await;
        }
    })
    .await
    .expect("early response must not leave request upload/physical socket alive past deadline");
    let ended = tokio::time::timeout(Duration::from_millis(100), async {
        while let Some(frame) = held.frame().await {
            if frame.is_err() {
                return;
            }
        }
    })
    .await;
    assert!(
        ended.is_ok(),
        "held upstream request must end without downstream completing upload"
    );
    let (mut next, next_driver) = h2(proxy.addr).await;
    check(
        next.send_request(request("http://test.local/next"))
            .await
            .unwrap(),
    )
    .await;
    drop(next);
    next_driver.await.unwrap();
    drop(client);
    driver.await.unwrap();
    proxy.close().await;
    tokio::time::timeout(Duration::from_secs(1), async {
        while closed.load(Ordering::SeqCst) != 2 {
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    stop.cancel();
    server.await.unwrap();
}

#[tokio::test]
async fn h2c_shutdown_during_pending_headers_closes_transport() {
    use std::{sync::atomic::Ordering, time::Duration};
    let upstream = Upstream::with_delay(Duration::from_secs(2)).await;
    let mut cfg = config(upstream.addr);
    cfg.clusters[0].endpoints[0] = format!("h2c://{}", upstream.addr);
    cfg.pool.drain_timeout_ms = 50;
    let proxy = Proxy::start(cfg).await;
    let (mut client, driver) = h2(proxy.addr).await;
    let response = tokio::spawn(async move {
        client
            .send_request(request("http://test.local/pending"))
            .await
    });
    tokio::time::timeout(Duration::from_secs(1), async {
        while upstream.count() == 0 {
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    tokio::time::sleep(Duration::from_millis(20)).await;
    proxy.close().await;
    tokio::time::timeout(Duration::from_millis(300), async {
        while upstream.closed.load(Ordering::SeqCst) != 1 {
            tokio::task::yield_now().await;
        }
    })
    .await
    .expect("shutdown must await transport cleanup before pending upstream headers arrive");
    assert!(response.await.unwrap().is_err());
    driver.await.unwrap();
    upstream.close().await;
}
