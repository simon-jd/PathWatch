#![allow(dead_code)]
use bytes::Bytes;
use edge_proxy::Shutdown;
use http_body_util::Full;
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
        atomic::{AtomicBool, AtomicUsize, Ordering},
    },
    time::Duration,
};
use tokio::{
    net::TcpListener,
    task::{JoinHandle, JoinSet},
};
pub struct Backend {
    pub addr: SocketAddr,
    pub calls: Arc<AtomicUsize>,
    pub active: Arc<AtomicUsize>,
    pub status: Arc<AtomicUsize>,
    pub health_ok: Arc<AtomicBool>,
    pub probes: Arc<AtomicUsize>,
    stop: Shutdown,
    task: JoinHandle<()>,
}
impl Backend {
    pub async fn start(delay: Duration, status: usize) -> Self {
        let socket = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = socket.local_addr().unwrap();
        let calls = Arc::new(AtomicUsize::new(0));
        let active = Arc::new(AtomicUsize::new(0));
        let status = Arc::new(AtomicUsize::new(status));
        let probes = Arc::new(AtomicUsize::new(0));
        let health_ok = Arc::new(AtomicBool::new(true));
        let stop = Shutdown::new();
        let (c, a, s, p, h, cancel) = (
            calls.clone(),
            active.clone(),
            status.clone(),
            probes.clone(),
            health_ok.clone(),
            stop.clone(),
        );
        let task = tokio::spawn(async move {
            let mut tasks = JoinSet::new();
            loop {
                tokio::select! {
                 _=cancel.cancelled()=>break,
                 Some(_)=tasks.join_next(),if !tasks.is_empty()=>{},
                 v=socket.accept()=>{let (io,_)=v.unwrap();let (c,a,s,p,h)=(c.clone(),a.clone(),s.clone(),p.clone(),h.clone());
                  tasks.spawn(async move {
                   let service=service_fn(move |req:Request<Incoming>| {
                    let (c,a,s,p,h)=(c.clone(),a.clone(),s.clone(),p.clone(),h.clone());
                    async move {
                     if req.uri().path()=="/healthz" {p.fetch_add(1,Ordering::SeqCst);return Ok::<_,Infallible>(Response::builder().status(if h.load(Ordering::SeqCst){200}else{503}).body(Full::new(Bytes::new())).unwrap());}
                     c.fetch_add(1,Ordering::SeqCst);a.fetch_add(1,Ordering::SeqCst);
                     struct Active(Arc<AtomicUsize>);impl Drop for Active{fn drop(&mut self){self.0.fetch_sub(1,Ordering::SeqCst);}}
                     let _active=Active(a);tokio::time::sleep(delay).await;
                     Ok(Response::builder().status(s.load(Ordering::SeqCst) as u16).header("x-backend",addr.to_string()).body(Full::new(Bytes::from_static(b"done"))).unwrap())
                    }
                   });let _=Builder::new(TokioExecutor::new()).serve_connection(TokioIo::new(io),service).await;
                  });
                 }
                }
            }
            tasks.abort_all();
            while tasks.join_next().await.is_some() {}
        });
        Self {
            addr,
            calls,
            active,
            status,
            probes,
            health_ok,
            stop,
            task,
        }
    }
    pub async fn close(self) {
        self.stop.cancel();
        self.task.await.unwrap();
    }
}
pub async fn until(f: impl Fn() -> bool) {
    tokio::time::timeout(Duration::from_secs(3), async {
        while !f() {
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
    })
    .await
    .expect("condition reached");
}
