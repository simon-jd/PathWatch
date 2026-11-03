//! A global fixed number of physical socket slots, leased through response EOF.
//! Low-level hyper senders never retry a request implicitly.
use crate::transport::{ConnectionTasks, PhysicalSocket};
use bytes::Bytes;
use config::Endpoint;
use http_body::{Body, Frame, SizeHint};
use hyper::rt::Executor;
use hyper::{Request, Response, body::Incoming};
use hyper_util::rt::TokioIo;
use std::{
    future::Future,
    pin::Pin,
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
    task::{Context, Poll},
};
use tokio::{
    net::TcpStream,
    sync::{OwnedSemaphorePermit, Semaphore},
    task::JoinHandle,
    time::{Instant, Sleep},
};

pub trait ExchangeGuard: Send {
    fn complete(&mut self);
}
impl ExchangeGuard for () {
    fn complete(&mut self) {}
}
pub type BoxError = Box<dyn std::error::Error + Send + Sync>;
enum Sender {
    H1(hyper::client::conn::http1::SendRequest<Upload>),
    H2(hyper::client::conn::http2::SendRequest<Upload>),
}
struct Connection {
    endpoint: Endpoint,
    sender: Sender,
    tasks: ConnectionTasks,
}
impl Connection {
    fn is_closed(&self) -> bool {
        match &self.sender {
            Sender::H1(tx) => tx.is_closed(),
            Sender::H2(tx) => tx.is_closed(),
        }
    }
}
#[derive(Default)]
struct Slot {
    conn: Option<Connection>,
    broken: bool,
}
pub struct Pool {
    slots: Mutex<Vec<Slot>>,
    available: Arc<Semaphore>,
    physical: Arc<Semaphore>,
}
impl Pool {
    pub fn new(cap: usize) -> Arc<Self> {
        Arc::new(Self {
            slots: Mutex::new((0..cap).map(|_| Slot::default()).collect()),
            available: Arc::new(Semaphore::new(cap)),
            physical: Arc::new(Semaphore::new(cap)),
        })
    }
    pub async fn request(
        self: &Arc<Self>,
        endpoint: &Endpoint,
        request: Request<crate::route::ProxyBody>,
        deadline: Instant,
        guard: Box<dyn ExchangeGuard>,
    ) -> anyhow::Result<Response<LeasedBody>> {
        let permit = self.available.clone().acquire_owned().await?;
        let slot = self
            .slots
            .lock()
            .unwrap()
            .pop()
            .expect("permit corresponds to a slot");
        let mut lease = Lease {
            pool: self.clone(),
            slot: Some(slot),
            permit: Some(permit),
            reusable: false,
            _guard: guard,
        };
        let slot = lease.slot.as_mut().unwrap();
        let replace = slot.broken
            || slot
                .conn
                .as_ref()
                .is_none_or(|c| c.endpoint != *endpoint || c.is_closed());
        if replace {
            if let Some(old) = slot.conn.take() {
                old.tasks.close().await;
            }
            let permit = self.physical.clone().acquire_owned().await?;
            let socket = TcpStream::connect(&endpoint.authority).await?;
            socket.set_nodelay(true)?;
            let io = TokioIo::new(PhysicalSocket {
                socket,
                _permit: permit,
            });
            // Owner exists before handshake: cancellation during handshake must
            // also stop tasks Hyper may already have spawned on the executor.
            let tasks = ConnectionTasks::new();
            let executor = tasks.executor();
            let sender = if endpoint.h2 {
                let (sender, connection) =
                    hyper::client::conn::http2::handshake(executor.clone(), io).await?;
                executor.execute(async move {
                    let _ = connection.await;
                });
                Sender::H2(sender)
            } else {
                let (sender, connection) = hyper::client::conn::http1::handshake(io).await?;
                executor.execute(async move {
                    let _ = connection.await;
                });
                Sender::H1(sender)
            };
            slot.conn = Some(Connection {
                endpoint: endpoint.clone(),
                sender,
                tasks,
            });
            slot.broken = false;
        }
        let upload_done = Arc::new(AtomicBool::new(request.body().is_end_stream()));
        let request = request.map(|body| Upload {
            body,
            complete: upload_done.clone(),
        });
        let response = match &mut slot.conn.as_mut().unwrap().sender {
            Sender::H1(tx) => {
                tx.ready().await?;
                tx.send_request(request).await?
            }
            Sender::H2(tx) => {
                tx.ready().await?;
                tx.send_request(request).await?
            }
        };
        Ok(response.map(|body| {
            let finished = body.is_end_stream();
            let lease = Arc::new(Mutex::new(if finished {
                lease._guard.complete();
                lease.reusable = upload_done.load(Ordering::Acquire);
                drop(lease);
                None
            } else {
                Some(lease)
            }));
            // Body polling can stop under downstream flow control. The deadline
            // must still abort the upstream and return the slot in that case.
            let watchdog = if finished {
                None
            } else {
                let shared = lease.clone();
                Some(tokio::spawn(async move {
                    tokio::time::sleep_until(deadline).await;
                    shared.lock().unwrap().take();
                }))
            };
            LeasedBody {
                body,
                lease,
                upload_done,
                watchdog,
                deadline: Box::pin(tokio::time::sleep_until(deadline)),
                finished,
            }
        }))
    }

    pub async fn close(&self) {
        let slots = std::mem::take(&mut *self.slots.lock().unwrap());
        for mut slot in slots {
            if let Some(conn) = slot.conn.take() {
                conn.tasks.close().await;
            }
        }
    }
}
struct Lease {
    pool: Arc<Pool>,
    slot: Option<Slot>,
    permit: Option<OwnedSemaphorePermit>,
    reusable: bool,
    _guard: Box<dyn ExchangeGuard>,
}
impl Drop for Lease {
    fn drop(&mut self) {
        let mut slot = self.slot.take().unwrap();
        if !self.reusable {
            slot.broken = true;
            if let Some(conn) = &slot.conn {
                conn.tasks.cancel();
            }
        }
        // Replacement waits for every transport/upload task before opening a socket.
        self.pool.slots.lock().unwrap().push(slot);
        self.permit.take();
    }
}
pub struct LeasedBody {
    body: Incoming,
    lease: Arc<Mutex<Option<Lease>>>,
    upload_done: Arc<AtomicBool>,
    watchdog: Option<JoinHandle<()>>,
    deadline: Pin<Box<Sleep>>,
    finished: bool,
}
impl LeasedBody {
    fn release(&mut self, reusable: bool) {
        if let Some(task) = self.watchdog.take() {
            task.abort();
        }
        if let Some(mut lease) = self.lease.lock().unwrap().take() {
            // HTTP/2 may finish the response before consuming the upload.
            // Conservatively cancel that unfinished upload/transport at response EOF.
            if reusable {
                lease._guard.complete();
            }
            lease.reusable = reusable && self.upload_done.load(Ordering::Acquire);
        }
    }
}
impl Drop for LeasedBody {
    fn drop(&mut self) {
        self.release(false);
    }
}
impl Body for LeasedBody {
    type Data = Bytes;
    type Error = BoxError;
    fn poll_frame(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
    ) -> Poll<Option<Result<Frame<Bytes>, BoxError>>> {
        if self.finished {
            return Poll::Ready(None);
        }
        if self.deadline.as_mut().poll(cx).is_ready() {
            self.finished = true;
            self.release(false);
            return Poll::Ready(Some(Err(std::io::Error::new(
                std::io::ErrorKind::TimedOut,
                "upstream body deadline",
            )
            .into())));
        }
        match Pin::new(&mut self.body).poll_frame(cx) {
            Poll::Ready(None) => {
                self.finished = true;
                self.release(true);
                Poll::Ready(None)
            }
            Poll::Ready(Some(Ok(frame))) => {
                if self.body.is_end_stream() {
                    self.finished = true;
                    self.release(true);
                }
                Poll::Ready(Some(Ok(frame)))
            }
            Poll::Ready(Some(Err(error))) => {
                self.finished = true;
                self.release(false);
                Poll::Ready(Some(Err(error.into())))
            }
            Poll::Pending => Poll::Pending,
        }
    }
    fn is_end_stream(&self) -> bool {
        self.finished || self.body.is_end_stream()
    }
    fn size_hint(&self) -> SizeHint {
        self.body.size_hint()
    }
}

/// Upload is complete only after Hyper drops its body after observing EOF.
/// Merely producing the last frame does not mean its independent pipe has ended.
struct Upload {
    body: crate::route::ProxyBody,
    complete: Arc<AtomicBool>,
}
impl Drop for Upload {
    fn drop(&mut self) {
        if self.body.is_end_stream() {
            self.complete.store(true, Ordering::Release);
        }
    }
}
impl Body for Upload {
    type Data = Bytes;
    type Error = BoxError;
    fn poll_frame(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
    ) -> Poll<Option<Result<Frame<Bytes>, BoxError>>> {
        Pin::new(&mut self.body).poll_frame(cx)
    }
    fn is_end_stream(&self) -> bool {
        self.body.is_end_stream()
    }
    fn size_hint(&self) -> SizeHint {
        self.body.size_hint()
    }
}
