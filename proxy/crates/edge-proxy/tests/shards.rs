mod common;
use common::*;
use std::time::Duration;

/// Start `edge_proxy::run_file` (the CLI entry point) on its own thread with the
/// given `[runtime]` section and a pool cap of one upstream socket.
fn launch(
    runtime: &str,
    upstream: std::net::SocketAddr,
) -> (
    std::net::SocketAddr,
    std::path::PathBuf,
    edge_proxy::Shutdown,
    std::thread::JoinHandle<anyhow::Result<()>>,
) {
    let addr = address();
    let path = std::env::temp_dir().join(format!("pw-shard-{}.toml", addr.port()));
    std::fs::write(
        &path,
        format!(
            r#"
listeners = [{{ addr = "{addr}" }}]
routes = [{{ host = "test.local", path_prefix = "/", cluster = "echo" }}]
clusters = [{{ name = "echo", endpoints = ["http://{upstream}"], lb = "least_request" }}]
[pool]
max_connections = 1
request_timeout_ms = 5000
drain_timeout_ms = 1000
{runtime}
"#
        ),
    )
    .unwrap();
    let stop = edge_proxy::Shutdown::new();
    let stop_thread = stop.clone();
    let path_thread = path.clone();
    let worker = std::thread::spawn(move || edge_proxy::run_file(path_thread, stop_thread));
    (addr, path, stop, worker)
}

/// Sequential requests, each on a fresh downstream connection, so SO_REUSEPORT
/// spreads them over the runtimes. Returns how many upstream sockets were opened.
async fn upstream_sockets_after_requests(runtime: &str) -> usize {
    let upstream = Upstream::start().await;
    let (addr, path, stop, worker) = launch(runtime, upstream.addr);
    tokio::time::timeout(Duration::from_secs(2), async {
        loop {
            if tokio::net::TcpStream::connect(addr).await.is_ok() {
                break;
            }
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
    })
    .await
    .expect("proxy listening");
    for _ in 0..64 {
        let (mut client, driver) = h2(addr).await;
        check(
            client
                .send_request(request("http://test.local/echo"))
                .await
                .unwrap(),
        )
        .await;
        drop(client);
        driver.abort();
    }
    stop.cancel();
    tokio::task::spawn_blocking(move || worker.join().unwrap().unwrap())
        .await
        .unwrap();
    let sockets = upstream.count();
    upstream.close().await;
    let _ = std::fs::remove_file(path);
    sockets
}

#[tokio::test]
async fn per_core_runtimes_each_keep_their_own_pool() {
    // Two runtimes, cap one socket each: both accept (64 connections over the
    // reuseport hash), and each opens its own upstream socket.
    let sockets =
        upstream_sockets_after_requests("[runtime]\nmode = \"per_core\"\nworkers = 2").await;
    assert_eq!(sockets, 2, "one upstream socket per runtime");
}

#[tokio::test]
async fn multi_thread_runtime_shares_one_pool() {
    let sockets =
        upstream_sockets_after_requests("[runtime]\nmode = \"multi_thread\"\nworkers = 2").await;
    assert_eq!(sockets, 1, "one shared pool");
}
