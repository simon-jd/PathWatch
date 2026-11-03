fn main() -> anyhow::Result<()> {
    let mut args = std::env::args_os().skip(1);
    let path = args
        .next()
        .ok_or_else(|| anyhow::anyhow!("usage: edge-proxy CONFIG.toml"))?;
    anyhow::ensure!(args.next().is_none(), "usage: edge-proxy CONFIG.toml");
    let shutdown = edge_proxy::Shutdown::new();
    let cancel = shutdown.clone();
    std::thread::spawn(move || {
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .expect("signal runtime");
        runtime.block_on(async move {
            if tokio::signal::ctrl_c().await.is_ok() {
                cancel.cancel();
            }
        });
    });
    edge_proxy::run_file(path.into(), shutdown)
}
