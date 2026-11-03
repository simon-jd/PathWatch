mod common;
mod support;
use common::*;
use http_body_util::BodyExt;
use std::{
    path::PathBuf,
    process::{Child, Command, Stdio},
    sync::atomic::Ordering,
    time::{Duration, Instant},
};
use support::*;
struct Cli {
    child: Child,
    _dir: tempfile::TempDir,
    path: PathBuf,
    addr: std::net::SocketAddr,
    management: std::net::SocketAddr,
}
impl Drop for Cli {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}
impl Cli {
    async fn start(upstream: std::net::SocketAddr) -> Self {
        Self::start_with(upstream, None).await
    }
    async fn start_with(upstream: std::net::SocketAddr, limits: Option<(usize, usize)>) -> Self {
        // A reserved port can still be taken by another process between reservation and the
        // child's bind; the child then exits, so retry with fresh ports.
        for _ in 0..3 {
            if let Some(cli) = Self::try_start(upstream, limits).await {
                return cli;
            }
        }
        panic!("CLI failed to start three times");
    }
    async fn try_start(
        upstream: std::net::SocketAddr,
        limits: Option<(usize, usize)>,
    ) -> Option<Self> {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("proxy.toml");
        let addr = address();
        let management = address();
        let mut cfg = configuration(addr, management, upstream);
        if let Some((active, pending)) = limits {
            cfg = cfg.replace(
                "max_requests=32,max_pending=0",
                &format!("max_requests={active},max_pending={pending}"),
            );
        }
        std::fs::write(&path, cfg).unwrap();
        let child = Command::new(env!("CARGO_BIN_EXE_edge-proxy"))
            .arg(&path)
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .unwrap();
        let mut cli = Self {
            child,
            _dir: dir,
            path,
            addr,
            management,
        };
        let ready = tokio::time::timeout(Duration::from_secs(3), async {
            loop {
                if cli.child.try_wait().unwrap().is_some() {
                    return false;
                }
                if tokio::net::TcpStream::connect(addr).await.is_ok()
                    && tokio::net::TcpStream::connect(management).await.is_ok()
                {
                    // Both ports answer; make sure they answer for our child, not a stranger.
                    tokio::time::sleep(Duration::from_millis(20)).await;
                    return cli.child.try_wait().unwrap().is_none();
                }
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
        })
        .await
        .expect("CLI listening");
        ready.then_some(cli)
    }
    fn signal(&mut self, name: &str) {
        assert!(
            Command::new("kill")
                .args([name, &self.child.id().to_string()])
                .status()
                .unwrap()
                .success()
        );
    }
    async fn metrics(&self) -> String {
        let (mut tx, t) = h1(self.management).await;
        let r = tx
            .send_request(
                hyper::Request::builder()
                    .uri("/metrics")
                    .body(http_body_util::Full::new(bytes::Bytes::new()))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(r.status(), 200);
        let bytes = r.into_body().collect().await.unwrap().to_bytes();
        drop(tx);
        t.abort();
        let _ = t.await;
        String::from_utf8(bytes.to_vec()).unwrap()
    }
    async fn close(mut self) {
        self.signal("-INT");
        tokio::time::timeout(Duration::from_secs(3), async {
            loop {
                if let Some(code) = self.child.try_wait().unwrap() {
                    assert!(code.success());
                    break;
                }
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
        })
        .await
        .unwrap();
        assert!(tokio::net::TcpStream::connect(self.addr).await.is_err());
        assert!(
            tokio::net::TcpStream::connect(self.management)
                .await
                .is_err()
        );
    }
}
fn configuration(
    addr: std::net::SocketAddr,
    management: std::net::SocketAddr,
    upstream: std::net::SocketAddr,
) -> String {
    format!(
        r#"listeners=[{{addr="{addr}"}}]
management={{addr="{management}"}}
routes=[{{host="test.local",path_prefix="/",cluster="echo"}}]
clusters=[{{name="echo",endpoints=["http://{upstream}"],circuit={{max_requests=32,max_pending=0,request_timeout_ms=15000}}}}]
[pool]
max_connections=32
request_timeout_ms=15000
drain_timeout_ms=30000
"#
    )
}
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn hot_reload_drains_inflight() {
    let old = Backend::start(Duration::from_secs(5), 200).await;
    let new = Backend::start(Duration::ZERO, 201).await;
    let mut cli = Cli::start(old.addr).await;
    let start = Instant::now();
    let mut tasks = tokio::task::JoinSet::new();
    for _ in 0..10 {
        let addr = cli.addr;
        tasks.spawn(async move {
            let (mut tx, t) = h1(addr).await;
            let r = tx.send_request(request("/")).await.unwrap();
            assert_eq!(r.status(), 200);
            assert_eq!(r.into_body().collect().await.unwrap().to_bytes(), "done");
            drop(tx);
            t.await.unwrap();
        });
    }
    let (tx, h2driver) = h2(cli.addr).await;
    for _ in 0..10 {
        let mut tx = tx.clone();
        tasks.spawn(async move {
            let r = tx.send_request(request("/")).await.unwrap();
            assert_eq!(r.status(), 200);
            assert_eq!(r.into_body().collect().await.unwrap().to_bytes(), "done");
        });
    }
    until(|| old.active.load(Ordering::SeqCst) == 20).await;
    assert_eq!(old.calls.load(Ordering::SeqCst), 20);
    std::fs::write(&cli.path, configuration(cli.addr, cli.management, new.addr)).unwrap();
    cli.signal("-HUP");
    tokio::time::sleep(Duration::from_millis(100)).await;
    assert!(
        cli.child.try_wait().unwrap().is_none(),
        "SIGHUP must retain same listener and process"
    );
    let (mut fresh, t) = h1(cli.addr).await;
    let r = fresh.send_request(request("/")).await.unwrap();
    assert_eq!(r.status(), 201);
    r.into_body().collect().await.unwrap();
    drop(fresh);
    t.abort();
    let _ = t.await;
    let mut completed = 0;
    while let Some(result) = tasks.join_next().await {
        result.unwrap();
        completed += 1;
    }
    assert_eq!(completed, 20);
    assert!(start.elapsed() >= Duration::from_secs(5));
    drop(tx);
    tokio::time::timeout(Duration::from_secs(1), h2driver)
        .await
        .unwrap()
        .unwrap();
    println!(
        "20 actual upstream inflight before SIGHUP; completed={completed}; resets=0; elapsed={:?}; h1=10 h2=10",
        start.elapsed()
    );
    assert!(
        cli.metrics()
            .await
            .contains("pathwatch_proxy_reload_success_total 1")
    );
    cli.close().await;
    old.close().await;
    new.close().await;
}
#[tokio::test]
async fn invalid_reload_keeps_generation_and_management_metrics() {
    let backend = Backend::start(Duration::ZERO, 200).await;
    let mut cli = Cli::start(backend.addr).await;
    std::fs::write(&cli.path, "invalid TOML!").unwrap();
    cli.signal("-HUP");
    tokio::time::sleep(Duration::from_millis(100)).await;
    assert!(cli.child.try_wait().unwrap().is_none());
    let (mut tx, t) = h1(cli.addr).await;
    let r = tx.send_request(request("/")).await.unwrap();
    assert_eq!(r.status(), 200);
    r.into_body().collect().await.unwrap();
    drop(tx);
    t.abort();
    let _ = t.await;
    let metrics = cli.metrics().await;
    assert!(metrics.contains("pathwatch_proxy_reload_failure_total 1"));
    assert!(metrics.contains("pathwatch_proxy_requests_total{cluster=\"echo\"} 1"));
    cli.close().await;
    backend.close().await;
}
#[tokio::test]
async fn certificate_reload_is_atomic_and_failed_certificate_retains_previous() {
    use rustls::{ClientConfig, RootCertStore, pki_types::ServerName};
    use tokio_rustls::TlsConnector;
    let backend = Backend::start(Duration::ZERO, 200).await;
    let mut cli = Cli::start(backend.addr).await;
    let mut roots = RootCertStore::empty();
    let mut expected = Vec::new();
    for i in 0..2 {
        let cert = rcgen::generate_simple_self_signed(vec!["test.local".into()]).unwrap();
        roots.add(cert.cert.der().clone()).unwrap();
        expected.push(cert.cert.der().clone());
        std::fs::write(cli._dir.path().join(format!("{i}.pem")), cert.cert.pem()).unwrap();
        std::fs::write(
            cli._dir.path().join(format!("{i}.key")),
            cert.signing_key.serialize_pem(),
        )
        .unwrap();
    }
    let mut cc = ClientConfig::builder_with_provider(std::sync::Arc::new(
        rustls::crypto::ring::default_provider(),
    ))
    .with_safe_default_protocol_versions()
    .unwrap()
    .with_root_certificates(roots)
    .with_no_client_auth();
    cc.alpn_protocols = vec![b"h2".to_vec(), b"http/1.1".to_vec()];
    let connector = TlsConnector::from(std::sync::Arc::new(cc));
    for i in 0..3 {
        let cert = cli._dir.path().join(format!("{}.pem", i.min(1)));
        let key = cli._dir.path().join(format!("{i}.key"));
        let cfg=configuration(cli.addr,cli.management,backend.addr).replace(&format!("listeners=[{{addr=\"{}\"}}]",cli.addr),&format!("listeners=[{{addr=\"{}\",tls={{certs=[{{sni=\"test.local\",cert=\"{}\",key=\"{}\"}}]}}}}]",cli.addr,cert.display(),key.display()));
        std::fs::write(&cli.path, cfg).unwrap();
        cli.signal("-HUP");
        tokio::time::sleep(Duration::from_millis(80)).await;
        let tls = connector
            .connect(
                ServerName::try_from("test.local").unwrap(),
                tokio::net::TcpStream::connect(cli.addr).await.unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(
            tls.get_ref().1.peer_certificates().unwrap()[0],
            expected[i.min(1)]
        );
        assert_eq!(tls.get_ref().1.alpn_protocol(), Some(b"h2".as_slice()));
        let (mut tx, c) = hyper::client::conn::http2::handshake(
            hyper_util::rt::TokioExecutor::new(),
            hyper_util::rt::TokioIo::new(tls),
        )
        .await
        .unwrap();
        let t = tokio::spawn(async move {
            let _ = c.await;
        });
        assert_eq!(
            tx.send_request(request("https://test.local/"))
                .await
                .unwrap()
                .status(),
            200
        );
        drop(tx);
        t.abort();
        let _ = t.await;
    }
    let text = cli.metrics().await;
    assert!(text.contains("reload_success_total 2"));
    assert!(text.contains("reload_failure_total 1"));
    cli.close().await;
    backend.close().await;
}
#[tokio::test]
async fn rejected_capacity_and_address_reload_do_not_replace_generation() {
    let backend = Backend::start(Duration::ZERO, 200).await;
    let mut cli = Cli::start(backend.addr).await;
    for cfg in [
        configuration(cli.addr, cli.management, backend.addr)
            .replace("max_connections=32", "max_connections=33"),
        configuration(address(), cli.management, backend.addr),
        // The runtime is chosen once at startup; a reload cannot switch it.
        configuration(cli.addr, cli.management, backend.addr)
            + "[runtime]\nmode=\"per_core\"\nworkers=2\n",
    ] {
        std::fs::write(&cli.path, cfg).unwrap();
        cli.signal("-HUP");
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    let (mut tx, t) = h1(cli.addr).await;
    assert_eq!(tx.send_request(request("/")).await.unwrap().status(), 200);
    drop(tx);
    t.abort();
    let _ = t.await;
    assert!(cli.metrics().await.contains("reload_failure_total 3"));
    cli.close().await;
    backend.close().await;
}
fn empty_get() -> hyper::Request<Body> {
    hyper::Request::builder()
        .uri("/")
        .header("host", "test.local")
        .body(http_body_util::Full::new(bytes::Bytes::new()))
        .unwrap()
}
async fn fresh_status(addr: std::net::SocketAddr, req: hyper::Request<Body>) -> http::StatusCode {
    let (mut tx, t) = h1(addr).await;
    let r = tx.send_request(req).await.unwrap();
    let status = r.status();
    r.into_body().collect().await.unwrap();
    drop(tx);
    t.abort();
    let _ = t.await;
    status
}
#[tokio::test]
async fn no_op_reload_preserves_retry_bucket_and_counters() {
    let l = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = l.local_addr().unwrap();
    let count = std::sync::Arc::new(std::sync::atomic::AtomicUsize::new(0));
    let c = count.clone();
    let server = tokio::spawn(async move {
        loop {
            let (io, _) = l.accept().await.unwrap();
            c.fetch_add(1, Ordering::SeqCst);
            drop(io);
        }
    });
    let start = Instant::now();
    let mut cli = Cli::start(addr).await;
    for _ in 0..4 {
        assert_eq!(fresh_status(cli.addr, empty_get()).await, 502);
    }
    assert_eq!(count.load(Ordering::SeqCst), 7);
    cli.signal("-HUP");
    tokio::time::sleep(Duration::from_millis(60)).await;
    assert_eq!(fresh_status(cli.addr, empty_get()).await, 502);
    assert!(start.elapsed() < Duration::from_millis(900));
    assert_eq!(
        count.load(Ordering::SeqCst),
        8,
        "no-op reload must not mint retry floor"
    );
    let text = cli.metrics().await;
    assert!(text.contains("requests_total{cluster=\"echo\"} 5"));
    assert!(text.contains("retries_total{cluster=\"echo\"} 3"));
    cli.close().await;
    server.abort();
    let _ = server.await;
}
#[tokio::test]
async fn reload_preserves_passive_ejection_for_same_endpoint() {
    let a = Backend::start(Duration::ZERO, 500).await;
    let mut cli = Cli::start(a.addr).await;
    for _ in 0..5 {
        assert_eq!(fresh_status(cli.addr, request("/")).await, 500);
    }
    cli.signal("-HUP");
    tokio::time::sleep(Duration::from_millis(60)).await;
    assert_eq!(fresh_status(cli.addr, request("/")).await, 503);
    assert_eq!(a.calls.load(Ordering::SeqCst), 5);
    cli.close().await;
    a.close().await;
}
#[tokio::test]
async fn overlapping_generations_share_body_held_circuit_and_current_limits() {
    let old = Upstream::start().await;
    let new = Backend::start(Duration::ZERO, 200).await;
    let mut cli = Cli::start_with(old.addr, Some((2, 1))).await;
    let (mut tx, t) = h2(cli.addr).await;
    let held = tx.send_request(request("/large-response")).await.unwrap();
    assert_eq!(held.status(), 201);
    let cfg = configuration(cli.addr, cli.management, new.addr).replace(
        "max_requests=32,max_pending=0",
        "max_requests=1,max_pending=0",
    );
    std::fs::write(&cli.path, cfg).unwrap();
    cli.signal("-HUP");
    tokio::time::sleep(Duration::from_millis(60)).await;
    let text = cli.metrics().await;
    assert!(
        text.contains("active_requests{cluster=\"echo\"} 1"),
        "old generation body must remain visible: {text}"
    );
    assert_eq!(fresh_status(cli.addr, request("/")).await, 503);
    assert_eq!(new.calls.load(Ordering::SeqCst), 0);
    drop(held);
    drop(tx);
    t.abort();
    let _ = t.await;
    tokio::time::sleep(Duration::from_millis(30)).await;
    assert_eq!(fresh_status(cli.addr, request("/")).await, 200);
    cli.close().await;
    old.close().await;
    new.close().await;
}
#[tokio::test]
async fn no_op_reload_does_not_accelerate_active_probe_schedule() {
    let a = Backend::start(Duration::ZERO, 200).await;
    let mut cli = Cli::start(a.addr).await;
    let cfg = configuration(cli.addr, cli.management, a.addr).replace(
        "name=\"echo\",endpoints",
        "name=\"echo\",health={interval_ms=500,timeout_ms=100},endpoints",
    );
    std::fs::write(&cli.path, &cfg).unwrap();
    cli.signal("-HUP");
    until(|| a.probes.load(Ordering::SeqCst) == 1).await;
    for _ in 0..3 {
        cli.signal("-HUP");
        tokio::time::sleep(Duration::from_millis(30)).await;
    }
    assert_eq!(
        a.probes.load(Ordering::SeqCst),
        1,
        "reload must preserve next probe deadline"
    );
    until(|| a.probes.load(Ordering::SeqCst) >= 2).await;
    cli.close().await;
    a.close().await;
}
