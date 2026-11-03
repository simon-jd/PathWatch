use serde::Deserialize;
use std::{net::SocketAddr, path::PathBuf};
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    pub listeners: Vec<Listener>,
    pub routes: Vec<Route>,
    pub clusters: Vec<Cluster>,
    #[serde(default)]
    pub pool: Pool,
    pub management: Option<Management>,
    #[serde(default)]
    pub runtime: Runtime,
}
/// Process threading model, fixed at startup (a reload cannot change it).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize, Default)]
#[serde(default, deny_unknown_fields)]
pub struct Runtime {
    pub mode: RuntimeMode,
    /// multi_thread: tokio worker threads (None: tokio's default, which honours
    /// `TOKIO_WORKER_THREADS`). per_core: number of current-thread runtimes
    /// (None: available parallelism).
    pub workers: Option<usize>,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum RuntimeMode {
    /// One work-stealing runtime shared by every connection (the default).
    #[default]
    MultiThread,
    /// One current-thread runtime per worker, each with its own SO_REUSEPORT
    /// listeners and its own upstream pool (`pool.max_connections` per runtime).
    PerCore,
}
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Management {
    pub addr: SocketAddr,
}
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Listener {
    pub addr: SocketAddr,
    pub tls: Option<Tls>,
}
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Tls {
    pub certs: Vec<Certificate>,
}
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Certificate {
    pub sni: String,
    pub cert: PathBuf,
    pub key: PathBuf,
}
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Route {
    pub host: String,
    pub path_prefix: String,
    pub cluster: String,
}
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Cluster {
    pub name: String,
    pub endpoints: Vec<String>,
    #[serde(default)]
    pub lb: LoadBalance,
    pub health: Option<Health>,
    #[serde(default)]
    pub circuit: Circuit,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum LoadBalance {
    #[default]
    LeastRequest,
    Maglev,
}
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Health {
    pub path: String,
    pub interval_ms: u64,
    pub timeout_ms: u64,
    pub unhealthy_threshold: u32,
    pub healthy_threshold: u32,
}
impl Default for Health {
    fn default() -> Self {
        Self {
            path: "/healthz".into(),
            interval_ms: 5000,
            timeout_ms: 1000,
            unhealthy_threshold: 3,
            healthy_threshold: 2,
        }
    }
}
#[derive(Debug, Clone, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Circuit {
    pub max_requests: usize,
    pub max_pending: usize,
    /// None inherits the global total request deadline.
    pub request_timeout_ms: Option<u64>,
}
impl Default for Circuit {
    fn default() -> Self {
        Self {
            max_requests: 128,
            max_pending: 128,
            request_timeout_ms: None,
        }
    }
}
#[derive(Debug, Clone, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Pool {
    pub max_connections: usize,
    pub request_timeout_ms: u64,
    pub drain_timeout_ms: u64,
}
impl Default for Pool {
    fn default() -> Self {
        Self {
            max_connections: 4,
            request_timeout_ms: 10000,
            drain_timeout_ms: 5000,
        }
    }
}
impl Config {
    pub fn parse(text: &str) -> anyhow::Result<Self> {
        let cfg: Self = toml::from_str(text)?;
        cfg.validate()?;
        Ok(cfg)
    }
    pub fn validate(&self) -> anyhow::Result<()> {
        use anyhow::{bail, ensure};
        use std::collections::HashSet;
        ensure!(!self.listeners.is_empty(), "at least one listener required");
        ensure!(
            self.pool.max_connections > 0 && self.pool.max_connections <= 4096,
            "pool cap must be 1..4096"
        );
        ensure!(
            self.pool.request_timeout_ms > 0 && self.pool.request_timeout_ms <= 300_000,
            "request timeout must be 1..300000ms"
        );
        ensure!(
            self.pool.drain_timeout_ms > 0 && self.pool.drain_timeout_ms <= 30_000,
            "drain timeout must be 1..30000ms"
        );
        ensure!(
            self.runtime.workers.is_none_or(|w| w > 0 && w <= 1024),
            "runtime workers must be 1..1024"
        );
        ensure!(self.clusters.len() <= 256, "too many clusters");
        let mut names = HashSet::new();
        for c in &self.clusters {
            ensure!(
                !c.name.is_empty() && names.insert(&c.name),
                "cluster names must be unique and nonempty"
            );
            ensure!(!c.endpoints.is_empty(), "cluster endpoints empty");
            let mut endpoints = HashSet::new();
            ensure!(c.endpoints.len() <= 4096, "too many endpoints");
            for e in &c.endpoints {
                let endpoint = Endpoint::parse(e)?;
                ensure!(
                    endpoints.insert((endpoint.h2, endpoint.authority.to_ascii_lowercase())),
                    "duplicate endpoint"
                );
            }
            ensure!(
                c.name.len() <= 128
                    && c.name
                        .bytes()
                        .all(|b| b.is_ascii_alphanumeric() || b"_-.".contains(&b)),
                "invalid cluster name"
            );
            ensure!(
                c.circuit.max_requests > 0
                    && c.circuit.max_requests <= 65536
                    && c.circuit.max_pending <= 65536,
                "invalid circuit limits"
            );
            ensure!(
                c.circuit
                    .request_timeout_ms
                    .is_none_or(|t| t > 0 && t <= 300000),
                "invalid cluster timeout"
            );
            if let Some(h) = &c.health {
                ensure!(
                    h.path.starts_with('/')
                        && !h.path.contains(['?', '#'])
                        && h.path.parse::<http::Uri>().is_ok()
                        && h.interval_ms > 0
                        && h.interval_ms <= 300000
                        && h.timeout_ms > 0
                        && h.timeout_ms <= h.interval_ms
                        && h.unhealthy_threshold > 0
                        && h.healthy_threshold > 0,
                    "invalid health configuration"
                );
            }
        }
        let mut routes = HashSet::new();
        for r in &self.routes {
            let host = normalize_host(&r.host)?;
            ensure!(
                r.path_prefix.starts_with('/') && !r.path_prefix.contains(['?', '#']),
                "invalid route prefix"
            );
            ensure!(names.contains(&r.cluster), "unknown route cluster");
            ensure!(
                routes.insert((host, r.path_prefix.clone())),
                "duplicate normalized route"
            );
        }
        let mut listeners = HashSet::new();
        for l in &self.listeners {
            ensure!(listeners.insert(l.addr), "duplicate listener");
            if let Some(tls) = &l.tls {
                ensure!(!tls.certs.is_empty(), "empty TLS certificate set");
                let mut sni = HashSet::new();
                for c in &tls.certs {
                    let name = normalize_host(&c.sni)?;
                    if c.sni.contains(':') || c.sni.contains('[') {
                        bail!("SNI must be a DNS name");
                    }
                    ensure!(sni.insert(name), "duplicate SNI");
                    ensure!(
                        !c.cert.as_os_str().is_empty() && !c.key.as_os_str().is_empty(),
                        "empty certificate path"
                    );
                }
            }
        }
        if let Some(m) = &self.management {
            ensure!(
                m.addr.ip().is_loopback() && m.addr.port() > 0 && !listeners.contains(&m.addr),
                "management requires a distinct loopback address"
            );
        }
        Ok(())
    }
}

/// Exact DNS/IP host matching, case-insensitive with an optional port and final dot.
pub fn normalize_host(host: &str) -> anyhow::Result<String> {
    use anyhow::ensure;
    let a: http::uri::Authority = host.parse()?;
    ensure!(
        !host.contains('@') && (a.as_str() == a.host() || a.port_u16().is_some()),
        "invalid authority"
    );
    let h = a.host().trim_end_matches('.').to_ascii_lowercase();
    ensure!(
        !h.is_empty()
            && h.bytes()
                .all(|b| b.is_ascii_alphanumeric() || b".-:[]".contains(&b)),
        "invalid host"
    );
    if !h.starts_with('[') {
        ensure!(
            h.split('.')
                .all(|label| !label.is_empty() && !label.starts_with('-') && !label.ends_with('-')),
            "invalid DNS label"
        );
    }
    Ok(h)
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Endpoint {
    pub authority: String,
    pub h2: bool,
}
impl Endpoint {
    pub fn parse(value: &str) -> anyhow::Result<Self> {
        use anyhow::ensure;
        let uri: http::Uri = value.parse()?;
        ensure!(
            matches!(uri.scheme_str(), Some("http" | "h2c")),
            "only http and h2c upstreams supported"
        );
        let a = uri
            .authority()
            .ok_or_else(|| anyhow::anyhow!("upstream authority required"))?;
        normalize_host(a.as_str())?;
        ensure!(
            a.port_u16().is_some_and(|p| p > 0),
            "explicit nonzero upstream port required"
        );
        ensure!(
            uri.path_and_query().is_none_or(|p| p.as_str() == "/"),
            "upstream must not include a path or query"
        );
        Ok(Self {
            authority: a.to_string(),
            h2: uri.scheme_str() == Some("h2c"),
        })
    }
}
