use crate::{Shutdown, route::Router};
use anyhow::Context;
use hyper::service::service_fn;
use hyper_util::{
    rt::{TokioIo, TokioTimer},
    server::conn::auto::Builder,
};
use rustls::{ServerConfig, server::ResolvesServerCertUsingSni, sign::CertifiedKey};
use std::net::SocketAddr;
use std::{fs::File, io::BufReader, sync::Arc, time::Duration};
use tokio::{
    io::{AsyncRead, AsyncWrite},
    net::{TcpListener, TcpSocket},
    task::JoinSet,
};
use tokio_rustls::TlsAcceptor;

pub fn tls_config(tls: &config::Tls) -> anyhow::Result<TlsAcceptor> {
    let provider = rustls::crypto::ring::default_provider();
    let mut resolver = ResolvesServerCertUsingSni::new();
    for entry in &tls.certs {
        let certs = rustls_pemfile::certs(&mut BufReader::new(
            File::open(&entry.cert).context("open TLS certificate")?,
        ))
        .collect::<Result<Vec<_>, _>>()?;
        let key = rustls_pemfile::private_key(&mut BufReader::new(
            File::open(&entry.key).context("open TLS private key")?,
        ))?
        .context("missing private key")?;
        let certified = CertifiedKey::from_der(certs, key, &provider)?;
        certified.keys_match()?;
        resolver.add(&config::normalize_host(&entry.sni)?, certified)?;
    }
    let mut server = ServerConfig::builder_with_provider(Arc::new(provider))
        .with_safe_default_protocol_versions()?
        .with_no_client_auth()
        .with_cert_resolver(Arc::new(resolver));
    server.alpn_protocols = vec![b"h2".to_vec(), b"http/1.1".to_vec()];
    Ok(TlsAcceptor::from(Arc::new(server)))
}

/// Bind with `SO_REUSEPORT` so each current-thread runtime accepts on the same address.
/// A connection stays on the runtime that accepted it, instead of being stolen across workers.
pub fn bind_shared(addr: SocketAddr) -> std::io::Result<TcpListener> {
    let socket = if addr.is_ipv4() {
        TcpSocket::new_v4()?
    } else {
        TcpSocket::new_v6()?
    };
    // SO_REUSEADDR as `TcpListener::bind` sets it: TIME_WAIT sockets left on the port
    // by an earlier process must not block the bind.
    socket.set_reuseaddr(true)?;
    socket.set_reuseport(true)?;
    socket.bind(addr)?;
    socket.listen(1024)
}

pub async fn run(
    listener: TcpListener,
    state: Arc<crate::reload::State>,
    index: usize,
    stop: Shutdown,
    drain: Duration,
) -> anyhow::Result<()> {
    let mut connections = JoinSet::new();
    let result = loop {
        tokio::select! {
            biased;
            _ = stop.cancelled() => break Ok(()),
            Some(_) = connections.join_next(), if !connections.is_empty() => {},
            accepted = listener.accept() => {
                let (socket, _) = match accepted {Ok(value)=>value,Err(error)=>break Err(error.into())};
                // Without this, Nagle holds a second H2 response behind the client's delayed ACK.
                // A failure means the peer is already gone; the connection then fails on its own.
                let _ = socket.set_nodelay(true);
                let generation=state.current.read().unwrap().clone();
                let tls=generation.tls[index].clone();
                let request_timeout=Duration::from_millis(generation.router.cfg.pool.request_timeout_ms);
                connections.spawn(async move {
                    let router=generation.router.clone();let stop=generation.stop.clone();
                    if let Some(tls) = tls {
                        let stream = tokio::select! {
                            biased;
                            _ = stop.cancelled() => return,
                            result = tokio::time::timeout(request_timeout, tls.accept(socket)) => {
                                match result {Ok(Ok(stream))=>stream,_=>return}
                            }
                        };
                        let h2 = stream.get_ref().1.alpn_protocol() == Some(b"h2");
                        connection(stream, router, stop, request_timeout, Some(h2)).await;
                    } else {
                        connection(socket, router, stop, request_timeout, None).await;
                    }
                });
            }
        }
    };
    drop(listener);
    stop.cancel();
    if tokio::time::timeout(drain, async {
        while connections.join_next().await.is_some() {}
    })
    .await
    .is_err()
    {
        connections.abort_all();
        while connections.join_next().await.is_some() {}
    }
    result
}
async fn connection<I>(
    io: I,
    router: Arc<Router>,
    stop: Shutdown,
    request_timeout: Duration,
    protocol: Option<bool>,
) where
    I: AsyncRead + AsyncWrite + Unpin + Send + 'static,
{
    let tasks = crate::transport::ConnectionTasks::new();
    let mut builder = Builder::new(tasks.executor());
    builder
        .http1()
        .timer(TokioTimer::new())
        .header_read_timeout(request_timeout);
    builder.http2().max_concurrent_streams(128);
    let builder = match protocol {
        Some(true) => builder.http2_only(),
        Some(false) => builder.http1_only(),
        None => builder,
    };
    let service = service_fn(move |req| {
        let router = router.clone();
        async move { router.handle(req).await }
    });
    {
        let conn = builder.serve_connection(TokioIo::new(io), service);
        tokio::pin!(conn);
        tokio::select! {
            biased;
            _ = stop.cancelled() => {
                conn.as_mut().graceful_shutdown();
                let _ = tokio::time::timeout(Duration::from_secs(30), &mut conn).await;
            },
            _ = &mut conn => {}
        }
    }
    tasks.close().await;
}
pub fn validate_runtime(cfg: &config::Config) -> anyhow::Result<()> {
    cfg.validate()
}

#[cfg(test)]
mod tests {
    use super::*;
    use bytes::Bytes;
    use http_body_util::Empty;
    use tokio::io::AsyncReadExt;
    #[tokio::test(start_paused = true)]
    async fn h1_and_h2_drain_deadline_is_thirty_seconds() {
        for h2 in [false, true] {
            let upstream = TcpListener::bind("127.0.0.1:0").await.unwrap();
            let addr = upstream.local_addr().unwrap();
            let (started, ready) = tokio::sync::oneshot::channel();
            let upstream = tokio::spawn(async move {
                let (mut io, _) = upstream.accept().await.unwrap();
                let mut bytes = [0; 4096];
                assert!(io.read(&mut bytes).await.unwrap() > 0);
                started.send(()).unwrap();
                loop {
                    if io.read(&mut bytes).await.unwrap() == 0 {
                        break;
                    }
                }
            });
            let cfg = config::Config::parse(&format!(
                r#"listeners=[{{addr="127.0.0.1:1"}}]
routes=[{{host="test.local",path_prefix="/",cluster="echo"}}]
clusters=[{{name="echo",endpoints=["http://{addr}"]}}]
[pool]
request_timeout_ms=300000
"#
            ))
            .unwrap();
            let pool = crate::upstream::Pool::new(1);
            let router = Arc::new(Router::with_pool(
                Arc::new(cfg),
                pool.clone(),
                &crate::admission::Registry::default(),
            ));
            let (server, client) = tokio::io::duplex(65536);
            let stop = Shutdown::new();
            let server = tokio::spawn(connection(
                server,
                router,
                stop.clone(),
                Duration::from_secs(300),
                Some(h2),
            ));
            let client = tokio::spawn(async move {
                let request = http::Request::builder()
                    .uri("http://test.local/")
                    .header("host", "test.local")
                    .body(Empty::<Bytes>::new())
                    .unwrap();
                if h2 {
                    let (mut tx, c) = hyper::client::conn::http2::handshake(
                        hyper_util::rt::TokioExecutor::new(),
                        TokioIo::new(client),
                    )
                    .await
                    .unwrap();
                    let task = tokio::spawn(c);
                    let _ = tx.send_request(request).await;
                    drop(tx);
                    task.abort();
                    let _ = task.await;
                } else {
                    let (mut tx, c) = hyper::client::conn::http1::handshake(TokioIo::new(client))
                        .await
                        .unwrap();
                    let task = tokio::spawn(c);
                    let _ = tx.send_request(request).await;
                    drop(tx);
                    task.abort();
                    let _ = task.await;
                }
            });
            ready.await.unwrap();
            stop.cancel();
            tokio::task::yield_now().await;
            tokio::time::advance(Duration::from_millis(29999)).await;
            assert!(!server.is_finished());
            tokio::time::advance(Duration::from_millis(1)).await;
            server.await.unwrap();
            client.await.unwrap();
            tokio::time::timeout(Duration::from_secs(1), upstream)
                .await
                .unwrap()
                .unwrap();
            pool.close().await;
        }
    }

    #[tokio::test]
    async fn shared_bind_succeeds_while_the_port_has_time_wait_sockets() {
        // A restarted proxy (the benchmark starts one per cell) must rebind while the
        // previous process's server-closed connections sit in TIME_WAIT, including
        // ones accepted by the multi-thread runtime's plain (non-reuseport) listener.
        let first = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = first.local_addr().unwrap();
        let client = tokio::net::TcpStream::connect(addr).await.unwrap();
        let (server, _) = first.accept().await.unwrap();
        drop(server); // the server closes first, so its side enters TIME_WAIT
        let mut client = client;
        let mut buf = [0u8; 1];
        assert_eq!(client.read(&mut buf).await.unwrap(), 0);
        drop(client);
        drop(first);
        bind_shared(addr).expect("rebind with TIME_WAIT sockets on the port");
    }

    #[tokio::test]
    async fn reuseport_two_sockets_both_accept() {
        let first = bind_shared("127.0.0.1:0".parse().unwrap()).unwrap();
        let addr = first.local_addr().unwrap();
        let second = bind_shared(addr).unwrap();
        assert_eq!(second.local_addr().unwrap(), addr);
        let saw_first = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
        let saw_second = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
        let flag = saw_first.clone();
        let first = tokio::spawn(async move {
            loop {
                first.accept().await.unwrap();
                flag.store(true, std::sync::atomic::Ordering::SeqCst);
            }
        });
        let flag = saw_second.clone();
        let second = tokio::spawn(async move {
            loop {
                second.accept().await.unwrap();
                flag.store(true, std::sync::atomic::Ordering::SeqCst);
            }
        });
        let mut held = Vec::new();
        for _ in 0..64 {
            if saw_first.load(std::sync::atomic::Ordering::SeqCst)
                && saw_second.load(std::sync::atomic::Ordering::SeqCst)
            {
                break;
            }
            if let Ok(stream) = tokio::net::TcpStream::connect(addr).await {
                held.push(stream);
            }
            tokio::task::yield_now().await;
        }
        first.abort();
        second.abort();
        assert!(
            saw_first.load(std::sync::atomic::Ordering::SeqCst)
                && saw_second.load(std::sync::atomic::Ordering::SeqCst),
            "both reuseport sockets must accept"
        );
        drop(held);
    }
}
