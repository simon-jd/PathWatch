//! Stable endpoint health/loads and one current-generation probe scheduler.
use crate::{
    Shutdown,
    admission::{Original, Registry, Retry, State},
    route::ProxyBody,
    upstream::Pool,
};
use bytes::Bytes;
use health::Health;
use http_body_util::{BodyExt, Empty};
use lb::{
    EndpointId, Picker,
    least_request::LeastRequest,
    maglev::{Maglev, hash_bytes},
};
use std::sync::{
    Arc, Mutex, Weak,
    atomic::{AtomicU64, AtomicUsize, Ordering},
};
use tokio::{
    task::JoinHandle,
    time::{Duration, Instant},
};
struct NodeHealth {
    policy: Option<config::Health>,
    status: Health,
    next_probe: Instant,
}
pub struct Node {
    pub endpoint: config::Endpoint,
    pub id: EndpointId,
    active: Arc<AtomicUsize>,
    health: Mutex<NodeHealth>,
}
pub struct Cluster {
    pub spec: config::Cluster,
    pub nodes: Vec<Arc<Node>>,
    pub state: Arc<State>,
    sequence: AtomicU64,
    table: Mutex<(Vec<EndpointId>, Maglev)>,
}
impl Cluster {
    pub fn new(spec: config::Cluster, registry: &Registry) -> Arc<Self> {
        let state = registry.get(&spec.name, &spec.circuit);
        let nodes = spec
            .endpoints
            .iter()
            .map(|s| {
                let endpoint = config::Endpoint::parse(s).expect("validated endpoint");
                let id = hash_bytes(
                    format!(
                        "{}://{}",
                        if endpoint.h2 { "h2c" } else { "http" },
                        endpoint.authority.to_ascii_lowercase()
                    )
                    .as_bytes(),
                );
                let existing = {
                    let mut nodes = state.nodes.lock().unwrap();
                    nodes.retain(|_, n| n.strong_count() > 0);
                    nodes.get(&id).and_then(Weak::upgrade)
                };
                let node = existing.unwrap_or_else(|| {
                    let policy = spec.health.clone().unwrap_or_default();
                    let node = Arc::new(Node {
                        endpoint,
                        id,
                        active: Arc::new(AtomicUsize::new(0)),
                        health: Mutex::new(NodeHealth {
                            policy: spec.health.clone(),
                            status: Health::new(
                                policy.unhealthy_threshold,
                                policy.healthy_threshold,
                            ),
                            next_probe: Instant::now(),
                        }),
                    });
                    state
                        .nodes
                        .lock()
                        .unwrap()
                        .insert(id, Arc::downgrade(&node));
                    node
                });
                {
                    let mut h = node.health.lock().unwrap();
                    if h.policy != spec.health {
                        let policy = spec.health.clone().unwrap_or_default();
                        h.status
                            .reconfigure(policy.unhealthy_threshold, policy.healthy_threshold);
                        h.policy = spec.health.clone();
                        h.next_probe = Instant::now();
                    }
                }
                node
            })
            .collect();
        Arc::new(Self {
            spec,
            nodes,
            state,
            sequence: AtomicU64::new(0),
            table: Mutex::new((Vec::new(), Maglev::new(&[]))),
        })
    }
    pub fn attempt(
        self: &Arc<Self>,
        original: Arc<Original>,
        key: u64,
        retry: Option<Retry>,
    ) -> Option<(config::Endpoint, Attempt)> {
        let mut cached = self.table.lock().unwrap();
        let eligible: Vec<_> = self
            .nodes
            .iter()
            .filter(|n| n.health.lock().unwrap().status.available(Instant::now()))
            .collect();
        let id = match self.spec.lb {
            config::LoadBalance::LeastRequest => LeastRequest::new(
                &eligible
                    .iter()
                    .map(|n| (n.id, n.active.clone()))
                    .collect::<Vec<_>>(),
            )
            .pick(Some(self.sequence.fetch_add(1, Ordering::Relaxed))),
            config::LoadBalance::Maglev => {
                let ids: Vec<_> = eligible.iter().map(|n| n.id).collect();
                if cached.0 != ids {
                    *cached = (ids.clone(), Maglev::new(&ids));
                }
                cached.1.pick(Some(key))
            }
        }?;
        let node = self
            .nodes
            .iter()
            .find(|n| n.id == id)
            .expect("eligible node")
            .clone();
        node.active.fetch_add(1, Ordering::SeqCst);
        if retry.is_some() {
            self.state.retries.fetch_add(1, Ordering::Relaxed);
        }
        Some((
            node.endpoint.clone(),
            Attempt {
                node,
                original,
                _retry: retry,
                complete: false,
            },
        ))
    }
    pub fn observe(&self, endpoint: &config::Endpoint, status: u16) {
        if let Some(n) = self.nodes.iter().find(|n| n.endpoint == *endpoint) {
            n.health
                .lock()
                .unwrap()
                .status
                .passive(status, Instant::now());
        }
    }
    pub fn probe(self: &Arc<Self>, pool: Arc<Pool>, stop: Shutdown) -> Option<JoinHandle<()>> {
        let spec = self.spec.health.clone()?;
        let cluster = self.clone();
        Some(tokio::spawn(async move {
            loop {
                let due = cluster
                    .nodes
                    .iter()
                    .map(|n| n.health.lock().unwrap().next_probe)
                    .min()
                    .unwrap();
                tokio::select! {
                 biased;
                 _=stop.cancelled()=>return,
                 _=tokio::time::sleep_until(due)=>{}
                }
                // One worker/cluster, shared physical pool, no additional task per endpoint.
                for node in &cluster.nodes {
                    {
                        let mut h = node.health.lock().unwrap();
                        let now = Instant::now();
                        if h.next_probe > now {
                            continue;
                        }
                        h.next_probe = now + Duration::from_millis(spec.interval_ms);
                    }
                    let deadline = Instant::now() + Duration::from_millis(spec.timeout_ms);
                    let endpoint = &node.endpoint;
                    let uri = if endpoint.h2 {
                        format!("http://{}{}", endpoint.authority, spec.path)
                    } else {
                        spec.path.clone()
                    };
                    let body: ProxyBody =
                        Empty::<Bytes>::new().map_err(|e| match e {}).boxed_unsync();
                    let request = http::Request::builder()
                        .uri(uri)
                        .header("host", &endpoint.authority)
                        .body(body)
                        .unwrap();
                    let probe = async {
                        let response = pool
                            .request(endpoint, request, deadline, Box::new(()))
                            .await?;
                        let ok = response.status().is_success();
                        let mut body = response.into_body();
                        while let Some(frame) = body.frame().await {
                            frame.map_err(|e| anyhow::anyhow!(e))?;
                        }
                        Ok::<_, anyhow::Error>(ok)
                    };
                    let success = tokio::select! {
                     biased;
                     _=stop.cancelled()=>return,
                     result=tokio::time::timeout_at(deadline,probe)=>matches!(result,Ok(Ok(true))),
                    };
                    node.health.lock().unwrap().status.active(success);
                }
            }
        }))
    }
    pub fn health_metrics(&self, out: &mut String) {
        use std::fmt::Write;
        for n in &self.nodes {
            let _ = writeln!(
                out,
                "pathwatch_proxy_endpoint_healthy{{cluster=\"{}\",endpoint=\"{:016x}\"}} {}",
                self.spec.name,
                n.id,
                u8::from(n.health.lock().unwrap().status.available(Instant::now()))
            );
        }
    }
}
pub struct Attempt {
    node: Arc<Node>,
    original: Arc<Original>,
    _retry: Option<Retry>,
    complete: bool,
}
impl crate::upstream::ExchangeGuard for Attempt {
    fn complete(&mut self) {
        self.complete = true;
    }
}
impl Drop for Attempt {
    fn drop(&mut self) {
        if !self.complete {
            self.original.error();
        }
        self.node.active.fetch_sub(1, Ordering::SeqCst);
    }
}
