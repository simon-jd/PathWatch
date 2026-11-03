//! Atomically replace validated generations while retaining the bound listeners.
use crate::{Shutdown, listener, route::Router, upstream::Pool};
use bytes::Bytes;
use http_body_util::Full;
use hyper_util::{
    rt::{TokioIo, TokioTimer},
    server::conn::auto::Builder,
};
use std::{
    convert::Infallible,
    path::PathBuf,
    sync::{
        Arc, RwLock,
        atomic::{AtomicU64, Ordering},
    },
    time::Duration,
};
use tokio::{net::TcpListener, task::JoinSet};
use tokio_rustls::TlsAcceptor;
pub(crate) struct Generation {
    pub router: Arc<Router>,
    pub tls: Vec<Option<TlsAcceptor>>,
    pub stop: Shutdown,
}
pub(crate) struct State {
    pub current: RwLock<Arc<Generation>>,
    registry: Arc<crate::admission::Registry>,
    success: AtomicU64,
    failure: AtomicU64,
}
fn certificates(cfg: &config::Config) -> anyhow::Result<Vec<Option<TlsAcceptor>>> {
    listener::validate_runtime(cfg)?;
    cfg.listeners
        .iter()
        .map(|l| l.tls.as_ref().map(listener::tls_config).transpose())
        .collect()
}
async fn replace(
    state: &State,
    cfg: Arc<config::Config>,
    pool: Arc<Pool>,
    stop: &Shutdown,
) -> anyhow::Result<()> {
    let tls = certificates(&cfg)?;
    let old = state.current.read().unwrap().clone();
    anyhow::ensure!(
        cfg.listeners
            .iter()
            .map(|l| l.addr)
            .eq(old.router.cfg.listeners.iter().map(|l| l.addr)),
        "reload cannot change bound listener addresses/order"
    );
    anyhow::ensure!(
        cfg.management.as_ref().map(|m| m.addr)
            == old.router.cfg.management.as_ref().map(|m| m.addr),
        "reload cannot change management address"
    );
    anyhow::ensure!(
        cfg.pool.max_connections == old.router.cfg.pool.max_connections,
        "reload cannot change global physical capacity"
    );
    anyhow::ensure!(
        cfg.runtime == old.router.cfg.runtime,
        "reload cannot change the runtime"
    );
    old.router.close_health().await;
    let generation = Arc::new(Generation {
        router: Arc::new(Router::with_pool(cfg, pool, &state.registry)),
        tls,
        stop: stop.child_token(),
    });
    *state.current.write().unwrap() = generation;
    old.stop.cancel();
    Ok(())
}
pub async fn serve(
    cfg: Arc<config::Config>,
    shutdown: Shutdown,
    path: Option<PathBuf>,
) -> anyhow::Result<()> {
    let tls = certificates(&cfg)?;
    // Install before listening, so the CLI never has a listening-but-unhandled SIGHUP window.
    let mut hup = if path.is_some() {
        Some(tokio::signal::unix::signal(
            tokio::signal::unix::SignalKind::hangup(),
        )?)
    } else {
        None
    };
    let mut listeners = Vec::new();
    for l in &cfg.listeners {
        listeners.push(TcpListener::bind(l.addr).await?);
    }
    let management = if let Some(m) = &cfg.management {
        Some(TcpListener::bind(m.addr).await?)
    } else {
        None
    };
    let stop = shutdown.child_token();
    let pool = Pool::new(cfg.pool.max_connections);
    let registry = Arc::new(crate::admission::Registry::default());
    let generation = Arc::new(Generation {
        router: Arc::new(Router::with_pool(cfg.clone(), pool.clone(), &registry)),
        tls,
        stop: stop.child_token(),
    });
    let state = Arc::new(State {
        current: RwLock::new(generation),
        registry,
        success: AtomicU64::new(0),
        failure: AtomicU64::new(0),
    });
    let mut tasks = JoinSet::new();
    for (i, socket) in listeners.into_iter().enumerate() {
        tasks.spawn(listener::run(
            socket,
            state.clone(),
            i,
            stop.clone(),
            Duration::from_millis(cfg.pool.drain_timeout_ms),
        ));
    }
    if let Some(socket) = management {
        tasks.spawn(manage(socket, state.clone(), stop.clone()));
    }
    let result = loop {
        let next_signal = async {
            match &mut hup {
                Some(signal) => {
                    signal.recv().await;
                }
                None => std::future::pending::<()>().await,
            }
        };
        tokio::select! {
            biased;
            _ = shutdown.cancelled() => break Ok(()),
            task = tasks.join_next() => {
                break match task {
                    Some(Ok(Err(e))) => Err(e),
                    Some(Err(e)) => Err(e.into()),
                    _ => Err(anyhow::anyhow!("listener stopped unexpectedly")),
                };
            }
            _ = next_signal => {
                let candidate = std::fs::read_to_string(path.as_ref().unwrap())
                    .map_err(anyhow::Error::from)
                    .and_then(|s| config::Config::parse(&s));
                let result = match candidate {
                    Ok(c) => replace(&state, Arc::new(c), pool.clone(), &stop).await,
                    Err(e) => Err(e),
                };
                match result {
                    Ok(()) => { state.success.fetch_add(1, Ordering::Relaxed); }
                    Err(e) => {
                        state.failure.fetch_add(1, Ordering::Relaxed);
                        eprintln!("reload rejected: {e}");
                    }
                }
            }
        }
    };
    stop.cancel();
    let current = state.current.read().unwrap().clone();
    current.router.close_health().await;
    while tasks.join_next().await.is_some() {}
    // Every downstream exchange has dropped before closing retained idle upstream sockets.
    pool.close().await;
    result
}

#[derive(Clone)]
struct Published {
    generation: u64,
    cfg: Arc<config::Config>,
}
impl PartialEq for Published {
    fn eq(&self, other: &Self) -> bool {
        self.generation == other.generation
    }
}

/// One current-thread runtime per worker. Each binds the same listeners with
/// SO_REUSEPORT and keeps its own upstream pool, so a request's tasks stay on
/// the core that accepted it. Selected by `[runtime] mode = "per_core"`; the
/// default is the single multi-thread runtime in `serve`.
pub(crate) fn serve_sharded(
    path: PathBuf,
    shutdown: Shutdown,
    workers: usize,
) -> anyhow::Result<()> {
    anyhow::ensure!(workers >= 1, "worker count");
    let initial = Arc::new(config::Config::parse(&std::fs::read_to_string(&path)?)?);
    let (tx, rx) = tokio::sync::watch::channel(Published {
        generation: 0,
        cfg: initial,
    });
    let mut joins = Vec::with_capacity(workers);
    for index in 0..workers {
        let shutdown = shutdown.clone();
        let rx = rx.clone();
        let tx = if index == 0 { Some(tx.clone()) } else { None };
        let path = path.clone();
        joins.push(
            std::thread::Builder::new()
                .name(format!("pw-rt-{index}"))
                .spawn(move || {
                    let rt = tokio::runtime::Builder::new_current_thread()
                        .enable_all()
                        .build()?;
                    rt.block_on(serve_shard(shutdown, index, path, tx, rx))
                })?,
        );
    }
    drop(tx);
    drop(rx);
    let mut result = Ok(());
    for join in joins {
        match join.join() {
            Ok(Ok(())) => {}
            Ok(Err(error)) => {
                shutdown.cancel();
                if result.is_ok() {
                    result = Err(error);
                }
            }
            Err(_) => {
                shutdown.cancel();
                if result.is_ok() {
                    result = Err(anyhow::anyhow!("proxy runtime panicked"));
                }
            }
        }
    }
    result
}

async fn serve_shard(
    shutdown: Shutdown,
    index: usize,
    path: PathBuf,
    reload_tx: Option<tokio::sync::watch::Sender<Published>>,
    mut updates: tokio::sync::watch::Receiver<Published>,
) -> anyhow::Result<()> {
    let cfg = updates.borrow().cfg.clone();
    let tls = certificates(&cfg)?;
    let mut listeners = Vec::new();
    for listener in &cfg.listeners {
        listeners.push(crate::listener::bind_shared(listener.addr)?);
    }
    let management = if index == 0 {
        if let Some(management) = &cfg.management {
            Some(TcpListener::bind(management.addr).await?)
        } else {
            None
        }
    } else {
        None
    };
    let mut hup = if reload_tx.is_some() {
        Some(tokio::signal::unix::signal(
            tokio::signal::unix::SignalKind::hangup(),
        )?)
    } else {
        None
    };
    let stop = shutdown.child_token();
    // The configured cap is per runtime. Workers do not share sockets, so the
    // process total is workers times this cap.
    let pool = Pool::new(cfg.pool.max_connections);
    let registry = Arc::new(crate::admission::Registry::default());
    let generation = Arc::new(Generation {
        router: Arc::new(Router::with_pool(cfg.clone(), pool.clone(), &registry)),
        tls,
        stop: stop.child_token(),
    });
    let state = Arc::new(State {
        current: RwLock::new(generation),
        registry,
        success: AtomicU64::new(0),
        failure: AtomicU64::new(0),
    });
    let mut tasks = JoinSet::new();
    for (i, socket) in listeners.into_iter().enumerate() {
        tasks.spawn(listener::run(
            socket,
            state.clone(),
            i,
            stop.clone(),
            Duration::from_millis(cfg.pool.drain_timeout_ms),
        ));
    }
    if let Some(socket) = management {
        tasks.spawn(manage(socket, state.clone(), stop.clone()));
    }
    let result = loop {
        let next_signal = async {
            match &mut hup {
                Some(signal) => {
                    signal.recv().await;
                }
                None => std::future::pending::<()>().await,
            }
        };
        tokio::select! {
            biased;
            _ = shutdown.cancelled() => break Ok(()),
            task = tasks.join_next() => {
                break match task {
                    Some(Ok(Err(e))) => Err(e),
                    Some(Err(e)) => Err(e.into()),
                    _ => Err(anyhow::anyhow!("listener stopped unexpectedly")),
                };
            }
            result = updates.changed() => {
                if result.is_err() { continue; }
                let next = updates.borrow().cfg.clone();
                match replace(&state, next, pool.clone(), &stop).await {
                    Ok(()) => { state.success.fetch_add(1, Ordering::Relaxed); }
                    Err(e) => {
                        state.failure.fetch_add(1, Ordering::Relaxed);
                        eprintln!("reload rejected: {e}");
                    }
                }
            }
            _ = next_signal => {
                let tx = reload_tx.as_ref().unwrap();
                let candidate = std::fs::read_to_string(&path)
                    .map_err(anyhow::Error::from)
                    .and_then(|s| config::Config::parse(&s));
                match candidate {
                    Ok(c) => {
                        let generation = tx.borrow().generation + 1;
                        let _ = tx.send(Published { generation, cfg: Arc::new(c) });
                    }
                    Err(e) => {
                        state.failure.fetch_add(1, Ordering::Relaxed);
                        eprintln!("reload rejected: {e}");
                    }
                }
            }
        }
    };
    stop.cancel();
    let current = state.current.read().unwrap().clone();
    current.router.close_health().await;
    while tasks.join_next().await.is_some() {}
    pool.close().await;
    result
}

async fn manage(socket: TcpListener, state: Arc<State>, stop: Shutdown) -> anyhow::Result<()> {
    let mut tasks = JoinSet::new();
    let permits = Arc::new(tokio::sync::Semaphore::new(16));
    let result = loop {
        tokio::select! {
            biased;
            _ = stop.cancelled() => break Ok(()),
            Some(_) = tasks.join_next(), if !tasks.is_empty() => {},
            accepted = socket.accept() => {
                let (io, _) = match accepted { Ok(v) => v, Err(e) => break Err(e.into()) };
                let Ok(permit) = permits.clone().try_acquire_owned() else { continue; };
                let state = state.clone();
                tasks.spawn(async move {
                    let _permit = permit;
                    let service = hyper::service::service_fn(move |req: hyper::Request<hyper::body::Incoming>| {
                        let mut text = String::new();
                        let status = if req.method() == http::Method::GET && req.uri().path() == "/metrics" {
                            let generation = state.current.read().unwrap().clone();
                            for (name,state) in state.registry.snapshot(){state.metrics(&name,&mut text);}
                            for c in &generation.router.clusters { c.health_metrics(&mut text); }
                            text.push_str(&format!(
                                "pathwatch_proxy_reload_success_total {}\npathwatch_proxy_reload_failure_total {}\n",
                                state.success.load(Ordering::Relaxed), state.failure.load(Ordering::Relaxed)
                            ));
                            200
                        } else { 404 };
                        async move {
                            Ok::<_, Infallible>(http::Response::builder().status(status)
                                .header("content-type", "text/plain; version=0.0.4; charset=utf-8")
                                .body(Full::new(Bytes::from(text))).unwrap())
                        }
                    });
                    let owner = crate::transport::ConnectionTasks::new();
                    let mut builder = Builder::new(owner.executor());
                    builder.http1().timer(TokioTimer::new()).header_read_timeout(Duration::from_secs(5));
                    let _ = tokio::time::timeout(Duration::from_secs(5),
                        builder.http1_only().serve_connection(TokioIo::new(io), service)).await;
                    owner.close().await;
                });
            }
        }
    };
    tasks.abort_all();
    while tasks.join_next().await.is_some() {}
    result
}
