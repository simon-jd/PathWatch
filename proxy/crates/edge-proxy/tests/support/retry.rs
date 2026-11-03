//! Owned raw HTTP/1 fixtures: a first 503, then a complete or deliberately open body.
use edge_proxy::Shutdown;
use std::{
    collections::HashMap,
    net::SocketAddr,
    sync::{
        Arc, Mutex,
        atomic::{AtomicUsize, Ordering},
    },
};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::{TcpListener, TcpStream},
    task::{JoinHandle, JoinSet},
};

#[derive(Default)]
pub struct Observed {
    pub paths: Mutex<HashMap<String, usize>>,
    pub live: AtomicUsize,
    pub peak: AtomicUsize,
    pub accepted: AtomicUsize,
    pub closed: AtomicUsize,
}
impl Observed {
    pub fn calls(&self, path: &str) -> usize {
        *self.paths.lock().unwrap().get(path).unwrap_or(&0)
    }
    pub fn attempts(&self) -> usize {
        self.paths.lock().unwrap().values().sum()
    }
}
struct SocketLease(Arc<Observed>);
impl Drop for SocketLease {
    fn drop(&mut self) {
        self.0.live.fetch_sub(1, Ordering::SeqCst);
        self.0.closed.fetch_add(1, Ordering::SeqCst);
    }
}
pub struct Backend {
    pub addr: SocketAddr,
    stop: Shutdown,
    task: Option<JoinHandle<()>>,
}
impl Backend {
    pub async fn start(observed: Arc<Observed>) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let stop = Shutdown::new();
        let cancel = stop.clone();
        let task = tokio::spawn(async move {
            let mut children = JoinSet::new();
            loop {
                tokio::select! {
                    biased;
                    _ = cancel.cancelled() => break,
                    Some(result) = children.join_next(), if !children.is_empty() => { result.unwrap(); }
                    accepted = listener.accept() => {
                        let (io, _) = accepted.unwrap();
                        observed.accepted.fetch_add(1, Ordering::SeqCst);
                        let live = observed.live.fetch_add(1, Ordering::SeqCst) + 1;
                        observed.peak.fetch_max(live, Ordering::SeqCst);
                        let observed = observed.clone();
                        children.spawn(async move {
                            let _lease = SocketLease(observed.clone());
                            connection(io, addr, observed).await;
                        });
                    }
                }
            }
            children.abort_all();
            while children.join_next().await.is_some() {}
        });
        Self {
            addr,
            stop,
            task: Some(task),
        }
    }
    pub async fn close(mut self) {
        self.stop.cancel();
        self.task.take().unwrap().await.unwrap();
    }
}
impl Drop for Backend {
    fn drop(&mut self) {
        self.stop.cancel();
        if let Some(task) = &self.task {
            task.abort();
        }
    }
}
async fn connection(mut io: TcpStream, addr: SocketAddr, observed: Arc<Observed>) {
    let mut buffered = Vec::new();
    loop {
        let end = loop {
            if let Some(i) = buffered.windows(4).position(|w| w == b"\r\n\r\n") {
                break i + 4;
            }
            let mut bytes = [0; 2048];
            match io.read(&mut bytes).await {
                Ok(0) | Err(_) => return,
                Ok(n) => buffered.extend_from_slice(&bytes[..n]),
            }
            assert!(buffered.len() <= 8192, "bounded fixture headers");
        };
        let headers = String::from_utf8(buffered.drain(..end).collect()).unwrap();
        let path = headers
            .lines()
            .next()
            .unwrap()
            .split_whitespace()
            .nth(1)
            .unwrap()
            .to_owned();
        let attempt = {
            let mut paths = observed.paths.lock().unwrap();
            let n = paths.entry(path.clone()).or_default();
            *n += 1;
            *n
        };
        let success = path.starts_with("/ok") || attempt > 1;
        if !success {
            if io
                .write_all(b"HTTP/1.1 503 Unavailable\r\nContent-Length: 0\r\n\r\n")
                .await
                .is_err()
            {
                return;
            }
        } else if path.starts_with("/hold") {
            let headers = format!(
                "HTTP/1.1 200 OK\r\nX-Backend: {addr}\r\nTransfer-Encoding: chunked\r\n\r\n1\r\nx\r\n"
            );
            if io.write_all(headers.as_bytes()).await.is_err() {
                return;
            }
            // No terminating chunk: this real socket must close when downstream cancels.
            let mut byte = [0];
            let result = io.read(&mut byte).await;
            assert!(
                matches!(result, Ok(0) | Err(_)),
                "held response socket reused before EOF"
            );
            return;
        } else {
            let headers =
                format!("HTTP/1.1 200 OK\r\nX-Backend: {addr}\r\nContent-Length: 0\r\n\r\n");
            if io.write_all(headers.as_bytes()).await.is_err() {
                return;
            }
        }
    }
}
