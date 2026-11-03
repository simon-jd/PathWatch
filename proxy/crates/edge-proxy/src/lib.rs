//! TLS reverse proxy with bounded transport ownership and atomic generations.
mod admission;
mod cluster;
mod listener;
mod reload;
mod route;
mod transport;
mod upstream;
use std::{path::PathBuf, sync::Arc};
pub type Shutdown = tokio_util::sync::CancellationToken;
pub async fn serve(cfg: Arc<config::Config>, shutdown: Shutdown) -> anyhow::Result<()> {
    reload::serve(cfg, shutdown, None).await
}
pub async fn serve_file(path: PathBuf, shutdown: Shutdown) -> anyhow::Result<()> {
    let cfg = Arc::new(config::Config::parse(&std::fs::read_to_string(&path)?)?);
    reload::serve(cfg, shutdown, Some(path)).await
}
/// Blocking CLI entry point: builds the runtime named by the config's
/// `[runtime]` section and serves until `shutdown`.
pub fn run_file(path: PathBuf, shutdown: Shutdown) -> anyhow::Result<()> {
    let cfg = config::Config::parse(&std::fs::read_to_string(&path)?)?;
    match cfg.runtime.mode {
        config::RuntimeMode::MultiThread => {
            let mut builder = tokio::runtime::Builder::new_multi_thread();
            if let Some(workers) = cfg.runtime.workers {
                builder.worker_threads(workers);
            }
            builder.enable_all().build()?.block_on(reload::serve(
                Arc::new(cfg),
                shutdown,
                Some(path),
            ))
        }
        config::RuntimeMode::PerCore => {
            let workers = match cfg.runtime.workers {
                Some(workers) => workers,
                None => std::thread::available_parallelism()?.get(),
            };
            reload::serve_sharded(path, shutdown, workers)
        }
    }
}
