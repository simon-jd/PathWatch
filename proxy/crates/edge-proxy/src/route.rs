use crate::{
    Shutdown,
    cluster::Cluster,
    upstream::{BoxError, Pool},
};
use bytes::Bytes;
use http::{
    HeaderMap, Request, Response, StatusCode, Version,
    header::{CONNECTION, HOST},
};
use http_body::Body;
use http_body_util::{BodyExt, Empty, Full, combinators::UnsyncBoxBody};
use hyper::body::Incoming;
use std::{
    convert::Infallible,
    sync::{Arc, Mutex, atomic::Ordering},
    time::Duration,
};
pub type ProxyBody = UnsyncBoxBody<Bytes, BoxError>;
pub struct Router {
    pub cfg: Arc<config::Config>,
    pub pool: Arc<Pool>,
    pub clusters: Vec<Arc<Cluster>>,
    health_stop: Shutdown,
    health_tasks: Mutex<Vec<tokio::task::JoinHandle<()>>>,
}
impl Router {
    pub fn with_pool(
        cfg: Arc<config::Config>,
        pool: Arc<Pool>,
        registry: &crate::admission::Registry,
    ) -> Self {
        let health_stop = Shutdown::new();
        let clusters: Vec<_> = cfg
            .clusters
            .iter()
            .cloned()
            .map(|c| Cluster::new(c, registry))
            .collect();
        let tasks = clusters
            .iter()
            .filter_map(|c| c.probe(pool.clone(), health_stop.clone()))
            .collect();
        Self {
            cfg,
            pool,
            clusters,
            health_stop,
            health_tasks: Mutex::new(tasks),
        }
    }
    pub async fn close_health(&self) {
        self.health_stop.cancel();
        let tasks = std::mem::take(&mut *self.health_tasks.lock().unwrap());
        for task in tasks {
            let _ = task.await;
        }
    }
    pub async fn handle(&self, req: Request<Incoming>) -> Result<Response<ProxyBody>, Infallible> {
        if req.method() == http::Method::CONNECT || req.headers().contains_key("upgrade") {
            return Ok(error(StatusCode::NOT_IMPLEMENTED));
        }
        let host = match request_host(&req) {
            Ok(h) => h,
            Err(()) => return Ok(error(StatusCode::BAD_REQUEST)),
        };
        let route = self
            .cfg
            .routes
            .iter()
            .filter(|r| {
                config::normalize_host(&r.host).ok().as_ref() == Some(&host)
                    && req.uri().path().starts_with(&r.path_prefix)
            })
            .max_by_key(|r| r.path_prefix.len());
        let Some(route) = route else {
            return Ok(error(StatusCode::NOT_FOUND));
        };
        let cluster = self
            .clusters
            .iter()
            .find(|c| c.spec.name == route.cluster)
            .expect("validated route");
        let deadline = tokio::time::Instant::now()
            + Duration::from_millis(
                cluster
                    .spec
                    .circuit
                    .request_timeout_ms
                    .unwrap_or(self.cfg.pool.request_timeout_ms),
            );
        let original = match tokio::time::timeout_at(deadline, cluster.state.admit()).await {
            Ok(Some(p)) => p,
            Ok(None) => {
                cluster.state.rejected.fetch_add(1, Ordering::Relaxed);
                return Ok(error(StatusCode::SERVICE_UNAVAILABLE));
            }
            Err(_) => {
                cluster.state.errors.fetch_add(1, Ordering::Relaxed);
                return Ok(error(StatusCode::GATEWAY_TIMEOUT));
            }
        };
        let replayable = matches!(*req.method(), http::Method::GET | http::Method::HEAD)
            && req.body().is_end_stream();
        let (parts, body) = req.into_parts();
        let method = parts.method.clone();
        let uri = parts.uri.clone();
        let headers = parts.headers.clone();
        let version = parts.version;
        let key = lb::maglev::hash_bytes(
            uri.path_and_query()
                .map(|p| p.as_str())
                .unwrap_or("/")
                .as_bytes(),
        );
        let mut req = Request::from_parts(
            parts,
            body.map_err(|e| Box::new(e) as BoxError).boxed_unsync(),
        );
        let mut retry = None;
        for attempt in 0..2 {
            let Some((endpoint, guard)) = cluster.attempt(original.clone(), key, retry.take())
            else {
                original.error();
                return Ok(error(StatusCode::SERVICE_UNAVAILABLE));
            };
            prepare(&mut req, &endpoint);
            let result = tokio::time::timeout_at(
                deadline,
                self.pool.request(&endpoint, req, deadline, Box::new(guard)),
            )
            .await;
            let should_retry = match &result {
                Ok(Ok(r)) => matches!(r.status().as_u16(), 502..=504),
                Ok(Err(_)) => true,
                Err(_) => false,
            };
            if let Ok(Ok(r)) = &result {
                cluster.observe(&endpoint, r.status().as_u16());
                if r.status().is_server_error() {
                    original.error();
                }
            }
            if attempt == 0 && replayable && should_retry && tokio::time::Instant::now() < deadline
            {
                retry = cluster.state.retry();
                if retry.is_some() {
                    drop(result);
                    req =
                        Request::new(Empty::<Bytes>::new().map_err(|e| match e {}).boxed_unsync());
                    *req.method_mut() = method.clone();
                    *req.uri_mut() = uri.clone();
                    *req.headers_mut() = headers.clone();
                    *req.version_mut() = version;
                    continue;
                }
            }
            let response = match result {
                Ok(Ok(mut r)) => {
                    strip_hop_headers(r.headers_mut());
                    r.map(|b| b.boxed_unsync())
                }
                Ok(Err(_)) => error(StatusCode::BAD_GATEWAY),
                Err(_) => error(StatusCode::GATEWAY_TIMEOUT),
            };
            if response.status().is_server_error() {
                original.error();
            }
            return Ok(response);
        }
        unreachable!("only one retry")
    }
}
impl Drop for Router {
    fn drop(&mut self) {
        self.health_stop.cancel();
        for t in self.health_tasks.get_mut().unwrap().drain(..) {
            t.abort();
        }
    }
}
fn prepare(req: &mut Request<ProxyBody>, endpoint: &config::Endpoint) {
    let path = req
        .uri()
        .path_and_query()
        .map(|p| p.as_str())
        .unwrap_or("/");
    *req.uri_mut() = if endpoint.h2 {
        format!("http://{}{path}", endpoint.authority)
            .parse()
            .unwrap()
    } else {
        path.parse().unwrap()
    };
    strip_hop_headers(req.headers_mut());
    req.headers_mut()
        .insert(HOST, endpoint.authority.parse().unwrap());
    *req.version_mut() = if endpoint.h2 {
        Version::HTTP_2
    } else {
        Version::HTTP_11
    };
}
fn request_host(req: &Request<Incoming>) -> Result<String, ()> {
    let mut hosts = req.headers().get_all(HOST).iter();
    let host = hosts
        .next()
        .map(|v| v.to_str().map_err(|_| ()))
        .transpose()?;
    if hosts.next().is_some() {
        return Err(());
    }
    let authority = req.uri().authority().map(|a| a.as_str());
    let normalized = host
        .or(authority)
        .ok_or(())
        .and_then(|h| config::normalize_host(h).map_err(|_| ()))?;
    if let Some(a) = authority
        && config::normalize_host(a).map_err(|_| ())? != normalized
    {
        return Err(());
    }
    if req
        .uri()
        .scheme_str()
        .is_some_and(|s| s != "http" && s != "https")
    {
        return Err(());
    }
    Ok(normalized)
}
/// RFC 9110 hop-by-hop fields, including every Connection-nominated field.
pub fn strip_hop_headers(headers: &mut HeaderMap) {
    let nominated: Vec<String> = headers
        .get_all(CONNECTION)
        .iter()
        .filter_map(|v| v.to_str().ok())
        .flat_map(|v| v.split(','))
        .map(|v| v.trim().to_ascii_lowercase())
        .collect();
    for name in nominated {
        headers.remove(name);
    }
    for name in [
        "connection",
        "keep-alive",
        "proxy-connection",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    ] {
        headers.remove(name);
    }
}
fn error(status: StatusCode) -> Response<ProxyBody> {
    Response::builder()
        .status(status)
        .body(
            Full::new(Bytes::from_static(
                status.canonical_reason().unwrap_or("error").as_bytes(),
            ))
            .map_err(|never: Infallible| match never {})
            .boxed_unsync(),
        )
        .unwrap()
}
