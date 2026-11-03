#![allow(dead_code)]
use bytes::Bytes;
use edge_proxy::Shutdown;
use http_body_util::{BodyExt, Full};
use hyper::{Request, Response, body::Incoming, service::service_fn};
use hyper_util::{
    rt::{TokioExecutor, TokioIo},
    server::conn::auto::Builder,
};
use std::{
    convert::Infallible,
    net::SocketAddr,
    sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    },
    time::Duration,
};
use tokio::{
    net::{TcpListener, TcpStream},
    task::{JoinHandle, JoinSet},
};
pub type Body = Full<Bytes>;
pub struct Upstream {
    pub addr: SocketAddr,
    pub accepts: Arc<AtomicUsize>,
    pub closed: Arc<AtomicUsize>,
    stop: Shutdown,
    task: JoinHandle<()>,
}
impl Upstream {
    pub async fn start() -> Self {
        Self::with_delay(Duration::ZERO).await
    }
    pub async fn with_delay(delay: Duration) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let accepts = Arc::new(AtomicUsize::new(0));
        let count = accepts.clone();
        let closed = Arc::new(AtomicUsize::new(0));
        let closes = closed.clone();
        let stop = Shutdown::new();
        let cancel = stop.clone();
        let task = tokio::spawn(async move {
            let mut tasks = JoinSet::new();
            loop {
                tokio::select! {
                 _=cancel.cancelled()=>break,
                 Some(_)=tasks.join_next(), if !tasks.is_empty()=>{},
                 accepted=listener.accept()=>{ let(s,_)=accepted.unwrap(); count.fetch_add(1,Ordering::SeqCst); let closes=closes.clone(); tasks.spawn(async move {
                  let service=service_fn(move |req: Request<Incoming>| async move {
                   tokio::time::sleep(delay).await;
                   let version=format!("{:?}",req.version()); let uri=req.uri().path_and_query().unwrap().to_string(); let host=req.headers().get("host").map(|v|v.to_str().unwrap().to_string()).unwrap_or_default();
                   let leaked=req.headers().contains_key("x-request-hop") || req.headers().contains_key("proxy-connection");
                   let body=req.into_body().collect().await.unwrap().to_bytes();
                   let body=if uri=="/large-response" { Bytes::from(vec![b'x';16*1024*1024]) } else { body };
                   Ok::<_,Infallible>(Response::builder().status(201).header("x-path",uri).header("x-upstream-version",version).header("x-host",host).header("x-leaked",leaked.to_string()).header("connection","x-response-hop").header("x-response-hop","remove").body(Full::new(body)).unwrap())
                  }); let _=Builder::new(TokioExecutor::new()).serve_connection(TokioIo::new(s),service).await; closes.fetch_add(1, Ordering::SeqCst);
                 }); }
                }
            }
            tasks.abort_all();
            while tasks.join_next().await.is_some() {}
        });
        Self {
            addr,
            accepts,
            closed,
            stop,
            task,
        }
    }
    pub fn count(&self) -> usize {
        self.accepts.load(Ordering::SeqCst)
    }
    pub async fn close(self) {
        self.stop.cancel();
        self.task.await.unwrap();
    }
}
/// Reserve a loopback port for a listener that binds later. Ports are never handed out twice
/// within one test binary, so two parallel tests (or one config's listener and management
/// address) cannot be given the same free port.
pub fn address() -> SocketAddr {
    static ISSUED: std::sync::Mutex<Vec<u16>> = std::sync::Mutex::new(Vec::new());
    let mut issued = ISSUED.lock().unwrap();
    loop {
        let s = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = s.local_addr().unwrap();
        if !issued.contains(&addr.port()) {
            issued.push(addr.port());
            return addr;
        }
    }
}
/// A loopback address whose port is bound but never listening: connections to it are refused,
/// and no other listener can take the port while the returned socket is alive.
pub fn refused_address() -> (tokio::net::TcpSocket, SocketAddr) {
    let socket = tokio::net::TcpSocket::new_v4().unwrap();
    socket.bind("127.0.0.1:0".parse().unwrap()).unwrap();
    let addr = socket.local_addr().unwrap();
    (socket, addr)
}
pub fn config(upstream: SocketAddr) -> config::Config {
    config::Config::parse(&format!(
        r#"
listeners = [{{ addr = "{}" }}]
routes = [{{ host = "test.local", path_prefix = "/", cluster = "echo" }}]
clusters = [{{ name = "echo", endpoints = ["http://{upstream}"], lb = "least_request" }}]
[pool]
max_connections = 4
request_timeout_ms = 5000
drain_timeout_ms = 1000
"#,
        address()
    ))
    .unwrap()
}
pub struct Proxy {
    pub addr: SocketAddr,
    pub stop: Shutdown,
    pub task: JoinHandle<anyhow::Result<()>>,
}
impl Proxy {
    pub async fn start(cfg: config::Config) -> Self {
        let addr = cfg.listeners[0].addr;
        let stop = Shutdown::new();
        let task = tokio::spawn(edge_proxy::serve(Arc::new(cfg), stop.clone()));
        tokio::time::timeout(Duration::from_secs(2), async {
            loop {
                if TcpStream::connect(addr).await.is_ok() {
                    break;
                }
                assert!(!task.is_finished(), "proxy startup failed");
                tokio::time::sleep(Duration::from_millis(5)).await;
            }
        })
        .await
        .expect("proxy listening");
        Self { addr, stop, task }
    }
    pub async fn close(self) {
        self.stop.cancel();
        tokio::time::timeout(Duration::from_secs(3), self.task)
            .await
            .unwrap()
            .unwrap()
            .unwrap();
        assert!(TcpStream::connect(self.addr).await.is_err());
    }
}
pub async fn h1(
    addr: SocketAddr,
) -> (
    hyper::client::conn::http1::SendRequest<Body>,
    JoinHandle<()>,
) {
    let s = TcpStream::connect(addr).await.unwrap();
    let (tx, conn) = hyper::client::conn::http1::handshake(TokioIo::new(s))
        .await
        .unwrap();
    (
        tx,
        tokio::spawn(async move {
            let _ = conn.await;
        }),
    )
}
pub async fn h2(
    addr: SocketAddr,
) -> (
    hyper::client::conn::http2::SendRequest<Body>,
    JoinHandle<()>,
) {
    let s = TcpStream::connect(addr).await.unwrap();
    let (tx, conn) = hyper::client::conn::http2::handshake(TokioExecutor::new(), TokioIo::new(s))
        .await
        .unwrap();
    (
        tx,
        tokio::spawn(async move {
            let _ = conn.await;
        }),
    )
}
pub fn request(path: &str) -> Request<Body> {
    Request::builder()
        .method("POST")
        .uri(path)
        .header("host", "test.local")
        .body(Full::new(Bytes::from_static(b"hello streaming proxy")))
        .unwrap()
}
pub async fn check(response: Response<Incoming>) {
    assert_eq!(response.status(), 201);
    assert_eq!(
        response.into_body().collect().await.unwrap().to_bytes(),
        "hello streaming proxy"
    );
}
