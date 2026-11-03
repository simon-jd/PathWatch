mod common;
use common::*;
#[tokio::test]
async fn h1_roundtrip() {
    let upstream = Upstream::start().await;
    let proxy = Proxy::start(config(upstream.addr)).await;
    let (mut client, driver) = h1(proxy.addr).await;
    let response = client
        .send_request(request("/echo?q=one%20two"))
        .await
        .unwrap();
    assert_eq!(response.status(), 201);
    assert_eq!(response.headers()["x-upstream-version"], "HTTP/1.1");
    assert_eq!(response.headers()["x-path"], "/echo?q=one%20two");
    check(response).await;
    drop(client);
    driver.await.unwrap();
    proxy.close().await;
    upstream.close().await;
}
#[tokio::test]
async fn routing_and_hop_headers() {
    let upstream = Upstream::start().await;
    let mut cfg = config(upstream.addr);
    cfg.routes[0].path_prefix = "/api".into();
    let proxy = Proxy::start(cfg).await;
    let (mut client, driver) = h1(proxy.addr).await;
    let mut req = request("/api/a?x=1");
    req.headers_mut()
        .insert("host", "TEST.LOCAL:80".parse().unwrap());
    req.headers_mut()
        .insert("connection", "x-request-hop, keep-alive".parse().unwrap());
    req.headers_mut()
        .insert("x-request-hop", "remove".parse().unwrap());
    req.headers_mut()
        .insert("proxy-connection", "keep-alive".parse().unwrap());
    let response = client.send_request(req).await.unwrap();
    assert_eq!(response.status(), 201);
    assert_eq!(response.headers()["x-leaked"], "false");
    assert!(!response.headers().contains_key("x-response-hop"));
    assert_eq!(response.headers()["x-host"], upstream.addr.to_string());
    check(response).await;
    for (path, host, status) in [
        ("/missing", "test.local", 404),
        ("/api", "unknown.test", 404),
        ("http://evil.test/api", "test.local", 400),
    ] {
        let mut req = request(path);
        req.headers_mut().insert("host", host.parse().unwrap());
        let r = client.send_request(req).await.unwrap();
        assert_eq!(r.status(), status);
        http_body_util::BodyExt::collect(r.into_body())
            .await
            .unwrap();
    }
    drop(client);
    driver.await.unwrap();
    proxy.close().await;
    upstream.close().await;
}
#[tokio::test]
async fn startup_failure_releases_first_listener_and_duplicate_endpoints_fail_closed() {
    let upstream = Upstream::start().await;
    let cfg = config(upstream.addr);
    let first = cfg.listeners[0].addr;
    let occupied = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let mut bad = cfg.clone();
    bad.listeners.push(config::Listener {
        addr: occupied.local_addr().unwrap(),
        tls: None,
    });
    let result = tokio::time::timeout(
        std::time::Duration::from_secs(1),
        edge_proxy::serve(std::sync::Arc::new(bad), edge_proxy::Shutdown::new()),
    )
    .await;
    assert!(result.is_ok() && result.unwrap().is_err());
    let released = tokio::net::TcpListener::bind(first)
        .await
        .expect("first bound listener released");
    drop(released);
    let mut bad = cfg;
    bad.clusters[0]
        .endpoints
        .push(format!("http://{}", upstream.addr));
    assert!(
        tokio::time::timeout(
            std::time::Duration::from_secs(1),
            edge_proxy::serve(std::sync::Arc::new(bad), edge_proxy::Shutdown::new())
        )
        .await
        .unwrap()
        .is_err()
    );
    upstream.close().await;
}
#[tokio::test]
async fn refused_address_stays_reserved_and_refuses_connections() {
    let (_reserved, addr) = refused_address();
    let bind = tokio::net::TcpListener::bind(addr).await;
    assert_eq!(
        bind.err().map(|e| e.kind()),
        Some(std::io::ErrorKind::AddrInUse)
    );
    let connect = tokio::net::TcpStream::connect(addr).await;
    assert_eq!(
        connect.err().map(|e| e.kind()),
        Some(std::io::ErrorKind::ConnectionRefused)
    );
}
#[tokio::test]
async fn unavailable_upstream_returns_bounded_gateway_error() {
    let (_reserved, unused) = refused_address();
    let mut cfg = config(unused);
    cfg.pool.request_timeout_ms = 100;
    let proxy = Proxy::start(cfg).await;
    let (mut client, driver) = h1(proxy.addr).await;
    let response = tokio::time::timeout(
        std::time::Duration::from_secs(1),
        client.send_request(request("/")),
    )
    .await
    .unwrap()
    .unwrap();
    assert_eq!(response.status(), 502);
    http_body_util::BodyExt::collect(response.into_body())
        .await
        .unwrap();
    drop(client);
    driver.await.unwrap();
    proxy.close().await;
}
#[tokio::test]
async fn failed_request_is_not_implicitly_retried() {
    use tokio::io::AsyncReadExt;
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let task = tokio::spawn(async move {
        let (mut s, _) = listener.accept().await.unwrap();
        let mut buf = [0; 4096];
        assert!(s.read(&mut buf).await.unwrap() > 0);
        drop(s);
        assert!(
            tokio::time::timeout(std::time::Duration::from_millis(100), listener.accept())
                .await
                .is_err(),
            "unexpected retry TCP connection"
        );
    });
    let proxy = Proxy::start(config(addr)).await;
    let (mut client, driver) = h1(proxy.addr).await;
    let r = client.send_request(request("/")).await.unwrap();
    assert_eq!(r.status(), 502);
    http_body_util::BodyExt::collect(r.into_body())
        .await
        .unwrap();
    drop(client);
    driver.await.unwrap();
    proxy.close().await;
    task.await.unwrap();
}
