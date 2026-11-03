use config::Config;
fn valid() -> String {
    r#"
listeners = [{ addr = "127.0.0.1:8080", tls = { certs = [{ sni = "test.local", cert = "cert.pem", key = "cert.key" }] } }]
routes = [{ host = "test.local", path_prefix = "/", cluster = "echo" }]
clusters = [{ name = "echo", endpoints = ["http://127.0.0.1:9000"], lb = "maglev", health = { path = "/health", interval_ms = 1000, timeout_ms = 100, unhealthy_threshold = 3, healthy_threshold = 2 } }]
"#.into()
}
#[test]
fn original_toml_shapes_and_future_settings() {
    let c = Config::parse(&valid()).unwrap();
    assert_eq!(
        c.listeners[0].tls.as_ref().unwrap().certs[0].sni,
        "test.local"
    );
    assert_eq!(c.clusters[0].lb, config::LoadBalance::Maglev);
    assert_eq!(c.clusters[0].health.as_ref().unwrap().path, "/health");
}
#[test]
fn invalid_configs_rejected_before_network_activity() {
    let cases = [
        valid().replace("cluster = \"echo\"", "cluster = \"absent\""),
        valid().replace("path_prefix = \"/\"", "path_prefix = \"bad\""),
        valid().replace("http://127.0.0.1:9000", "https://127.0.0.1:9000"),
        valid().replace("http://127.0.0.1:9000", "http://127.0.0.1:9000/path"),
        valid().replace("interval_ms = 1000", "interval_ms = 0"),
        format!("{}\n[pool]\nmax_connections = 0", valid()),
        valid().replace("test.local", "bad host"),
    ];
    for c in cases {
        assert!(Config::parse(&c).is_err(), "accepted invalid config {c}");
    }
}
#[test]
fn runtime_defaults_to_multi_thread_and_accepts_per_core() {
    let c = Config::parse(&valid()).unwrap();
    assert_eq!(c.runtime, config::Runtime::default());
    assert_eq!(c.runtime.mode, config::RuntimeMode::MultiThread);
    assert_eq!(c.runtime.workers, None);
    let c = Config::parse(&format!(
        "{}\n[runtime]\nmode = \"per_core\"\nworkers = 2",
        valid()
    ))
    .unwrap();
    assert_eq!(c.runtime.mode, config::RuntimeMode::PerCore);
    assert_eq!(c.runtime.workers, Some(2));
    for bad in [
        "[runtime]\nmode = \"per_core\"\nworkers = 0",
        "[runtime]\nmode = \"per_core\"\nworkers = 1025",
        "[runtime]\nmode = \"work_stealing\"",
        "[runtime]\nthreads = 2",
    ] {
        assert!(
            Config::parse(&format!("{}\n{bad}", valid())).is_err(),
            "{bad}"
        );
    }
}
