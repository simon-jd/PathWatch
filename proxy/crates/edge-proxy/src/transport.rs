//! Own every Hyper transport/upload task independently of retained body handles.
use hyper::rt::Executor;
use std::{
    future::Future,
    pin::Pin,
    sync::{Arc, Mutex},
    task::{Context, Poll},
};
use tokio::{
    io::{AsyncRead, AsyncWrite, ReadBuf},
    net::TcpStream,
    sync::OwnedSemaphorePermit,
};
use tokio_util::{sync::CancellationToken, task::TaskTracker};

struct State {
    // Serialize registration with closing: TaskTracker::close alone permits spawn.
    closed: Mutex<bool>,
    cancel: CancellationToken,
    tracker: TaskTracker,
}
#[derive(Clone)]
pub struct ConnectionExecutor(Arc<State>);
impl<F> Executor<F> for ConnectionExecutor
where
    F: Future + Send + 'static,
    F::Output: Send + 'static,
{
    fn execute(&self, future: F) {
        let closed = self.0.closed.lock().unwrap();
        if *closed {
            // Drop the unspawned future outside the lock (it may own an executor).
            drop(closed);
            drop(future);
            return;
        }
        let cancel = self.0.cancel.clone();
        self.0.tracker.spawn(async move {
            tokio::select! {
                biased;
                _ = cancel.cancelled() => {},
                _ = future => {},
            }
        });
    }
}
/// Non-cloned guard: executor clones held by tasks never own cancellation.
pub struct ConnectionTasks(ConnectionExecutor);
impl ConnectionTasks {
    pub fn new() -> Self {
        Self(ConnectionExecutor(Arc::new(State {
            closed: Mutex::new(false),
            cancel: CancellationToken::new(),
            tracker: TaskTracker::new(),
        })))
    }
    pub fn executor(&self) -> ConnectionExecutor {
        self.0.clone()
    }
    pub fn cancel(&self) {
        let mut closed = self.0.0.closed.lock().unwrap();
        *closed = true;
        self.0.0.cancel.cancel();
        self.0.0.tracker.close();
    }
    pub async fn close(&self) {
        self.cancel();
        // Wait includes future destructors, hence actual socket/permit release.
        self.0.0.tracker.wait().await;
    }
}
impl Drop for ConnectionTasks {
    fn drop(&mut self) {
        self.cancel();
    }
}

// Field drop order closes the TCP socket before returning its physical permit.
// This also covers cancellation during replacement/join and hyper's h2 tasks.
pub struct PhysicalSocket {
    pub socket: TcpStream,
    pub _permit: OwnedSemaphorePermit,
}
impl AsyncRead for PhysicalSocket {
    fn poll_read(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &mut ReadBuf<'_>,
    ) -> Poll<std::io::Result<()>> {
        Pin::new(&mut self.socket).poll_read(cx, buf)
    }
}
impl AsyncWrite for PhysicalSocket {
    fn poll_write(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &[u8],
    ) -> Poll<std::io::Result<usize>> {
        Pin::new(&mut self.socket).poll_write(cx, buf)
    }
    fn poll_flush(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<std::io::Result<()>> {
        Pin::new(&mut self.socket).poll_flush(cx)
    }
    fn poll_shutdown(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<std::io::Result<()>> {
        Pin::new(&mut self.socket).poll_shutdown(cx)
    }
}
